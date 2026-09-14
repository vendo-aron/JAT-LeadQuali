"""The usage meter against a real Postgres.

``tests/unit/test_metering.py`` proves the *arithmetic*: what is billable, where a day
ends, what a quota level means. ``tests/unit/test_metering_postgres.py`` proves the
statements are well-formed Postgres. This file is the half neither of them can reach — that
the database agrees with both:

* the rollup produces the right sums from real ``leads`` and ``assessments`` rows,
* **re-running it produces an identical row** in every column but ``computed_at``, which
  is the acceptance criterion "rollups are idempotent and safe to re-run" stated as a
  property of the SQL rather than of a fake,
* a second tenant's rows on the same day never reach the first tenant's totals,
* the CHECK constraints and the cascade behave as the schema claims.

Skipped, never failed, when there is no database — see ``conftest.py``. Bring one up with
``docker compose up -d``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, delete, insert, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Assessment, Lead, Tenant, UsageDaily
from leadquali.adapters.metering_postgres import PostgresMeteringStore
from leadquali.adapters.seed import seed_tenant, tenant_id_for
from leadquali.app.metering import (
    BillingPeriod,
    MeteringError,
    MeteringService,
    MeteringStorePort,
    QuotaLevel,
)
from tests.fakes import FakeClock, StaticRevenue
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Two tenants, because half of what is under test is what one of them cannot see of the
#: other in a number somebody is going to be invoiced for.
TENANT_A = "metering-tenant-a"
TENANT_B = "metering-tenant-b"

DAY = date(2026, 9, 3)
SEPTEMBER = BillingPeriod.of_month(2026, 9)
NOW = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

#: Columns that must be byte-identical when a day is rolled up twice. ``computed_at`` is
#: deliberately not in the list: it is the only thing a re-run is allowed to change.
IDEMPOTENT_COLUMNS = (
    "leads_ingested",
    "leads_assessed",
    "leads_billable",
    "assessments_failed",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
)


# ------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def metering_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own, because these tests commit."""
    name = f"{_database_url.database}_metering_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def metering_engine(metering_database: URL) -> Iterator[Engine]:
    engine = create_engine(metering_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def seeded_tenants(metering_engine: Engine) -> tuple[str, str]:
    """Two committed tenant rows. A lead cannot exist without one."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    with metering_engine.begin() as connection:
        for slug in (TENANT_A, TENANT_B):
            seed_tenant(connection, {**document, "tenant_id": slug, "name": f"Tenant {slug}"})
    return (TENANT_A, TENANT_B)


@pytest.fixture
def connection(metering_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with metering_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    """Sessions joined to the test's transaction as savepoints.

    The adapter commits in every method — that is the behaviour under test — so the only
    way to keep tests from leaking rows is to make those commits release a savepoint
    inside a transaction the fixture rolls back.
    """
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def store(
    sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]
) -> PostgresMeteringStore:
    del seeded_tenants  # ordering only: the tenants must exist before any lead does.
    return PostgresMeteringStore(sessions)


@pytest.fixture
def service(store: PostgresMeteringStore) -> MeteringService:
    return MeteringService(store=store, clock=FakeClock(start=NOW), revenue=StaticRevenue())


# --------------------------------------------------------------------------- seeding


def add_lead(connection: Connection, *, tenant: str, received_at: datetime, submission: str) -> Any:
    """Insert one ``leads`` row and return its id."""
    return connection.execute(
        insert(Lead)
        .values(
            tenant_id=tenant_id_for(tenant),
            submission_id=submission,
            raw_payload={"message": "hello"},
            source="web_form",
            received_at=received_at,
        )
        .returning(Lead.id)
    ).scalar_one()


def add_assessment(
    connection: Connection,
    *,
    tenant: str,
    lead_id: Any,
    created_at: datetime,
    status: str = "ok",
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: Decimal = Decimal("0.010000"),
) -> None:
    """Insert one ``assessments`` row, successful or failed, with its metering."""
    model_columns: dict[str, Any] = (
        {
            "tier": "warm",
            "total_score": Decimal("55.00"),
            "dimension_scores": {"fit": 5},
            "extracted": {"industry": "saas"},
            "reasoning": "because",
            "confidence": Decimal("0.900"),
        }
        if status == "ok"
        else {"escalation_reason": "api_error"}
    )
    connection.execute(
        insert(Assessment).values(
            tenant_id=tenant_id_for(tenant),
            lead_id=lead_id,
            created_at=created_at,
            status=status,
            model_id="claude-opus-5",
            prompt_version="v1",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cost_usd=cost_usd,
            **model_columns,
        )
    )


def seed_a_typical_day(
    connection: Connection, *, tenant: str = TENANT_A, day: date = DAY, prefix: str = "a"
) -> None:
    """Six submissions: two stopped by the pre-filter, one billed failure, three clean.

    The same shape as the unit suite's, so the two can be compared line by line — which is
    the point: the fake and the SQL have to agree about what these six rows mean.
    """
    leads = [
        add_lead(
            connection,
            tenant=tenant,
            received_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
            submission=f"{prefix}-{day}-{hour}",
        )
        for hour in range(6)
    ]
    add_assessment(
        connection,
        tenant=tenant,
        lead_id=leads[2],
        created_at=datetime(day.year, day.month, day.day, 2, tzinfo=UTC),
        status="failed",
        input_tokens=1_200,
        output_tokens=0,
        cost_usd=Decimal("0.006000"),
    )
    for index in (3, 4, 5):
        add_assessment(
            connection,
            tenant=tenant,
            lead_id=leads[index],
            created_at=datetime(day.year, day.month, day.day, index, tzinfo=UTC),
            input_tokens=1_000,
            output_tokens=400,
            cache_read_tokens=2_000,
            cost_usd=Decimal("0.016000"),
        )


def stored_row(connection: Connection, *, tenant: str, day: date) -> Any:
    return connection.execute(
        select(UsageDaily).where(
            UsageDaily.tenant_id == tenant_id_for(tenant), UsageDaily.usage_date == day
        )
    ).one()


# --------------------------------------------------------------------------- rollup


def test_the_rollup_sums_the_day_from_the_source_tables(
    service: MeteringService, connection: Connection
) -> None:
    seed_a_typical_day(connection)

    totals = service.rollup_day(tenant_id=TENANT_A, day=DAY)

    assert totals.leads_ingested == 6
    assert totals.leads_assessed == 4
    assert totals.leads_billable == 4
    assert totals.assessments_failed == 1
    assert totals.input_tokens == 4_200
    assert totals.output_tokens == 1_200
    assert totals.cache_read_tokens == 6_000
    assert totals.cache_creation_tokens == 0
    assert totals.cost_usd == Decimal("0.054000")


def test_the_rollup_writes_exactly_one_row_per_tenant_day(
    service: MeteringService, connection: Connection
) -> None:
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)

    count = connection.execute(
        select(UsageDaily.tenant_id).where(UsageDaily.tenant_id == tenant_id_for(TENANT_A))
    ).all()
    assert len(count) == 1


def test_re_running_the_rollup_produces_an_identical_row(
    service: MeteringService, connection: Connection
) -> None:
    """The acceptance criterion, asserted column by column against the database.

    Every counter is replaced with the freshly computed value rather than incremented, so
    a second run is a no-op — and ``computed_at`` moves, which is how an operator can tell
    a stale rollup from a fresh one.
    """
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)
    first = stored_row(connection, tenant=TENANT_A, day=DAY)

    service.rollup_day(tenant_id=TENANT_A, day=DAY)
    second = stored_row(connection, tenant=TENANT_A, day=DAY)

    for column in IDEMPOTENT_COLUMNS:
        assert getattr(first, column) == getattr(second, column), column
    assert second.computed_at >= first.computed_at


def test_a_row_that_arrives_late_is_picked_up_by_the_next_run(
    service: MeteringService, connection: Connection
) -> None:
    """Idempotent is not frozen: re-running yesterday after a late SQS redelivery is the
    ordinary way to correct a day."""
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)

    late = add_lead(
        connection,
        tenant=TENANT_A,
        received_at=datetime(2026, 9, 3, 23, 50, tzinfo=UTC),
        submission="a-late",
    )
    add_assessment(
        connection,
        tenant=TENANT_A,
        lead_id=late,
        created_at=datetime(2026, 9, 3, 23, 55, tzinfo=UTC),
        input_tokens=500,
        output_tokens=100,
        cost_usd=Decimal("0.005000"),
    )
    again = service.rollup_day(tenant_id=TENANT_A, day=DAY)

    assert again.leads_ingested == 7
    assert again.leads_billable == 5
    assert again.cost_usd == Decimal("0.059000")


def test_an_empty_day_rolls_up_to_zeroes(service: MeteringService, connection: Connection) -> None:
    """A row of zeroes, not a missing row: "rolled up, nothing happened" and "never rolled
    up" are different facts, and only the first can be billed from."""
    totals = service.rollup_day(tenant_id=TENANT_A, day=date(2026, 9, 20))

    assert totals.leads_ingested == 0
    assert totals.cost_usd == Decimal(0)
    assert stored_row(connection, tenant=TENANT_A, day=date(2026, 9, 20)).leads_billable == 0


# -------------------------------------------------------------------- the day boundary


def test_the_day_boundary_is_utc_midnight(service: MeteringService, connection: Connection) -> None:
    """A lead at 23:59:59 UTC belongs to the day that is ending and one at 00:00:00 to the
    day that is starting — for every tenant, wherever they are. Without one definition, a
    nightly billing job double-counts somebody at every month boundary.
    """
    for moment, submission in (
        (datetime(2026, 9, 2, 23, 59, 59, tzinfo=UTC), "before"),
        (datetime(2026, 9, 3, 0, 0, 0, tzinfo=UTC), "start"),
        (datetime(2026, 9, 3, 23, 59, 59, tzinfo=UTC), "end"),
        (datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC), "after"),
    ):
        lead = add_lead(connection, tenant=TENANT_A, received_at=moment, submission=submission)
        add_assessment(connection, tenant=TENANT_A, lead_id=lead, created_at=moment)

    third = service.rollup_day(tenant_id=TENANT_A, day=DAY)

    assert third.leads_ingested == 2
    assert third.leads_assessed == 2


