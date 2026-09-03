"""Authenticating a request from a customer's web form.

The ingest endpoint is the only public, internet-facing surface in the system and it is
unauthenticated by default: everything that arrives is attacker-controlled. Two secrets
per tenant guard it, and they do different jobs.

* The **API key** travels in a header on every request and says *who is calling*. It is
  stored as a SHA-256 hash, never in the clear, and compared with
  :func:`hmac.compare_digest`.
* The **signing secret** never leaves the two ends. It authenticates the request itself:
  the exact bytes of the body, the method, the path, the tenant, a timestamp and a nonce
  are folded into one string and HMAC-SHA256'd. A key lifted from a browser's network tab
  or a proxy log is useless without it.

**Why SHA-256 and not argon2** (plan §8 says argon2-hashed at rest): argon2 exists to make
*low-entropy human-chosen passwords* expensive to guess. An ingest key is 128+ bits of
machine-generated randomness, where a brute-force is infeasible regardless of the hash, and
a memory-hard KDF on the request path would spend a large slice of a 200 ms budget on every
single lead — a self-inflicted denial of service. If keys ever become human-chosen, this is
the one function to change.

**Replay.** The signed material carries a unix timestamp and a client nonce. A request
outside :data:`MAX_CLOCK_SKEW_SECONDS` is refused, and a nonce already seen inside that
window is refused by :class:`ReplayGuard`. The guard is per-process, which is the honest
limit of what an in-process structure can do: behind several Lambda instances a replay can
land on a different one inside the window. Two things behind it make that harmless rather
than merely unlikely — the ingest handler's own ``(tenant_id, submission_id)`` idempotency
means a replayed body creates no second lead and no second enqueue, and #26's API Gateway
usage plans cap the rate at which anyone can try. A shared nonce store (Redis/DynamoDB)
would close it completely and is deliberately not built here for one endpoint.

Everything in this module is standard library only, so #26's Lambda handler and #30's
form-side signer can both import it, and so the construction can be reimplemented from
:func:`signing_string` alone in whatever language the customer's site is written in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

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

#: Compared against when no credential was found, so an unknown tenant costs the same
#: work as a known one. The response is identical either way; this closes the timing
#: channel that would otherwise turn the endpoint into a tenant-enumeration oracle.
_DUMMY_HASH: Final[str] = hashlib.sha256(b"leadquali-unknown-tenant").hexdigest()


class IngestCredentialsError(ValueError):
    """The configured ingest credentials are unusable.

    Raised at load time, not at request time: a deployment whose credentials cannot be
    parsed must fail to start rather than start and reject every real customer — or, far
    worse, start with an empty credential set and a code path that treats "no credentials
    configured" as "no authentication required".
    """


@dataclass(frozen=True, slots=True)
class IngestCredential:
    """One tenant's ingest secrets.

    ``signing_secret`` is bytes because that is what :func:`hmac.new` wants and because it
    discourages the string handling that ends with a secret in an f-string.
    """

    tenant_id: str
    api_key_sha256: str
    signing_secret: bytes

    def __repr__(self) -> str:
        """Never render the secrets: a repr ends up in tracebacks, and tracebacks in logs."""
        return f"IngestCredential(tenant_id={self.tenant_id!r}, api_key_sha256='<redacted>')"


@runtime_checkable
class IngestCredentialSource(Protocol):
    """Where the ingest endpoint looks up a tenant's credentials.

    Returns ``None`` for a tenant it does not know rather than raising: an unknown tenant
    is an ordinary rejection on a public endpoint, not an exceptional condition, and the
    caller must be unable to tell it apart from a bad key anyway.
    """

    def get(self, tenant_id: str) -> IngestCredential | None:
        """Return the credential for ``tenant_id``, or ``None`` if there is none."""
        ...


class StaticCredentials:
    """An :class:`IngestCredentialSource` over a dict decided at startup."""

    def __init__(self, credentials: Mapping[str, IngestCredential]) -> None:
        self._credentials = dict(credentials)

    def get(self, tenant_id: str) -> IngestCredential | None:
        """Return the credential for ``tenant_id``, or ``None``."""
        return self._credentials.get(tenant_id)

    def __len__(self) -> int:
        return len(self._credentials)


def hash_api_key(api_key: str) -> str:
    """The at-rest form of an API key: lowercase hex SHA-256.

    Use it to produce the ``api_key_sha256`` value for a tenant's configuration entry. The
    key itself is generated by whoever onboards the tenant, is shown to them once, and is
    never stored anywhere in this system.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def load_credentials(raw: str) -> StaticCredentials:
    """Parse the ingest credential map from its configured JSON form.

    The shape is ``{"<tenant_id>": {"api_key_sha256": "<64 hex>", "signing_secret": "..."}}``.
    Every field is checked here, at startup, because the alternative is discovering a typo
    in a secret at 3am through a customer's form silently 401-ing.

    Raises:
        IngestCredentialsError: the JSON is not an object of well-formed entries.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise IngestCredentialsError(f"ingest credentials are not valid JSON: {error}") from None
    if not isinstance(parsed, dict):
        raise IngestCredentialsError("ingest credentials must be a JSON object keyed by tenant id")

    credentials: dict[str, IngestCredential] = {}
    for tenant_id, entry in parsed.items():
        if not _TENANT_ID_RE.match(str(tenant_id)):
            raise IngestCredentialsError(f"'{tenant_id}' is not a valid tenant id")
        if not isinstance(entry, dict):
            raise IngestCredentialsError(f"tenant '{tenant_id}': entry must be an object")
        key_hash = entry.get("api_key_sha256")
        secret = entry.get("signing_secret")
        if not isinstance(key_hash, str) or not _SHA256_HEX_RE.match(key_hash):
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': api_key_sha256 must be 64 lowercase hex characters"
            )
        if not isinstance(secret, str) or len(secret) < MIN_SIGNING_SECRET_CHARS:
            raise IngestCredentialsError(
                f"tenant '{tenant_id}': signing_secret must be at least "
                f"{MIN_SIGNING_SECRET_CHARS} characters"
            )
        credentials[tenant_id] = IngestCredential(
            tenant_id=tenant_id,
            api_key_sha256=key_hash,
            signing_secret=secret.encode("utf-8"),
        )
    return StaticCredentials(credentials)


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


class AuthFailure(StrEnum):
    """Why a request was refused.

    For logs and metrics only. It is never returned to the caller and never distinguishes
    one 401 from another on the wire: telling a stranger that the tenant exists but the key
    is wrong is a free enumeration oracle.
    """

    MALFORMED = "malformed"
    """A required header is missing or cannot be parsed."""

    UNKNOWN_TENANT = "unknown_tenant"
    BAD_KEY = "bad_key"
    BAD_SIGNATURE = "bad_signature"
    STALE = "stale"
    """The timestamp is outside the accepted window in either direction."""

    REPLAY = "replay"
    """This nonce was already used inside the window."""


@dataclass(frozen=True, slots=True)
class Authenticated:
    """The request is from the tenant it claims to be from."""

    tenant_id: str


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
        :class:`Authenticated` with the tenant id, or :class:`AuthRejected` with the
        reason — which is for the log, not for the caller.
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

    credential = credentials.get(tenant_id)
    # The comparison happens either way: an unknown tenant must cost what a known one
    # costs, or the response time answers the question the response body refuses to.
    stored_hash = _DUMMY_HASH if credential is None else credential.api_key_sha256
    key_matches = hmac.compare_digest(stored_hash, hash_api_key(api_key))
    if credential is None:
        return AuthRejected(AuthFailure.UNKNOWN_TENANT)
    if not key_matches:
        return AuthRejected(AuthFailure.BAD_KEY)

    expected = sign(
        secret=credential.signing_secret,
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

    return Authenticated(tenant_id=tenant_id)


__all__ = [
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
    "AuthFailure",
    "AuthRejected",
    "AuthResult",
    "Authenticated",
    "IngestCredential",
    "IngestCredentialSource",
    "IngestCredentialsError",
    "ReplayGuard",
    "StaticCredentials",
    "hash_api_key",
    "load_credentials",
    "sign",
    "signing_string",
    "verify",
]
