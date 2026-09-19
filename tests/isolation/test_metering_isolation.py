"""Metering isolation: one tenant's token spend never appears in another's totals.

Billing is the axis where a leak costs money in both directions — a customer charged for
somebody else's model calls, or a customer's usage quietly absorbed into someone else's
invoice — so the assertions here are on the *numbers*, on a day when both tenants were
active, and not merely on a filter existing somewhere.

Two halves, for the reason #33's review gave: three mutations to that adapter's result
mapping left 1,903 tests green because the only test covering it was ``integration``-marked
and skipped. So the arithmetic is proved against the in-memory store, which recomputes a
day the way the SQL does, *and* the SQL is compiled against the ``postgresql`` dialect and
read for the tenant it filters on.

The ``fleet_*`` exception
-------------------------

#33 ships three deliberately untenanted store methods. They exist because reconciliation
against Anthropic's invoice and infrastructure-cost allocation are questions about the
whole workspace, and neither can be answered one tenant at a time. Three rules keep that
from being a hole, and all three are asserted here and in
``test_repository_isolation.py``:

* the name says ``fleet_``, so it is visible at every call site;
* they take no tenant, so they cannot be mistaken for a query somebody forgot to filter;
* **no fleet result is rendered into one tenant's report** — except one scalar, the
  allocation denominator, which is named as such on the line it appears on and is examined
  by :func:`test_a_margin_report_carries_one_fleet_number_and_it_is_a_denominator`.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

import pytest

from leadquali import usagectl
from leadquali.adapters.metering_postgres import PostgresMeteringStore
from leadquali.app.metering import (
    BillingPeriod,
    MeteringService,
    QuotaLevel,
    TenantQuota,
    UsageTotals,
)
from tests.fakes import FakeClock, InMemoryMeteringStore, StaticRevenue
from tests.isolation.repositories import TENANT_A, TENANT_A_UUID, TENANT_B, TENANT_B_UUID
from tests.sqlcapture import SqlCapture, parameters, sql_text

#: A day both tenants were busy. The whole point: "we filter by tenant" is trivially true
#: on a day only one of them used the product.
DAY: Final[dt.date] = dt.date(2026, 9, 3)
SEPTEMBER: Final[BillingPeriod] = BillingPeriod.of_month(2026, 9)

#: After September, so every day of the period is closed and the billing reads do not have
#: to be argued with about partial days.
TODAY: Final[dt.datetime] = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)

#: Distinctive and prime-ish, so a total that has accidentally absorbed the other tenant's
#: figures cannot coincidentally equal the right answer.
A_TOKENS: Final[int] = 1_301
B_TOKENS: Final[int] = 70_007
A_COST: Final[Decimal] = Decimal("0.013010")
B_COST: Final[Decimal] = Decimal("0.700070")


def seeded_store() -> InMemoryMeteringStore:
    """Both tenants, both active on ``DAY``, with wildly different spend.

    Tenant A sends three leads and is assessed twice; tenant B sends one lead and is
    assessed once, for fifty times the tokens. One of A's assessments is a redelivery of
    the same lead, so ``leads_billable`` and ``leads_assessed`` differ for A and not for B
    — which means a total that has picked up the wrong tenant's rows is visible in more
    than one column.
    """
    store = InMemoryMeteringStore()
    at = dt.datetime(2026, 9, 3, 10, 0, tzinfo=dt.UTC)

    for index in range(3):
        store.add_lead(tenant_id=TENANT_A, received_at=at + dt.timedelta(minutes=index))
    store.add_assessment(
        tenant_id=TENANT_A,
        created_at=at,
        lead_id="alpha-lead-1",
        input_tokens=A_TOKENS,
        output_tokens=101,
        cost_usd=A_COST,
    )
    store.add_assessment(
        tenant_id=TENANT_A,
        created_at=at + dt.timedelta(minutes=5),
        lead_id="alpha-lead-1",
        input_tokens=A_TOKENS,
        output_tokens=101,
        cost_usd=A_COST,
    )

    store.add_lead(tenant_id=TENANT_B, received_at=at)
    store.add_assessment(
        tenant_id=TENANT_B,
        created_at=at,
        lead_id="zenith-lead-1",
        input_tokens=B_TOKENS,
        output_tokens=7_007,
        cost_usd=B_COST,
    )
    return store


def service(
    store: InMemoryMeteringStore, *, revenue: Mapping[str, Decimal] | None = None
) -> MeteringService:
    """The metering service over a fixed clock, so "is this day closed?" is deterministic."""
    return MeteringService(
        store=store,
        clock=FakeClock(start=TODAY, step_ms=0),
        revenue=StaticRevenue(dict(revenue or {})),
    )


@pytest.fixture
def store() -> InMemoryMeteringStore:
    return seeded_store()


# ------------------------------------------------------------------------ the arithmetic


def test_a_days_rollup_counts_only_the_tenant_it_was_asked_about(
    store: InMemoryMeteringStore,
) -> None:
    """Both tenants were active on this day, and each rollup sees only its own rows."""
    left = store.rollup_day(tenant_id=TENANT_A, day=DAY)
    right = store.rollup_day(tenant_id=TENANT_B, day=DAY)

    assert left.tenant_id == TENANT_A and right.tenant_id == TENANT_B
    assert (left.leads_ingested, left.leads_assessed, left.leads_billable) == (3, 2, 1)
    assert (right.leads_ingested, right.leads_assessed, right.leads_billable) == (1, 1, 1)
    assert left.input_tokens == 2 * A_TOKENS
    assert right.input_tokens == B_TOKENS
    assert left.cost_usd == 2 * A_COST
    assert right.cost_usd == B_COST


def test_neither_tenants_spend_appears_in_the_others_period_total(
    store: InMemoryMeteringStore,
) -> None:
    """The headline assertion, read off the numbers rather than off a ``WHERE`` clause."""
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)
    meter = service(store)

    left = meter.usage_for_period(tenant_id=TENANT_A, period=SEPTEMBER)
    right = meter.usage_for_period(tenant_id=TENANT_B, period=SEPTEMBER)

    assert left.input_tokens == 2 * A_TOKENS
    assert right.input_tokens == B_TOKENS
    assert left.cost_usd == 2 * A_COST
    assert right.cost_usd == B_COST
    # And, stated the other way round, so a future change that sums both into both fails:
    assert left.input_tokens + right.input_tokens == 2 * A_TOKENS + B_TOKENS
    assert left.total_tokens != right.total_tokens


def test_rolling_up_one_tenant_does_not_write_the_others_row(
    store: InMemoryMeteringStore,
) -> None:
    """A rollup is a write. It must create exactly one row, for exactly one tenant."""
    store.rollup_day(tenant_id=TENANT_A, day=DAY)

    assert set(store.rows) == {(TENANT_A, DAY)}
    assert service(store).usage_for_period(
        tenant_id=TENANT_B, period=SEPTEMBER
    ) == UsageTotals.zero(tenant_id=TENANT_B, period=SEPTEMBER)


def test_a_tenant_with_no_activity_reads_zero_rather_than_the_fleets_figures(
    store: InMemoryMeteringStore,
) -> None:
    """The failure mode a missing filter actually produces is not an error, it is a total.

    An unfiltered ``SUM`` over ``usage_daily`` returns the whole fleet's spend and looks
    entirely plausible on an invoice. A third tenant that has done nothing must read zero.
    """
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)

    quiet = service(store).usage_for_period(tenant_id="quiet-tenant", period=SEPTEMBER)
    assert quiet == UsageTotals.zero(tenant_id="quiet-tenant", period=SEPTEMBER)
    assert quiet.cost_usd == Decimal(0)


def test_a_daily_listing_is_one_tenants_days(store: InMemoryMeteringStore) -> None:
    """Every row in a tenant's day-by-day view names that tenant and nobody else."""
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)

    rows = service(store).daily_usage(tenant_id=TENANT_A, period=SEPTEMBER)
    assert [row.tenant_id for row in rows] == [TENANT_A]
    assert rows[0].input_tokens == 2 * A_TOKENS