def test_a_lead_is_metered_on_the_day_it_arrived_not_the_day_it_was_written(
    service: MeteringService, connection: Connection
) -> None:
    """``received_at``, not ``created_at``. After an SQS retry the row can be written much
    later, and a customer's "leads on 3 September" means the ones they sent that day."""
    lead = add_lead(
        connection,
        tenant=TENANT_A,
        received_at=datetime(2026, 9, 3, 22, tzinfo=UTC),
        submission="retried",
    )
    connection.execute(
        update(Lead).where(Lead.id == lead).values(created_at=datetime(2026, 9, 5, 9, tzinfo=UTC))
    )

    assert service.rollup_day(tenant_id=TENANT_A, day=DAY).leads_ingested == 1
    assert service.rollup_day(tenant_id=TENANT_A, day=date(2026, 9, 5)).leads_ingested == 0


# ------------------------------------------------------------------- tenant isolation


def test_a_second_tenants_rows_never_reach_the_first_tenants_totals(
    service: MeteringService, connection: Connection
) -> None:
    """Invariant 4, on the number a customer is invoiced from."""
    seed_a_typical_day(connection, tenant=TENANT_A, prefix="a")
    seed_a_typical_day(connection, tenant=TENANT_B, prefix="b")

    first = service.rollup_day(tenant_id=TENANT_A, day=DAY)
    second = service.rollup_day(tenant_id=TENANT_B, day=DAY)

    assert first.leads_ingested == 6
    assert first.cost_usd == Decimal("0.054000")
    assert second.leads_ingested == 6
    assert service.usage_for_period(tenant_id=TENANT_A, period=SEPTEMBER).cost_usd == Decimal(
        "0.054000"
    )


