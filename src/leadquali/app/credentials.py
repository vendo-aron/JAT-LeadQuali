"""What an ingest credential is, and the one decision that accepts or refuses one.

Two things live here, and they live *here* rather than in ``api/signing.py`` for two
different reasons.

**The types**, because both resolvers need them and one of them is an adapter. The layering
rule in ``CLAUDE.md`` is one-directional — ``domain`` <- ``app`` <- ``adapters``/``api`` —
so ``adapters/store_tenants.py`` may not import ``leadquali.api``. Declaring
:class:`IngestCredential`, :class:`CredentialRejected`, :class:`IngestCredentialSource`,
:class:`SecretVerifierPort` and :class:`AuthFailure` in the application layer is what lets
the Postgres resolver and the in-memory one speak the same language without either of them
reaching across the grain. ``tests/unit/test_layering.py`` pins it.

**The decision**, because a second hand-written copy of it is worse than useless. The
rejection rules — is this key's row the claimed tenant's, is it revoked, has its rotation
overlap closed, does its secret verify, is the tenant active — are *pure* once the row is
in hand. They were written twice, once in each resolver, and only one of those two is what
production runs; the other is what the offline test suite exercised. So they are written
once, in :func:`decide_credential`, which both resolvers call and which is unit-tested
directly, with no database anywhere near it.

Order of the checks, and why
----------------------------

Cheapest first, with one deliberate exception.

1. **Wrong tenant** — the row exists but was presented under someone else's name. Free, and
   reported as :attr:`AuthFailure.UNKNOWN_TENANT` so it is indistinguishable from a
   ``key_id`` that does not exist at all.
2. **Revoked or expired** — free, and it is what makes "a revoked key stops working
   immediately" true: the row is read fresh from Postgres on every request.
3. **The KDF** — the expensive one. It runs only for a caller who already holds a live
   ``key_id`` belonging to the tenant they claim to be.
4. **Tenant not active** — checked *after* the KDF, which costs an argon2 verification that
   a cheaper ordering would avoid. That is the point. ``403 "tenant is not active"`` is the
   one answer this endpoint gives that is not identical to every other rejection, so it must
   be reachable only by someone who has proved they hold the secret. Checking the status
   first would let anyone who has ever seen a customer's key — it travels in a header, in
   the clear, on every submission — discover whether that account has been suspended for
   non-payment, without holding the secret. It would also tell an integrator with a
   genuinely wrong key, in writing, that their key was fine. The extra KDF only ever applies
   to someone already holding a live key, and it is memoised after the first one.
5. **No signing secret** — a tenant with a key and no HMAC secret cannot produce a valid
   signature, so there is nothing to authenticate against. Half-finished onboarding, logged
   by the caller and refused as an unknown tenant.

Standard library only. ``api/signing.py`` imports from this module, and #30's browser-side
form signer imports *that*, so a ``pydantic`` or a ``domain`` import here would break the
property that the signing construction is reimplementable from one file and a spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "ACTIVE_STATUS",
    "TENANT_STATUSES",
    "AuthFailure",
    "CredentialAccepted",
    "CredentialDecision",
    "CredentialLookup",
    "CredentialRejected",
    "IngestCredential",
    "IngestCredentialSource",
    "SecretVerifierPort",
    "StoredApiKey",
    "decide_credential",
]

#: The statuses a tenant row may carry. Mirrors the CHECK constraint on ``tenants.status``;
#: only :data:`ACTIVE_STATUS` may submit leads.
TENANT_STATUSES: Final[tuple[str, ...]] = ("active", "suspended", "disabled")
ACTIVE_STATUS: Final[str] = "active"


class AuthFailure(StrEnum):
    """Why a request was refused.

    For logs and metrics only. It is never returned to the caller, and every value but
    :attr:`TENANT_SUSPENDED` and :attr:`UNAVAILABLE` produces the identical 401 on the
    wire: telling a stranger that the tenant exists but the key is wrong is a free
    enumeration oracle.
    """

    MALFORMED = "malformed"
    """A required header is missing, or a header — including the API key itself — cannot
    be parsed. Costs no I/O and no KDF."""

    UNKNOWN_TENANT = "unknown_tenant"
    """No key row matched the presented ``key_id``, or the row that matched belongs to a
    different tenant than the one in the header. One rejection for both, because a caller
    must not be able to tell them apart."""

    BAD_KEY = "bad_key"
    """A key row was found and the argon2 check on its secret failed."""

    REVOKED_KEY = "revoked_key"
    """The key row exists but has been revoked, or its rotation overlap has expired."""

    TENANT_SUSPENDED = "tenant_suspended"
    """The key verified and the tenant is not active. One of the two failures that is not a
    401; see this module's docstring for why it is checked after the KDF."""

    UNAVAILABLE = "unavailable"
    """A dependency this decision needs — the tenant's signing secret, its rate limit —
    could not be read. **Not** the caller's fault, so not a 401: a browser form told its
    key is bad will not retry, and the lead is gone (invariant 3). It becomes a 503 with a
    ``Retry-After``, which is the one answer that asks the sender to come back."""

    BAD_SIGNATURE = "bad_signature"

    STALE = "stale"
    """The timestamp is outside the accepted window in either direction."""

    REPLAY = "replay"
    """This nonce was already used inside the window."""


@runtime_checkable
class SecretVerifierPort(Protocol):
    """ "Does this secret match this stored hash?", and nothing else.

    Implemented by :class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher`. The
    indirection is what keeps this module, and ``api/signing.py`` above it, standard-library
    only: a reimplementation of the signing construction in another language must not have
    to install a KDF to read it.
    """

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        """Return whether ``secret`` is the secret behind ``key_hash``.

        Implementations must not raise for a wrong secret or an unreadable stored hash;
        both are ``False``. ``key_id`` is the clear-text row handle, for rate-limiting the
        KDF per key and for log lines — never for the comparison itself.
        """
        ...


