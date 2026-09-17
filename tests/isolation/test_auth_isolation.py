"""Authentication isolation: a key is one tenant's, and the body does not get a vote.

Three things are being proved, and the third is the one people get wrong.

1. **A key cannot be presented under another tenant's name.** Tenant A holds a real,
   unrevoked key. Sending it with ``X-LeadQuali-Tenant: B`` is refused — and refused as
   ``unknown_tenant``, so it is indistinguishable on the wire from a ``key_id`` that has
   never existed. Asserted against **both** credential sources: the in-memory one the tests
   use and the Postgres one that actually runs in production. #31's review found that every
   security property there was proved against the double alone, and that deleting the
   adapter's cross-tenant check left the whole suite green.

2. **A signature is over one tenant's secret.** A's key with A's header, signed with B's
   signing secret, is a 401. Per-tenant secrets are what make that true; a single process
   secret would not.

3. **The authenticated identity wins, always.** A request body that names a tenant does not
   get to choose one. The envelope forbids unknown fields outright, so a top-level
   ``tenant_id`` is a 422 before anything is stored. A *form field* called ``tenant_id``
   is legal — the form belongs to the customer — and it is carried through as data: the
   assertion is on **what the store received**, not on the status code, because a 202 says
   nothing about which tenant the row was filed under.

Suspension is here too rather than in a module of its own, because it is the same
mechanism seen from the other side: suspending one tenant must stop that tenant and only
that tenant. Both halves are in one test, since a test that only proved A stopped would
pass if the whole endpoint had.

Where the decision actually lives
---------------------------------

Since #31's review fixes, every rule about whether a presented key may authenticate a
request is :func:`~leadquali.app.credentials.decide_credential` — a pure function both
resolvers call once their own ``key_id`` lookup has produced a row. That is #31's answer to
the same trap this suite exists for: the rules used to be written twice, once on the path
production runs and once on the path the offline suite exercised. So the first section below
drives that function directly, exhaustively and with no database, and the two resolvers are
then checked for agreeing with it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any, Final, NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import ClauseElement
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.adapters.store_tenants import PostgresIngestCredentials
from leadquali.api.main import INGEST_PATH, IngestDeps, create_app
from leadquali.api.ratelimit import (
    DEFAULT_TENANT_RATE_LIMIT,
    TenantRateLimit,
    TenantRateLimiter,
)
from leadquali.api.signing import (
    ACTIVE_STATUS,
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    TENANT_STATUSES,
    AuthFailure,
    CredentialLookup,
    CredentialRejected,
    IngestCredential,
    IngestCredentialSource,
    StaticCredentials,
    StaticTenantCredentials,
    StoredApiKey,
    sign,
)
from leadquali.app.credentials import CredentialAccepted, CredentialDecision, decide_credential
from leadquali.app.ingest import IngestService
from tests.fakes import FakeClock, InMemoryLeadStore
from tests.isolation.repositories import (
    KEY_ID_A,
    KEY_ID_B,
    KEY_SECRET_A,
    KEY_SECRET_B,
    SIGNING_SECRET_REF,
    TENANT_A,
    TENANT_B,
    DictSecretResolver,
    api_key_for,
)
from tests.sqlcapture import CannedResult, parameters, sql_text

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)

SECRET_A: Final[bytes] = b"alpha-instruments-signing-secret-32ch"
SECRET_B: Final[bytes] = b"zenith-freight-signing-secret-32chars"

#: One hasher for the module, so the argon2 verifications are paid for once and memoised.
VERIFIER: Final[Argon2KeyHasher] = Argon2KeyHasher()
KEY_HASH_A: Final[str] = VERIFIER.hash_secret(KEY_SECRET_A)
KEY_HASH_B: Final[str] = VERIFIER.hash_secret(KEY_SECRET_B)

API_KEY_A: Final[str] = api_key_for(KEY_ID_A, KEY_SECRET_A)
API_KEY_B: Final[str] = api_key_for(KEY_ID_B, KEY_SECRET_B)

SECRETS: Final[Mapping[str, bytes]] = {TENANT_A: SECRET_A, TENANT_B: SECRET_B}
API_KEYS: Final[Mapping[str, str]] = {TENANT_A: API_KEY_A, TENANT_B: API_KEY_B}


# ------------------------------------------------------------------- the one decision


def decide(
    *,
    claimed: str,
    owner: str = TENANT_A,
    status: str = ACTIVE_STATUS,
    secret: str = KEY_SECRET_A,
    revoked: bool = False,
    expires_at: dt.datetime | None = None,
    now: dt.datetime = NOW,
) -> CredentialDecision:
    """Run the shared decision over one key row, with each input independently selectable.

    Every argument has a default that is the accepting case, so each test below changes
    exactly one thing and the reader can see which.
    """
    return decide_credential(
        claimed_tenant_id=claimed,
        row_tenant_id=owner,
        tenant_status=status,
        key=StoredApiKey(
            key_id=KEY_ID_A, key_hash=KEY_HASH_A, revoked=revoked, expires_at=expires_at
        ),
        presented_secret=secret,
        now=now,
        verifier=VERIFIER,
    )


def test_the_decision_accepts_a_live_key_under_its_own_tenant() -> None:
    """The positive control. Every refusal below is one change away from this."""
    decision = decide(claimed=TENANT_A)

    assert isinstance(decision, CredentialAccepted)
    assert decision.tenant_id == TENANT_A
    assert decision.key_id == KEY_ID_A


def test_the_decision_refuses_a_key_claimed_by_the_wrong_tenant() -> None:
    """The cross-tenant rule, at the one place it is now written.

    Reported as ``unknown_tenant`` rather than as anything more specific: a caller must not
    be able to tell "that key belongs to somebody else" from "that key does not exist", or
    the endpoint enumerates its own customers for anyone who has seen a key in a header.
    """
    decision = decide(claimed=TENANT_B, owner=TENANT_A)

    assert decision == CredentialRejected(AuthFailure.UNKNOWN_TENANT)


def test_the_tenant_check_runs_before_everything_that_could_leak_more() -> None:
    """A key that is wrong in two ways reports only the first, and the first is ownership.

    A revoked key belonging to somebody else must answer ``unknown_tenant``, not
    ``revoked_key``: the second would confirm that the key_id exists and that the tenant
    named in the header is not its owner, which is two facts more than a stranger should
    get. The same for a suspended owner and for a wrong secret.
    """
    also_wrong: tuple[Mapping[str, Any], ...] = (
        {"revoked": True},
        {"status": "suspended"},
        {"secret": "not-the-right-secret"},
        {"expires_at": NOW - dt.timedelta(days=1)},
    )
    for changes in also_wrong:
        decision = decide(claimed=TENANT_B, owner=TENANT_A, **changes)
        assert decision == CredentialRejected(AuthFailure.UNKNOWN_TENANT), changes


@pytest.mark.parametrize(
    ("name", "changes", "failure"),
    [
        ("revoked", {"revoked": True}, AuthFailure.REVOKED_KEY),
        (
            "rotation overlap closed",
            {"expires_at": NOW - dt.timedelta(seconds=1)},
            AuthFailure.REVOKED_KEY,
        ),
        ("wrong secret", {"secret": "not-the-right-secret"}, AuthFailure.BAD_KEY),
        ("tenant suspended", {"status": "suspended"}, AuthFailure.TENANT_SUSPENDED),
        ("tenant disabled", {"status": "disabled"}, AuthFailure.TENANT_SUSPENDED),
    ],
)
def test_the_decision_refuses_each_way_a_key_can_be_unusable(
    name: str, changes: Mapping[str, Any], failure: AuthFailure
) -> None:
    """Every refusal the shared decision can produce, under the key's own tenant.

    Exhaustive on purpose: this function is now the only implementation of these rules, so a
    gap here is a gap everywhere rather than in one of two copies.
    """
    assert decide(claimed=TENANT_A, **changes) == CredentialRejected(failure), name


def test_a_suspended_tenant_with_a_wrong_secret_is_told_its_key_is_bad() -> None:
    """The consequence of checking status *after* the KDF, and it is the point of doing so.

    ``tenant_suspended`` is the one refusal that is not the identical 401, so it must be
    reachable only by a caller who has proved it holds the secret. An API key travels in a
    header in the clear on every submission; if status were checked first, anyone who had
    ever seen one could ask whether that account had been suspended for non-payment without
    holding the secret at all.
    """
    assert decide(claimed=TENANT_A, status="suspended") == CredentialRejected(
        AuthFailure.TENANT_SUSPENDED
    )
    assert decide(
        claimed=TENANT_A, status="suspended", secret="not-the-right-secret"
    ) == CredentialRejected(AuthFailure.BAD_KEY)


def test_the_decision_never_answers_unavailable() -> None:
    """``UNAVAILABLE`` means a dependency could not be read, and this function reads none.

    It is pure — no database, no secret store, no clock of its own — so every outcome it
    can produce is a statement about the caller. A dependency outage is the resolver's
    business, and ``test_one_tenants_secret_store_outage_is_not_another_tenants_outage``
    covers what that turns into.
    """
    outcomes = {
        decide(claimed=claimed, owner=TENANT_A, status=status, secret=secret, revoked=revoked)
        for claimed in (TENANT_A, TENANT_B)
        for status in TENANT_STATUSES
        for secret in (KEY_SECRET_A, "not-the-right-secret")
        for revoked in (False, True)
    }
    failures = {item.failure for item in outcomes if isinstance(item, CredentialRejected)}
    assert AuthFailure.UNAVAILABLE not in failures, failures
    assert failures == {
        AuthFailure.UNKNOWN_TENANT,
        AuthFailure.REVOKED_KEY,
        AuthFailure.BAD_KEY,
        AuthFailure.TENANT_SUSPENDED,
    }


# --------------------------------------------------------------- the credential sources


def static_credentials(*, suspended: frozenset[str] = frozenset()) -> StaticCredentials:
    """Both tenants' credentials, with any of them optionally not active."""
    return StaticCredentials(
        {
            tenant: StaticTenantCredentials(
                tenant_id=tenant,
                signing_secret=SECRETS[tenant],
                keys=(StoredApiKey(key_id=key_id, key_hash=key_hash),),
                status="suspended" if tenant in suspended else ACTIVE_STATUS,
            )
            for tenant, key_id, key_hash in (
                (TENANT_A, KEY_ID_A, KEY_HASH_A),
                (TENANT_B, KEY_ID_B, KEY_HASH_B),
            )
        },
        verifier=VERIFIER,
        now=lambda: NOW,
    )


