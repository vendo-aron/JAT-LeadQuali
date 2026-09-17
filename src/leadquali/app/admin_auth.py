"""Who may use the admin, and for how long.

Three mechanisms, all of them standard library, all of them pure functions of bytes and a
clock so that every property below is testable without a browser, a database or a cookie
jar:

* a **signed session token** carrying ``{subject, issued_at, expires_at}``;
* a **CSRF token** derived from the session token, so a form post has to come from a page
  this session was actually served;
* a **login gate** that makes a password guess cost the same whoever it is for, and makes
  the tenth wrong guess for one username cost nothing at all.

Why there is no session library
-------------------------------

A session here is one HMAC over one JSON object with an absolute expiry. A library would
bring cookie parsing, a middleware, a storage backend and a signing scheme we would then
have to read anyway to know what it guarantees — for a page a handful of staff open. The
whole verification is :func:`verify_session`, and it fits on a screen.

**Absolute expiry, no sliding renewal.** Twelve hours from issue, full stop. A sliding
session is one that a stolen cookie keeps alive forever as long as the thief keeps using
it, which is precisely the case the expiry exists for. The cost is that a long day ends
with a second login.

Why argon2 here when #31 says machine keys do not need it
---------------------------------------------------------

:mod:`leadquali.adapters.keyhash_argon2` argues at length that a *memory-hard* KDF is not
what protects a tenant's ingest key: that key is 128 bits of machine-generated randomness,
so an offline attacker with the hash has nothing to guess and any preimage-resistant hash
would do. The argon2 there buys defence in depth, and the module spends its docstring
explaining why it is affordable rather than why it is necessary.

**Staff passwords are the opposite case, and that is why the same hasher is used for the
opposite reason.** A password is chosen by a person, so its entropy is perhaps 30 bits on
a good day and it is very likely reused somewhere else. Against that, the cost per guess
*is* the defence, and a memory-hard KDF is the only thing standing between a leaked hash
map and every account in it. The two decisions look contradictory — "argon2 is overkill
for keys" and "argon2 is essential for passwords" — and they are not: they are the same
question asked of two inputs with a thousand-fold difference in entropy.

So :class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher` is reused here through the
:class:`StaffVerifierPort` Protocol, at the same OWASP parameters, and this module never
imports the KDF (``CLAUDE.md``'s layering rule, enforced by ``tests/unit/test_layering``).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "ADMIN_PREFIX",
    "CSRF_FIELD",
    "LOGIN_FAILED_MESSAGE",
    "LOGIN_FAILURE_THRESHOLD",
    "LOGIN_FAILURE_WINDOW",
    "MIN_SESSION_SECRET_BYTES",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "AdminAuthError",
    "LoginOutcome",
    "SessionFailure",
    "SessionRejected",
    "StaffAuthenticator",
    "StaffSession",
    "StaffVerifierPort",
    "csrf_token",
    "csrf_token_matches",
    "load_staff_credentials",
    "mint_session",
    "verify_session",
]

#: Where the admin is mounted. Defined here rather than in the router so that the login
#: redirect and the cookie path cannot drift apart from the routes they protect.
ADMIN_PREFIX: Final[str] = "/admin"

#: The session cookie's name. ``__Host-`` is deliberate: browsers only accept that prefix
#: on a Secure cookie with ``Path=/`` and no ``Domain``, which makes a subdomain unable to
#: set or overwrite it. It therefore cannot be used over plain HTTP, which is correct for
#: a staff console and stated in ``docs/admin.md``.
SESSION_COOKIE: Final[str] = "__Host-lq_admin"

#: The hidden form field every state-changing POST carries.
CSRF_FIELD: Final[str] = "csrf_token"

#: Absolute session lifetime. Twelve hours: a working day plus the overrun, and short
#: enough that a cookie copied off a laptop is not a standing credential.
SESSION_TTL: Final[timedelta] = timedelta(hours=12)

#: Shortest signing secret accepted. 32 bytes is the HMAC-SHA256 block's worth of entropy;
#: below it the signature is only as strong as whatever was configured in a hurry.
MIN_SESSION_SECRET_BYTES: Final[int] = 32

#: Wrong passwords for one username before the gate trips. Mirrors
#: :data:`~leadquali.adapters.keyhash_argon2.FAILURE_THRESHOLD` and for the same reason:
#: the KDF is the expensive part, so an attacker who can make us run it at will has a
#: denial of service whether or not they ever guess anything.
LOGIN_FAILURE_THRESHOLD: Final[int] = 10

#: How long a tripped gate stays tripped, and how long a failure counts for.
LOGIN_FAILURE_WINDOW: Final[timedelta] = timedelta(minutes=5)

#: The only thing a failed login is ever told. Not "no such user", not "wrong password",
#: not "too many attempts" — each of those answers a question a stranger was asking.
LOGIN_FAILED_MESSAGE: Final[str] = "Login failed."

#: Verified against when the username is unknown, so that an unknown user costs the same
#: KDF call a known one does. Encodes the string ``"no such account"``, which is not a
#: password and cannot be one: nothing ever hashes to it because nothing hashes here at
#: all except through the configured verifier.
_ABSENT_USER_HASH: Final[str] = (
    "$argon2id$v=19$m=19456,t=2,p=1$bm9zdWNoYWNjb3VudHg$bm9zdWNoYWNjb3VudG5vc3VjaGFjYw"
)

#: The prefix an encoded argon2 hash starts with. Checked when the credential document is
#: loaded, because a plaintext password in that file would otherwise "work" — the verifier
#: would refuse every login and nobody would know why.
_ARGON2_PREFIX: Final[str] = "$argon2"

_SESSION_VERSION: Final[str] = "v1"


class AdminAuthError(Exception):
    """The admin's authentication configuration is unusable. Never carries a secret."""


