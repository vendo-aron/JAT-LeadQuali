"""Authenticating a request from a customer's web form.

The ingest endpoint is the only public, internet-facing surface in the system and it is
unauthenticated by default: everything that arrives is attacker-controlled. Two secrets
per tenant guard it, and they do different jobs.

* The **API key** travels in a header on every request and says *who is calling*. Its
  secret half is stored as an argon2id hash, never in the clear, and a tenant may hold
  several keys at once so that rotation has no downtime.
* The **signing secret** never leaves the two ends. It authenticates the request itself:
  the exact bytes of the body, the method, the path, the tenant, a timestamp and a nonce
  are folded into one string and HMAC-SHA256'd. A key lifted from a browser's network tab
  or a proxy log is useless without it.

**Why argon2, and why it costs almost nothing here.** A memory-hard KDF on the request
path is normally a self-inflicted denial of service: ~19 MiB and several milliseconds per
call, on every lead, inside a 200 ms budget. Two properties of the key format
(:mod:`leadquali.app.api_keys`) remove that cost.

1. *A key names its own row.* ``lq_<env>_<key_id>_<secret>`` carries a 64-bit ``key_id`` in
   the clear. Verification is one indexed read by ``key_id``; only if that read returns a
   row which is unrevoked, unexpired, owned by the tenant in the header and belonging to an
   active tenant does the KDF run at all. A stranger who does not hold a real ``key_id``
   can never make us spend 19 MiB.
2. *The answer is memoised.* ``verify(hash, secret)`` is a pure function of two immutable
   values, so the KDF runs at most once per key per process. The **row** is not memoised —
   it is read fresh from Postgres every time — which is what makes "a revoked key stops
   working immediately" true rather than "within some cache TTL".

The residual timing channel is that an existing ``key_id`` costs a KDF where a non-existent
one does not, so response time answers "does this 64-bit random identifier exist?". A
caller must already hold the answer to ask the question, and the exchange is strictly a
gain: it *replaces* the tenant-enumeration oracle the old scheme had to defend against with
a dummy-hash comparison, because ``unknown_tenant`` and ``bad_key`` are now literally the
same code path and so cannot drift apart.

**Two exceptions to "every rejection looks the same".**

A *suspended* tenant gets a 403 (:attr:`AuthFailure.TENANT_SUSPENDED`). Such a caller has
already proved its identity, so there is no enumeration left to protect, and an operator on
the customer's side deserves to be told that the account and not the integration is the
problem. That claim is only true because the status is checked **after** the KDF, in
:func:`~leadquali.app.credentials.decide_credential` — a ``key_id`` travels in the clear in
a header on every submission, so checking the status first would let anyone who has ever
seen a customer's key discover whether that account had been suspended for non-payment,
without holding the secret. A revoked *key* is a 401 like any other bad key.

A dependency that cannot be read — a tenant's signing secret, its rate limit — gets a 503
with a ``Retry-After`` (:attr:`AuthFailure.UNAVAILABLE`). It is not the caller's fault, and
a browser form told its key is bad does not retry, so a 401 there would silently lose the
lead (invariant 3).

**Replay.** The signed material carries a unix timestamp and a client nonce. A request
outside :data:`MAX_CLOCK_SKEW_SECONDS` is refused, and a nonce already seen inside that
window is refused by :class:`ReplayGuard`. The guard is per-process, which is the honest
limit of what an in-process structure can do: behind several Lambda instances a replay can
land on a different one inside the window. Two things behind it make that harmless rather
than merely unlikely — the ingest handler's own ``(tenant_id, submission_id)`` idempotency
means a replayed body creates no second lead and no second enqueue, and the stage-level
throttle in ``infra/template.yaml`` caps the rate at which anyone can try. A shared nonce
store (Redis/DynamoDB) would close it completely and is deliberately not built here for one
endpoint.

Everything in this module is standard library only — the argon2 implementation lives behind
:class:`~leadquali.app.credentials.SecretVerifierPort`, and the credential types and the
accept/refuse decision live in :mod:`leadquali.app.credentials`, which is stdlib-only for
the same reason — so #26's Lambda handler and #30's form-side signer can both import it,
and so the construction can be reimplemented from :func:`signing_string` alone in whatever
language the customer's site is written in. The credential names are re-exported here so
that the modules which already import them from this one keep working.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from leadquali.app.api_keys import KEY_ID_CHARS, parse_api_key
from leadquali.app.credentials import (
    ACTIVE_STATUS,
    TENANT_STATUSES,
    AuthFailure,
    CredentialAccepted,
    CredentialLookup,
    CredentialRejected,
    IngestCredential,
    IngestCredentialSource,
    SecretVerifierPort,
    StoredApiKey,
    decide_credential,
)
from leadquali.domain.tenant_config import TENANT_ID_PATTERN

#: Names the algorithm inside the signed string, so a future v2 with different material
#: cannot be confused for a v1 signature by either end.
SIGNATURE_ALGORITHM: Final[str] = "LEADQUALI-HMAC-SHA256"

#: The scheme version, sent as the ``v1=`` prefix of the signature header.
SIGNATURE_VERSION: Final[str] = "v1"

HEADER_TENANT: Final[str] = "X-LeadQuali-Tenant"
HEADER_KEY: Final[str] = "X-LeadQuali-Key"
HEADER_TIMESTAMP: Final[str] = "X-LeadQuali-Timestamp"
HEADER_NONCE: Final[str] = "X-LeadQuali-Nonce"
HEADER_SIGNATURE: Final[str] = "X-LeadQuali-Signature"

#: How far a request's timestamp may sit from ours, in either direction. Five minutes is
#: the usual allowance for an unsynchronised web server; it also bounds how long a captured
#: request stays useful and how much the replay guard has to remember.
MAX_CLOCK_SKEW_SECONDS: Final[int] = 300

#: Nonce shape. Long enough that two honest clients never collide, short enough that a
#: header cannot be used as free storage. The character class keeps it loggable.
MIN_NONCE_CHARS: Final[int] = 12
MAX_NONCE_CHARS: Final[int] = 128
_NONCE_RE: Final[re.Pattern[str]] = re.compile(
    rf"\A[A-Za-z0-9_.:-]{{{MIN_NONCE_CHARS},{MAX_NONCE_CHARS}}}\Z"
)

#: Shortest signing secret a deployment may configure. 32 characters of random material is
#: well past what an HMAC needs; the check exists to catch a placeholder in a config file.
MIN_SIGNING_SECRET_CHARS: Final[int] = 32

_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
_TENANT_ID_RE: Final[re.Pattern[str]] = re.compile(TENANT_ID_PATTERN)
_KEY_ID_RE: Final[re.Pattern[str]] = re.compile(rf"\A[0-9a-f]{{{KEY_ID_CHARS}}}\Z")

#: Encoded argon2 hashes start with this. Checked at load so a SHA-256 hex digest left in
#: a config file after the move to argon2 is refused at startup rather than silently
#: failing every request from that customer.
_ARGON2_PREFIX: Final[str] = "$argon2"


class IngestCredentialsError(ValueError):
    """The configured ingest credentials are unusable.

    Raised at load time, not at request time: a deployment whose credentials cannot be
    parsed must fail to start rather than start and reject every real customer — or, far
    worse, start with an empty credential set and a code path that treats "no credentials
    configured" as "no authentication required".
    """


@dataclass(frozen=True, slots=True)
class StaticTenantCredentials:
    """One tenant's entry in a statically configured credential map."""

    tenant_id: str
    signing_secret: bytes
    keys: tuple[StoredApiKey, ...]
    status: str = ACTIVE_STATUS

    def __repr__(self) -> str:
        """Never render the signing secret."""
        return (
            f"StaticTenantCredentials(tenant_id={self.tenant_id!r}, status={self.status!r}, "
            f"keys={len(self.keys)}, signing_secret='<redacted>')"
        )