def test_the_fleet_view_sees_both_tenants_and_keeps_them_apart(
    service: MeteringService, connection: Connection
) -> None:
    """What reconciliation needs — Anthropic bills the workspace — without ever producing
    a total with no tenant attached to it."""
    seed_a_typical_day(connection, tenant=TENANT_A, prefix="a")
    seed_a_typical_day(connection, tenant=TENANT_B, prefix="b")
    service.rollup_day(tenant_id=TENANT_A, day=DAY)
    service.rollup_day(tenant_id=TENANT_B, day=DAY)

    report = service.margin(tenant_id=TENANT_A, period=SEPTEMBER)
    spend = service.reconcile(invoice=[], period=SEPTEMBER)

    assert report.fleet_billable_leads == 8
    assert spend.ours_usd == Decimal("0.108000")


# ---------------------------------------------------------------------- reading back


def test_a_period_reads_back_what_was_rolled_up(
    service: MeteringService, connection: Connection
) -> None:
    for day in (date(2026, 9, 3), date(2026, 9, 4)):
        seed_a_typical_day(connection, day=day, prefix=f"a{day.day}")
        service.rollup_day(tenant_id=TENANT_A, day=day)

    totals = service.usage_for_period(tenant_id=TENANT_A, period=SEPTEMBER)

    assert totals.leads_ingested == 12
    assert totals.leads_billable == 8
    assert totals.cost_usd == Decimal("0.108000")
    assert totals.computed_at is not None