class _KeyRow(NamedTuple):
    """The row ``PostgresIngestCredentials.resolve`` reads off its one indexed ``SELECT``."""

    slug: str
    status: str
    hmac_secret_ref: str | None
    key_hash: str
    revoked_at: dt.datetime | None
    expires_at: dt.datetime | None


class _KeyTableSession:
    """A session that answers the credential lookup out of a dict keyed by ``key_id``.

    A double of the *table*, not of the adapter: the real statement is built, the real
    ``key_id`` is read out of its bound parameters, and everything the adapter decides
    afterwards — ownership, revocation, expiry, status, argon2 — is the production code
    path. Substituting the adapter itself is what #31's review found wanting.
    """

    def __init__(self, rows: Mapping[str, _KeyRow], seen: list[str]) -> None:
        self._rows = rows
        self._seen = seen

    def execute(self, statement: ClauseElement, *args: Any, **kwargs: Any) -> CannedResult:
        del args, kwargs
        sql = sql_text(statement)
        assert sql.startswith("select"), f"the credential lookup issued a write:\n{sql}"
        assert "tenant_api_keys.key_id =" in sql, sql
        presented = [
            str(value) for value in parameters(statement).values() if str(value) in self._rows
        ]
        assert len(presented) == 1, f"could not find the key_id in {parameters(statement)}"
        self._seen.append(presented[0])
        return CannedResult(row=self._rows[presented[0]])

    def __enter__(self) -> _KeyTableSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _KeyTableSessions(sessionmaker[Session]):
    """A ``sessionmaker`` over the fake ``tenant_api_keys`` table."""

    def __init__(self, rows: Mapping[str, _KeyRow]) -> None:
        super().__init__()
        self._rows = dict(rows)
        self.lookups: list[str] = []

    def begin(self) -> Any:
        """Hand out the key-table session instead of a real one."""
        return _KeyTableSession(self._rows, self.lookups)