class StaticCredentials:
    """An :class:`IngestCredentialSource` over a dict decided at startup.

    For the tests and for a laptop. A deployment resolves against Postgres instead, so that
    a revocation is one ``UPDATE`` rather than an edit to a JSON secret every container has
    already cached; see ``docs/tenant-onboarding.md``.

    Args:
        credentials: One entry per tenant slug.
        verifier: The KDF. Injected because this module is standard-library only.
        now: The clock, for the rotation overlap window. Injected so a test can express
            "this key expired yesterday" without waiting.

    Raises:
        IngestCredentialsError: two tenants claim the same ``key_id``. It is the lookup
            handle, so a duplicate would make which tenant a key belongs to depend on dict
            ordering.
    """

    def __init__(
        self,
        credentials: Mapping[str, StaticTenantCredentials],
        *,
        verifier: SecretVerifierPort,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = dict(credentials)
        self._verifier = verifier
        self._now = now
        self._by_key_id: dict[str, tuple[StaticTenantCredentials, StoredApiKey]] = {}
        for tenant in self._credentials.values():
            for key in tenant.keys:
                if key.key_id in self._by_key_id:
                    raise IngestCredentialsError(
                        f"key_id {key.key_id!r} is configured for more than one tenant; "
                        "a key id is the lookup handle and has to be unique"
                    )
                self._by_key_id[key.key_id] = (tenant, key)

    def resolve(self, *, tenant_id: str, api_key: str) -> CredentialLookup:
        """Resolve the presented key. See :meth:`IngestCredentialSource.resolve`.

        Parsing and the row lookup are this class's; everything after that is
        :func:`~leadquali.app.credentials.decide_credential`, shared with the Postgres
        resolver so the two cannot drift.
        """
        parsed = parse_api_key(api_key)
        if parsed is None:
            return CredentialRejected(AuthFailure.MALFORMED)
        found = self._by_key_id.get(parsed.key_id)
        if found is None:
            return CredentialRejected(AuthFailure.UNKNOWN_TENANT)
        tenant, key = found

        decision = decide_credential(
            claimed_tenant_id=tenant_id,
            row_tenant_id=tenant.tenant_id,
            tenant_status=tenant.status,
            key=key,
            presented_secret=parsed.secret,
            now=self._now(),
            verifier=self._verifier,
        )
        if not isinstance(decision, CredentialAccepted):
            return decision
        return IngestCredential(
            tenant_id=decision.tenant_id,
            key_id=decision.key_id,
            signing_secret=tenant.signing_secret,
        )

    def __len__(self) -> int:
        return len(self._credentials)


def load_credentials(raw: str, *, verifier: SecretVerifierPort) -> StaticCredentials:
    """Parse the ingest credential map from its configured JSON form.

    The shape is::

        {"<tenant slug>": {
            "signing_secret": "...",
            "status": "active",
            "keys": [{"key_id": "<16 hex>", "key_hash": "$argon2id$...",
                      "revoked": false, "expires_at": "2026-09-10T00:00:00+00:00"}]
        }}

    ``status``, ``revoked`` and ``expires_at`` are optional; everything else is required.
    Every field is checked here, at startup, because the alternative is discovering a typo
    in a secret at 3am through a customer's form silently 401-ing.

    Args:
        raw: The JSON document.
        verifier: The KDF the resulting source will use.

    Returns:
        A credential source over the parsed map.

    Raises:
        IngestCredentialsError: the JSON is not an object of well-formed entries.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IngestCredentialsError(f"ingest credentials are not valid JSON: {error}") from None
    if not isinstance(parsed, dict):
        raise IngestCredentialsError("ingest credentials must be a JSON object keyed by tenant id")

    credentials: dict[str, StaticTenantCredentials] = {}
    for tenant_id, entry in parsed.items():
        if not _TENANT_ID_RE.match(str(tenant_id)):
            raise IngestCredentialsError(f"'{tenant_id}' is not a valid tenant id")
        if not isinstance(entry, dict):
            raise IngestCredentialsError(f"tenant '{tenant_id}': entry must be an object")
        secret = entry.get("signing_secret")
        if not isinstance(secret, str) or len(secret) < MIN_SIGNING_SECRET_CHARS:
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': signing_secret must be at least "
                f"{MIN_SIGNING_SECRET_CHARS} characters"
            )
        status = entry.get("status", ACTIVE_STATUS)
        if status not in TENANT_STATUSES:
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': status must be one of {', '.join(TENANT_STATUSES)}"
            )
        credentials[tenant_id] = StaticTenantCredentials(
            tenant_id=tenant_id,
            signing_secret=secret.encode("utf-8"),
            keys=_load_keys(tenant_id, entry.get("keys")),
            status=str(status),
        )
    return StaticCredentials(credentials, verifier=verifier)


def _load_keys(tenant_id: str, raw: object) -> tuple[StoredApiKey, ...]:
    """Validate one tenant's key list. A tenant with no key at all is a configuration bug."""
    if not isinstance(raw, list) or not raw:
        raise IngestCredentialsError(
            f"tenant '{tenant_id}': keys must be a non-empty list of key objects"
        )
    keys: list[StoredApiKey] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise IngestCredentialsError(f"tenant '{tenant_id}': each key must be an object")
        key_id = entry.get("key_id")
        key_hash = entry.get("key_hash")
        if not isinstance(key_id, str) or not _KEY_ID_RE.match(key_id):
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': key_id must be {KEY_ID_CHARS} lowercase hex characters"
            )
        if key_id in seen:
            raise IngestCredentialsError(f"tenant '{tenant_id}': duplicate key_id {key_id!r}")
        seen.add(key_id)
        if not isinstance(key_hash, str) or not key_hash.startswith(_ARGON2_PREFIX):
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': key_hash for {key_id} must be an encoded argon2 hash "
                f"(starting '{_ARGON2_PREFIX}')"
            )
        revoked = entry.get("revoked", False)
        if not isinstance(revoked, bool):
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': revoked for {key_id} must be true or false"
            )
        keys.append(
            StoredApiKey(
                key_id=key_id,
                key_hash=key_hash,
                revoked=revoked,
                expires_at=_load_expiry(tenant_id, key_id, entry.get("expires_at")),
            )
        )
    return tuple(keys)