class SessionFailure(StrEnum):
    """Why a presented session was not accepted.

    Recorded for the operator's logs, never rendered to the browser: every one of these
    produces the same redirect to the login page, because telling a stranger that their
    cookie was well-formed but expired is telling them the signing secret has not changed.
    """

    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"


@dataclass(frozen=True, slots=True)
class StaffSession:
    """A verified session: who is logged in, since when, and until when."""

    subject: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionRejected:
    """A presented session that will not be honoured."""

    failure: SessionFailure


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """What one login attempt did.

    :attr:`message` is the same string on every failure, and there is no field carrying a
    reason to the browser. :attr:`gated` exists for the log line only.
    """

    subject: str | None
    gated: bool = False

    @property
    def authenticated(self) -> bool:
        """Whether this attempt produced a session subject."""
        return self.subject is not None

    @property
    def message(self) -> str:
        """What the page says. Identical for every kind of failure."""
        return "" if self.authenticated else LOGIN_FAILED_MESSAGE


@runtime_checkable
class StaffVerifierPort(Protocol):
    """Checks a presented password against a stored hash.

    Structurally satisfied by
    :class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher`, which is what the
    production wiring passes. A Protocol so that ``app`` never imports the KDF and a test
    can verify instantly.
    """

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        """Whether ``secret`` is the secret behind ``key_hash``."""
        ...


# ------------------------------------------------------------------------- the session


def mint_session(
    *, secret: bytes, subject: str, now: datetime, ttl: timedelta = SESSION_TTL
) -> str:
    """Issue a session token for ``subject``, valid from ``now`` for ``ttl``.

    Args:
        secret: The signing secret, at least :data:`MIN_SESSION_SECRET_BYTES` long.
        subject: The staff username this session belongs to.
        now: Wall-clock time from the clock port.
        ttl: How long the session lasts. Absolute; nothing renews it.

    Returns:
        ``<base64url payload>.<base64url signature>``.

    Raises:
        AdminAuthError: the secret is too short, or the subject is blank.
    """
    _check_secret(secret)
    cleaned = subject.strip()
    if not cleaned:
        raise AdminAuthError("a session needs a subject")
    payload = {
        "v": _SESSION_VERSION,
        "sub": cleaned,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded}.{_sign(secret, encoded)}"


