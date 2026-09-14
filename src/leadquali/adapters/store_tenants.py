"""Postgres behind the tenant control plane, and behind ingest authentication.

Two adapters over ``tenants`` and ``tenant_api_keys``, in one file because they are one
table pair and the layering rule asks for one file per external system:

* :class:`PostgresTenantAdminStore` implements
  :class:`~leadquali.app.tenants.TenantAdminStorePort` — the CRUD an operator drives
  through ``python -m leadquali.tenantctl``. It is not on any request path.
* :class:`PostgresIngestCredentials` implements
  :class:`~leadquali.api.signing.IngestCredentialSource` — one indexed read per lead, on
  the hot path, and the reason the whole scheme is affordable.

Why this is separate from ``store_postgres.py``
-----------------------------------------------

Same database, different blast radius. Everything in ``store_postgres.py`` writes *lead*
data; everything here writes or reads *credentials and policy*. Keeping them apart means
the answer to "what can touch a tenant's keys?" is one module, and it means the ingest
Lambda's hot path does not import the assessment-writing code it never calls.

The one read on the request path
--------------------------------

:meth:`PostgresIngestCredentials.resolve` is a single ``SELECT`` joining the key row to its
tenant, keyed on the unique ``key_id`` the presented key carries in the clear. Nothing about
it is cached, and that is deliberate: it is what makes "a revoked key is rejected
immediately" true. The expensive part — the argon2 verification — *is* memoised, inside
:class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher`, and only for verifications that
succeeded. See :mod:`leadquali.api.signing` for the whole argument.

``last_used_at`` is written at most once per key per :data:`LAST_USED_COARSENESS` per
process, after the credential has already been resolved, and any failure to write it is
swallowed. A write on every request would put a row-level lock contended by every concurrent
request for the same key on the path of every lead, to answer a question nobody asks to the
minute.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Tenant, TenantApiKey
from leadquali.api.ratelimit import TenantRateLimit
from leadquali.api.signing import (
    ACTIVE_STATUS,
    AuthFailure,
    CredentialLookup,
    CredentialRejected,
    IngestCredential,
    SecretVerifierPort,
)
from leadquali.app.api_keys import parse_api_key
from leadquali.app.tenants import (
    ApiKeyRecord,
    TenantAlreadyExistsError,
    TenantRecord,
    TenantStatus,
    UnknownApiKeyError,
    UnknownTenantError,
)
from leadquali.config import SecretResolver, Settings
from leadquali.observability import log_event

LOGGER: Final = logging.getLogger(__name__)

__all__ = [
    "LAST_USED_COARSENESS",
    "PostgresIngestCredentials",
    "PostgresTenantAdminStore",
]

LAST_USED_COARSENESS: Final[timedelta] = timedelta(hours=1)
"""How stale ``tenant_api_keys.last_used_at`` is allowed to be.