def _load_expiry(tenant_id: str, key_id: str, raw: object) -> datetime | None:
    """Parse an optional RFC 3339 expiry, insisting it be timezone-aware.

    A naive timestamp would be compared against an aware ``now`` and raise at request time,
    on the one code path where an exception is least welcome. Refused at load instead.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise IngestCredentialsError(
            f"tenant '{tenant_id}': expires_at for {key_id} must be an RFC 3339 string"
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise IngestCredentialsError(
            f"tenant '{tenant_id}': expires_at for {key_id} is not a valid timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise IngestCredentialsError(
            f"tenant '{tenant_id}': expires_at for {key_id} must carry a UTC offset"
        )
    return parsed


# ------------------------------------------------------------------ the signed string


def signing_string(
    *, method: str, path: str, tenant_id: str, timestamp: str, nonce: str, body: bytes
) -> str:
    """The canonical string a signature is computed over.

    Eight newline-separated lines::

        LEADQUALI-HMAC-SHA256
        v1
        <HTTP method, uppercased>
        <request path, no query string>
        <tenant id>
        <unix timestamp, seconds>
        <nonce>
        <lowercase hex SHA-256 of the raw request body>

    Method and path are in there so a signature captured for one route cannot be replayed
    against another; the tenant is in there so a key holder cannot sign for a neighbour;
    the timestamp and nonce are in there so a captured request expires. The body appears as
    its digest rather than inline so the string stays a fixed size and the construction is
    the same for a 200-byte form post and a 60 KB one.
    """
    return "\n".join(
        (
            SIGNATURE_ALGORITHM,
            SIGNATURE_VERSION,
            method.upper(),
            path,
            tenant_id,
            timestamp,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    )


def sign(
    *,
    secret: bytes,
    method: str,
    path: str,
    tenant_id: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    """Return the value for :data:`HEADER_SIGNATURE`: ``v1=<hex hmac-sha256>``."""
    material = signing_string(
        method=method,
        path=path,
        tenant_id=tenant_id,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    digest = hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


# -------------------------------------------------------------------------- verifying


@dataclass(frozen=True, slots=True)
class Authenticated:
    """The request is from the tenant it claims to be from, using this key."""

    tenant_id: str
    key_id: str = ""
    """Which of the tenant's keys was used. Not secret, and recorded so an operator can see
    that a customer's old key is still in use days into a rotation overlap."""