class FlakySecretResolver(DictSecretResolver):
    """A secret store that is down for some tenants and fine for the rest.

    Models a Secrets Manager throttle or a network blip, which #31's review made a first
    class outcome: a caller that has *already proved it holds a live key* must not be told
    its key is bad, because a browser form told that does not retry and the lead is gone
    (invariant 3). It is an outage on our side, so it is a 503.
    """

    def __init__(self, secrets: Mapping[str, str], *, down_for: frozenset[str]) -> None:
        super().__init__(secrets)
        self.down_for = down_for

    def resolve(self, secret_arn: str) -> str:
        """Fail for a tenant whose secret store is down; answer normally for the rest."""
        if any(secret_arn.endswith(f"-{tenant}") for tenant in self.down_for):
            raise RuntimeError(f"secrets manager is throttling ({secret_arn})")
        return super().resolve(secret_arn)


def postgres_credentials(
    *,
    suspended: frozenset[str] = frozenset(),
    secret_store_down_for: frozenset[str] = frozenset(),
) -> PostgresIngestCredentials:
    """The production credential source over a fake ``tenant_api_keys``/``tenants`` join."""
    rows = {
        key_id: _KeyRow(
            slug=tenant,
            status="suspended" if tenant in suspended else ACTIVE_STATUS,
            hmac_secret_ref=f"{SIGNING_SECRET_REF}-{tenant}",
            key_hash=key_hash,
            revoked_at=None,
            expires_at=None,
        )
        for tenant, key_id, key_hash in (
            (TENANT_A, KEY_ID_A, KEY_HASH_A),
            (TENANT_B, KEY_ID_B, KEY_HASH_B),
        )
    }
    return PostgresIngestCredentials(
        _KeyTableSessions(rows),
        verifier=VERIFIER,
        resolver=FlakySecretResolver(
            {
                f"{SIGNING_SECRET_REF}-{tenant}": SECRETS[tenant].decode("utf-8")
                for tenant in (TENANT_A, TENANT_B)
            },
            down_for=secret_store_down_for,
        ),
        now=lambda: NOW,
        # Off: the last_used stamp is a write made after the decision and is no part of it.
        # test_repository_isolation.py pins what it does and why it carries no tenant.
        last_used_coarseness=None,
    )