An hour, because the question it answers is "is anyone still using the old key?" during a
seven-day rotation overlap, and an hour's resolution answers that as well as a second's
would — at one write per key per hour per container instead of one per lead.
"""


def _record_from_row(row: Row[Any]) -> TenantRecord:
    """Map a ``tenants`` row onto the record the application layer speaks in."""
    return TenantRecord(
        id=row.id,
        slug=row.slug,
        name=row.name,
        status=TenantStatus(row.status),
        config=dict(row.icp_config),
        hmac_secret_ref=row.hmac_secret_ref,
        rate_limit_per_minute=row.rate_limit_per_minute,
        rate_limit_burst=row.rate_limit_burst,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _key_from_row(row: Row[Any]) -> ApiKeyRecord:
    """Map a ``tenant_api_keys`` row, dropping the hash on the way out.

    The hash is selected only where it is needed (authentication) and never travels in an
    :class:`~leadquali.app.tenants.ApiKeyRecord`.
    """
    return ApiKeyRecord(
        key_id=row.key_id,
        key_prefix=row.key_prefix,
        label=row.label,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


_TENANT_COLUMNS = (
    Tenant.id,
    Tenant.slug,
    Tenant.name,
    Tenant.status,
    Tenant.icp_config,
    Tenant.hmac_secret_ref,
    Tenant.rate_limit_per_minute,
    Tenant.rate_limit_burst,
    Tenant.created_at,
    Tenant.updated_at,
)

_KEY_COLUMNS = (
    TenantApiKey.key_id,
    TenantApiKey.key_prefix,
    TenantApiKey.label,
    TenantApiKey.created_at,
    TenantApiKey.expires_at,
    TenantApiKey.revoked_at,
    TenantApiKey.last_used_at,
)


class PostgresTenantAdminStore:
    """The control-plane store. Every method takes a tenant, and filters on it.

    ``key_id`` is globally unique, so filtering a revocation on the tenant as well is
    redundant against the index — and it is there anyway, in every statement, because
    invariant 4 is only worth anything if there is no code path where the tenant predicate
    is optional. A support tool that passed the wrong tenant gets "no such key", not
    somebody else's key.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use.

        See :func:`leadquali.adapters.store_postgres.session_factory`.
        """
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresTenantAdminStore:
        """A store over the memoised engine for ``url``."""
        from leadquali.adapters.store_postgres import session_factory

        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresTenantAdminStore:
        """A store over the configured ``DATABASE_URL``."""
        from leadquali.adapters.store_postgres import session_factory_from_env

        return cls(session_factory_from_env(settings))

    # ------------------------------------------------------------------ tenant CRUD

    def create_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        slug: str,
        name: str,
        config: Mapping[str, Any],
        hmac_secret_ref: str | None,
    ) -> TenantRecord:
        """Insert a tenant, letting the unique constraints decide a race.

        Raises:
            TenantAlreadyExistsError: the slug or the derived id is taken. Detected from
                the database's own constraint rather than from a preceding ``SELECT``, so
                two operators onboarding the same customer at once cannot both win.
        """
        statement = (
            insert(Tenant)
            .values(
                id=tenant_id,
                slug=slug,
                name=name,
                icp_config=dict(config),
                hmac_secret_ref=hmac_secret_ref,
            )
            .returning(*_TENANT_COLUMNS)
        )
        try:
            with self._sessions.begin() as session:
                row = session.execute(statement).one()
        except IntegrityError as error:
            raise TenantAlreadyExistsError(f"tenant '{slug}' already exists") from error
        return _record_from_row(row)

    def get_tenant(self, *, slug: str) -> TenantRecord | None:
        """Return one tenant, or ``None``."""
        statement = select(*_TENANT_COLUMNS).where(Tenant.slug == slug)
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        return None if row is None else _record_from_row(row)

    def list_tenants(self) -> Sequence[TenantRecord]:
        """Every tenant, oldest first."""
        statement = select(*_TENANT_COLUMNS).order_by(Tenant.created_at, Tenant.slug)
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_record_from_row(row) for row in rows]

    def update_config(self, *, slug: str, config: Mapping[str, Any]) -> TenantRecord:
        """Replace one tenant's ``icp_config`` and stamp ``updated_at``."""
        return self._update(slug=slug, values={"icp_config": dict(config)})

    def set_status(self, *, slug: str, status: TenantStatus) -> TenantRecord:
        """Set one tenant's status."""
        return self._update(slug=slug, values={"status": status.value})

    def rate_limit_for(self, tenant_id: str) -> TenantRateLimit | None:
        """The tenant's throttle, for :class:`~leadquali.api.ratelimit.TenantRateLimiter`.

        ``None`` for a tenant that does not exist. The limiter treats that as "no allowance
        configured" and falls back to its default rather than letting an unknown tenant
        through unlimited — though in practice authentication has already refused a request
        whose tenant does not exist, so this is belt and braces.
        """
        statement = select(Tenant.rate_limit_per_minute, Tenant.rate_limit_burst).where(
            Tenant.slug == tenant_id
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            return None
        return TenantRateLimit(per_minute=row[0], burst=row[1])

    # -------------------------------------------------------------------------- keys

    def add_key(
        self, *, slug: str, key_id: str, key_prefix: str, key_hash: str, label: str | None
    ) -> ApiKeyRecord:
        """Store one issued key's hash against a tenant.

        The tenant id is resolved with a sub-select on ``slug`` rather than by deriving it
        from the slug in Python, so that issuing a key to a tenant that does not exist is a
        missing row rather than a foreign key violation against a plausible-looking UUID.
        """
        with self._sessions.begin() as session:
            tenant = session.execute(select(Tenant.id).where(Tenant.slug == slug)).one_or_none()
            if tenant is None:
                raise UnknownTenantError(f"no tenant '{slug}'")
            statement = (
                insert(TenantApiKey)
                .values(
                    tenant_id=tenant[0],
                    key_id=key_id,
                    key_prefix=key_prefix,
                    key_hash=key_hash,
                    label=label,
                )
                .returning(*_KEY_COLUMNS)
            )
            row = session.execute(statement).one()
        return _key_from_row(row)

    def list_keys(self, *, slug: str) -> Sequence[ApiKeyRecord]:
        """Every key ever issued to this tenant, newest first.

        Revoked and expired rows stay: "which key did we revoke, and when?" is a question
        an incident asks, and a row that disappeared cannot answer it.
        """
        statement = (
            select(*_KEY_COLUMNS)
            .join(Tenant, Tenant.id == TenantApiKey.tenant_id)
            .where(Tenant.slug == slug)
            .order_by(TenantApiKey.created_at.desc())
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_key_from_row(row) for row in rows]

    def expire_key(self, *, slug: str, key_id: str, expires_at: datetime) -> ApiKeyRecord:
        """Set a key's rotation deadline."""
        return self._update_key(slug=slug, key_id=key_id, values={"expires_at": expires_at})

    def revoke_key(self, *, slug: str, key_id: str, revoked_at: datetime) -> ApiKeyRecord:
        """Revoke a key, effective on the next request.

        Already-revoked rows keep their original ``revoked_at``: the first revocation is
        the one that matters, and overwriting it would erase when the key actually stopped
        working — which is the only thing an incident review wants from this column.
        """
        with self._sessions.begin() as session:
            statement = (
                update(TenantApiKey)
                .where(
                    TenantApiKey.key_id == key_id,
                    TenantApiKey.revoked_at.is_(None),
                    TenantApiKey.tenant_id.in_(select(Tenant.id).where(Tenant.slug == slug)),
                )
                .values(revoked_at=revoked_at)
                .returning(*_KEY_COLUMNS)
            )
            row = session.execute(statement).one_or_none()
            if row is not None:
                return _key_from_row(row)
            existing = session.execute(
                select(*_KEY_COLUMNS)
                .join(Tenant, Tenant.id == TenantApiKey.tenant_id)
                .where(TenantApiKey.key_id == key_id, Tenant.slug == slug)
            ).one_or_none()
        if existing is None:
            raise UnknownApiKeyError(f"tenant '{slug}' has no key {key_id!r}")
        return _key_from_row(existing)

    # ----------------------------------------------------------------------- internals

    def _update(self, *, slug: str, values: Mapping[str, Any]) -> TenantRecord:
        statement = (
            update(Tenant)
            .where(Tenant.slug == slug)
            .values(**values, updated_at=func.now())
            .returning(*_TENANT_COLUMNS)
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise UnknownTenantError(f"no tenant '{slug}'")
        return _record_from_row(row)

    def _update_key(self, *, slug: str, key_id: str, values: Mapping[str, Any]) -> ApiKeyRecord:
        statement = (
            update(TenantApiKey)
            .where(
                TenantApiKey.key_id == key_id,
                TenantApiKey.tenant_id.in_(select(Tenant.id).where(Tenant.slug == slug)),
            )
            .values(**values)
            .returning(*_KEY_COLUMNS)
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise UnknownApiKeyError(f"tenant '{slug}' has no key {key_id!r}")
        return _key_from_row(row)


class PostgresIngestCredentials:
    """The request path's credential source: one indexed read, then maybe a KDF.

    Args:
        sessions: The session factory.
        verifier: The argon2 verifier.
        resolver: Reads a tenant's HMAC signing secret from its ``hmac_secret_ref``.
        now: The clock, injected so a test can place a key inside or outside its overlap.
        last_used_coarseness: How stale ``last_used_at`` may be. ``None`` switches the
            column off entirely, which is the right setting for any deployment that would
            rather not pay an extra write at all.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        verifier: SecretVerifierPort,
        resolver: SecretResolver,
        now: Callable[[], datetime] | None = None,
        last_used_coarseness: timedelta | None = LAST_USED_COARSENESS,
    ) -> None:
        self._sessions = sessions
        self._verifier = verifier
        self._resolver = resolver
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._coarseness = last_used_coarseness
        self._last_recorded: dict[str, datetime] = {}

    def resolve(self, *, tenant_id: str, api_key: str) -> CredentialLookup:
        """Resolve the presented key against ``tenant_api_keys``.

        The order is the order of cost, and every step before the last is free relative to
        it: parse (no I/O at all), one indexed read, four comparisons in Python, then the
        KDF. See :mod:`leadquali.api.signing`.
        """
        parsed = parse_api_key(api_key)
        if parsed is None:
            return CredentialRejected(AuthFailure.MALFORMED)

        statement = (
            select(
                Tenant.slug,
                Tenant.status,
                Tenant.hmac_secret_ref,
                TenantApiKey.key_hash,
                TenantApiKey.revoked_at,
                TenantApiKey.expires_at,
            )
            .join(Tenant, Tenant.id == TenantApiKey.tenant_id)
            .where(TenantApiKey.key_id == parsed.key_id)
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()

        if row is None:
            return CredentialRejected(AuthFailure.UNKNOWN_TENANT)
        if row.slug != tenant_id:
            # A real key presented under someone else's name. Reported the same way as a
            # key_id that does not exist, so the two are indistinguishable to the caller.
            return CredentialRejected(AuthFailure.UNKNOWN_TENANT)
        now = self._now()
        if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
            return CredentialRejected(AuthFailure.REVOKED_KEY)
        if row.status != ACTIVE_STATUS:
            return CredentialRejected(AuthFailure.TENANT_SUSPENDED)
        if row.hmac_secret_ref is None:
            # A tenant with a key but no signing secret cannot produce a valid signature,
            # so there is nothing to authenticate against. Logged loudly because it means
            # onboarding stopped half way, and refused rather than raised because a 500
            # here would tell a stranger that this key_id exists.
            log_event(
                LOGGER,
                "ingest.tenant_without_signing_secret",
                level=logging.ERROR,
                tenant_id=row.slug,
                key_id=parsed.key_id,
            )
            return CredentialRejected(AuthFailure.UNKNOWN_TENANT)
        if not self._verifier.verify_secret(
            key_id=parsed.key_id, secret=parsed.secret, key_hash=row.key_hash
        ):
            return CredentialRejected(AuthFailure.BAD_KEY)

        self._touch(key_id=parsed.key_id, now=now)
        return IngestCredential(
            tenant_id=row.slug,
            key_id=parsed.key_id,
            signing_secret=self._resolver.resolve(row.hmac_secret_ref).encode("utf-8"),
        )

    def _touch(self, *, key_id: str, now: datetime) -> None:
        """Record that this key was used, coarsely, and never at the cost of the request.

        Outside the authentication decision — the credential is already established by the
        time this runs — rate-limited per key per process, and wrapped in a bare ``except``
        because there is no failure here worth turning a good lead into a 500.
        """
        if self._coarseness is None:
            return
        recorded = self._last_recorded.get(key_id)
        if recorded is not None and now - recorded < self._coarseness:
            return
        self._last_recorded[key_id] = now
        try:
            with self._sessions.begin() as session:
                session.execute(
                    update(TenantApiKey)
                    .where(TenantApiKey.key_id == key_id)
                    .values(last_used_at=now)
                )
        except Exception:  # see the docstring: recording use must never fail a lead
            LOGGER.warning(
                "ingest.last_used_not_recorded",
                extra={"event": "ingest.last_used_not_recorded", "key_id": key_id},
            )

    def __repr__(self) -> str:
        """Render the shape, never a secret."""
        return f"PostgresIngestCredentials(tracked_keys={len(self._last_recorded)})"