@dataclass(frozen=True, slots=True)
class AuthRejected:
    """The request is not, and the caller is told nothing beyond "no"."""

    failure: AuthFailure


AuthResult = Authenticated | AuthRejected


class ReplayGuard:
    """Remembers recently used nonces so a captured request cannot be sent twice.

    Bounded in both directions: entries expire after ``ttl_seconds`` (which should exceed
    the signing window, or an attacker simply waits) and the whole structure is capped at
    ``max_entries``, oldest evicted first, so a flood of nonces cannot exhaust memory.
    Eviction under pressure means a replay could slip through during an attack, which is
    the right trade against the alternative of the process dying.

    Not thread-safe by construction because every operation is a single dict mutation under
    the GIL and the failure mode of a lost race is one extra remembered nonce.
    """

    def __init__(self, *, ttl_seconds: int = MAX_CLOCK_SKEW_SECONDS * 2, max_entries: int = 50_000):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    @property
    def size(self) -> int:
        """How many nonces are currently remembered."""
        return len(self._seen)

    def check_and_record(self, *, tenant_id: str, nonce: str, now: datetime) -> bool:
        """Record this nonce and return whether it was unused. ``False`` means replay."""
        stamp = now.timestamp()
        self._expire(stamp)
        key = (tenant_id, nonce)
        if key in self._seen:
            return False
        self._seen[key] = stamp
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return True

    def _expire(self, stamp: float) -> None:
        cutoff = stamp - self._ttl
        while self._seen:
            _, recorded = next(iter(self._seen.items()))
            if recorded > cutoff:
                return
            self._seen.popitem(last=False)


