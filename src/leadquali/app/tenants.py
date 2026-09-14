"""Tenant administration: the control plane behind ``python -m leadquali.tenantctl``.

Invariant 1 says onboarding a customer is a config write and never a deploy. This module is
the write. It creates tenants, validates and replaces their rubric, suspends and resumes
them, and issues, rotates and revokes the API keys their forms authenticate with — all
without a single branch on *which* tenant is being operated on. ``tests/unit/test_tenants.py``
asserts that last property directly by driving the same code path with two very different
tenants and finding no difference in the code that ran.

Three rules shape the design.

**Validation happens before any write, and a failed validation changes nothing.**
:meth:`TenantService.update_config` runs the new document through
:class:`~leadquali.domain.tenant_config.TenantConfig` and only then touches the store. This
is the "a bad paste cannot take a tenant down" criterion: the failure mode being designed
against is an operator pasting a config with a gap between the ``warm`` and ``cold`` bands
at 5pm, and every one of that tenant's leads mis-routing overnight.

**The plaintext key exists in exactly one place: the return value.** :class:`IssuedApiKey`
carries it, its ``__repr__`` redacts it, no record derived from the database can hold it,
and nothing here logs it. The store is given an argon2 hash and never sees the key.

**Every method takes a tenant by slug, and every store method filters on the tenant**
(invariant 4). ``revoke_key(slug, key_id)`` filters on both even though ``key_id`` is
globally unique, because "the tenant predicate is optional here" is exactly the reasoning
that eventually lets one customer revoke another's key.

The layering rule applies as usual: nothing here imports an adapter. The store, the key
hasher and the HMAC secret provisioner are all Protocols, wired to real implementations by
:mod:`leadquali.tenantctl`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.api_keys import KeyEnvironment, generate_api_key
from leadquali.app.ports import ClockPort
from leadquali.app.tenant_ids import is_tenant_slug, tenant_id_for
from leadquali.domain.tenant_config import TenantConfig

__all__ = [
    "DEFAULT_ROTATION_OVERLAP",
    "ApiKeyRecord",
    "IssuedApiKey",
    "SecretHasherPort",
    "TenantAdminError",
    "TenantAdminStorePort",
    "TenantAlreadyExistsError",
    "TenantRecord",
    "TenantSecretsPort",
    "TenantService",
    "TenantStatus",
    "UnknownApiKeyError",
    "UnknownTenantError",
]

DEFAULT_ROTATION_OVERLAP: Final[timedelta] = timedelta(days=7)
"""How long a rotated key keeps working after its replacement is issued.

Seven days, because the customer-side half of a rotation is a code change on somebody
else's website: it has to survive a weekend, a release freeze and one person being on
holiday. Shorter would turn every rotation into an outage negotiation; much longer would
leave a compromised key live for a month. Documented in ``docs/tenant-onboarding.md`` so
the number a customer is told matches the number the code uses.
"""


class TenantAdminError(Exception):
    """Something an operator did cannot be done, with a reason they can act on."""


class TenantAlreadyExistsError(TenantAdminError):
    """A tenant with this slug is already on file."""


class UnknownTenantError(TenantAdminError):
    """No tenant with this slug."""


class UnknownApiKeyError(TenantAdminError):
    """No such key for this tenant — it never existed, or it belongs to someone else."""


class TenantStatus(StrEnum):
    """Whether a tenant may ingest. Mirrors the CHECK on ``tenants.status``."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    """Temporarily stopped — non-payment, an incident, a customer's own request. Ingest is
    refused with a 403 and everything already stored is untouched."""

    DISABLED = "disabled"
    """Gone for good, short of #37's erasure. Behaves like suspended at the door."""