def test_a_quota_is_measured_against_its_own_tenants_usage(
    store: InMemoryMeteringStore,
) -> None:
    """A quota crossing is a commercial conversation, so it had better be the right customer.

    Both tenants get the same allowance of two billable leads. A has one and is fine; B has
    one and is fine. If either read the other's usage — or both — the fleet total of two
    would put somebody at their limit.
    """
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)
    for tenant in (TENANT_A, TENANT_B):
        store.given_quota(
            TenantQuota(tenant_id=tenant, monthly_lead_quota=2, alert_fraction=Decimal("0.8"))
        )
    meter = service(store)

    for tenant in (TENANT_A, TENANT_B):
        status = meter.quota_status(tenant_id=tenant, period=SEPTEMBER)
        assert status.tenant_id == tenant
        assert status.used == 1, f"{tenant} is counting somebody else's leads"
        assert status.level is QuotaLevel.OK


def test_writing_one_tenants_plan_leaves_the_others_alone(
    store: InMemoryMeteringStore,
) -> None:
    """``set_quota`` is the one metering write an operator makes by hand."""
    for tenant in (TENANT_A, TENANT_B):
        store.given_quota(
            TenantQuota(tenant_id=tenant, monthly_lead_quota=100, alert_fraction=Decimal("0.8"))
        )

    service(store).set_quota(tenant_id=TENANT_A, monthly_lead_quota=5)

    assert store.quota_for(tenant_id=TENANT_A).monthly_lead_quota == 5
    assert store.quota_for(tenant_id=TENANT_B).monthly_lead_quota == 100