def test_a_period_with_no_rollup_rows_reads_as_zero_rather_than_failing(
    service: MeteringService,
) -> None:
    totals = service.usage_for_period(tenant_id=TENANT_A, period=BillingPeriod.of_month(2025, 1))
    assert totals.leads_billable == 0
    assert totals.cost_usd == Decimal(0)
    assert totals.computed_at is None


def test_the_daily_listing_returns_one_row_per_rolled_up_day(
    service: MeteringService, connection: Connection
) -> None:
    for day in (date(2026, 9, 3), date(2026, 9, 5)):
        seed_a_typical_day(connection, day=day, prefix=f"a{day.day}")
        service.rollup_day(tenant_id=TENANT_A, day=day)

    rows = service.daily_usage(tenant_id=TENANT_A, period=SEPTEMBER)

    assert [row.period.start for row in rows] == [date(2026, 9, 3), date(2026, 9, 5)]


def test_a_token_total_comes_back_as_an_integer(
    service: MeteringService, connection: Connection
) -> None:
    """``SUM`` over ``bigint`` returns ``numeric`` in Postgres, so this would otherwise be
    a ``Decimal`` in a field a JSON report renders as a count."""
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)

    totals = service.usage_for_period(tenant_id=TENANT_A, period=SEPTEMBER)

    assert isinstance(totals.input_tokens, int)
    assert isinstance(totals.cost_usd, Decimal)


# ------------------------------------------------------------------------------ quota


def test_a_tenant_with_no_quota_is_unlimited(
    service: MeteringService, connection: Connection
) -> None:
    """The column is NULL by default, and every tenant that existed before this migration
    comes out of it unlimited — which is what they were."""
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)

    status = service.quota_status(tenant_id=TENANT_A, period=SEPTEMBER)

    assert status.quota is None
    assert status.level is QuotaLevel.OK


def test_a_configured_quota_is_measured_in_billable_leads(
    service: MeteringService, connection: Connection
) -> None:
    """Four of the day's six submissions cost tokens; the two the pre-filter caught do not
    eat the customer's plan."""
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)
    connection.execute(
        update(Tenant)
        .where(Tenant.id == tenant_id_for(TENANT_A))
        .values(monthly_lead_quota=5, quota_alert_fraction=Decimal("0.80"))
    )

    status = service.quota_status(tenant_id=TENANT_A, period=SEPTEMBER)

    assert status.used == 4
    assert status.level is QuotaLevel.WARNING


def test_a_quota_written_by_the_command_reads_back(
    service: MeteringService, connection: Connection
) -> None:
    """The round trip an operator does: set a plan, then look at it. A quota nobody can
    configure would not be a feature, and one that does not survive the write is worse."""
    seed_a_typical_day(connection)
    service.rollup_day(tenant_id=TENANT_A, day=DAY)

    service.set_quota(tenant_id=TENANT_A, monthly_lead_quota=4, alert_fraction=Decimal("0.75"))
    status = service.quota_status(tenant_id=TENANT_A, period=SEPTEMBER)

    assert status.quota == 4
    assert status.alert_fraction == Decimal("0.75")
    assert status.level is QuotaLevel.WARNING

    service.set_quota(tenant_id=TENANT_A, monthly_lead_quota=None, alert_fraction=Decimal("0.80"))
    assert service.quota_status(tenant_id=TENANT_A, period=SEPTEMBER).quota is None


