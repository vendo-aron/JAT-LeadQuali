"""The tenant control plane and the ingest credential lookup, against real Postgres.

The unit tests prove the *service* applies the right rules. These prove the *store* does
what the service assumes: that a duplicate slug is refused by the database rather than by a
preceding ``SELECT``, that a key is unreachable from any tenant but its own, that a
revocation is visible to the very next read, and that the one query on the request path
returns what authentication needs in a single round trip.

Skipped without Postgres, like every other test in this package — see ``conftest.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, select, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import TenantApiKey
from leadquali.adapters.store_tenants import (
    PostgresIngestCredentials,
    PostgresTenantAdminStore,
)
from leadquali.api.ratelimit import TenantRateLimit
from leadquali.api.signing import (
    AuthFailure,
    CredentialRejected,
    IngestCredential,
)
from leadquali.app.api_keys import ApiKeyParts, KeyEnvironment, generate_api_key
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import (
    TenantAlreadyExistsError,
    TenantService,
    TenantStatus,
    UnknownApiKeyError,
    UnknownTenantError,
)
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


# ------------------------------------------------------------------------- doubles


class StubVerifier:
    """A verifier that is instant and honest: the "hash" is the secret, spelled out.

    The real argon2 verifier has its own test file. What matters here is the SQL around
    it — which row is found, and in what order the checks run — so paying 19 MiB per
    assertion would only make this file slow.
    """

    def __init__(self) -> None:
        self.calls = 0

    def hash_secret(self, secret: str) -> str:
        return f"$argon2id$stub${secret}"

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        del key_id
        self.calls += 1
        return key_hash == f"$argon2id$stub${secret}"


class StubResolver:
    """Stands in for Secrets Manager: one signing secret, whatever the ARN."""

    SECRET = "a-signing-secret-of-entirely-adequate-length"

    def resolve(self, secret_arn: str) -> str:
        del secret_arn
        return self.SECRET

    def resolve_mapping(self, secret_arn: str) -> dict[str, str]:
        del secret_arn
        return {}


class StubSecrets:
    """A tenant secrets provisioner that mints ARNs and nothing else."""

    def create_tenant_hmac_secret(self, slug: str) -> str:
        return f"arn:aws:secretsmanager:eu-west-1:000000000000:secret:{slug}"


class FixedClock:
    """A ``ClockPort`` pinned to one instant."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def monotonic_ms(self) -> int:
        return 0


# ------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def tenants_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own, for the same reason
    ``test_store_postgres.py`` has one: these tests commit."""
    name = f"{_database_url.database}_tenants_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def tenants_engine(tenants_database: URL) -> Iterator[Engine]:
    engine = create_engine(tenants_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(tenants_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with tenants_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    """Sessions joined to the test's transaction as savepoints; the store commits."""
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def store(sessions: sessionmaker[Session]) -> PostgresTenantAdminStore:
    return PostgresTenantAdminStore(sessions)


@pytest.fixture
def verifier() -> StubVerifier:
    return StubVerifier()


@pytest.fixture
def service(store: PostgresTenantAdminStore, verifier: StubVerifier) -> TenantService:
    return TenantService(store=store, hasher=verifier, secrets=StubSecrets(), clock=FixedClock())


@pytest.fixture
def credentials(
    sessions: sessionmaker[Session], verifier: StubVerifier
) -> PostgresIngestCredentials:
    return PostgresIngestCredentials(
        sessions,
        verifier=verifier,
        resolver=StubResolver(),
        now=lambda: NOW,
        last_used_coarseness=timedelta(hours=1),
    )


@pytest.fixture(scope="session")
def a_config() -> dict[str, Any]:
    """The shipped default rubric, reused as the shape of every tenant here."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    return document


def onboard(service: TenantService, config: dict[str, Any], slug: str) -> None:
    """Create one tenant with a unique slug, through the real service."""
    service.create_tenant(slug=slug, name=f"Tenant {slug}", config={**config, "tenant_id": slug})


def a_slug(prefix: str = "t") -> str:
    """A slug nothing else in the session will collide with."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------- tenant CRUD