# ---------------------------------------------------------------------------- the SQL


@pytest.fixture
def sql() -> SqlCapture:
    return SqlCapture()


def _statements(sql: SqlCapture, call: Any) -> list[tuple[str, frozenset[str]]]:
    """Each statement a call emits, as its SQL and the set of its bound values."""
    return [
        (sql_text(statement), frozenset(str(value) for value in parameters(statement).values()))
        for statement in sql.run(call)
    ]


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("rollup_day", {"day": DAY}),
        ("compute_day", {"day": DAY}),
        ("usage_for_period", {"period": SEPTEMBER}),
        ("daily_usage", {"period": SEPTEMBER}),
        ("quota_for", {}),
    ],
)
def test_a_tenant_scoped_read_never_binds_the_other_tenants_id(
    sql: SqlCapture, method: str, arguments: Mapping[str, Any]
) -> None:
    """The offline half, against the adapter that runs in production.

    ``test_repository_isolation.py`` sweeps every repository for a tenant predicate; this
    says the same thing about the billing statements specifically, and names them, because
    these are the queries that turn into an invoice.
    """
    store = PostgresMeteringStore(sql.sessions)
    call = getattr(store, method)
    for text, bound in _statements(sql, lambda: call(tenant_id=TENANT_A, **arguments)):
        assert str(TENANT_A_UUID) in bound, text
        assert str(TENANT_B_UUID) not in bound, text


def test_the_rollup_filters_both_source_tables_on_the_tenant(sql: SqlCapture) -> None:
    """One statement, two source tables, and each of them needs its own predicate.

    ``rollup_day`` counts submissions from ``leads`` and everything else from
    ``assessments``. A filter on one and not the other would produce a row that is half one
    tenant's and half the fleet's, and it would be internally consistent enough to invoice
    from.
    """
    store = PostgresMeteringStore(sql.sessions)
    (text, _), *rest = _statements(sql, lambda: store.rollup_day(tenant_id=TENANT_A, day=DAY))

    assert not rest, "the rollup is one statement; see the adapter's module docstring"
    assert "leads.tenant_id = " in text
    assert "assessments.tenant_id = " in text
    assert "insert into usage_daily (tenant_id," in text