SourceFactory = Callable[..., IngestCredentialSource]

#: Both implementations of the same port, swept by every credential test below. The whole
#: point: a property proved only against the double is a property the deployment does not
#: have.
SOURCES: Final[tuple[tuple[str, SourceFactory], ...]] = (
    ("StaticCredentials", static_credentials),
    ("PostgresIngestCredentials", postgres_credentials),
)


def _source_id(case: tuple[str, SourceFactory]) -> str:
    return case[0]


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_both_resolvers_answer_exactly_what_the_shared_decision_answers(
    case: tuple[str, SourceFactory],
) -> None:
    """The two implementations and the function they now share, compared on every input.

    This is the assertion that would have caught #31's original defect: the rules existed
    twice and only one copy was executed. It is written as a comparison rather than as two
    sets of expectations so that it cannot be satisfied by updating one side.
    """
    _, factory = case
    for claimed, suspended in (
        (TENANT_A, frozenset[str]()),
        (TENANT_B, frozenset[str]()),
        (TENANT_A, frozenset({TENANT_A})),
        (TENANT_B, frozenset({TENANT_A})),
    ):
        resolved = factory(suspended=suspended).resolve(tenant_id=claimed, api_key=API_KEY_A)
        expected = decide(
            claimed=claimed,
            owner=TENANT_A,
            status="suspended" if TENANT_A in suspended else ACTIVE_STATUS,
        )
        if isinstance(expected, CredentialAccepted):
            assert isinstance(resolved, IngestCredential), (claimed, suspended, resolved)
            assert resolved.tenant_id == expected.tenant_id
            assert resolved.key_id == expected.key_id
        else:
            assert resolved == expected, (claimed, suspended)


# ------------------------------------------------------------------------- the harness


VALID_FORM: Final[Mapping[str, Any]] = {
    "full_name": "Ada Lovelace",
    "email": "ada@analytical-engines.invalid",
    "company": "Analytical Engines",
    "role": "VP Engineering",
    "message": "We take about 400 inbound enquiries a month and cannot triage them by hand.",
}