@dataclass(frozen=True, slots=True)
class TenantRecord:
    """One ``tenants`` row, as the control plane sees it. Carries no secret."""

    id: uuid.UUID
    slug: str
    name: str
    status: TenantStatus
    config: Mapping[str, Any]
    """The stored ``icp_config`` document, verbatim. Validated on the way in, so a reader
    may hand it straight to :meth:`TenantConfig.from_dict`."""

    hmac_secret_ref: str | None
    """The Secrets Manager ARN of the tenant's signing secret — a reference, never a value.
    ``None`` only for a tenant created before provisioning existed, or with it stubbed."""

    rate_limit_per_minute: int
    rate_limit_burst: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """One ``tenant_api_keys`` row as anything outside the store may see it.

    There is deliberately no ``key_hash`` field. A hash is not a credential, but a record
    type that carries one ends up in a log line or an API response eventually, and the
    cheapest way to guarantee that never happens is for the value not to be here.
    """

    key_id: str
    key_prefix: str
    label: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None

    def is_live(self, now: datetime) -> bool:
        """Whether a request presenting this key would be accepted at ``now``."""
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """A newly minted key: the record, plus the only copy of the key that will ever exist.

    Hand :attr:`key` to the customer and then forget it. It is not stored, not logged, not
    recoverable, and not in this object's ``repr``.
    """

    record: ApiKeyRecord
    key: str

    def __repr__(self) -> str:
        """Never render the key: a repr ends up in tracebacks, and tracebacks in logs."""
        return f"IssuedApiKey(key_id={self.record.key_id!r}, key='<redacted>')"


@runtime_checkable
class SecretHasherPort(Protocol):
    """Turns a key's secret half into its at-rest form.

    Implemented by :class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher`; a Protocol
    here so that ``app`` never imports the KDF and a test can hash instantly.
    """

    def hash_secret(self, secret: str) -> str:
        """Return the encoded hash of ``secret``."""
        ...


@runtime_checkable
class TenantSecretsPort(Protocol):
    """Provisions the per-tenant HMAC signing secret and reports where it lives."""

    def create_tenant_hmac_secret(self, slug: str) -> str:
        """Create (or find) this tenant's signing secret and return its ARN.

        Must be idempotent and must **never** overwrite an existing secret: clobbering a
        live signing secret breaks every form the customer has already deployed.
        """
        ...


@runtime_checkable
class TenantAdminStorePort(Protocol):
    """The control-plane half of the tenant store.

    Separate from :class:`~leadquali.app.ports.LeadStorePort` because it is a different
    access pattern with a different blast radius: this one writes credentials and policy,
    is called by a CLI a handful of times a week, and is not on any request path.
    """

    def create_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        slug: str,
        name: str,
        config: Mapping[str, Any],
        hmac_secret_ref: str | None,
    ) -> TenantRecord:
        """Insert a tenant.

        Raises:
            TenantAlreadyExistsError: the slug, or the derived id, is already taken.
        """
        ...

    def get_tenant(self, *, slug: str) -> TenantRecord | None:
        """Return one tenant, or ``None``."""
        ...

    def list_tenants(self) -> Sequence[TenantRecord]:
        """Every tenant, oldest first."""
        ...

    def update_config(self, *, slug: str, config: Mapping[str, Any]) -> TenantRecord:
        """Replace one tenant's ``icp_config``.

        Raises:
            UnknownTenantError: no such tenant.
        """
        ...

    def set_status(self, *, slug: str, status: TenantStatus) -> TenantRecord:
        """Set one tenant's status.

        Raises:
            UnknownTenantError: no such tenant.
        """
        ...

    def add_key(
        self, *, slug: str, key_id: str, key_prefix: str, key_hash: str, label: str | None
    ) -> ApiKeyRecord:
        """Store one issued key's hash against a tenant.

        Raises:
            UnknownTenantError: no such tenant.
        """
        ...

    def list_keys(self, *, slug: str) -> Sequence[ApiKeyRecord]:
        """Every key ever issued to this tenant, newest first, hashes excluded."""
        ...

    def expire_key(self, *, slug: str, key_id: str, expires_at: datetime) -> ApiKeyRecord:
        """Set a key's rotation deadline.

        Raises:
            UnknownApiKeyError: this tenant has no such key.
        """
        ...

    def revoke_key(self, *, slug: str, key_id: str, revoked_at: datetime) -> ApiKeyRecord:
        """Revoke a key, effective on the next request.

        Raises:
            UnknownApiKeyError: this tenant has no such key.
        """
        ...


