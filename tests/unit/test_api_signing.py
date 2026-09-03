"""Request authentication for the public ingest edge.

This is the one module in the system where a test failing open would be a security hole
rather than a bug, so the assertions are about what is *rejected*: a forged signature, a
signature over a different body, a stale clock, a replayed nonce, a key that belongs to
another tenant. The happy path is one test; the rest is the attack surface.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from leadquali.api.signing import (
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    MAX_CLOCK_SKEW_SECONDS,
    SIGNATURE_ALGORITHM,
    SIGNATURE_VERSION,
    Authenticated,
    AuthFailure,
    AuthRejected,
    IngestCredential,
    IngestCredentialsError,
    ReplayGuard,
    StaticCredentials,
    hash_api_key,
    load_credentials,
    sign,
    signing_string,
    verify,
)

TENANT = "acme"
API_KEY = "lq_live_2f7c1d6a9b4e5f80"
SECRET = "s3cr3t-signing-material-at-least-32-chars"
BODY = b'{"submission_id":"abc","form":{}}'
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

CREDENTIALS = StaticCredentials(
    {
        TENANT: IngestCredential(
            tenant_id=TENANT,
            api_key_sha256=hash_api_key(API_KEY),
            signing_secret=SECRET.encode("utf-8"),
        )
    }
)


def headers(
    *,
    tenant: str = TENANT,
    api_key: str = API_KEY,
    timestamp: datetime | str = NOW,
    nonce: str = "nonce-0000000001",
    body: bytes = BODY,
    signature: str | None = None,
    secret: str = SECRET,
    path: str = "/leads",
    method: str = "POST",
) -> dict[str, str]:
    """A complete, correctly signed header set, with any one piece overridable."""
    stamp = timestamp if isinstance(timestamp, str) else str(int(timestamp.timestamp()))
    if signature is None:
        signature = sign(
            secret=secret.encode("utf-8"),
            method=method,
            path=path,
            tenant_id=tenant,
            timestamp=stamp,
            nonce=nonce,
            body=body,
        )
    return {
        HEADER_TENANT: tenant,
        HEADER_KEY: api_key,
        HEADER_TIMESTAMP: stamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
    }


def check(
    *,
    tenant: str = TENANT,
    api_key: str = API_KEY,
    timestamp: datetime | str = NOW,
    nonce: str = "nonce-0000000001",
    body: bytes = BODY,
    signature: str | None = None,
    secret: str = SECRET,
    path: str = "/leads",
    now: datetime = NOW,
    guard: ReplayGuard | None = None,
) -> Authenticated | AuthRejected:
    """Verify a request against ``POST /leads``, with one piece of it tampered with."""
    return verify(
        method="POST",
        path="/leads",
        headers=headers(
            tenant=tenant,
            api_key=api_key,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
            signature=signature,
            secret=secret,
            path=path,
        ),
        body=body,
        credentials=CREDENTIALS,
        replay_guard=guard if guard is not None else ReplayGuard(),
        now=now,
    )


def rejection(result: Authenticated | AuthRejected) -> AuthFailure:
    assert isinstance(result, AuthRejected), f"expected a rejection, got {result!r}"
    return result.failure


# ---------------------------------------------------------------- the signing string


def test_the_signing_string_binds_method_path_tenant_time_nonce_and_body() -> None:
    """Every field the receiver trusts is inside the MAC, or it is not trusted."""
    material = signing_string(
        method="post",
        path="/leads",
        tenant_id=TENANT,
        timestamp="1772539200",
        nonce="n1",
        body=BODY,
    )
    assert material.split("\n") == [
        SIGNATURE_ALGORITHM,
        SIGNATURE_VERSION,
        "POST",
        "/leads",
        TENANT,
        "1772539200",
        "n1",
        hashlib.sha256(BODY).hexdigest(),
    ]


def test_the_signature_is_hmac_sha256_over_that_string() -> None:
    """Stated as an equation so another language can reimplement it from this file."""
    material = signing_string(
        method="POST",
        path="/leads",
        tenant_id=TENANT,
        timestamp="1772539200",
        nonce="n1",
        body=BODY,
    )
    expected = hmac.new(SECRET.encode(), material.encode("utf-8"), hashlib.sha256).hexdigest()
    assert (
        sign(
            secret=SECRET.encode(),
            method="POST",
            path="/leads",
            tenant_id=TENANT,
            timestamp="1772539200",
            nonce="n1",
            body=BODY,
        )
        == f"{SIGNATURE_VERSION}={expected}"
    )


# ------------------------------------------------------------------------ acceptance


def test_a_correctly_signed_request_is_accepted() -> None:
    result = check()
    assert isinstance(result, Authenticated)
    assert result.tenant_id == TENANT


def test_a_timestamp_at_the_edge_of_the_window_is_still_accepted() -> None:
    stamp = NOW - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS - 1)
    assert isinstance(check(timestamp=stamp), Authenticated)


# ------------------------------------------------------------------------ rejection


def test_an_unknown_tenant_is_rejected() -> None:
    assert rejection(check(tenant="nobody")) is AuthFailure.UNKNOWN_TENANT


def test_a_wrong_api_key_is_rejected_even_with_a_valid_signature() -> None:
    assert rejection(check(api_key="lq_live_wrong")) is AuthFailure.BAD_KEY


def test_a_missing_header_is_rejected_without_a_lookup() -> None:
    for header in (HEADER_TENANT, HEADER_KEY, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE):
        incomplete = headers()
        del incomplete[header]
        result = verify(
            method="POST",
            path="/leads",
            headers=incomplete,
            body=BODY,
            credentials=CREDENTIALS,
            replay_guard=ReplayGuard(),
            now=NOW,
        )
        assert rejection(result) is AuthFailure.MALFORMED, header


def test_headers_are_matched_case_insensitively() -> None:
    lowered = {name.lower(): value for name, value in headers().items()}
    result = verify(
        method="POST",
        path="/leads",
        headers=lowered,
        body=BODY,
        credentials=CREDENTIALS,
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert isinstance(result, Authenticated)


def test_a_forged_signature_is_rejected() -> None:
    forged = f"{SIGNATURE_VERSION}={'0' * 64}"
    assert rejection(check(signature=forged)) is AuthFailure.BAD_SIGNATURE


def test_a_signature_made_with_the_wrong_secret_is_rejected() -> None:
    assert rejection(check(secret="another-tenants-signing-secret-32")) is AuthFailure.BAD_SIGNATURE


def test_a_body_altered_in_flight_is_rejected() -> None:
    """The signature covers the raw bytes, so a proxy that reformats the JSON breaks it."""
    signed = headers(body=BODY)
    result = verify(
        method="POST",
        path="/leads",
        headers=signed,
        body=BODY.replace(b"abc", b"xyz"),
        credentials=CREDENTIALS,
        replay_guard=ReplayGuard(),
        now=NOW,
    )
    assert rejection(result) is AuthFailure.BAD_SIGNATURE


def test_a_signature_for_another_route_is_rejected() -> None:
    assert rejection(check(path="/leads/other")) is AuthFailure.BAD_SIGNATURE


def test_an_unknown_signature_version_is_rejected() -> None:
    assert rejection(check(signature="v9=" + "0" * 64)) is AuthFailure.MALFORMED
    assert rejection(check(signature="0" * 64)) is AuthFailure.MALFORMED


@pytest.mark.parametrize("offset", [MAX_CLOCK_SKEW_SECONDS + 1, -(MAX_CLOCK_SKEW_SECONDS + 1)])
def test_a_stale_or_far_future_timestamp_is_rejected(offset: int) -> None:
    assert rejection(check(timestamp=NOW + timedelta(seconds=offset))) is AuthFailure.STALE


@pytest.mark.parametrize("stamp", ["", "not-a-number", "1e9", "12.5", "99999999999999999999"])
def test_an_unparseable_timestamp_is_rejected(stamp: str) -> None:
    assert rejection(check(timestamp=stamp)) in {AuthFailure.MALFORMED, AuthFailure.STALE}


def test_a_replayed_nonce_is_rejected_the_second_time() -> None:
    guard = ReplayGuard()
    assert isinstance(check(guard=guard), Authenticated)
    assert rejection(check(guard=guard)) is AuthFailure.REPLAY


def test_a_replay_is_only_recorded_for_a_request_that_actually_verified() -> None:
    """A forged request must not be able to burn a nonce a real client will use."""
    guard = ReplayGuard()
    assert rejection(check(guard=guard, signature=f"{SIGNATURE_VERSION}={'0' * 64}")) is (
        AuthFailure.BAD_SIGNATURE
    )
    assert isinstance(check(guard=guard), Authenticated)


def test_nonces_are_scoped_per_tenant() -> None:
    guard = ReplayGuard()
    other = StaticCredentials(
        {
            "other": IngestCredential(
                tenant_id="other",
                api_key_sha256=hash_api_key(API_KEY),
                signing_secret=SECRET.encode(),
            )
        }
    )
    assert isinstance(check(guard=guard), Authenticated)
    result = verify(
        method="POST",
        path="/leads",
        headers=headers(tenant="other"),
        body=BODY,
        credentials=other,
        replay_guard=guard,
        now=NOW,
    )
    assert isinstance(result, Authenticated)


@pytest.mark.parametrize("nonce", ["", "short", "x" * 200, "bad nonce\n"])
def test_an_implausible_nonce_is_rejected(nonce: str) -> None:
    assert rejection(check(nonce=nonce)) is AuthFailure.MALFORMED


def test_the_replay_guard_forgets_nonces_older_than_the_signing_window() -> None:
    """Unbounded memory is its own denial of service; the timestamp window bounds it."""
    guard = ReplayGuard(ttl_seconds=60)
    assert guard.check_and_record(tenant_id=TENANT, nonce="n" * 16, now=NOW) is True
    assert guard.check_and_record(tenant_id=TENANT, nonce="n" * 16, now=NOW) is False
    later = NOW + timedelta(seconds=61)
    assert guard.check_and_record(tenant_id=TENANT, nonce="n" * 16, now=later) is True
    assert guard.size <= 1


def test_the_replay_guard_is_bounded_in_size() -> None:
    guard = ReplayGuard(ttl_seconds=3600, max_entries=10)
    for index in range(50):
        assert guard.check_and_record(tenant_id=TENANT, nonce=f"nonce-{index:08d}", now=NOW) is True
    assert guard.size <= 10


# ----------------------------------------------------------------------- credentials


def test_hash_api_key_is_the_sha256_of_the_key() -> None:
    assert hash_api_key(API_KEY) == hashlib.sha256(API_KEY.encode()).hexdigest()


def test_credentials_load_from_json() -> None:
    entry = f'{{"api_key_sha256": "{hash_api_key(API_KEY)}", "signing_secret": "{SECRET}"}}'
    raw = f'{{"acme": {entry}}}'
    credentials = load_credentials(raw)
    stored = credentials.get("acme")
    assert stored is not None
    assert stored.signing_secret == SECRET.encode()


def test_an_unknown_tenant_reads_back_as_none_not_an_error() -> None:
    assert CREDENTIALS.get("nobody") is None


HASHED = hash_api_key(API_KEY)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"acme": "just-a-string"}',
        f'{{"acme": {{"signing_secret": "{SECRET}"}}}}',
        f'{{"acme": {{"api_key_sha256": "zz", "signing_secret": "{SECRET}"}}}}',
        f'{{"acme": {{"api_key_sha256": "{HASHED}", "signing_secret": "tooshort"}}}}',
        f'{{"Bad Tenant": {{"api_key_sha256": "{HASHED}", "signing_secret": "{SECRET}"}}}}',
    ],
)
def test_malformed_credential_configuration_fails_loudly_at_load(raw: str) -> None:
    """A deployment with unreadable credentials must not start and accept everything."""
    with pytest.raises(IngestCredentialsError):
        load_credentials(raw)


def test_a_credential_never_renders_its_secret() -> None:
    """Invariant-adjacent: a repr lands in a traceback, and tracebacks land in logs."""
    rendered = repr(CREDENTIALS.get(TENANT))
    assert SECRET not in rendered
    assert API_KEY not in rendered
