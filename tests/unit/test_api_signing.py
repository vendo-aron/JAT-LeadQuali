"""Request authentication for the public ingest edge.

This is the one module in the system where a test failing open would be a security hole
rather than a bug, so the assertions are about what is *rejected*: a forged signature, a
signature over a different body, a stale clock, a replayed nonce, a revoked key, an expired
key, a key that belongs to another tenant. The happy path is one test; the rest is the
attack surface.

The verifier is the real :class:`~leadquali.adapters.keyhash_argon2.Argon2KeyHasher` rather
than a double. A fake that always said "yes" would let a mistake in the credential source's
ordering — running the KDF before the revocation check, say — pass every test here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
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
    CredentialRejected,
    IngestCredential,
    IngestCredentialsError,
    ReplayGuard,
    StaticCredentials,
    StaticTenantCredentials,
    StoredApiKey,
    load_credentials,
    sign,
    signing_string,
    verify,
)
from leadquali.app.api_keys import ApiKeyParts, KeyEnvironment

TENANT = "acme"
KEY_ID = "2f7c1d6a9b4e5f80"
KEY_SECRET = "kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI"
API_KEY = ApiKeyParts(environment=KeyEnvironment.LIVE, key_id=KEY_ID, secret=KEY_SECRET).text
SECRET = "s3cr3t-signing-material-at-least-32-chars"
OTHER_SECRET = "Zx91QsPIkf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5"
BODY = b'{"submission_id":"abc","form":{}}'
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

#: One process-wide hasher, so the whole file costs a handful of real KDF calls.
VERIFIER = Argon2KeyHasher()
KEY_HASH = VERIFIER.hash_secret(KEY_SECRET)
OTHER_HASH = VERIFIER.hash_secret(OTHER_SECRET)


def key_text(key_id: str, secret: str) -> str:
    """The wire form of a key with these parts."""
    return ApiKeyParts(environment=KeyEnvironment.LIVE, key_id=key_id, secret=secret).text


def tenant_entry(
    *,
    tenant_id: str = TENANT,
    key_id: str = KEY_ID,
    key_hash: str = KEY_HASH,
    revoked: bool = False,
    expires_at: datetime | None = None,
    status: str = "active",
    secret: str = SECRET,
) -> StaticTenantCredentials:
    """One tenant's stored credentials, with any one piece overridable."""
    return StaticTenantCredentials(
        tenant_id=tenant_id,
        signing_secret=secret.encode("utf-8"),
        keys=(
            StoredApiKey(key_id=key_id, key_hash=key_hash, revoked=revoked, expires_at=expires_at),
        ),
        status=status,
    )


def credentials(*entries: StaticTenantCredentials, now: datetime = NOW) -> StaticCredentials:
    """A credential source over the given tenants, with the clock pinned."""
    chosen = entries or (tenant_entry(),)
    return StaticCredentials(
        {entry.tenant_id: entry for entry in chosen}, verifier=VERIFIER, now=lambda: now
    )


CREDENTIALS = credentials()


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
    source: StaticCredentials | None = None,
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
        credentials=source if source is not None else CREDENTIALS,
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
    assert result.key_id == KEY_ID


def test_a_timestamp_at_the_edge_of_the_window_is_still_accepted() -> None:
    stamp = NOW - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS - 1)
    assert isinstance(check(timestamp=stamp), Authenticated)


def test_a_second_key_lets_a_tenant_rotate_without_downtime() -> None:
    """The property the whole ``tenant_api_keys`` table exists for."""
    source = StaticCredentials(
        {
            TENANT: StaticTenantCredentials(
                tenant_id=TENANT,
                signing_secret=SECRET.encode(),
                keys=(
                    StoredApiKey(key_id=KEY_ID, key_hash=KEY_HASH),
                    StoredApiKey(key_id="b" * 16, key_hash=OTHER_HASH),
                ),
            )
        },
        verifier=VERIFIER,
        now=lambda: NOW,
    )
    assert isinstance(check(source=source), Authenticated)
    assert isinstance(
        check(source=source, api_key=key_text("b" * 16, OTHER_SECRET), nonce="nonce-0000000002"),
        Authenticated,
    )


# ------------------------------------------------------------------------ rejection


def test_an_unknown_key_is_rejected() -> None:
    """No row for this ``key_id``: one indexed miss, no KDF, no distinction on the wire."""
    assert rejection(check(api_key=key_text("f" * 16, KEY_SECRET))) is AuthFailure.UNKNOWN_TENANT


def test_an_unknown_tenant_header_with_a_real_key_is_rejected() -> None:
    """A key holder cannot borrow a neighbour's name, and is told nothing about it."""
    assert rejection(check(tenant="nobody")) is AuthFailure.UNKNOWN_TENANT