def verify_session(*, secret: bytes, token: str, now: datetime) -> StaffSession | SessionRejected:
    """Verify a presented session token. Never raises for anything a browser can send.

    The order matters: the signature is checked before the payload is believed, so an
    expiry read out of the token is one this process put there.

    Args:
        secret: The signing secret.
        token: The cookie's value, whatever it happens to contain.
        now: Wall-clock time.

    Returns:
        A :class:`StaffSession`, or a :class:`SessionRejected` saying which check failed.
    """
    _check_secret(secret)
    encoded, separator, signature = token.partition(".")
    if not separator or not encoded or not signature:
        return SessionRejected(failure=SessionFailure.MALFORMED)
    if not hmac.compare_digest(signature, _sign(secret, encoded)):
        return SessionRejected(failure=SessionFailure.BAD_SIGNATURE)
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return SessionRejected(failure=SessionFailure.MALFORMED)
    if not isinstance(payload, dict) or payload.get("v") != _SESSION_VERSION:
        return SessionRejected(failure=SessionFailure.MALFORMED)
    subject = payload.get("sub")
    issued = payload.get("iat")
    expires = payload.get("exp")
    if not isinstance(subject, str) or not subject:
        return SessionRejected(failure=SessionFailure.MALFORMED)
    if not isinstance(issued, int) or not isinstance(expires, int) or isinstance(issued, bool):
        return SessionRejected(failure=SessionFailure.MALFORMED)
    issued_at = datetime.fromtimestamp(issued, tz=UTC)
    expires_at = datetime.fromtimestamp(expires, tz=UTC)
    if now < issued_at:
        # A host whose clock ran fast when the token was minted. Refusing is the safe
        # direction: the alternative is honouring a session that outlives its own window.
        return SessionRejected(failure=SessionFailure.NOT_YET_VALID)
    if now >= expires_at:
        return SessionRejected(failure=SessionFailure.EXPIRED)
    return StaffSession(subject=subject, issued_at=issued_at, expires_at=expires_at)


# ---------------------------------------------------------------------------- the CSRF


def csrf_token(*, secret: bytes, session_token: str) -> str:
    """Derive this session's CSRF token.

    Derived rather than stored, so there is no server-side table to keep, and bound to the
    session token rather than to the subject, so a token minted for one login does not
    survive a logout and a second one. A config editor with no CSRF protection is a
    one-click rubric rewrite from any page a logged-in operator happens to open.
    """
    _check_secret(secret)
    return _sign(secret, f"csrf:{session_token}")


def csrf_token_matches(presented: str, *, secret: bytes, session_token: str) -> bool:
    """Whether ``presented`` is this session's CSRF token. Constant-time, never raises."""
    if not presented:
        return False
    return hmac.compare_digest(presented, csrf_token(secret=secret, session_token=session_token))


# --------------------------------------------------------------------------- the login


def load_staff_credentials(document: str) -> dict[str, str]:
    """Parse the staff credential secret: a JSON object of username to argon2 hash.

    The document is what #28's Secrets Manager entry holds. It is a map and not a list of
    records because there is exactly one hash per staff username and a list would let two
    entries disagree about which.

    Args:
        document: The secret's string value.

    Returns:
        ``{username: encoded argon2 hash}``, with at least one entry.

    Raises:
        AdminAuthError: the document is not a non-empty object of username to encoded
            argon2 hash. The message says which rule was broken and **never quotes the
            document**, which is a file of password hashes.
    """
    try:
        parsed = json.loads(document)
    except ValueError as error:
        raise AdminAuthError(
            "the staff credential secret is not valid JSON; it must be an object of "
            '{"username": "$argon2id$..."}'
        ) from error
    if not isinstance(parsed, dict) or not parsed:
        raise AdminAuthError(
            "the staff credential secret must be a non-empty JSON object mapping each "
            "staff username to its argon2 hash; an empty one would lock everybody out"
        )
    credentials: dict[str, str] = {}
    for username, encoded in parsed.items():
        name = username.strip() if isinstance(username, str) else ""
        if not name:
            raise AdminAuthError("the staff credential secret has a blank username")
        if not isinstance(encoded, str) or not encoded.startswith(_ARGON2_PREFIX):
            raise AdminAuthError(
                f"the staff credential secret's entry for '{name}' is not an encoded "
                "argon2 hash. Generate it with `python -m leadquali.adminctl hash`; a "
                "plaintext password here would refuse every login without saying why"
            )
        credentials[name] = encoded
    return credentials


