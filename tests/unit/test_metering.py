"""The usage meter: what is billable, what a day is, and what the numbers mean.

The arithmetic here decides what a customer is charged, so the tests are written against
the cases where the three counts *disagree* — a day holding pre-filtered spam, a failed
model call and a couple of clean assessments produces three different numbers, and getting
any of them confused with another is a billing bug that nobody notices until an invoice is
disputed.

Everything runs against :class:`~tests.fakes.InMemoryMeteringStore`, which recomputes and
replaces a day exactly as the SQL does. That the SQL agrees is
``tests/integration/test_metering_postgres.py``'s job; that it is even well-formed is
``tests/unit/test_metering_postgres.py``'s.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from leadquali.app.metering import (
    DEFAULT_QUOTA_ALERT_FRACTION,
    MONTHLY_INFRASTRUCTURE_USD,
    RECONCILIATION_TOLERANCE,
    BillingPeriod,
    DailySpend,
    InvoiceFormatError,
    MeteringError,
    MeteringService,
    MeteringStorePort,
    QuotaLevel,
    QuotaStatus,
    TenantQuota,
    UsageTotals,
    infrastructure_usd_for,
    is_billable,
    parse_invoice_csv,
)
from leadquali.observability.events import EVENT_QUOTA_CROSSED
from tests.fakes import FakeClock, InMemoryMeteringStore, StaticRevenue
from tests.logcapture import capture_json_logs

TENANT = "acme"
OTHER = "globex"

#: The clock every test runs at: 1 October, so all of September is closed.
NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
SEPTEMBER = BillingPeriod.of_month(2026, 9)
DAY = date(2026, 9, 3)


def at(day: date, hour: int = 12) -> datetime:
    """An instant inside ``day``, in UTC."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def build(
    *,
    store: InMemoryMeteringStore | None = None,
    now: datetime = NOW,
    revenue: StaticRevenue | None = None,
) -> tuple[MeteringService, InMemoryMeteringStore]:
    """A service over a fake store, with a clock that does not move."""
    backing = store if store is not None else InMemoryMeteringStore()
    service = MeteringService(
        store=backing,
        clock=FakeClock(start=now),
        revenue=revenue if revenue is not None else StaticRevenue(),
    )
    return service, backing


def seed_a_typical_day(
    store: InMemoryMeteringStore, *, day: date = DAY, tenant: str = TENANT
) -> None:
    """Six submissions: two stopped by the pre-filter, one failed call, three clean.

    This is the shape the whole billing argument turns on, so it is one function used by
    several tests rather than four slightly different inline setups.
    """
    for hour in range(6):
        store.add_lead(tenant_id=tenant, received_at=at(day, hour))
    # Two never reached the model: no assessment row at all, no tokens, no charge.
    store.add_assessment(
        tenant_id=tenant,
        created_at=at(day, 2),
        status="failed",
        input_tokens=1_200,
        cost_usd=Decimal("0.006000"),
    )
    for hour in (3, 4, 5):
        store.add_assessment(
            tenant_id=tenant,
            created_at=at(day, hour),
            input_tokens=1_000,
            output_tokens=400,
            cache_read_tokens=2_000,
            cache_creation_tokens=0,
            cost_usd=Decimal("0.016000"),
        )


# ------------------------------------------------------------------- what is billable


def test_a_call_that_burned_input_tokens_is_billable() -> None:
    assert is_billable(input_tokens=1)
    assert is_billable(input_tokens=1_200)


def test_a_call_that_never_reached_the_model_is_not_billable() -> None:
    """The deterministic pre-filter costs nothing, so it charges nothing."""
    assert not is_billable(input_tokens=0)


def test_the_three_counts_are_three_different_numbers() -> None:
    """A day with spam, a failure and three clean assessments.

    Six submissions arrived, four were assessed, four are billable and one of those four
    failed. Reading any of those numbers as another is a billing bug: charging the six
    would bill for our own spam filter, charging the three would give away the refusal we
    were invoiced for.
    """
    service, store = build()
    seed_a_typical_day(store)

    totals = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert totals.leads_ingested == 6
    assert totals.leads_assessed == 4
    assert totals.leads_billable == 4
    assert totals.assessments_failed == 1
    assert totals.leads_filtered == 2
    assert totals.cost_usd == Decimal("0.054000")


def test_a_failed_model_call_is_billed_and_a_filtered_lead_is_not() -> None:
    """The two halves of the rule, isolated from each other."""
    service, store = build()
    store.add_lead(tenant_id=TENANT, received_at=at(DAY))
    store.add_lead(tenant_id=TENANT, received_at=at(DAY, 1))
    store.add_assessment(
        tenant_id=TENANT,
        created_at=at(DAY, 1),
        status="failed",
        input_tokens=900,
        cost_usd=Decimal("0.004500"),
    )

    totals = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert totals.leads_ingested == 2
    assert totals.leads_billable == 1
    assert totals.assessments_failed == 1
    assert totals.cost_usd == Decimal("0.004500")


def test_an_assessment_that_cost_no_tokens_is_counted_but_not_billed() -> None:
    """A defensive case the schema allows: an assessment row with zero metering.

    ``assessments.input_tokens`` defaults to zero and is written even when the call never
    completed, so "assessed" and "billable" have to be able to disagree in this direction
    too — otherwise a bug that lost the metering would silently start charging for it.
    """
    service, store = build()
    store.add_lead(tenant_id=TENANT, received_at=at(DAY))
    store.add_assessment(tenant_id=TENANT, created_at=at(DAY), status="failed")

    totals = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert totals.leads_assessed == 1
    assert totals.leads_billable == 0
    assert totals.cost_usd == Decimal(0)