def test_a_key_from_another_tenant_is_rejected() -> None:
    """Tenant A's key presented under tenant B's header. Invariant 4 at the door."""
    source = credentials(
        tenant_entry(),
        tenant_entry(tenant_id="other", key_id="b" * 16, key_hash=OTHER_HASH),
    )
    assert (
        rejection(check(source=source, api_key=key_text("b" * 16, OTHER_SECRET)))
        is AuthFailure.UNKNOWN_TENANT
    )


def test_a_wrong_secret_on_a_real_key_id_is_rejected() -> None:
    assert rejection(check(api_key=key_text(KEY_ID, "a" * 43))) is AuthFailure.BAD_KEY


def test_a_revoked_key_is_rejected_immediately() -> None:
    """The acceptance criterion. Nothing about a row is cached, so this needs no expiry."""
    assert rejection(check(source=credentials(tenant_entry(revoked=True)))) is (
        AuthFailure.REVOKED_KEY
    )


def test_a_key_past_its_rotation_overlap_is_rejected() -> None:
    source = credentials(tenant_entry(expires_at=NOW - timedelta(seconds=1)))
    assert rejection(check(source=source)) is AuthFailure.REVOKED_KEY


def test_a_key_inside_its_rotation_overlap_still_works() -> None:
    source = credentials(tenant_entry(expires_at=NOW + timedelta(days=7)))
    assert isinstance(check(source=source), Authenticated)


@pytest.mark.parametrize("status", ["suspended", "disabled"])
def test_a_tenant_that_is_not_active_is_refused_with_its_own_reason(status: str) -> None:
    """Distinct from a bad key on purpose: this caller has already proved who it is."""
    source = credentials(tenant_entry(status=status))
    assert rejection(check(source=source)) is AuthFailure.TENANT_SUSPENDED