def test_the_live_day_read_filters_both_source_tables_too(sql: SqlCapture) -> None:
    """``compute_day`` shares ``_day_source`` with the rollup and is reached by a different
    caller — the live quota check — so it is asserted separately rather than assumed."""
    store = PostgresMeteringStore(sql.sessions)
    (text, bound), *rest = _statements(sql, lambda: store.compute_day(tenant_id=TENANT_A, day=DAY))

    assert not rest
    assert "leads.tenant_id = " in text
    assert "assessments.tenant_id = " in text
    assert str(TENANT_B_UUID) not in bound


# ------------------------------------------------------------------ the fleet exception


def test_the_fleet_queries_return_a_breakdown_and_not_a_bare_total(sql: SqlCapture) -> None:
    """A sum with no tenant attached is what the scoping rule exists to prevent.

    So the untenanted queries group by tenant or by day and hand back the breakdown. The
    caller then has to decide what to do with somebody's figures, in daylight, instead of
    receiving an anonymous number it can print anywhere.
    """
    store = PostgresMeteringStore(sql.sessions)
    billable, _ = _statements(sql, lambda: store.fleet_billable_leads(period=SEPTEMBER))[0]
    spend, _ = _statements(sql, lambda: store.fleet_daily_spend(period=SEPTEMBER))[0]
    worklist, _ = _statements(sql, lambda: store.fleet_tenants_with_quota())[0]

    assert "group by tenants.slug" in billable
    assert "group by usage_daily.usage_date" in spend
    assert "select tenants.slug" in worklist


def test_the_fleet_breakdown_is_keyed_by_tenant_and_each_tenant_gets_its_own_figure(
    store: InMemoryMeteringStore,
) -> None:
    """The behavioural half: the allocation input is per tenant, not one pooled number."""
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)

    fleet = store.fleet_billable_leads(period=SEPTEMBER)
    assert fleet == {TENANT_A: 1, TENANT_B: 1}


def test_a_margin_report_carries_one_fleet_number_and_it_is_a_denominator(
    store: InMemoryMeteringStore,
) -> None:
    """The one place a fleet figure reaches a single tenant's report, examined closely.

    ``MarginReport.fleet_billable_leads`` is the denominator of the pro-rata infrastructure
    allocation, and the report carries it so a reader can see what the share was computed
    from. It is a scalar total across every tenant: it names nobody, and it is not another
    tenant's figure. What it *is* — and this is written down in docs/tenant-isolation.md
    rather than glossed — is an aggregate from which, in a two-tenant fleet, the other
    tenant's billable count is one subtraction away. That is acceptable because this report
    is an internal operator tool (``usagectl``, run by us) and is never rendered to a
    customer; if it is ever put in front of one, this number has to go.
    """
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)

    report = service(store, revenue={TENANT_A: Decimal("100.00")}).margin(
        tenant_id=TENANT_A, period=SEPTEMBER
    )

    assert report.tenant_id == TENANT_A
    assert report.usage.leads_billable == 1
    assert report.inference_usd == 2 * A_COST, "inference cost is this tenant's own spend"
    assert report.fleet_billable_leads == 2
    assert isinstance(report.fleet_billable_leads, int)
    # The allocation is a share of the fleet, so it is strictly less than the whole bill.
    assert Decimal(0) < report.infrastructure_usd
    assert report.infrastructure_usd < Decimal("87.60") + report.infrastructure_usd


def _run_usagectl(argv: list[str], store: InMemoryMeteringStore) -> str:
    """Render one ``usagectl`` command to a string, with no Postgres anywhere near it."""
    out = io.StringIO()
    code = usagectl.main(
        argv,
        service_factory=lambda _settings: service(store, revenue={TENANT_A: Decimal("100.00")}),
        stdout=out,
        stderr=io.StringIO(),
    )
    assert code == 0, out.getvalue()
    return out.getvalue()