class Harness:
    """The real ingest app, wired to in-memory doubles and both tenants' credentials."""

    def __init__(self, *, source: IngestCredentialSource | None = None) -> None:
        self.store = InMemoryLeadStore()
        self.clock = FakeClock(start=NOW, step_ms=0)
        self.deps = IngestDeps(
            service=IngestService(store=self.store, queue=InProcessLeadQueue(), clock=self.clock),
            credentials=source if source is not None else static_credentials(),
            clock=self.clock,
        )
        self.client = TestClient(create_app(self.deps))

    def post(
        self,
        *,
        as_tenant: str,
        key_of: str | None = None,
        secret_of: str | None = None,
        body: Mapping[str, Any] | None = None,
        nonce: str = "nonce-isolation-0001",
    ) -> Any:
        """Post one lead, with every part of the identity independently selectable.

        ``as_tenant`` goes in the header and into the signed string, ``key_of`` chooses
        whose API key is presented, and ``secret_of`` chooses whose signing secret the HMAC
        is computed with. Separating the three is what lets a test express "A's key, B's
        name" and "A's name, B's secret" as different requests.
        """
        payload = dict(body or {"submission_id": "isolation-0001", "form": dict(VALID_FORM)})
        raw = _json_bytes(payload)
        timestamp = str(int(NOW.timestamp()))
        signature = sign(
            secret=SECRETS[secret_of or as_tenant],
            method="POST",
            path=INGEST_PATH,
            tenant_id=as_tenant,
            timestamp=timestamp,
            nonce=nonce,
            body=raw,
        )
        return self.client.post(
            INGEST_PATH,
            content=raw,
            headers={
                HEADER_TENANT: as_tenant,
                HEADER_KEY: API_KEYS[key_of or as_tenant],
                HEADER_TIMESTAMP: timestamp,
                HEADER_NONCE: nonce,
                HEADER_SIGNATURE: signature,
                "content-type": "application/json",
            },
        )

    @property
    def tenants_written(self) -> set[str]:
        """Every tenant the store has a lead for."""
        return {tenant for tenant, _ in self.store.leads}


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialise the body once, so the same bytes are signed and parsed."""
    import json

    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------- a key belongs to a tenant


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_a_real_key_presented_under_another_tenants_name_is_refused(
    case: tuple[str, SourceFactory],
) -> None:
    """The core of auth isolation, against every implementation of the port."""
    _, factory = case
    lookup = factory().resolve(tenant_id=TENANT_B, api_key=API_KEY_A)
    assert isinstance(lookup, CredentialRejected), lookup
    assert lookup.failure is AuthFailure.UNKNOWN_TENANT, (
        "a key presented under the wrong tenant must be reported the same way as a key "
        "that does not exist, or the endpoint becomes an oracle for which tenants exist"
    )


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_each_tenants_key_resolves_to_that_tenant(case: tuple[str, SourceFactory]) -> None:
    """The positive control. Without it the test above would pass on a broken source."""
    _, factory = case
    source = factory()
    for tenant, key in ((TENANT_A, API_KEY_A), (TENANT_B, API_KEY_B)):
        lookup: CredentialLookup = source.resolve(tenant_id=tenant, api_key=key)
        assert isinstance(lookup, IngestCredential), (tenant, lookup)
        assert lookup.tenant_id == tenant
        assert lookup.signing_secret == SECRETS[tenant]


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_a_tenant_never_receives_another_tenants_signing_secret(
    case: tuple[str, SourceFactory],
) -> None:
    """The secret is the thing a forged request would need, so it gets its own assertion."""
    _, factory = case
    source = factory()
    credential = source.resolve(tenant_id=TENANT_A, api_key=API_KEY_A)
    assert isinstance(credential, IngestCredential)
    assert credential.signing_secret != SECRET_B


def test_the_endpoint_refuses_a_key_used_under_another_tenants_header() -> None:
    """The same property through the real route, end to end, and nothing is stored."""
    harness = Harness()
    response = harness.post(as_tenant=TENANT_B, key_of=TENANT_A, secret_of=TENANT_B)

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication failed"}
    assert harness.store.leads == {}


def test_the_endpoint_refuses_a_request_signed_with_another_tenants_secret() -> None:
    """A's key, A's name, B's secret. Per-tenant secrets are what make this a 401."""
    harness = Harness()
    response = harness.post(as_tenant=TENANT_A, key_of=TENANT_A, secret_of=TENANT_B)

    assert response.status_code == 401
    assert harness.store.leads == {}


def test_a_forged_request_cannot_burn_the_nonce_a_real_one_is_about_to_use() -> None:
    """Cross-tenant, and the reason the replay guard runs last.

    B forges a request under A's name with the wrong secret; A then sends the real thing
    with the same nonce. If the forgery had consumed the nonce, one tenant could deny
    another service by guessing nonces.
    """
    harness = Harness()
    forged = harness.post(as_tenant=TENANT_A, key_of=TENANT_A, secret_of=TENANT_B)
    genuine = harness.post(as_tenant=TENANT_A)

    assert forged.status_code == 401
    assert genuine.status_code == 202
    assert harness.tenants_written == {TENANT_A}