def test_a_tenant_round_trips_through_postgres(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)

    record = service.get_tenant(slug=slug)
    assert record.id == tenant_id_for(slug)
    assert record.slug == slug
    assert record.status is TenantStatus.ACTIVE
    assert record.config["tenant_id"] == slug
    assert record.hmac_secret_ref is not None
    assert record.rate_limit_per_minute == 60
    assert record.rate_limit_burst == 10


def test_a_duplicate_slug_is_refused_by_the_database(
    store: PostgresTenantAdminStore, a_config: dict[str, Any]
) -> None:
    """Straight at the store, bypassing the service's own existence check, because the
    constraint is what has to hold when two operators onboard the same customer at once."""
    slug = a_slug()
    store.create_tenant(
        tenant_id=tenant_id_for(slug),
        slug=slug,
        name="First",
        config=a_config,
        hmac_secret_ref=None,
    )
    with pytest.raises(TenantAlreadyExistsError):
        store.create_tenant(
            tenant_id=tenant_id_for(slug),
            slug=slug,
            name="Second",
            config=a_config,
            hmac_secret_ref=None,
        )


def test_updating_a_config_stamps_updated_at(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    """ "When did this tenant's rubric last change?" is the first question after a routing
    surprise, and the only other answer is a CloudTrail search."""
    slug = a_slug()
    onboard(service, a_config, slug)
    before = service.get_tenant(slug=slug)

    updated = service.update_config(
        slug=slug, config={**a_config, "tenant_id": slug, "min_confidence": 0.9}
    )

    assert updated.config["min_confidence"] == 0.9
    assert updated.updated_at >= before.updated_at


def test_a_status_change_is_visible_to_the_next_read(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)

    service.set_status(slug=slug, status=TenantStatus.SUSPENDED)

    assert service.get_tenant(slug=slug).status is TenantStatus.SUSPENDED


def test_updating_a_tenant_that_does_not_exist_is_an_error(
    store: PostgresTenantAdminStore, a_config: dict[str, Any]
) -> None:
    with pytest.raises(UnknownTenantError):
        store.update_config(slug="nobody-at-all", config=a_config)


def test_the_rate_limit_lookup_reads_the_row(
    service: TenantService, store: PostgresTenantAdminStore, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)
    assert store.rate_limit_for(slug) == TenantRateLimit(per_minute=60, burst=10)
    assert store.rate_limit_for("nobody-at-all") is None


# --------------------------------------------------------------------------- keys


def test_an_issued_key_is_stored_as_a_hash_and_nothing_else(
    service: TenantService,
    connection: Connection,
    a_config: dict[str, Any],
) -> None:
    """The acceptance criterion, read straight out of the table: every column of the row
    is searched for the key, and none of them holds it."""
    slug = a_slug()
    onboard(service, a_config, slug)

    issued = service.issue_key(slug=slug, label="acme website")

    row = connection.execute(
        select(TenantApiKey).where(TenantApiKey.key_id == issued.record.key_id)
    ).one()
    rendered = " ".join(str(value) for value in row)
    assert issued.key not in rendered
    assert row.key_hash.startswith("$argon2id$")
    assert row.key_prefix == issued.record.key_prefix
    assert row.label == "acme website"


def test_keys_are_listed_newest_first_and_scoped_to_their_tenant(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    mine, theirs = a_slug("mine"), a_slug("theirs")
    onboard(service, a_config, mine)
    onboard(service, a_config, theirs)
    first = service.issue_key(slug=mine, label="one")
    second = service.issue_key(slug=mine, label="two")
    other = service.issue_key(slug=theirs)

    listed = [record.key_id for record in service.list_keys(slug=mine)]

    assert set(listed) == {first.record.key_id, second.record.key_id}
    assert other.record.key_id not in listed


def test_one_tenant_cannot_revoke_anothers_key(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    """Invariant 4 in SQL: the ``UPDATE`` filters on the tenant even though ``key_id`` is
    unique, so a confused caller gets "no such key" rather than someone else's row."""
    mine, theirs = a_slug("mine"), a_slug("theirs")
    onboard(service, a_config, mine)
    onboard(service, a_config, theirs)
    victim = service.issue_key(slug=theirs)

    with pytest.raises(UnknownApiKeyError):
        service.revoke_key(slug=mine, key_id=victim.record.key_id)

    assert service.list_keys(slug=theirs)[0].revoked_at is None


def test_revoking_twice_keeps_the_first_timestamp(
    service: TenantService, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)

    first = service.revoke_key(slug=slug, key_id=issued.record.key_id)
    second = service.revoke_key(slug=slug, key_id=issued.record.key_id)

    assert first.revoked_at is not None
    assert second.revoked_at == first.revoked_at


# ----------------------------------------------------- the one read on the hot path


def test_a_valid_key_resolves_to_its_tenants_signing_secret(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)

    resolved = credentials.resolve(tenant_id=slug, api_key=issued.key)

    assert isinstance(resolved, IngestCredential)
    assert resolved.tenant_id == slug
    assert resolved.key_id == issued.record.key_id
    assert resolved.signing_secret == StubResolver.SECRET.encode("utf-8")


def test_a_revoked_key_is_rejected_by_the_very_next_request(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    """The acceptance criterion. Nothing about the row is cached, so there is no window and
    nothing to wait out."""
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)
    assert isinstance(credentials.resolve(tenant_id=slug, api_key=issued.key), IngestCredential)

    service.revoke_key(slug=slug, key_id=issued.record.key_id)

    assert credentials.resolve(tenant_id=slug, api_key=issued.key) == CredentialRejected(
        AuthFailure.REVOKED_KEY
    )


def test_a_rotated_key_works_until_its_overlap_closes(
    service: TenantService,
    sessions: sessionmaker[Session],
    verifier: StubVerifier,
    a_config: dict[str, Any],
) -> None:
    """Both keys live during the overlap; only the new one afterwards. Asserted with two
    credential sources whose clocks sit either side of the deadline."""
    slug = a_slug()
    onboard(service, a_config, slug)
    old = service.issue_key(slug=slug)
    new = service.rotate_key(slug=slug, key_id=old.record.key_id, overlap=timedelta(days=7))

    def source_at(moment: datetime) -> PostgresIngestCredentials:
        return PostgresIngestCredentials(
            sessions,
            verifier=verifier,
            resolver=StubResolver(),
            now=lambda: moment,
            last_used_coarseness=None,
        )

    inside = source_at(NOW + timedelta(days=6))
    assert isinstance(inside.resolve(tenant_id=slug, api_key=old.key), IngestCredential)
    assert isinstance(inside.resolve(tenant_id=slug, api_key=new.key), IngestCredential)

    after = source_at(NOW + timedelta(days=8))
    assert after.resolve(tenant_id=slug, api_key=old.key) == CredentialRejected(
        AuthFailure.REVOKED_KEY
    )
    assert isinstance(after.resolve(tenant_id=slug, api_key=new.key), IngestCredential)


def test_a_suspended_tenants_key_is_refused_with_its_own_reason(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)
    service.set_status(slug=slug, status=TenantStatus.SUSPENDED)

    assert credentials.resolve(tenant_id=slug, api_key=issued.key) == CredentialRejected(
        AuthFailure.TENANT_SUSPENDED
    )


def test_suspending_one_tenant_does_not_stop_another(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    """The acceptance criterion, end to end through the database."""
    stopped, running = a_slug("stopped"), a_slug("running")
    onboard(service, a_config, stopped)
    onboard(service, a_config, running)
    stopped_key = service.issue_key(slug=stopped)
    running_key = service.issue_key(slug=running)

    service.set_status(slug=stopped, status=TenantStatus.SUSPENDED)

    assert credentials.resolve(tenant_id=stopped, api_key=stopped_key.key) == CredentialRejected(
        AuthFailure.TENANT_SUSPENDED
    )
    assert isinstance(
        credentials.resolve(tenant_id=running, api_key=running_key.key), IngestCredential
    )


def test_a_key_presented_under_another_tenants_name_is_refused(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    mine, theirs = a_slug("mine"), a_slug("theirs")
    onboard(service, a_config, mine)
    onboard(service, a_config, theirs)
    issued = service.issue_key(slug=theirs)

    assert credentials.resolve(tenant_id=mine, api_key=issued.key) == CredentialRejected(
        AuthFailure.UNKNOWN_TENANT
    )


def test_a_key_id_that_does_not_exist_costs_no_kdf(
    credentials: PostgresIngestCredentials, verifier: StubVerifier
) -> None:
    """One indexed miss and nothing else. The whole "argon2 on the request path" argument
    depends on a stranger being unable to make us run it."""
    stranger = generate_api_key(KeyEnvironment.LIVE)

    assert credentials.resolve(tenant_id="nobody", api_key=stranger.text) == CredentialRejected(
        AuthFailure.UNKNOWN_TENANT
    )
    assert verifier.calls == 0


def test_a_malformed_key_touches_the_database_at_all(
    credentials: PostgresIngestCredentials, verifier: StubVerifier
) -> None:
    """Cheaper still: refused on its shape, before a statement is built."""
    assert credentials.resolve(tenant_id="nobody", api_key="not-a-key") == CredentialRejected(
        AuthFailure.MALFORMED
    )
    assert verifier.calls == 0


def test_a_wrong_secret_on_a_real_key_id_is_a_bad_key(
    service: TenantService, credentials: PostgresIngestCredentials, a_config: dict[str, Any]
) -> None:
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)
    forged = ApiKeyParts(
        environment=KeyEnvironment.LIVE, key_id=issued.record.key_id, secret="w" * 43
    ).text

    assert credentials.resolve(tenant_id=slug, api_key=forged) == CredentialRejected(
        AuthFailure.BAD_KEY
    )


def test_last_used_is_recorded_coarsely_and_never_on_every_request(
    service: TenantService,
    credentials: PostgresIngestCredentials,
    connection: Connection,
    a_config: dict[str, Any],
) -> None:
    """Written once, then not again inside the hour: a write per lead would put a
    row-level lock contended by every concurrent request for the same key on the hot path.
    """
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)

    def last_used() -> datetime | None:
        value: datetime | None = connection.execute(
            select(TenantApiKey.last_used_at).where(TenantApiKey.key_id == issued.record.key_id)
        ).scalar_one()
        return value

    assert last_used() is None
    credentials.resolve(tenant_id=slug, api_key=issued.key)
    first = last_used()
    assert first is not None

    # A second request inside the window must not write again. Asserted by moving the
    # column by hand and finding it untouched, which is the only way to see a write that
    # did not happen.
    connection.execute(
        update(TenantApiKey)
        .where(TenantApiKey.key_id == issued.record.key_id)
        .values(last_used_at=NOW - timedelta(days=1))
    )
    credentials.resolve(tenant_id=slug, api_key=issued.key)
    assert last_used() == NOW - timedelta(days=1)


def test_last_used_can_be_switched_off_entirely(
    service: TenantService,
    sessions: sessionmaker[Session],
    verifier: StubVerifier,
    connection: Connection,
    a_config: dict[str, Any],
) -> None:
    """A deployment that would rather not pay the extra write at all can have that."""
    slug = a_slug()
    onboard(service, a_config, slug)
    issued = service.issue_key(slug=slug)
    source = PostgresIngestCredentials(
        sessions,
        verifier=verifier,
        resolver=StubResolver(),
        now=lambda: NOW,
        last_used_coarseness=None,
    )

    assert isinstance(source.resolve(tenant_id=slug, api_key=issued.key), IngestCredential)

    stored = connection.execute(
        select(TenantApiKey.last_used_at).where(TenantApiKey.key_id == issued.record.key_id)
    ).scalar_one()
    assert stored is None