def verify(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    credentials: IngestCredentialSource,
    replay_guard: ReplayGuard,
    now: datetime,
) -> AuthResult:
    """Authenticate one signed request against one tenant's credentials.

    The steps are in order of cost: header shape, then the clock, then the credential
    lookup — the only step that touches a database or a KDF — then the HMAC, then the
    nonce. Everything a stranger can trigger without holding a real ``key_id`` is refused
    before the expensive step.

    Args:
        method: the HTTP method, as received.
        path: the request path, without query string. Must be what the client signed.
        headers: the request headers; matched case-insensitively.
        body: the **raw** body bytes, exactly as they arrived. Re-serialising the parsed
            JSON here would break every signature the moment a key order or a float
            repr differed, which is why the caller reads the stream once and passes the
            bytes through.
        credentials: the credential source.
        replay_guard: the nonce memory. Only a request that has otherwise verified
            completely consumes a nonce, so a forged request cannot burn the nonce a
            legitimate client is about to use.
        now: current time, injected so the window is testable without sleeping.

    Returns:
        :class:`Authenticated` with the tenant id and the key that was used, or
        :class:`AuthRejected` with the reason — which is for the log, not for the caller.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    tenant_id = lowered.get(HEADER_TENANT.lower(), "")
    api_key = lowered.get(HEADER_KEY.lower(), "")
    timestamp = lowered.get(HEADER_TIMESTAMP.lower(), "")
    nonce = lowered.get(HEADER_NONCE.lower(), "")
    signature = lowered.get(HEADER_SIGNATURE.lower(), "")

    if not (tenant_id and api_key and timestamp and nonce and signature):
        return AuthRejected(AuthFailure.MALFORMED)
    if not _TENANT_ID_RE.match(tenant_id) or not _NONCE_RE.match(nonce):
        return AuthRejected(AuthFailure.MALFORMED)

    version, separator, presented = signature.partition("=")
    if not separator or version != SIGNATURE_VERSION or not _SHA256_HEX_RE.match(presented.lower()):
        return AuthRejected(AuthFailure.MALFORMED)

    if not timestamp.isdigit() or len(timestamp) > 12:
        return AuthRejected(AuthFailure.MALFORMED)
    if abs(now.timestamp() - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return AuthRejected(AuthFailure.STALE)

    lookup = credentials.resolve(tenant_id=tenant_id, api_key=api_key)
    if isinstance(lookup, CredentialRejected):
        return AuthRejected(lookup.failure)

    expected = sign(
        secret=lookup.signing_secret,
        method=method,
        path=path,
        tenant_id=tenant_id,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    if not hmac.compare_digest(expected, f"{SIGNATURE_VERSION}={presented.lower()}"):
        return AuthRejected(AuthFailure.BAD_SIGNATURE)

    if not replay_guard.check_and_record(tenant_id=tenant_id, nonce=nonce, now=now):
        return AuthRejected(AuthFailure.REPLAY)

    return Authenticated(tenant_id=lookup.tenant_id, key_id=lookup.key_id)


__all__ = [
    "ACTIVE_STATUS",
    "HEADER_KEY",
    "HEADER_NONCE",
    "HEADER_SIGNATURE",
    "HEADER_TENANT",
    "HEADER_TIMESTAMP",
    "MAX_CLOCK_SKEW_SECONDS",
    "MAX_NONCE_CHARS",
    "MIN_NONCE_CHARS",
    "MIN_SIGNING_SECRET_CHARS",
    "SIGNATURE_ALGORITHM",
    "SIGNATURE_VERSION",
    "TENANT_STATUSES",
    "AuthFailure",
    "AuthRejected",
    "AuthResult",
    "Authenticated",
    "CredentialLookup",
    "CredentialRejected",
    "IngestCredential",
    "IngestCredentialSource",
    "IngestCredentialsError",
    "ReplayGuard",
    "SecretVerifierPort",
    "StaticCredentials",
    "StaticTenantCredentials",
    "StoredApiKey",
    "load_credentials",
    "sign",
    "signing_string",
    "verify",
]