@dataclass(frozen=True, slots=True)
class IngestCredential:
    """One tenant's ingest identity, once a presented key has been accepted.

    ``signing_secret`` is bytes because that is what :func:`hmac.new` wants and because it
    discourages the string handling that ends with a secret in an f-string. ``key_id`` is
    *not* secret and is carried so the caller can log which of a tenant's keys was used
    and record ``last_used_at`` against it.
    """

    tenant_id: str
    key_id: str
    signing_secret: bytes

    def __repr__(self) -> str:
        """Never render the secret: a repr ends up in tracebacks, and tracebacks in logs."""
        return (
            f"IngestCredential(tenant_id={self.tenant_id!r}, key_id={self.key_id!r}, "
            f"signing_secret='<redacted>')"
        )


@dataclass(frozen=True, slots=True)
class CredentialRejected:
    """No usable credential, and the reason — which is for the log, not for the caller."""

    failure: AuthFailure


CredentialLookup = IngestCredential | CredentialRejected
"""What a credential source answers with. Never ``None``: the reason a lookup failed is the
only thing that lets an operator tell "a customer's form is using a revoked key" apart from
"someone is probing us", and collapsing it to ``None`` at the port boundary loses it."""


@dataclass(frozen=True, slots=True)
class CredentialAccepted:
    """The presented key is this tenant's, live, and its secret verifies.

    Deliberately *not* an :class:`IngestCredential`: it carries no signing secret, because
    fetching one is I/O and :func:`decide_credential` does none. Returning a half-filled
    credential with ``signing_secret=b""`` instead would mean a caller that forgot the last
    step signed every request with the empty key — which verifies, for anyone who guesses
    that it happened.
    """

    tenant_id: str
    key_id: str


CredentialDecision = CredentialAccepted | CredentialRejected
"""What :func:`decide_credential` answers with."""


@runtime_checkable
class IngestCredentialSource(Protocol):
    """Where the ingest endpoint resolves a presented key to a tenant's secrets.

    The source parses the key and finds its row; :func:`decide_credential` does everything
    after that. Splitting it there is what keeps the two implementations — Postgres on the
    request path, a dict for a laptop — from being two hand-written copies of one security
    decision, only one of which production ever runs.
    """

    def resolve(self, *, tenant_id: str, api_key: str) -> CredentialLookup:
        """Resolve the presented key, or say why it was refused."""
        ...


@dataclass(frozen=True, slots=True)
class StoredApiKey:
    """One row of ``tenant_api_keys``, as the request path needs to see it."""

    key_id: str
    key_hash: str
    """The encoded argon2id hash of the key's secret half. Never the key."""

    revoked: bool = False
    expires_at: datetime | None = None
    """When a rotated key's overlap window closes; ``None`` means it never expires."""

    def __repr__(self) -> str:
        """A hash is not a secret, but it is not something to spill into a log either."""
        return f"StoredApiKey(key_id={self.key_id!r}, revoked={self.revoked!r})"

    def is_live(self, now: datetime) -> bool:
        """Whether this key may still be used at ``now``."""
        if self.revoked:
            return False
        return self.expires_at is None or self.expires_at > now


def decide_credential(
    *,
    claimed_tenant_id: str,
    row_tenant_id: str,
    tenant_status: str,
    key: StoredApiKey,
    presented_secret: str,
    now: datetime,
    verifier: SecretVerifierPort,
) -> CredentialDecision:
    """Accept or refuse one presented key, given the row it named.

    Pure: no I/O, no clock of its own, no secret of its own. Everything that decides whether
    a request is authenticated — once its ``key_id`` has found a row — happens here and
    nowhere else, so that the Postgres resolver and the in-memory one cannot drift apart and
    so that the rules can be tested without a database. The order of the checks, and the one
    place it is deliberately not cheapest-first, are explained in the module docstring.

    Args:
        claimed_tenant_id: The slug in the ``X-LeadQuali-Tenant`` header. An assertion by a
            stranger, not a fact.
        row_tenant_id: The slug of the tenant that actually owns the key row.
        tenant_status: That tenant's status, from :data:`TENANT_STATUSES`.
        key: The key row.
        presented_secret: The secret half of the presented key.
        now: Current time, for the rotation overlap window.
        verifier: The KDF.

    Returns:
        :class:`CredentialAccepted` — which the caller turns into an
        :class:`IngestCredential` by fetching the tenant's signing secret, the one part of
        this that is I/O — or :class:`CredentialRejected` saying why not.
    """
    if row_tenant_id != claimed_tenant_id:
        # A real key presented under someone else's name. Reported as an unknown tenant so
        # that it is indistinguishable from a key_id that does not exist at all.
        return CredentialRejected(AuthFailure.UNKNOWN_TENANT)
    if not key.is_live(now):
        return CredentialRejected(AuthFailure.REVOKED_KEY)
    if not verifier.verify_secret(
        key_id=key.key_id, secret=presented_secret, key_hash=key.key_hash
    ):
        return CredentialRejected(AuthFailure.BAD_KEY)
    # After the KDF, deliberately: see the module docstring. This is the one answer the
    # endpoint gives that differs from every other rejection, so only a caller who has
    # proved it holds the secret may reach it.
    if tenant_status != ACTIVE_STATUS:
        return CredentialRejected(AuthFailure.TENANT_SUSPENDED)
    return CredentialAccepted(tenant_id=row_tenant_id, key_id=key.key_id)