def test_one_lead_attempted_three_times_is_one_billable_lead() -> None:
    """The bug the per-lead rule exists to stop.

    ``qualify`` re-raises on a failed dispatch so SQS redelivers, ``already_routed``
    deliberately answers ``False`` for a failed send, and ``record_assessment`` is a plain
    insert — so an SES outage plus ``maxReceiveCount: 3`` writes three assessment rows for
    one lead. Counting rows would bill the customer three times for our own outage, and
    their report would read "1 lead received, 3 billable leads".
    """
    service, store = build()
    store.add_lead(tenant_id=TENANT, received_at=at(DAY))
    for hour in (1, 2, 3):
        store.add_assessment(
            tenant_id=TENANT,
            created_at=at(DAY, hour),
            lead_id="lead-redelivered",
            input_tokens=1_000,
            output_tokens=200,
            cost_usd=Decimal("0.010000"),
        )

    totals = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert totals.leads_ingested == 1
    assert totals.leads_assessed == 3, "the attempts happened and are worth seeing"
    assert totals.leads_billable == 1, "the customer has one lead"
    # ...and we really were charged for all three, so our own cost still shows all three.
    # That gap is the retry cost we absorb, and it has to stay visible or reconciliation
    # against the Anthropic invoice would fail by exactly that amount every month.
    assert totals.input_tokens == 3_000
    assert totals.cost_usd == Decimal("0.030000")
    assert totals.leads_filtered == 0


