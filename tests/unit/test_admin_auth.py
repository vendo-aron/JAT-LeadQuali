"""The staff session cookie, the CSRF token and the login gate.

Everything here runs without a database and without a browser, which is the point: #31's
and #33's reviews both found a security-critical property asserted only by a Docker-gated
test, so a mutation that broke it left the suite green. Session forgery, expiry, CSRF
binding and the login rate limit are all pure functions of bytes and a clock, and they are
tested as such.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from leadquali.app.admin_auth import (
    LOGIN_FAILURE_THRESHOLD,
    LOGIN_FAILURE_WINDOW,
    SESSION_TTL,
    AdminAuthError,
    LoginOutcome,
    SessionFailure,
    SessionRejected,
    StaffAuthenticator,
    StaffSession,
    csrf_token,
    csrf_token_matches,
    load_staff_credentials,
    mint_session,
    verify_session,
)

SECRET = b"a" * 32
OTHER_SECRET = b"b" * 32
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def test_a_minted_session_verifies_and_carries_its_subject() -> None:
    token = mint_session(secret=SECRET, subject="ada", now=NOW)
    verified = verify_session(secret=SECRET, token=token, now=NOW)

    assert isinstance(verified, StaffSession)
    assert verified.subject == "ada"
    assert verified.issued_at == NOW
    assert verified.expires_at == NOW + SESSION_TTL


def test_a_session_is_refused_under_a_different_secret() -> None:
    token = mint_session(secret=SECRET, subject="ada", now=NOW)

    rejected = verify_session(secret=OTHER_SECRET, token=token, now=NOW)

    assert isinstance(rejected, SessionRejected)
    assert rejected.failure is SessionFailure.BAD_SIGNATURE


def test_editing_the_subject_invalidates_the_signature() -> None:
    """The payload is not merely encoded; it is signed, so it cannot be edited."""
    token = mint_session(secret=SECRET, subject="ada", now=NOW)
    payload, _, signature = token.partition(".")
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    reforged = base64.urlsafe_b64encode(decoded.replace(b'"ada"', b'"eve"')).decode().rstrip("=")
    forged = f"{reforged}.{signature}"

    assert isinstance(verify_session(secret=SECRET, token=forged, now=NOW), SessionRejected)


@pytest.mark.parametrize("token", ["", "nonsense", "a.b", "....", "YWJj.YWJj"])
def test_a_malformed_token_is_a_rejection_and_never_an_exception(token: str) -> None:
    rejected = verify_session(secret=SECRET, token=token, now=NOW)

    assert isinstance(rejected, SessionRejected)


def test_a_session_expires_absolutely_and_does_not_slide() -> None:
    token = mint_session(secret=SECRET, subject="ada", now=NOW)

    just_inside = NOW + SESSION_TTL - timedelta(seconds=1)
    still_good = verify_session(secret=SECRET, token=token, now=just_inside)
    assert isinstance(still_good, StaffSession)

    just_outside = NOW + SESSION_TTL + timedelta(seconds=1)
    expired = verify_session(secret=SECRET, token=token, now=just_outside)
    assert isinstance(expired, SessionRejected)
    assert expired.failure is SessionFailure.EXPIRED


def test_a_session_from_the_future_is_refused() -> None:
    """A clock skewed forward on the minting host must not extend anybody's session."""
    token = mint_session(secret=SECRET, subject="ada", now=NOW + timedelta(hours=1))

    rejected = verify_session(secret=SECRET, token=token, now=NOW)

    assert isinstance(rejected, SessionRejected)
    assert rejected.failure is SessionFailure.NOT_YET_VALID


def test_a_short_secret_is_refused_rather_than_used() -> None:
    with pytest.raises(AdminAuthError):
        mint_session(secret=b"short", subject="ada", now=NOW)


# ------------------------------------------------------------------------------- CSRF


def test_a_csrf_token_is_bound_to_one_session() -> None:
    mine = mint_session(secret=SECRET, subject="ada", now=NOW)
    theirs = mint_session(secret=SECRET, subject="eve", now=NOW)

    presented = csrf_token(secret=SECRET, session_token=mine)

    assert csrf_token_matches(presented, secret=SECRET, session_token=mine)
    assert not csrf_token_matches(presented, secret=SECRET, session_token=theirs)


def test_a_csrf_token_is_bound_to_the_secret_and_refuses_junk() -> None:
    session = mint_session(secret=SECRET, subject="ada", now=NOW)
    presented = csrf_token(secret=SECRET, session_token=session)

    assert not csrf_token_matches(presented, secret=OTHER_SECRET, session_token=session)
    assert not csrf_token_matches("", secret=SECRET, session_token=session)
    assert not csrf_token_matches("nonsense", secret=SECRET, session_token=session)