class StaffAuthenticator:
    """Checks a staff username and password, at a constant cost and a bounded rate.

    Two properties, and both of them are about what an attacker learns rather than about
    what a legitimate operator experiences:

    **An unknown username costs the same as a known one.** The verifier is run against
    :data:`_ABSENT_USER_HASH` when there is no such account, so the response time does not
    say which staff accounts exist.

    **A username stops being guessable after :data:`LOGIN_FAILURE_THRESHOLD` failures.**
    The counter is per username and per :data:`LOGIN_FAILURE_WINDOW`, one success clears
    it, and a gated attempt is refused *before* the KDF runs — the same shape as #31's key
    gate, for the same reason: the KDF is the expensive part, so an attacker who can make
    us run it at will has a denial of service whether or not they ever guess anything.

    The gate is per process, like #31's and like the rate limiter's, and under N warm
    containers an attacker gets N times the allowance. That is the honest limit of an
    in-process counter and it is stated in ``docs/admin.md``; the bound that does not
    depend on process count is the identity-aware proxy the document recommends in front.

    Args:
        credentials: ``{username: encoded argon2 hash}`` from
            :func:`load_staff_credentials`.
        verifier: The KDF, behind :class:`StaffVerifierPort`.
        failure_threshold: Failures for one username before the gate trips.
        failure_window: How long the gate stays tripped.
    """

    def __init__(
        self,
        *,
        credentials: dict[str, str],
        verifier: StaffVerifierPort,
        failure_threshold: int = LOGIN_FAILURE_THRESHOLD,
        failure_window: timedelta = LOGIN_FAILURE_WINDOW,
    ) -> None:
        if failure_threshold < 1:
            raise AdminAuthError(f"failure_threshold must be positive, got {failure_threshold}")
        self._credentials = dict(credentials)
        self._verifier = verifier
        self._threshold = failure_threshold
        self._window = failure_window
        self._failures: dict[str, tuple[int, datetime]] = {}

    def authenticate(self, *, username: str, password: str, now: datetime) -> LoginOutcome:
        """Check one login attempt.

        Args:
            username: Whatever was typed into the form.
            password: Whatever was typed into the form.
            now: Wall-clock time, from the clock port.

        Returns:
            A :class:`LoginOutcome`. A failure carries no reason beyond
            :data:`LOGIN_FAILED_MESSAGE`.
        """
        name = username.strip()
        if self._gated(name, now):
            return LoginOutcome(subject=None, gated=True)
        stored = self._credentials.get(name, _ABSENT_USER_HASH)
        matched = self._verifier.verify_secret(key_id=name, secret=password, key_hash=stored)
        if matched and name in self._credentials:
            self._failures.pop(name, None)
            return LoginOutcome(subject=name)
        self._record_failure(name, now)
        return LoginOutcome(subject=None)

    # ----------------------------------------------------------------------- the gate

    def _gated(self, username: str, now: datetime) -> bool:
        found = self._failures.get(username)
        if found is None:
            return False
        count, last = found
        if now - last > self._window:
            del self._failures[username]
            return False
        return count >= self._threshold

    def _record_failure(self, username: str, now: datetime) -> None:
        count, last = self._failures.get(username, (0, now))
        if now - last > self._window:
            count = 0
        self._failures[username] = (count + 1, now)


# ------------------------------------------------------------------------- internals


def _check_secret(secret: bytes) -> None:
    if len(secret) < MIN_SESSION_SECRET_BYTES:
        raise AdminAuthError(
            f"the admin session secret must be at least {MIN_SESSION_SECRET_BYTES} bytes; "
            f"got {len(secret)}"
        )


def _sign(secret: bytes, message: str) -> str:
    return _b64(hmac.digest(secret, message.encode("utf-8"), "sha256"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)