def test_a_retried_lead_does_not_eat_three_units_of_a_plan() -> None:
    """The same fix, seen from the quota: a plan is a monthly *lead* allowance."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=2))
    for hour in (1, 2, 3):
        store.add_assessment(
            tenant_id=TENANT, created_at=at(DAY, hour), lead_id="one-lead", input_tokens=900
        )
    service, _ = build(store=store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    status = service.quota_status(tenant_id=TENANT, period=SEPTEMBER)

    assert status.used == 1
    assert status.level is QuotaLevel.OK


def test_another_tenants_rows_never_reach_this_tenants_totals() -> None:
    """Invariant 4, at the level a billing number cares about."""
    service, store = build()
    seed_a_typical_day(store)
    seed_a_typical_day(store, tenant=OTHER)

    totals = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert totals.leads_ingested == 6
    assert totals.leads_billable == 4


# ----------------------------------------------------------------------- idempotency


def test_rolling_a_day_up_twice_produces_the_same_row() -> None:
    """The acceptance criterion: rollups are idempotent and safe to re-run.

    A full replacement, never an increment — which is also why re-running yesterday after
    a late SQS redelivery is the ordinary way to correct a day.
    """
    service, store = build()
    seed_a_typical_day(store)

    first = service.rollup_day(tenant_id=TENANT, day=DAY)
    second = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert first == second
    assert len(store.rows) == 1


def test_a_late_arrival_changes_the_day_when_it_is_rolled_up_again() -> None:
    """Idempotent is not the same as frozen: re-running must see new source rows."""
    service, store = build()
    seed_a_typical_day(store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    store.add_lead(tenant_id=TENANT, received_at=at(DAY, 22))
    store.add_assessment(
        tenant_id=TENANT, created_at=at(DAY, 22), input_tokens=500, cost_usd=Decimal("0.002500")
    )
    again = service.rollup_day(tenant_id=TENANT, day=DAY)

    assert again.leads_ingested == 7
    assert again.leads_billable == 5
    assert again.cost_usd == Decimal("0.056500")


# ----------------------------------------------------------------- closed days only


def test_rollup_range_stops_at_yesterday_by_default() -> None:
    """A day is not final until it is over, and the default must be the safe one."""
    service, store = build(now=datetime(2026, 9, 5, 4, 0, tzinfo=UTC))

    rolled = service.rollup_range(tenant_id=TENANT, start=date(2026, 9, 1), end=date(2026, 9, 30))

    assert [totals.period.start for totals in rolled] == [
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 3),
        date(2026, 9, 4),
    ]
    assert store.rollups == [
        (TENANT, day)
        for day in (
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 3),
            date(2026, 9, 4),
        )
    ]


def test_rollup_range_can_be_asked_for_the_day_in_progress() -> None:
    """The admin view wants today; it is marked partial so nothing bills from it."""
    service, _ = build(now=datetime(2026, 9, 5, 4, 0, tzinfo=UTC))

    rolled = service.rollup_range(
        tenant_id=TENANT,
        start=date(2026, 9, 4),
        end=date(2026, 9, 5),
        closed_days_only=False,
    )

    assert [totals.period.start for totals in rolled] == [date(2026, 9, 4), date(2026, 9, 5)]
    assert [totals.partial for totals in rolled] == [False, True]


def test_rolling_up_a_month_that_has_not_started_closing_does_nothing() -> None:
    """On the first of the month there is no closed day in it yet."""
    service, store = build(now=datetime(2026, 9, 1, 0, 30, tzinfo=UTC))

    assert (
        service.rollup_range(tenant_id=TENANT, start=date(2026, 9, 1), end=date(2026, 9, 30)) == []
    )
    assert store.rollups == []


def test_rolling_up_today_says_so() -> None:
    service, _ = build(now=datetime(2026, 9, 5, 4, 0, tzinfo=UTC))
    assert service.rollup_day(tenant_id=TENANT, day=date(2026, 9, 5)).partial
    assert not service.rollup_day(tenant_id=TENANT, day=date(2026, 9, 4)).partial


# ------------------------------------------------------------------ reading a period


def test_usage_for_a_period_reads_the_rollup_and_not_the_source_tables() -> None:
    """The acceptance criterion "a single query returns billable usage", and the reason
    the rollup table exists: an assessment nobody has rolled up yet is not in the total."""
    service, store = build()
    seed_a_typical_day(store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    store.add_assessment(
        tenant_id=TENANT, created_at=at(DAY, 23), input_tokens=9_999, cost_usd=Decimal("1.00")
    )
    totals = service.usage_for_period(tenant_id=TENANT, period=SEPTEMBER)

    assert totals.leads_billable == 4
    assert totals.cost_usd == Decimal("0.054000")


def test_a_period_sums_the_days_in_it_and_nothing_else() -> None:
    service, store = build()
    seed_a_typical_day(store, day=date(2026, 9, 3))
    seed_a_typical_day(store, day=date(2026, 9, 4))
    seed_a_typical_day(store, day=date(2026, 10, 1))
    for day in (date(2026, 9, 3), date(2026, 9, 4), date(2026, 10, 1)):
        service.rollup_day(tenant_id=TENANT, day=day)

    totals = service.usage_for_period(tenant_id=TENANT, period=SEPTEMBER)

    assert totals.leads_ingested == 12
    assert totals.leads_billable == 8
    assert totals.period == SEPTEMBER
    assert not totals.partial


def test_a_period_that_runs_to_today_excludes_today_by_default() -> None:
    service, store = build(now=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    seed_a_typical_day(store, day=date(2026, 9, 4))
    seed_a_typical_day(store, day=date(2026, 9, 5))
    service.rollup_range(
        tenant_id=TENANT, start=date(2026, 9, 4), end=date(2026, 9, 5), closed_days_only=False
    )

    closed = service.usage_for_period(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))
    live = service.usage_for_period(
        tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9), closed_days_only=False
    )

    assert closed.leads_billable == 4
    assert closed.period.end == date(2026, 9, 4)
    assert not closed.partial
    assert live.leads_billable == 8
    assert live.partial


def test_a_period_with_no_closed_day_meters_as_zero() -> None:
    """Not an error, and not an exception: on the first of the month, nothing is billable
    yet. The zero is over the *requested* period, so a report still names the month."""
    service, store = build(now=datetime(2026, 9, 1, 3, 0, tzinfo=UTC))
    seed_a_typical_day(store, day=date(2026, 9, 1))
    service.rollup_day(tenant_id=TENANT, day=date(2026, 9, 1))

    totals = service.usage_for_period(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))

    assert totals.leads_billable == 0
    assert totals.cost_usd == Decimal(0)
    assert totals.period == BillingPeriod.of_month(2026, 9)


def test_a_wholly_open_period_reports_its_zero_as_partial() -> None:
    """A confident zero is the wrong answer for a month nobody has counted any of yet."""
    service, _ = build(now=datetime(2026, 9, 1, 3, 0, tzinfo=UTC))
    totals = service.usage_for_period(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))
    assert totals.leads_billable == 0
    assert totals.partial


def test_the_daily_view_drops_the_day_in_progress_by_default() -> None:
    """F5: ``--include-today`` was accepted and silently ignored on this branch, so a
    day-by-day view of a billing period quietly included a day that could still grow."""
    service, store = build(now=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    for day in (date(2026, 9, 4), date(2026, 9, 5)):
        seed_a_typical_day(store, day=day)
        service.rollup_day(tenant_id=TENANT, day=day)

    closed = service.daily_usage(tenant_id=TENANT, period=SEPTEMBER)
    live = service.daily_usage(tenant_id=TENANT, period=SEPTEMBER, closed_days_only=False)

    assert [row.period.start for row in closed] == [date(2026, 9, 4)]
    assert [row.period.start for row in live] == [date(2026, 9, 4), date(2026, 9, 5)]


def test_the_daily_view_marks_the_open_day_partial() -> None:
    """It used to report ``"partial": false`` for today, contradicting the CLI's own
    documented promise that a partial day is always labelled."""
    service, store = build(now=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    for day in (date(2026, 9, 4), date(2026, 9, 5)):
        seed_a_typical_day(store, day=day)
        service.rollup_day(tenant_id=TENANT, day=day)

    rows = service.daily_usage(tenant_id=TENANT, period=SEPTEMBER, closed_days_only=False)

    assert [row.partial for row in rows] == [False, True]


def test_daily_usage_lists_only_the_days_that_were_rolled_up() -> None:
    """A missing day is absent rather than zero: "never rolled up" and "nothing happened"
    are different facts and only the caller knows which one matters."""
    service, store = build()
    seed_a_typical_day(store, day=date(2026, 9, 3))
    service.rollup_day(tenant_id=TENANT, day=date(2026, 9, 3))

    rows = service.daily_usage(tenant_id=TENANT, period=SEPTEMBER)

    assert [row.period.start for row in rows] == [date(2026, 9, 3)]


# ---------------------------------------------------------------------------- totals


def test_leads_filtered_counts_leads_against_leads() -> None:
    """Ingested minus *billable*, not minus attempts.

    Both sides of the subtraction have to be a count of leads. Subtracting attempts would
    report a lead that was retried after a dispatch failure as negative spam — the same
    confusion that made ``leads_billable`` overcharge before it was made per-lead.
    """
    retried = replace(
        UsageTotals.zero(tenant_id=TENANT, period=SEPTEMBER),
        leads_ingested=5,
        leads_assessed=7,
        leads_billable=3,
    )
    assert retried.leads_filtered == 2


def test_leads_filtered_is_never_negative() -> None:
    """One cause survives the fix: a lead received at 23:59 and assessed at 00:01 belongs
    to two different days, so a day can hold a billable lead whose submission is in
    yesterday's count. A negative "spam caught" figure would be unexplainable, and nothing
    that could reach an invoice is hidden — ``leads_billable`` is computed directly."""
    inverted = replace(
        UsageTotals.zero(tenant_id=TENANT, period=SEPTEMBER),
        leads_ingested=1,
        leads_billable=4,
    )
    assert inverted.leads_filtered == 0


def test_cost_per_billable_lead_is_unknown_when_nothing_was_billed() -> None:
    totals = UsageTotals.zero(tenant_id=TENANT, period=SEPTEMBER)
    assert totals.cost_per_billable_lead_usd is None


def test_cost_per_billable_lead_divides_by_the_billable_count() -> None:
    """Not by leads ingested: dividing by the spam would flatter the cost per lead."""
    service, store = build()
    seed_a_typical_day(store)
    totals = service.rollup_day(tenant_id=TENANT, day=DAY)
    assert totals.cost_per_billable_lead_usd == Decimal("0.054000") / 4


def test_the_four_token_counters_are_summed_disjointly() -> None:
    service, store = build()
    seed_a_typical_day(store)
    totals = service.rollup_day(tenant_id=TENANT, day=DAY)
    assert totals.input_tokens == 4_200
    assert totals.output_tokens == 1_200
    assert totals.cache_read_tokens == 6_000
    assert totals.cache_creation_tokens == 0
    assert totals.total_tokens == 11_400


# ----------------------------------------------------------------------------- quota


def quota_service(
    *, used: int, quota: int | None, alert: Decimal = DEFAULT_QUOTA_ALERT_FRACTION
) -> tuple[MeteringService, InMemoryMeteringStore]:
    """A service whose tenant has exactly ``used`` billable leads in September."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=quota, alert_fraction=alert))
    for index in range(used):
        store.add_lead(tenant_id=TENANT, received_at=at(DAY))
        store.add_assessment(
            tenant_id=TENANT,
            created_at=at(DAY),
            input_tokens=100,
            cost_usd=Decimal("0.000500"),
        )
        del index
    service, _ = build(store=store)
    service.rollup_day(tenant_id=TENANT, day=DAY)
    return service, store


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        (0, QuotaLevel.OK),
        (79, QuotaLevel.OK),
        # Exactly at the alert fraction: 80% of a plan is what "alert at 80%" promises,
        # and firing at 80.0001% would make the alert depend on rounding.
        (80, QuotaLevel.WARNING),
        (99, QuotaLevel.WARNING),
        # Exactly at the quota: the hundredth lead of a hundred-lead plan is included in
        # the plan. The hundred-and-first is overage.
        (100, QuotaLevel.WARNING),
        (101, QuotaLevel.EXCEEDED),
    ],
)
def test_quota_levels_at_the_boundaries(used: int, expected: QuotaLevel) -> None:
    service, _ = quota_service(used=used, quota=100)
    status = service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert status.level is expected
    assert status.used == used