def test_writing_a_quota_for_an_unknown_tenant_is_an_error(service: MeteringService) -> None:
    with pytest.raises(MeteringError, match="no tenant"):
        service.set_quota(
            tenant_id="metering-tenant-nobody",
            monthly_lead_quota=10,
            alert_fraction=Decimal("0.80"),
        )


def test_a_quota_for_an_unknown_tenant_is_an_error(service: MeteringService) -> None:
    with pytest.raises(MeteringError, match="no tenant"):
        service.quota_status(tenant_id="metering-tenant-nobody", period=SEPTEMBER)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("monthly_lead_quota", 0),
        ("quota_alert_fraction", Decimal("0")),
        ("quota_alert_fraction", Decimal("1.5")),
    ],
)
def test_the_database_refuses_a_quota_setting_that_could_never_work(
    connection: Connection, seeded_tenants: tuple[str, str], column: str, value: Any
) -> None:
    """A zero quota is a suspension and there is a status column for that; an alert at 0%
    fires on the first lead of every month and one above 100% can never fire at all."""
    del seeded_tenants
    with pytest.raises(IntegrityError):
        connection.execute(
            update(Tenant).where(Tenant.id == tenant_id_for(TENANT_A)).values(**{column: value})
        )


# ------------------------------------------------------------------ schema behaviour


def test_the_rollup_refuses_a_negative_counter(
    connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """Negative counters mean the arithmetic is wrong, and it is far better to fail the
    write than to bill from it."""
    del seeded_tenants
    with pytest.raises(IntegrityError):
        connection.execute(
            insert(UsageDaily).values(
                tenant_id=tenant_id_for(TENANT_A), usage_date=DAY, leads_billable=-1
            )
        )


def test_a_rollup_row_needs_a_tenant_that_exists(connection: Connection) -> None:
    import uuid as uuid_module

    with pytest.raises(IntegrityError):
        connection.execute(insert(UsageDaily).values(tenant_id=uuid_module.uuid4(), usage_date=DAY))


def test_two_rows_for_one_tenant_day_are_impossible(
    connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """The primary key *is* the identity of the row: a second one is not a new fact."""
    del seeded_tenants
    values = {"tenant_id": tenant_id_for(TENANT_A), "usage_date": DAY}
    connection.execute(insert(UsageDaily).values(**values))
    with pytest.raises(IntegrityError):
        connection.execute(insert(UsageDaily).values(**values))


def test_the_rollup_survives_a_token_count_beyond_a_32_bit_integer(
    connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """The reason the counters are ``bigint``: a busy tenant passes 2^31 input tokens
    inside a year, and an overflow would be discovered on an invoice."""
    del seeded_tenants
    beyond = 2**31 + 1
    connection.execute(
        insert(UsageDaily).values(
            tenant_id=tenant_id_for(TENANT_A), usage_date=DAY, input_tokens=beyond
        )
    )
    assert stored_row(connection, tenant=TENANT_A, day=DAY).input_tokens == beyond


def test_the_adapter_satisfies_the_port(store: PostgresMeteringStore) -> None:
    assert isinstance(store, MeteringStorePort)


def test_a_rollup_row_is_deleted_with_its_tenant(
    connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """CASCADE, unlike ``leads``: this table is a cache of a ``SUM``, so blocking #37's
    erasure on it would be theatre. Checked with a tenant of this test's own, because the
    seeded pair has leads hanging off it that deliberately do restrict."""
    del seeded_tenants
    slug = "metering-tenant-transient"
    seed_tenant(connection, {"tenant_id": slug, "name": "Transient"})
    connection.execute(insert(UsageDaily).values(tenant_id=tenant_id_for(slug), usage_date=DAY))

    connection.execute(delete(Tenant).where(Tenant.id == tenant_id_for(slug)))

    remaining = connection.execute(
        select(UsageDaily.tenant_id).where(UsageDaily.tenant_id == tenant_id_for(slug))
    ).all()
    assert remaining == []


def test_the_rollup_reads_only_the_day_it_was_asked_for(
    service: MeteringService, connection: Connection
) -> None:
    """A guard against an off-by-one in the half-open range: rolling up one day must not
    absorb the neighbouring ones."""
    for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)):
        seed_a_typical_day(connection, day=day, prefix=f"n{day.day}")

    assert service.rollup_day(tenant_id=TENANT_A, day=DAY).leads_ingested == 6