# ------------------------------------------------ the body does not choose the tenant


def test_a_tenant_id_in_the_envelope_is_refused_outright() -> None:
    """``extra="forbid"`` on the envelope, seen from the attack it prevents.

    The strongest possible answer to "the body claims to be somebody else": the request
    does not parse, nothing is stored, and the client is told which field is wrong without
    being told anything about tenants.
    """
    harness = Harness()
    response = harness.post(
        as_tenant=TENANT_A,
        body={
            "submission_id": "isolation-0002",
            "form": dict(VALID_FORM),
            "tenant_id": TENANT_B,
        },
    )

    assert response.status_code == 422
    assert harness.store.leads == {}
    assert any(error["field"] == "tenant_id" for error in response.json()["errors"])


def test_a_tenant_id_form_field_is_stored_as_data_under_the_authenticated_tenant() -> None:
    """The one people get wrong, asserted on the store rather than on the status code.

    A form field named ``tenant_id`` is perfectly legal — the form belongs to the customer
    and they may call a field whatever they like — so this request is accepted. What
    matters is where the row was filed. A 202 proves nothing on its own; the assertion is
    that the store's key is ``(A, submission_id)`` and that B's name survives only as an
    inert string in the lead's extra fields, where the renderer will escape it.
    """
    harness = Harness()
    form = dict(VALID_FORM) | {"tenant_id": TENANT_B}
    response = harness.post(
        as_tenant=TENANT_A,
        body={"submission_id": "isolation-0003", "form": form},
    )

    assert response.status_code == 202
    assert list(harness.store.leads) == [(TENANT_A, "isolation-0003")]
    assert harness.tenants_written == {TENANT_A}

    lead_id = harness.store.leads[(TENANT_A, "isolation-0003")]
    stored = harness.store.payloads[lead_id]
    assert stored.extra["tenant_id"] == TENANT_B, (
        "the claimed tenant should survive as form data — it is a field the customer sent "
        "— but only as data"
    )


def test_the_recorded_identity_comes_from_the_credential_and_not_the_header() -> None:
    """``Authenticated.tenant_id`` is the source's answer, not the header's claim.

    Header and credential agree on every legitimate request, so the difference is invisible
    from outside; it is the line of code that decides whether a future source that maps a
    key to a different slug would be believed. Asserted through ``resolve`` because that is
    where the authority lives.
    """
    source = static_credentials()
    credential = source.resolve(tenant_id=TENANT_A, api_key=API_KEY_A)
    assert isinstance(credential, IngestCredential)
    assert credential.tenant_id == TENANT_A
    assert credential.key_id == KEY_ID_A


# --------------------------------------------------------------------- suspension


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_suspending_one_tenant_stops_that_tenant_and_leaves_the_other_working(
    case: tuple[str, SourceFactory],
) -> None:
    """Both halves in one test, because either alone would pass on a broken endpoint.

    A suspended tenant gets a 403 rather than a 401 (#31): it has already proved its
    identity with a valid key, so there is no enumeration left to protect, and the
    integrator on the customer's side needs to know that the account and not their
    integration is what stopped working.
    """
    _, factory = case
    harness = Harness(source=factory(suspended=frozenset({TENANT_A})))

    stopped = harness.post(as_tenant=TENANT_A)
    working = harness.post(as_tenant=TENANT_B, nonce="nonce-isolation-0002")

    assert stopped.status_code == 403
    assert stopped.json() == {"detail": "tenant is not active"}
    assert working.status_code == 202
    assert harness.tenants_written == {TENANT_B}


@pytest.mark.parametrize("case", SOURCES, ids=_source_id)
def test_a_suspended_tenants_key_still_cannot_be_used_under_another_name(
    case: tuple[str, SourceFactory],
) -> None:
    """Suspension does not open a side door.

    The ownership check runs before the status check, so a suspended tenant's key is an
    ``unknown_tenant`` under someone else's name rather than a ``tenant_suspended`` — which
    also means the 403 cannot be used to ask "does this tenant exist and is it suspended?"
    about a tenant whose key you do not hold.
    """
    _, factory = case
    lookup = factory(suspended=frozenset({TENANT_A})).resolve(tenant_id=TENANT_B, api_key=API_KEY_A)
    assert isinstance(lookup, CredentialRejected)
    assert lookup.failure is AuthFailure.UNKNOWN_TENANT