def test_an_unlimited_plan_is_always_ok() -> None:
    service, _ = quota_service(used=10_000, quota=None)
    status = service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert status.level is QuotaLevel.OK
    assert status.quota is None
    assert status.fraction is None
    assert status.remaining is None


def test_the_alert_fraction_is_per_tenant() -> None:
    service, _ = quota_service(used=50, quota=100, alert=Decimal("0.50"))
    assert service.quota_status(tenant_id=TENANT, period=SEPTEMBER).level is QuotaLevel.WARNING


def test_a_quota_counts_billable_leads_and_not_spam() -> None:
    """The decision that matters commercially: bot traffic to a customer's own form does
    not eat their plan."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=4))
    for hour in range(10):
        store.add_lead(tenant_id=TENANT, received_at=at(DAY, hour % 24))
    for hour in range(3):
        store.add_assessment(tenant_id=TENANT, created_at=at(DAY, hour), input_tokens=100)
    service, _ = build(store=store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    status = service.quota_status(tenant_id=TENANT, period=SEPTEMBER)

    assert status.used == 3
    assert status.level is QuotaLevel.OK


def test_remaining_is_floored_at_zero() -> None:
    service, _ = quota_service(used=150, quota=100)
    assert service.quota_status(tenant_id=TENANT, period=SEPTEMBER).remaining == 0


def test_a_live_quota_check_includes_today() -> None:
    """An alert is about what is happening now; excluding today would make a tenant that
    blew through its plan this morning look fine until midnight."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=1))
    today = date(2026, 9, 5)
    for _ in range(5):
        store.add_assessment(tenant_id=TENANT, created_at=at(today), input_tokens=100)
    service, _ = build(store=store, now=datetime(2026, 9, 5, 10, 0, tzinfo=UTC))
    service.rollup_day(tenant_id=TENANT, day=today)

    status = service.quota_status(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))

    assert status.level is QuotaLevel.EXCEEDED
    assert status.partial


def test_a_live_quota_check_reads_today_from_the_source_tables() -> None:
    """F7: the rollup is written after midnight, so today has no row in it.

    A quota check that read only the rollup would report zero for today however its
    arguments were set, and a tenant who blew through their plan this morning would look
    fine until tomorrow — which is the entire failure the alert exists to prevent. The
    earlier test passed only because it rolled today up first, a step the runbook never
    prescribes.
    """
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=3))
    today = date(2026, 9, 5)
    for index in range(9):
        store.add_assessment(
            tenant_id=TENANT, created_at=at(today), lead_id=f"live-{index}", input_tokens=100
        )
    service, _ = build(store=store, now=datetime(2026, 9, 5, 10, 0, tzinfo=UTC))
    # Deliberately no rollup of today: this is the state the prescribed cron leaves.

    status = service.quota_status(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))

    assert status.used == 9
    assert status.level is QuotaLevel.EXCEEDED
    assert status.partial
    assert store.computed == [(TENANT, today)], "today, once, for this tenant only"


def test_a_live_quota_check_reads_but_never_writes() -> None:
    """A write inside a read path is how a reporting command takes a row lock on a table
    a billing job is updating."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=3))
    store.add_assessment(tenant_id=TENANT, created_at=at(date(2026, 9, 5)), input_tokens=100)
    service, _ = build(store=store, now=datetime(2026, 9, 5, 10, 0, tzinfo=UTC))

    service.quota_status(tenant_id=TENANT, period=BillingPeriod.of_month(2026, 9))

    assert store.rollups == []
    assert store.rows == {}


def test_a_live_quota_check_adds_today_to_the_closed_days() -> None:
    """Closed days come from the rollup and today from source; neither is double counted."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=100))
    for index in range(4):
        store.add_assessment(
            tenant_id=TENANT,
            created_at=at(date(2026, 9, 4)),
            lead_id=f"closed-{index}",
            input_tokens=100,
        )
    for index in range(3):
        store.add_assessment(
            tenant_id=TENANT,
            created_at=at(date(2026, 9, 5)),
            lead_id=f"open-{index}",
            input_tokens=100,
        )
    service, _ = build(store=store, now=datetime(2026, 9, 5, 10, 0, tzinfo=UTC))
    service.rollup_day(tenant_id=TENANT, day=date(2026, 9, 4))

    period = BillingPeriod.of_month(2026, 9)
    assert service.quota_status(tenant_id=TENANT, period=period).used == 7
    billed = service.quota_status(tenant_id=TENANT, period=period, closed_days_only=True)
    assert billed.used == 4, "the figure a billing job would use excludes today"