# ------------------------------------------------------------------------------ login


class ReversibleHasher:
    """A staff verifier with argon2's interface, instant and reversible.

    Counts verifications, because "the gate stops the KDF running" is a claim about work
    done and the only honest way to assert it is to count the work.
    """

    def __init__(self) -> None:
        self.calls = 0

    def hash_secret(self, secret: str) -> str:
        return f"$argon2id$fake${secret}"

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        del key_id
        self.calls += 1
        return key_hash == f"$argon2id$fake${secret}"


def authenticator(hasher: ReversibleHasher | None = None) -> StaffAuthenticator:
    resolved = hasher if hasher is not None else ReversibleHasher()
    return StaffAuthenticator(
        credentials={"ada": "$argon2id$fake$correct horse"},
        verifier=resolved,
    )


def test_the_right_password_authenticates() -> None:
    outcome = authenticator().authenticate(username="ada", password="correct horse", now=NOW)

    assert outcome.authenticated
    assert outcome.subject == "ada"


def test_the_wrong_password_does_not() -> None:
    outcome = authenticator().authenticate(username="ada", password="wrong", now=NOW)

    assert not outcome.authenticated
    assert outcome.subject is None


def test_an_unknown_user_still_costs_a_verification() -> None:
    """Otherwise the response time is an oracle for which staff accounts exist."""
    hasher = ReversibleHasher()

    outcome = authenticator(hasher).authenticate(username="eve", password="anything", now=NOW)

    assert not outcome.authenticated
    assert hasher.calls == 1, "an unknown username must not short-circuit the KDF"


def test_repeated_failures_gate_the_username_without_running_the_kdf() -> None:
    hasher = ReversibleHasher()
    auth = authenticator(hasher)

    for _ in range(LOGIN_FAILURE_THRESHOLD):
        assert not auth.authenticate(username="ada", password="wrong", now=NOW).authenticated
    calls_before = hasher.calls

    gated = auth.authenticate(username="ada", password="correct horse", now=NOW)

    assert not gated.authenticated
    assert gated.gated
    assert hasher.calls == calls_before, "a gated username must not reach the KDF"


def test_the_gate_lifts_when_the_window_passes() -> None:
    auth = authenticator()
    for _ in range(LOGIN_FAILURE_THRESHOLD):
        auth.authenticate(username="ada", password="wrong", now=NOW)

    later = NOW + LOGIN_FAILURE_WINDOW + timedelta(seconds=1)

    assert auth.authenticate(username="ada", password="correct horse", now=later).authenticated


def test_one_correct_password_clears_the_counter() -> None:
    auth = authenticator()
    for _ in range(LOGIN_FAILURE_THRESHOLD - 1):
        auth.authenticate(username="ada", password="wrong", now=NOW)

    assert auth.authenticate(username="ada", password="correct horse", now=NOW).authenticated
    for _ in range(LOGIN_FAILURE_THRESHOLD - 1):
        auth.authenticate(username="ada", password="wrong", now=NOW)
    assert auth.authenticate(username="ada", password="correct horse", now=NOW).authenticated


def test_the_outcome_never_carries_a_reason_a_stranger_could_read() -> None:
    """A failed login says only "login failed" — never which half was wrong."""
    unknown = authenticator().authenticate(username="eve", password="x", now=NOW)
    wrong = authenticator().authenticate(username="ada", password="x", now=NOW)

    assert isinstance(unknown, LoginOutcome)
    assert unknown.message == wrong.message


# ------------------------------------------------------------- the credential document


def test_credentials_load_from_a_username_to_hash_map() -> None:
    loaded = load_staff_credentials('{"ada": "$argon2id$v=19$m=19456,t=2,p=1$abc$def"}')

    assert loaded == {"ada": "$argon2id$v=19$m=19456,t=2,p=1$abc$def"}


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        '{"ada": 7}',
        '{"": "$argon2id$x"}',
        '{"ada": ""}',
        '{"ada": "plaintext"}',
        "{}",
    ],
)
def test_a_credential_document_that_is_not_a_username_to_argon2_map_is_refused(
    document: str,
) -> None:
    with pytest.raises(AdminAuthError):
        load_staff_credentials(document)


def test_the_credential_error_never_quotes_the_document() -> None:
    """The document is a file of password hashes; an error message about it is a leak."""
    secretish = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"
    with pytest.raises(AdminAuthError) as raised:
        load_staff_credentials(f'{{"ada": "{secretish}", "bad": 7}}')

    assert secretish not in str(raised.value)