def test_the_credential_lookup_is_one_read_and_never_a_write() -> None:
    """The Postgres source's statement, checked for shape as well as for scoping.

    ``_KeyTableSession`` asserts it on every call in this module; this names the property
    so that it is findable, and proves the fake was actually exercised rather than silently
    bypassed.
    """
    sessions = _KeyTableSessions(
        {
            KEY_ID_A: _KeyRow(
                slug=TENANT_A,
                status=ACTIVE_STATUS,
                hmac_secret_ref=f"{SIGNING_SECRET_REF}-{TENANT_A}",
                key_hash=KEY_HASH_A,
                revoked_at=None,
                expires_at=None,
            )
        }
    )
    source = PostgresIngestCredentials(
        sessions,
        verifier=VERIFIER,
        resolver=DictSecretResolver({f"{SIGNING_SECRET_REF}-{TENANT_A}": SECRET_A.decode("utf-8")}),
        now=lambda: NOW,
        last_used_coarseness=None,
    )

    assert isinstance(source.resolve(tenant_id=TENANT_A, api_key=API_KEY_A), IngestCredential)
    assert sessions.lookups == [KEY_ID_A]


# ----------------------------------------------------- one tenant's outage is one tenant's


def test_one_tenants_secret_store_outage_is_not_another_tenants_outage() -> None:
    """Tenant A's signing secret cannot be read; tenant B keeps working, and A gets a 503.

    Three separate claims, and the third is the one #31's review added. The failure is on
    our side, so it must not be reported as an authentication failure: a 401 tells a
    customer's form that its key is wrong, and a form told that stops trying — which loses
    the lead, and invariant 3 says a lead is never silently dropped. It is a 503 with a
    ``Retry-After``, the one answer that asks the sender to come back.

    Only the Postgres resolver can produce this outcome, because it is the only one with a
    secret store to lose. That asymmetry is exactly why it needs a test that executes: the
    in-memory double this suite uses elsewhere holds its secrets in a dict and can never
    exhibit it.
    """
    harness = Harness(source=postgres_credentials(secret_store_down_for=frozenset({TENANT_A})))

    stopped = harness.post(as_tenant=TENANT_A)
    working = harness.post(as_tenant=TENANT_B, nonce="nonce-isolation-0003")

    assert stopped.status_code == 503
    assert int(stopped.headers["Retry-After"]) > 0
    assert stopped.json() != {"detail": "authentication failed"}, (
        "a dependency outage reported as an auth failure tells a good caller to stop trying"
    )
    assert working.status_code == 202
    assert harness.tenants_written == {TENANT_B}


def test_an_outage_is_reported_as_unavailable_and_not_as_a_bad_key() -> None:
    """The same property one layer down, on the reason rather than on the status code.

    The status code is derived from this value in ``api/main.py``, so pinning the value is
    what keeps the mapping honest if the handler is ever rewritten.
    """
    source = postgres_credentials(secret_store_down_for=frozenset({TENANT_A}))

    refused = source.resolve(tenant_id=TENANT_A, api_key=API_KEY_A)
    assert isinstance(refused, CredentialRejected)
    assert refused.failure is AuthFailure.UNAVAILABLE

    assert isinstance(source.resolve(tenant_id=TENANT_B, api_key=API_KEY_B), IngestCredential)


def test_an_outage_still_does_not_let_a_key_be_used_under_another_name() -> None:
    """A degraded dependency must not degrade the tenant check.

    The secret is fetched only after :func:`decide_credential` has accepted, so a
    cross-tenant attempt during an outage is still ``unknown_tenant`` — the outage never
    enters the decision, and it cannot be used to tell an existing tenant from a
    non-existent one either.
    """
    source = postgres_credentials(secret_store_down_for=frozenset({TENANT_A, TENANT_B}))
    lookup = source.resolve(tenant_id=TENANT_B, api_key=API_KEY_A)

    assert isinstance(lookup, CredentialRejected)
    assert lookup.failure is AuthFailure.UNKNOWN_TENANT


# ------------------------------------------------- the throttle is shared, per process