def test_a_quota_check_outside_the_current_month_does_not_read_source_tables() -> None:
    """September, checked in October: every day of it is closed and in the rollup."""
    service, store = quota_service(used=10, quota=100)
    store.computed.clear()
    service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert store.computed == []


def test_the_fleet_sweep_checks_every_tenant_that_has_a_plan() -> None:
    """The thing to schedule. ``quota <slug>`` reports one customer, which is no use as a
    standing alert across a growing customer list."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=10))
    store.given_quota(TenantQuota(tenant_id=OTHER, monthly_lead_quota=10))
    store.given_quota(TenantQuota(tenant_id="unlimited-co", monthly_lead_quota=None))
    for index in range(20):
        store.add_assessment(
            tenant_id=OTHER, created_at=at(DAY), lead_id=f"o-{index}", input_tokens=100
        )
    service, _ = build(store=store)
    service.rollup_day(tenant_id=OTHER, day=DAY)

    statuses = service.quota_statuses(period=SEPTEMBER)

    assert [status.tenant_id for status in statuses] == [TENANT, OTHER]
    assert {status.tenant_id: status.level for status in statuses} == {
        TENANT: QuotaLevel.OK,
        OTHER: QuotaLevel.EXCEEDED,
    }


def test_crossing_a_quota_emits_an_event_and_a_metric() -> None:
    service, _ = quota_service(used=90, quota=100)
    with capture_json_logs() as logs:
        service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    record = logs.one(EVENT_QUOTA_CROSSED)
    assert record["tenant_id"] == TENANT
    assert record["quota_level"] == "warning"
    assert record["leads_billable"] == 90
    assert record["monthly_lead_quota"] == 100
    assert record["QuotaUsedLeads"] == 90
    assert record["QuotaFraction"] == pytest.approx(0.9)
    assert record["QuotaExceeded"] == 0


def test_going_over_the_quota_marks_the_metric() -> None:
    service, _ = quota_service(used=120, quota=100)
    with capture_json_logs() as logs:
        service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert logs.one(EVENT_QUOTA_CROSSED)["QuotaExceeded"] == 1


def test_a_tenant_inside_its_plan_emits_nothing() -> None:
    """The event is the crossing, not a heartbeat: one metric per tenant per check would
    be a per-customer custom metric bill for a number nobody is watching."""
    service, _ = quota_service(used=10, quota=100)
    with capture_json_logs() as logs:
        service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert logs.events(EVENT_QUOTA_CROSSED) == []


def test_an_unlimited_tenant_emits_nothing_however_much_it_uses() -> None:
    service, _ = quota_service(used=10_000, quota=None)
    with capture_json_logs() as logs:
        service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert logs.events(EVENT_QUOTA_CROSSED) == []


def test_going_over_a_quota_still_returns_a_status_and_refuses_nothing() -> None:
    """Invariant 3 is not negotiable. There is no exception, no flag to act on and no
    method here that could stop a lead: a customer over their plan gets an invoice and a
    conversation, not silently unqualified leads."""
    service, _ = quota_service(used=10_000, quota=1)
    status = service.quota_status(tenant_id=TENANT, period=SEPTEMBER)
    assert status.level is QuotaLevel.EXCEEDED
    assert not hasattr(status, "allowed")
    assert not hasattr(status, "blocked")
    forbidden = {"block", "enforce", "reject", "suspend", "throttle"}
    for name, _member in inspect.getmembers(MeteringService, inspect.isfunction):
        assert not any(word in name for word in forbidden), f"MeteringService.{name}"


def test_a_quota_for_an_unknown_tenant_is_an_error() -> None:
    """Reported rather than defaulted to unlimited: "this customer is fine" is the wrong
    thing to say about a tenant that does not exist."""
    service, _ = build()
    with pytest.raises(Exception, match="no tenant"):
        service.quota_status(tenant_id="ghost", period=SEPTEMBER)


def test_quota_status_of_an_empty_period_is_ok() -> None:
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=100))
    service, _ = build(store=store)
    assert service.quota_status(tenant_id=TENANT, period=SEPTEMBER).level is QuotaLevel.OK


def test_a_quota_of_zero_is_treated_as_spent_rather_than_dividing_by_it() -> None:
    """The database refuses a zero quota, so this is only reachable by a hand-written row
    — and it must produce a number rather than a ZeroDivisionError in a billing job."""
    status = QuotaStatus.evaluate(
        tenant_id=TENANT,
        period=SEPTEMBER,
        used=5,
        quota=TenantQuota(tenant_id=TENANT, monthly_lead_quota=0),
    )
    assert status.level is QuotaLevel.EXCEEDED
    assert status.fraction is None


def test_a_plan_can_be_set_and_taken_away() -> None:
    """A quota nobody can configure is not a feature. ``usagectl set-quota`` writes it, and
    unlimited — the state every tenant starts in — has to be reachable again afterwards."""
    service, store = quota_service(used=90, quota=None)

    service.set_quota(tenant_id=TENANT, monthly_lead_quota=100)
    assert service.quota_status(tenant_id=TENANT, period=SEPTEMBER).level is QuotaLevel.WARNING

    service.set_quota(tenant_id=TENANT, monthly_lead_quota=None)
    assert service.quota_status(tenant_id=TENANT, period=SEPTEMBER).level is QuotaLevel.OK
    assert store.quotas[TENANT].monthly_lead_quota is None


def test_a_plan_of_zero_is_refused_with_the_alternative_named() -> None:
    """Zero leads a month is a suspension, and there is a status column for that."""
    service, _ = quota_service(used=0, quota=None)
    with pytest.raises(MeteringError, match="not a plan"):
        service.set_quota(tenant_id=TENANT, monthly_lead_quota=0)


@pytest.mark.parametrize("fraction", [Decimal("0"), Decimal("-0.1"), Decimal("1.01")])
def test_an_alert_fraction_outside_zero_to_one_is_refused(fraction: Decimal) -> None:
    """At 0 it fires on the first lead of every month; above 1 it can never fire at all.
    Checked here as well as by the CHECK constraint, so an operator gets a sentence."""
    service, _ = quota_service(used=0, quota=None)
    with pytest.raises(MeteringError, match="outside"):
        service.set_quota(tenant_id=TENANT, monthly_lead_quota=100, alert_fraction=fraction)


def test_the_alert_fraction_bounds_are_inclusive_at_one() -> None:
    """An alert at exactly 100% of the plan is a legitimate setting: it means "tell me when
    they run out", and it is the one a customer on a hard-ish plan would ask for."""
    service, _ = quota_service(used=0, quota=None)
    written = service.set_quota(
        tenant_id=TENANT, monthly_lead_quota=100, alert_fraction=Decimal("1")
    )
    assert written.alert_fraction == Decimal("1")


