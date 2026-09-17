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
from leadquali.api.signing import (
    ACTIVE_STATUS,
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
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


def postgres_credentials(*, suspended: frozenset[str] = frozenset()) -> PostgresIngestCredentials:
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
        resolver=DictSecretResolver(
            {
                f"{SIGNING_SECRET_REF}-{tenant}": SECRETS[tenant].decode("utf-8")
                for tenant in (TENANT_A, TENANT_B)
            }
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