class TenantService:
    """Everything an operator can do to a tenant, with the rules applied once.

    Args:
        store: The control-plane store.
        hasher: The KDF used to store an issued key's secret half.
        secrets: The HMAC secret provisioner.
        clock: The clock, injected so rotation windows are testable without waiting.
    """

    def __init__(
        self,
        *,
        store: TenantAdminStorePort,
        hasher: SecretHasherPort,
        secrets: TenantSecretsPort,
        clock: ClockPort,
    ) -> None:
        self._store = store
        self._hasher = hasher
        self._secrets = secrets
        self._clock = clock

    # ------------------------------------------------------------------ tenant CRUD

    def create_tenant(self, *, slug: str, name: str, config: Mapping[str, Any]) -> TenantRecord:
        """Onboard a customer.

        The whole of onboarding, and none of it is a code change: validate the rubric,
        derive the row id from the slug, provision a signing secret, insert.

        Args:
            slug: The tenant's external identity. Must match the slug pattern and must
                equal ``config["tenant_id"]`` — two names for one tenant is how a config
                ends up applied to the wrong customer's leads.
            name: Human-readable name.
            config: The rubric document, stored verbatim in ``icp_config``.

        Returns:
            The stored tenant.

        Raises:
            TenantAdminError: the slug is malformed or disagrees with the config.
            TenantConfigError: the rubric is invalid. Nothing has been written.
            TenantAlreadyExistsError: this slug is already on file.
        """
        self._check_slug(slug)
        validated = self._validated(config)
        if validated.tenant_id != slug:
            raise TenantAdminError(
                f"config tenant_id is '{validated.tenant_id}' but the tenant is being created "
                f"as '{slug}'; they have to be the same name"
            )
        if self._store.get_tenant(slug=slug) is not None:
            raise TenantAlreadyExistsError(f"tenant '{slug}' already exists")

        # Provisioned before the insert and idempotent by contract, so a retry after a
        # failed insert reuses the same secret rather than minting a second one and
        # breaking whichever forms already hold the first.
        secret_ref = self._secrets.create_tenant_hmac_secret(slug)
        return self._store.create_tenant(
            tenant_id=tenant_id_for(slug),
            slug=slug,
            name=name,
            config=dict(config),
            hmac_secret_ref=secret_ref,
        )

    def get_tenant(self, *, slug: str) -> TenantRecord:
        """Return one tenant.

        Raises:
            UnknownTenantError: no such tenant.
        """
        found = self._store.get_tenant(slug=slug)
        if found is None:
            raise UnknownTenantError(f"no tenant '{slug}'")
        return found

    def list_tenants(self) -> Sequence[TenantRecord]:
        """Every tenant on file, oldest first."""
        return self._store.list_tenants()

    def update_config(self, *, slug: str, config: Mapping[str, Any]) -> TenantRecord:
        """Replace a tenant's rubric, or leave the stored one exactly as it was.

        There is no partial application and no "best effort": the document is validated in
        full before the store is touched, so a config that would break scoring cannot reach
        the database at all.

        Raises:
            TenantConfigError: the rubric is invalid. The stored config is untouched.
            TenantAdminError: the config names a different tenant.
            UnknownTenantError: no such tenant.
        """
        validated = self._validated(config)
        if validated.tenant_id != slug:
            raise TenantAdminError(
                f"config tenant_id is '{validated.tenant_id}' but it is being written to "
                f"tenant '{slug}'"
            )
        return self._store.update_config(slug=slug, config=dict(config))

    def set_status(self, *, slug: str, status: TenantStatus) -> TenantRecord:
        """Suspend, resume or disable a tenant.

        Takes effect on the next request: the ingest path reads the tenant's status fresh
        from Postgres every time, so nothing has to be waited out or restarted.
        """
        return self._store.set_status(slug=slug, status=status)

    # -------------------------------------------------------------------------- keys

    def issue_key(
        self,
        *,
        slug: str,
        label: str | None = None,
        environment: KeyEnvironment = KeyEnvironment.LIVE,
    ) -> IssuedApiKey:
        """Mint a key for a tenant and store only its hash.

        Args:
            slug: The tenant.
            label: A note for whoever reads the key listing later, e.g. "acme website".
            environment: ``live`` or ``test``, written into the key itself.

        Returns:
            The record and the plaintext key. The key is not stored anywhere and cannot be
            retrieved again; show it once.

        Raises:
            UnknownTenantError: no such tenant.
        """
        minted = generate_api_key(environment)
        record = self._store.add_key(
            slug=slug,
            key_id=minted.key_id,
            key_prefix=minted.prefix,
            key_hash=self._hasher.hash_secret(minted.secret),
            label=label,
        )
        return IssuedApiKey(record=record, key=minted.text)

    def rotate_key(
        self,
        *,
        slug: str,
        key_id: str,
        overlap: timedelta = DEFAULT_ROTATION_OVERLAP,
        label: str | None = None,
    ) -> IssuedApiKey:
        """Issue a replacement key and put the old one on a deadline.

        Both keys work until the deadline, which is what makes rotation a customer-side
        deploy at a time of their choosing rather than a coordinated outage. The new key is
        issued *first*: if the expiry write then fails, the customer has a working new key
        and an old one that is still live, which is recoverable. The other order would
        leave a tenant with a dying key and no replacement.

        Args:
            slug: The tenant.
            key_id: The key being retired.
            overlap: How long the old key keeps working. Defaults to
                :data:`DEFAULT_ROTATION_OVERLAP`.
            label: A note for the new key; defaults to the old key's label.

        Returns:
            The new key, in plaintext, once.

        Raises:
            TenantAdminError: the overlap is negative.
            UnknownApiKeyError: this tenant has no such key.
            UnknownTenantError: no such tenant.
        """
        if overlap < timedelta(0):
            raise TenantAdminError("a rotation overlap cannot be negative")
        existing = self._key_or_raise(slug=slug, key_id=key_id)
        issued = self.issue_key(
            slug=slug,
            label=label if label is not None else existing.label,
            environment=self._environment_of(existing),
        )
        self._store.expire_key(slug=slug, key_id=key_id, expires_at=self._clock.now() + overlap)
        return issued

    def revoke_key(self, *, slug: str, key_id: str) -> ApiKeyRecord:
        """Kill a key now.

        Effective on the very next request, with no window and nothing to wait out: the row
        is read from Postgres on every authentication and no part of it is cached.

        Raises:
            UnknownApiKeyError: this tenant has no such key.
        """
        return self._store.revoke_key(slug=slug, key_id=key_id, revoked_at=self._clock.now())

    def list_keys(self, *, slug: str) -> Sequence[ApiKeyRecord]:
        """Every key this tenant has ever been issued, newest first.

        Revoked and expired keys stay in the listing: "which key did we revoke, and when?"
        is a question an incident asks, and a row that disappears cannot answer it.
        """
        return self._store.list_keys(slug=slug)

    # ----------------------------------------------------------------------- internals

    @staticmethod
    def _check_slug(slug: str) -> None:
        if not is_tenant_slug(slug):
            raise TenantAdminError(
                f"'{slug}' is not a valid tenant slug: lowercase letters, digits, '-' and "
                "'_', starting with a letter or digit, at most 63 characters"
            )

    @staticmethod
    def _validated(config: Mapping[str, Any]) -> TenantConfig:
        """Run a config document through the one validator there is.

        :meth:`TenantConfig.from_dict` is ``model_validate`` plus the tenant's own name in
        the error message, which is the first thing an operator needs when a paste goes
        wrong at 5pm.
        """
        return TenantConfig.from_dict(dict(config))

    def _key_or_raise(self, *, slug: str, key_id: str) -> ApiKeyRecord:
        for record in self._store.list_keys(slug=slug):
            if record.key_id == key_id:
                return record
        raise UnknownApiKeyError(f"tenant '{slug}' has no key {key_id!r}")

    @staticmethod
    def _environment_of(record: ApiKeyRecord) -> KeyEnvironment:
        """A rotated key stays in the environment its predecessor was issued for.

        Otherwise rotating a ``test`` key hands the customer a ``live`` one, and the
        environment literal in the key — whose whole purpose is to make that mistake
        visible — would be the thing that caused it.
        """
        parts = record.key_prefix.split("_")
        if len(parts) >= 2 and parts[1] in set(KeyEnvironment):
            return KeyEnvironment(parts[1])
        return KeyEnvironment.LIVE