# ---------------------------------------------------------------------------- margin


def test_margin_is_unknown_while_revenue_is_unknown() -> None:
    """A fabricated revenue figure in a margin report is worse than an absent one: it gets
    quoted, put in a spreadsheet and used to price a plan."""
    service, store = build()
    seed_a_typical_day(store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    report = service.margin(tenant_id=TENANT, period=SEPTEMBER)

    assert report.revenue_usd is None
    assert report.margin_usd is None
    assert report.margin_fraction is None
    assert report.inference_usd == Decimal("0.054000")
    assert report.cost_usd > Decimal(0)


def test_margin_is_revenue_minus_every_cost_once_revenue_is_known() -> None:
    service, store = build(revenue=StaticRevenue({TENANT: Decimal("500.00")}))
    seed_a_typical_day(store)
    service.rollup_day(tenant_id=TENANT, day=DAY)

    report = service.margin(tenant_id=TENANT, period=SEPTEMBER)

    assert report.revenue_usd == Decimal("500.00")
    assert report.margin_usd == Decimal("500.00") - report.cost_usd
    assert report.margin_fraction == report.margin_usd / Decimal("500.00")


def test_margin_excludes_the_day_in_progress_by_default() -> None:
    """F6: flipping this default to ``False`` used to pass the whole suite. A margin
    report is a billing artefact and must not be computed from a day that can still grow."""
    service, store = build(now=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    for day in (date(2026, 9, 4), date(2026, 9, 5)):
        seed_a_typical_day(store, day=day)
        service.rollup_day(tenant_id=TENANT, day=day)
    period = BillingPeriod.of_month(2026, 9)

    closed = service.margin(tenant_id=TENANT, period=period)
    live = service.margin(tenant_id=TENANT, period=period, closed_days_only=False)

    assert closed.usage.period.end == date(2026, 9, 4)
    assert closed.inference_usd == Decimal("0.054000")
    assert not closed.usage.partial
    assert live.inference_usd == Decimal("0.108000")
    assert live.usage.partial


def test_infrastructure_is_allocated_pro_rata_by_billable_leads() -> None:
    """Two tenants, one with three quarters of the billable leads, pays three quarters of
    the (fixed) bill. It is a convention and the report has to say so."""
    service, store = build()
    for _ in range(30):
        store.add_assessment(tenant_id=TENANT, created_at=at(DAY), input_tokens=100)
    for _ in range(10):
        store.add_assessment(tenant_id=OTHER, created_at=at(DAY), input_tokens=100)
    service.rollup_day(tenant_id=TENANT, day=DAY)
    service.rollup_day(tenant_id=OTHER, day=DAY)

    report = service.margin(tenant_id=TENANT, period=SEPTEMBER)

    assert report.fleet_billable_leads == 40
    assert report.infrastructure_usd == MONTHLY_INFRASTRUCTURE_USD * Decimal(30) / Decimal(40)
    assert "allocated pro rata" in report.allocation_caveat
    assert "fixed" in report.allocation_caveat


def test_a_tenant_with_no_billable_leads_is_allocated_nothing() -> None:
    service, _ = build()
    report = service.margin(tenant_id=TENANT, period=SEPTEMBER)
    assert report.fleet_billable_leads == 0
    assert report.infrastructure_usd == Decimal(0)
    assert report.cost_usd == Decimal(0)


def test_a_whole_calendar_month_is_charged_the_whole_monthly_bill() -> None:
    assert infrastructure_usd_for(BillingPeriod.of_month(2026, 9)) == MONTHLY_INFRASTRUCTURE_USD
    assert infrastructure_usd_for(BillingPeriod.of_month(2026, 2)) == MONTHLY_INFRASTRUCTURE_USD


def test_part_of_a_month_is_charged_in_proportion_to_its_days() -> None:
    half = BillingPeriod(start=date(2026, 9, 1), end=date(2026, 9, 15))
    assert infrastructure_usd_for(half) == MONTHLY_INFRASTRUCTURE_USD * Decimal(15) / Decimal(30)


def test_a_period_spanning_two_months_is_charged_each_month_s_share() -> None:
    period = BillingPeriod(start=date(2026, 1, 30), end=date(2026, 2, 2))
    expected = MONTHLY_INFRASTRUCTURE_USD * Decimal(2) / Decimal(
        31
    ) + MONTHLY_INFRASTRUCTURE_USD * Decimal(2) / Decimal(28)
    assert infrastructure_usd_for(period) == expected


# --------------------------------------------------------------------- reconciliation


INVOICE_CSV = """date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd
2026-09-03,4200,1200,6000,0,0.054000
2026-09-04,4200,1200,6000,0,0.054000
"""


def reconcilable_service() -> MeteringService:
    """A service with two September days rolled up at $0.054 each."""
    service, store = build()
    for day in (date(2026, 9, 3), date(2026, 9, 4)):
        seed_a_typical_day(store, day=day)
        service.rollup_day(tenant_id=TENANT, day=day)
    return service


def test_an_invoice_that_matches_is_within_tolerance() -> None:
    report = reconcilable_service().reconcile(
        invoice=parse_invoice_csv(INVOICE_CSV), period=SEPTEMBER
    )
    assert report.ours_usd == Decimal("0.108000")
    assert report.invoice_usd == Decimal("0.108000")
    assert report.variance_usd == Decimal(0)
    assert report.variance_fraction == Decimal(0)
    assert report.within_tolerance


def test_a_small_drift_is_within_tolerance() -> None:
    """The rate card is a snapshot and the console rounds; 1% is not a bug."""
    invoice = [
        DailySpend(usage_date=date(2026, 9, 3), cost_usd=Decimal("0.0545")),
        DailySpend(usage_date=date(2026, 9, 4), cost_usd=Decimal("0.0540")),
    ]
    report = reconcilable_service().reconcile(invoice=invoice, period=SEPTEMBER)
    assert report.variance_usd == Decimal("-0.0005")
    assert report.within_tolerance


def test_a_large_drift_is_outside_tolerance() -> None:
    invoice = [
        DailySpend(usage_date=date(2026, 9, 3), cost_usd=Decimal("0.100000")),
        DailySpend(usage_date=date(2026, 9, 4), cost_usd=Decimal("0.100000")),
    ]
    report = reconcilable_service().reconcile(invoice=invoice, period=SEPTEMBER)
    assert not report.within_tolerance
    assert report.variance_fraction is not None
    assert report.variance_fraction < -RECONCILIATION_TOLERANCE


def test_a_day_the_export_did_not_cover_shows_as_a_full_variance() -> None:
    """Dropped rather than shown, this is how a month quietly reconciles while a day of
    spend is missing from one side."""
    invoice = [DailySpend(usage_date=date(2026, 9, 3), cost_usd=Decimal("0.054000"))]
    report = reconcilable_service().reconcile(invoice=invoice, period=SEPTEMBER)
    missing = next(day for day in report.days if day.usage_date == date(2026, 9, 4))
    assert missing.invoice_usd == Decimal(0)
    assert missing.ours_usd == Decimal("0.054000")
    assert missing.variance_fraction is None
    assert not report.within_tolerance


def test_a_day_we_have_no_rollup_for_still_appears() -> None:
    invoice = [
        *parse_invoice_csv(INVOICE_CSV),
        DailySpend(usage_date=date(2026, 9, 10), cost_usd=Decimal("2.000000")),
    ]
    report = reconcilable_service().reconcile(invoice=invoice, period=SEPTEMBER)
    orphan = next(day for day in report.days if day.usage_date == date(2026, 9, 10))
    assert orphan.ours_usd == Decimal(0)
    assert orphan.variance_fraction == Decimal(-1)
    assert not report.within_tolerance


def test_invoice_rows_outside_the_period_are_ignored() -> None:
    """So a whole-year export can be reconciled one month at a time."""
    invoice = [
        *parse_invoice_csv(INVOICE_CSV),
        DailySpend(usage_date=date(2026, 8, 30), cost_usd=Decimal("99.00")),
    ]
    report = reconcilable_service().reconcile(invoice=invoice, period=SEPTEMBER)
    assert report.invoice_usd == Decimal("0.108000")
    assert report.within_tolerance


def test_two_empty_sides_reconcile() -> None:
    service, _ = build()
    report = service.reconcile(invoice=[], period=SEPTEMBER)
    assert report.days == []
    assert report.variance_fraction is None
    assert report.within_tolerance


def test_spend_against_an_invoice_of_zero_does_not_reconcile() -> None:
    """There is nothing to take a percentage of, and "we think this cost money and they
    did not bill it" is exactly the thing worth looking at."""
    report = reconcilable_service().reconcile(invoice=[], period=SEPTEMBER)
    assert report.variance_fraction is None
    assert not report.within_tolerance


# ------------------------------------------------------------------- the invoice file


def test_an_export_parses_into_one_row_per_day() -> None:
    rows = parse_invoice_csv(INVOICE_CSV)
    assert [row.usage_date for row in rows] == [date(2026, 9, 3), date(2026, 9, 4)]
    assert rows[0].input_tokens == 4_200
    assert rows[0].cost_usd == Decimal("0.054000")


def test_rows_for_the_same_day_are_summed() -> None:
    """The console exports one row per day *per model*, and both sides of the comparison
    have to be one row per day for it to mean anything."""
    text = (
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,1000,100,0,0,0.010000\n"
        "2026-09-03,2000,200,0,0,0.020000\n"
    )
    rows = parse_invoice_csv(text)
    assert len(rows) == 1
    assert rows[0].input_tokens == 3_000
    assert rows[0].cost_usd == Decimal("0.030000")


def test_the_consoles_other_column_names_are_accepted() -> None:
    """``cache_read_input_tokens`` and ``amount_usd`` are spellings the console has used;
    failing to load a CSV on invoice day over a header change is a bad trade."""
    text = (
        "usage_date,uncached_input_tokens,output_tokens,cache_read_input_tokens,"
        "cache_creation_input_tokens,amount_usd\n"
        '2026-09-03,1000,100,50,25,"$1,234.50"\n'
    )
    rows = parse_invoice_csv(text)
    assert rows[0].cache_read_tokens == 50
    assert rows[0].cache_creation_tokens == 25
    assert rows[0].cost_usd == Decimal("1234.50")


def test_a_timestamped_export_is_truncated_to_its_day() -> None:
    text = (
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03T00:00:00Z,1000,100,0,0,0.010000\n"
    )
    assert parse_invoice_csv(text)[0].usage_date == date(2026, 9, 3)


def test_a_csv_with_the_wrong_columns_names_what_it_found() -> None:
    """The usual cause is an export of the wrong report, and the fastest fix is seeing
    what the file actually contained."""
    text = "day,model,tokens,spend\n2026-09-03,claude-opus-5,1000,1.00\n"
    with pytest.raises(InvoiceFormatError) as caught:
        parse_invoice_csv(text)
    message = str(caught.value)
    assert "input_tokens" in message
    assert "model" in message and "spend" in message


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(InvoiceFormatError, match="no header"):
        parse_invoice_csv("")


def test_a_row_with_a_bad_date_names_its_line() -> None:
    text = (
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,1,1,1,1,1\n"
        "the third,1,1,1,1,1\n"
    )
    with pytest.raises(InvoiceFormatError, match="line 3"):
        parse_invoice_csv(text)


def test_a_row_with_a_bad_number_names_its_line() -> None:
    text = (
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,lots,1,1,1,1\n"
    )
    with pytest.raises(InvoiceFormatError, match="line 2"):
        parse_invoice_csv(text)


def test_blank_cells_read_as_zero() -> None:
    """A console export leaves a cell empty rather than writing 0 for a day with no cache
    activity; refusing that would make the common case the failing one."""
    text = (
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,1000,100,,,0.010000\n"
    )
    assert parse_invoice_csv(text)[0].cache_read_tokens == 0


# ---------------------------------------------------------------------------- periods


def test_a_month_parses_from_its_usual_spelling() -> None:
    assert BillingPeriod.parse_month("2026-09") == BillingPeriod(
        start=date(2026, 9, 1), end=date(2026, 9, 30)
    )


@pytest.mark.parametrize("text", ["09-2026", "september", "2026-13", "2026", ""])
def test_a_month_that_is_not_a_month_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"YYYY-MM|month must be"):
        BillingPeriod.parse_month(text)


def test_an_unpadded_month_is_accepted() -> None:
    """``2026-9`` can only mean September 2026, so it is read rather than refused. The
    reversed spelling ``09-2026`` is the one that is ambiguous to a reader and wrong to a
    parser, and that one is refused above."""
    assert BillingPeriod.parse_month("2026-9") == BillingPeriod.of_month(2026, 9)


def test_february_knows_how_long_it_is() -> None:
    assert BillingPeriod.of_month(2026, 2).end == date(2026, 2, 28)
    assert BillingPeriod.of_month(2028, 2).end == date(2028, 2, 29)


def test_a_period_that_ends_before_it_starts_is_refused() -> None:
    """Returning zero usage for it would look exactly like a quiet month."""
    with pytest.raises(ValueError, match="ends"):
        BillingPeriod(start=date(2026, 9, 10), end=date(2026, 9, 1))


def test_a_period_counts_both_of_its_ends() -> None:
    period = BillingPeriod(start=date(2026, 9, 1), end=date(2026, 9, 3))
    assert period.days == 3
    assert list(period.dates()) == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def test_the_last_closed_day_is_yesterday() -> None:
    closed = SEPTEMBER.closed_as_of(today=date(2026, 9, 15))
    assert closed == BillingPeriod(start=date(2026, 9, 1), end=date(2026, 9, 14))


def test_a_period_entirely_in_the_past_is_closed_in_full() -> None:
    assert SEPTEMBER.closed_as_of(today=date(2026, 10, 1)) == SEPTEMBER


def test_a_period_with_nothing_closed_yet_is_none_rather_than_empty() -> None:
    """``None`` and an empty period are different answers: "there is nothing to bill yet"
    is not "the bill is zero", and a caller should not be able to confuse them silently."""
    assert SEPTEMBER.closed_as_of(today=date(2026, 9, 1)) is None


def test_a_period_renders_the_way_an_invoice_line_reads() -> None:
    assert str(SEPTEMBER) == "2026-09-01..2026-09-30"
    assert str(BillingPeriod.of_day(DAY)) == "2026-09-03"


# -------------------------------------------------------------- structural properties


def test_every_tenant_scoped_store_method_names_the_tenant() -> None:
    """Invariant 4 as a property of the Protocol rather than of one implementation.

    The two ``fleet_`` methods are the deliberate exceptions — reconciliation compares
    against an invoice for the whole workspace and cost allocation needs every tenant's
    share — and they are named for it, and return their results keyed by tenant or by day,
    so an unattributed total cannot be produced by accident at a call site.
    """
    for name, method in inspect.getmembers(MeteringStorePort, inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = inspect.signature(method).parameters
        if name.startswith("fleet_"):
            assert "tenant_id" not in parameters, f"{name} is not fleet-wide after all"
            continue
        assert "tenant_id" in parameters, f"{name} has no tenant scope"
        assert parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_service_takes_every_argument_by_keyword() -> None:
    """``rollup_day(tenant, day)`` and ``rollup_day(day, tenant)`` must not both typecheck
    into something that runs; the store conventions in this repository are keyword-only."""
    for name, method in inspect.getmembers(MeteringService, inspect.isfunction):
        if name.startswith("_") or name in {"today"}:
            continue
        positional = [
            parameter
            for parameter in inspect.signature(method).parameters.values()
            if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.name != "self"
        ]
        assert not positional, f"MeteringService.{name} takes {positional} positionally"


def test_the_default_alert_fraction_is_four_fifths() -> None:
    """Pinned because the database has the same number as a server default and the two
    drifting apart would make a tenant's alert depend on how its row was created."""
    assert Decimal("0.80") == DEFAULT_QUOTA_ALERT_FRACTION