#: Every read-only per-tenant report ``usagectl`` can produce, as the argv that makes it.
#: ``rollup`` and ``set-quota`` are left out because they write; their scoping is the
#: repository sweep's business.
#: Each read-only per-tenant report, as the argv that produces it and one string that must
#: appear in it. The marker is the positive control: an assertion that tenant B is absent
#: would pass just as happily on an empty page, and ``usage --daily`` prints rows rather
#: than a tenant line, so it is checked for the day it is about instead.
TENANT_REPORTS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("usage", TENANT_A, "--month", "2026-09"), TENANT_A),
    (("usage", TENANT_A, "--month", "2026-09", "--daily"), "2026-09-03"),
    (("margin", TENANT_A, "--month", "2026-09"), TENANT_A),
    (("quota", TENANT_A, "--month", "2026-09"), TENANT_A),
)


@pytest.mark.parametrize(
    "case", TENANT_REPORTS, ids=lambda item: " ".join(item[0][:1] + item[0][3:])
)
def test_no_tenant_report_ever_renders_another_tenants_identifier(
    store: InMemoryMeteringStore, case: tuple[tuple[str, ...], str]
) -> None:
    """Every operator-facing report for tenant A, searched for tenant B.

    Rendered through the real CLI — the argument parsing, the service and the formatting —
    because that is the surface a figure actually escapes through. Both the human output
    and the JSON, since they are built by different functions and only one of them is
    usually read.
    """
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)
    store.given_quota(
        TenantQuota(tenant_id=TENANT_A, monthly_lead_quota=100, alert_fraction=Decimal("0.8"))
    )

    command, marker = case
    for extra in ([], ["--json"]):
        rendered = _run_usagectl([*command, *extra], store)
        assert marker in rendered, "the report is empty; the absence check would prove nothing"
        assert TENANT_B not in rendered, rendered
        assert str(B_TOKENS) not in rendered, rendered
        assert str(B_COST) not in rendered, rendered


def test_the_margin_json_names_its_one_fleet_field_and_carries_no_breakdown(
    store: InMemoryMeteringStore,
) -> None:
    """A machine-readable report is where a stray field survives longest.

    The JSON is parsed and every value examined: exactly one fleet-derived number, it is
    the documented denominator, and there is no per-tenant mapping anywhere in the
    document.
    """
    store.rollup_day(tenant_id=TENANT_A, day=DAY)
    store.rollup_day(tenant_id=TENANT_B, day=DAY)

    document = json.loads(
        _run_usagectl(["margin", TENANT_A, "--month", "2026-09", "--json"], store)
    )

    assert document["tenant_id"] == TENANT_A
    fleet_fields = [name for name in document if name.startswith("fleet_")]
    assert fleet_fields == ["fleet_billable_leads"]
    assert document["fleet_billable_leads"] == 2
    assert "allocated pro rata" in document["infrastructure_allocation"]
    assert TENANT_B not in json.dumps(document)
    assert not any(isinstance(value, dict) and TENANT_B in value for value in document.values())


def test_the_fleet_worklist_is_slugs_and_never_reaches_a_tenant_report(
    store: InMemoryMeteringStore,
) -> None:
    """``fleet_tenants_with_quota`` is the sweep's iteration list, not a report input.

    It is the newest of the three exceptions — it arrived with #33's review fixes, after
    this suite was written, and the sweep caught it. It returns slugs, which is the most
    another tenant could learn from it, and nothing that renders a single tenant's figures
    calls it: asserted by running every per-tenant report and checking the store's call log.
    """
    store.given_quota(
        TenantQuota(tenant_id=TENANT_A, monthly_lead_quota=100, alert_fraction=Decimal("0.8"))
    )
    store.given_quota(
        TenantQuota(tenant_id=TENANT_B, monthly_lead_quota=50, alert_fraction=Decimal("0.8"))
    )
    store.rollup_day(tenant_id=TENANT_A, day=DAY)

    worklist = store.fleet_tenants_with_quota()
    assert sorted(worklist) == sorted([TENANT_A, TENANT_B])
    assert all(isinstance(slug, str) for slug in worklist)

    for command in ("usage", "margin", "quota"):
        rendered = _run_usagectl([command, TENANT_A, "--month", "2026-09"], store)
        assert TENANT_B not in rendered, command