@pytest.mark.parametrize(
    "api_key",
    ["", "not-a-key", "lq_live_short_x", "lq_staging_" + "0" * 16 + "_" + "a" * 43],
)
def test_a_malformed_key_is_refused_without_touching_the_kdf(api_key: str) -> None:
    """The cheapest rejection there is: no lookup, no KDF."""
    before = VERIFIER.kdf_calls
    assert rejection(check(api_key=api_key)) is AuthFailure.MALFORMED
    assert VERIFIER.kdf_calls == before


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
    other = credentials(tenant_entry(tenant_id="other", key_id="c" * 16, key_hash=OTHER_HASH))
    assert isinstance(check(guard=guard), Authenticated)
    result = verify(
        method="POST",
        path="/leads",
        headers=headers(tenant="other", api_key=key_text("c" * 16, OTHER_SECRET)),
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


# ------------------------------------------------------- the order of the cheap checks


def test_the_kdf_runs_only_after_every_free_check_has_passed() -> None:
    """The property the whole "argon2 on the request path" argument rests on.

    The hasher is built **inside the test**, not shared with the rest of the file: the
    module-level one has already memoised ``(KEY_HASH, sha256(KEY_SECRET))``, so a probe
    presenting the correct secret would be a free memo hit and this test would pass with
    the checks in any order at all. Each probe also presents a *wrong* secret, so that a
    KDF call is the only way the counter can move.
    """
    fresh = Argon2KeyHasher()
    forged = key_text(KEY_ID, "w" * 43)

    def source(**overrides: object) -> StaticCredentials:
        entry = tenant_entry(**overrides)  # type: ignore[arg-type]
        return StaticCredentials({entry.tenant_id: entry}, verifier=fresh, now=lambda: NOW)

    assert rejection(check(source=source(revoked=True), api_key=forged)) is (
        AuthFailure.REVOKED_KEY
    )
    assert (
        rejection(check(source=source(expires_at=NOW - timedelta(days=1)), api_key=forged))
        is AuthFailure.REVOKED_KEY
    )
    assert rejection(check(source=source(), tenant="nobody", api_key=forged)) is (
        AuthFailure.UNKNOWN_TENANT
    )
    assert fresh.kdf_calls == 0, "a free check let the KDF run"

    # ...and the same forged key, with nothing free left to refuse it, does cost one.
    assert rejection(check(source=source(), api_key=forged)) is AuthFailure.BAD_KEY
    assert fresh.kdf_calls == 1


def test_a_suspended_tenant_is_not_an_oracle_for_someone_without_the_secret() -> None:
    """The 403 is the one answer that says something about the account, so only a caller
    that has proved it holds the secret may reach it. A wrong secret gets the ordinary,
    indistinguishable rejection instead — which also means an integrator with a genuinely
    wrong key is never told in writing that their key was fine."""
    suspended = credentials(tenant_entry(status="suspended"))

    assert rejection(check(source=suspended, api_key=key_text(KEY_ID, "w" * 43))) is (
        AuthFailure.BAD_KEY
    )
    assert rejection(check(source=suspended)) is AuthFailure.TENANT_SUSPENDED


# ----------------------------------------------------------------------- credentials


def _doc(
    *,
    tenant: str = "acme",
    tenant_fields: dict[str, object] | None = None,
    **key_fields: object,
) -> str:
    """A well-formed credential document with one field overridden.

    Built with ``json.dumps`` rather than string interpolation so that a test about
    malformed JSON is the only place malformed JSON appears.
    """
    key: dict[str, object] = {"key_id": KEY_ID, "key_hash": KEY_HASH}
    key.update(key_fields)
    entry: dict[str, object] = {"signing_secret": SECRET, "keys": [key]}
    entry.update(tenant_fields or {})
    return json.dumps({tenant: entry})


def test_credentials_load_from_json() -> None:
    loaded = load_credentials(_doc(), verifier=VERIFIER)
    resolved = loaded.resolve(tenant_id="acme", api_key=API_KEY)
    assert isinstance(resolved, IngestCredential)
    assert resolved.signing_secret == SECRET.encode()
    assert resolved.key_id == KEY_ID


def test_a_loaded_revoked_key_is_rejected() -> None:
    loaded = load_credentials(_doc(revoked=True), verifier=VERIFIER)
    assert loaded.resolve(tenant_id="acme", api_key=API_KEY) == CredentialRejected(
        AuthFailure.REVOKED_KEY
    )


def test_a_loaded_expiry_is_honoured() -> None:
    loaded = load_credentials(_doc(expires_at="2020-01-01T00:00:00+00:00"), verifier=VERIFIER)
    assert loaded.resolve(tenant_id="acme", api_key=API_KEY) == CredentialRejected(
        AuthFailure.REVOKED_KEY
    )


def test_an_unknown_tenant_reads_back_as_a_rejection_not_an_error() -> None:
    assert CREDENTIALS.resolve(tenant_id="nobody", api_key=API_KEY) == CredentialRejected(
        AuthFailure.UNKNOWN_TENANT
    )


def test_one_key_id_may_not_belong_to_two_tenants() -> None:
    """It is the lookup handle; a duplicate would make ownership depend on dict order."""
    with pytest.raises(IngestCredentialsError, match="more than one tenant"):
        StaticCredentials(
            {"a": tenant_entry(tenant_id="a"), "b": tenant_entry(tenant_id="b")},
            verifier=VERIFIER,
        )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"acme": "just-a-string"}',
        json.dumps({"acme": {"keys": [{"key_id": KEY_ID, "key_hash": KEY_HASH}]}}),
        json.dumps({"acme": {"signing_secret": SECRET}}),
        json.dumps({"acme": {"signing_secret": SECRET, "keys": []}}),
        json.dumps({"acme": {"signing_secret": SECRET, "keys": ["nope"]}}),
        _doc(key_id="ZZ"),
        _doc(key_id=KEY_ID.upper()),
        # A SHA-256 digest left behind by the pre-argon2 scheme.
        _doc(key_hash="0" * 64),
        json.dumps({"acme": {"signing_secret": "tooshort", "keys": [{"key_id": KEY_ID}]}}),
        # A naive expiry would raise at request time rather than at load.
        _doc(expires_at="2020-01-01T00:00:00"),
        _doc(expires_at="not a date"),
        _doc(expires_at=17),
        _doc(revoked="yes"),
        _doc(tenant_fields={"status": "asleep"}),
        _doc(tenant="Bad Tenant"),
    ],
)
def test_malformed_credential_configuration_fails_loudly_at_load(raw: str) -> None:
    """A deployment with unreadable credentials must not start and accept everything."""
    with pytest.raises(IngestCredentialsError):
        load_credentials(raw, verifier=VERIFIER)


def test_two_keys_in_one_entry_may_not_share_a_key_id() -> None:
    raw = json.dumps(
        {
            "acme": {
                "signing_secret": SECRET,
                "keys": [
                    {"key_id": KEY_ID, "key_hash": KEY_HASH},
                    {"key_id": KEY_ID, "key_hash": OTHER_HASH},
                ],
            }
        }
    )
    with pytest.raises(IngestCredentialsError, match="duplicate key_id"):
        load_credentials(raw, verifier=VERIFIER)


def test_a_credential_never_renders_its_secret() -> None:
    """Invariant-adjacent: a repr lands in a traceback, and tracebacks land in logs."""
    resolved = CREDENTIALS.resolve(tenant_id=TENANT, api_key=API_KEY)
    assert isinstance(resolved, IngestCredential)
    rendered = repr(resolved)
    assert SECRET not in rendered
    assert API_KEY not in rendered
    assert KEY_SECRET not in rendered
    assert KEY_ID in rendered


def test_the_stored_records_never_render_secret_material() -> None:
    entry = tenant_entry()
    assert SECRET not in repr(entry)
    assert KEY_HASH not in repr(entry.keys[0])