class _Allowances:
    """A :class:`~leadquali.api.ratelimit.TenantRateLimitSource` over a dict, optionally down.

    ``TenantRateLimiter`` keeps two per-process, per-tenant maps — cached allowances and
    token buckets — which #31's review fixes introduced. Shared mutable state keyed by
    tenant is exactly the shape a one-character edit turns into a crossover, so the tests
    below pin the keying rather than trusting it.
    """

    def __init__(self, limits: Mapping[str, TenantRateLimit], *, down: bool = False) -> None:
        self.limits = dict(limits)
        self.down = down
        self.reads: list[str] = []

    def rate_limit_for(self, tenant_id: str) -> TenantRateLimit | None:
        """This tenant's allowance, or raise if the source is down."""
        self.reads.append(tenant_id)
        if self.down:
            raise RuntimeError("the tenants table is unreachable")
        return self.limits.get(tenant_id)


def test_each_tenants_token_bucket_is_its_own() -> None:
    """One tenant spending its whole burst leaves the other's allowance untouched.

    Tenant A is given a bucket of one and spends it; tenant B, with a bucket of five, is
    unaffected. A limiter that keyed its buckets on anything but the tenant would show up
    here as B being refused for A's traffic.
    """
    limiter = TenantRateLimiter(
        _Allowances(
            {
                TENANT_A: TenantRateLimit(per_minute=60, burst=1),
                TENANT_B: TenantRateLimit(per_minute=60, burst=5),
            }
        )
    )

    assert limiter.check(tenant_id=TENANT_A, now=NOW).allowed
    assert not limiter.check(tenant_id=TENANT_A, now=NOW).allowed
    for _ in range(5):
        assert limiter.check(tenant_id=TENANT_B, now=NOW).allowed, "A's traffic throttled B"


def test_a_limits_outage_falls_back_to_the_default_and_never_to_another_tenant() -> None:
    """When the allowance source is down, a tenant gets its own last value or the default.

    Never another tenant's. The fallback path caches per tenant like the happy path does,
    and the failure mode worth ruling out is a single shared "last known limit" that the
    most recent tenant populates for everybody.
    """
    generous = TenantRateLimit(per_minute=6_000, burst=500)
    source = _Allowances({TENANT_B: generous})
    limiter = TenantRateLimiter(source, cache_seconds=0)

    # B's generous allowance is read and cached first, then the source falls over.
    assert limiter.check(tenant_id=TENANT_B, now=NOW).allowed
    source.down = True

    allowed = 0
    for offset in range(DEFAULT_TENANT_RATE_LIMIT.burst + 5):
        if limiter.check(tenant_id=TENANT_A, now=NOW + dt.timedelta(microseconds=offset)).allowed:
            allowed += 1
    assert allowed == DEFAULT_TENANT_RATE_LIMIT.burst, (
        f"tenant A was served an allowance of {allowed}; the default burst is "
        f"{DEFAULT_TENANT_RATE_LIMIT.burst} and B's is {generous.burst}"
    )


def test_bucket_eviction_can_reset_a_tenants_throttle_and_the_bound_is_explicit() -> None:
    """A documented limit, pinned so it cannot quietly get worse.

    The bucket map is capped and evicts least-recently-used, so enough traffic from enough
    other tenants can drop a tenant's bucket and hand it a fresh full one. That is a bound
    on the throttle's accuracy, not a data leak — nothing of one tenant's is readable by
    another — but it *is* one tenant's volume affecting another's effective rate limit, so
    it is recorded here and in docs/tenant-isolation.md rather than left to be discovered.

    The mitigation is that the API Gateway stage throttle, not this, is the security
    control; this is a fairness measure.
    """
    limiter = TenantRateLimiter(
        _Allowances({TENANT_A: TenantRateLimit(per_minute=60, burst=1)}), max_tenants=2
    )

    assert limiter.check(tenant_id=TENANT_A, now=NOW).allowed
    assert not limiter.check(tenant_id=TENANT_A, now=NOW).allowed

    # Two other tenants arrive and push A's bucket out of a map that holds two.
    for index in range(2):
        limiter.check(tenant_id=f"noisy-neighbour-{index}", now=NOW)

    assert limiter.check(tenant_id=TENANT_A, now=NOW).allowed, (
        "this assertion documents the known limit; if eviction stops resetting a bucket, "
        "that is an improvement and docs/tenant-isolation.md should say so"
    )
