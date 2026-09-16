"""``python -m leadquali.usagectl``, driven end to end with a fake store.

The real argument parsing, the real service and the real output formatting run in every
test here; only Postgres is replaced. That is deliberate — the things most likely to break
a billing command are a period that parses into the wrong month, a report that quietly
prints a partial day as a final one, and an exit code a cron reads the wrong way round,
and none of those is visible from a unit test of the service alone.

Money in ``--json`` is asserted as a **string** on purpose: a JSON number is a double by
the time anything else has parsed it, and a billing figure that has been through binary
floating point can no longer be reconciled against one that has not.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from leadquali import usagectl
from leadquali.app.metering import MeteringService, TenantQuota
from leadquali.config import Settings
from tests.fakes import FakeClock, InMemoryMeteringStore, StaticRevenue

TENANT = "acme"
OTHER = "globex"
NOW = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
DAY = date(2026, 9, 3)

INVOICE_CSV = """date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd
2026-09-03,3000,300,0,0,0.030000
2026-09-04,3000,300,0,0,0.030000
"""


class Run:
    """One command's result: exit code, stdout and stderr, kept apart."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json(self) -> Any:
        """``--json`` output, parsed. Fails loudly if anything else got onto stdout."""
        return json.loads(self.out)


def seed(store: InMemoryMeteringStore, *, tenant: str = TENANT, day: date = DAY) -> None:
    """Five submissions, three assessed, one of those a billed failure."""
    for hour in range(5):
        store.add_lead(
            tenant_id=tenant, received_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
        )
    store.add_assessment(
        tenant_id=tenant,
        created_at=datetime(day.year, day.month, day.day, 1, tzinfo=UTC),
        status="failed",
        input_tokens=1_000,
        cost_usd=Decimal("0.005000"),
    )
    for hour in (2, 3):
        store.add_assessment(
            tenant_id=tenant,
            created_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
            input_tokens=1_000,
            output_tokens=150,
            cost_usd=Decimal("0.012500"),
        )


def run(
    *argv: str,
    store: InMemoryMeteringStore | None = None,
    now: datetime = NOW,
    revenue: StaticRevenue | None = None,
) -> tuple[Run, InMemoryMeteringStore]:
    """Run one command against a fake store and capture both streams."""
    backing = store if store is not None else InMemoryMeteringStore()
    out, err = io.StringIO(), io.StringIO()

    def factory(settings: Settings) -> MeteringService:
        del settings
        return MeteringService(
            store=backing,
            clock=FakeClock(start=now),
            revenue=revenue if revenue is not None else StaticRevenue(),
        )

    code = usagectl.main(
        list(argv),
        service_factory=factory,
        settings=Settings(),
        stdout=out,
        stderr=err,
    )
    return Run(code, out.getvalue(), err.getvalue()), backing


# -------------------------------------------------------------------------- the shell


def test_no_command_prints_help_and_is_a_usage_error() -> None:
    result, _ = run()
    assert result.code == usagectl.EXIT_INPUT_ERROR
    assert "usage" in result.out.lower()


def test_the_parser_declares_every_command() -> None:
    """Pinned so that a command cannot be removed without a test saying so; the runbook in
    docs/metering-and-billing.md names all five."""
    help_text = usagectl.build_parser().format_help()
    for command in ("rollup", "usage", "quota", "set-quota", "margin", "reconcile"):
        assert command in help_text


# ------------------------------------------------------------------------------ rollup


def test_rollup_recomputes_every_closed_day_of_the_period() -> None:
    result, store = run("rollup", TENANT, "--month", "2026-09")
    assert result.code == 0
    assert len(store.rollups) == 30
    assert store.rollups[0] == (TENANT, date(2026, 9, 1))


def test_the_daily_default_is_a_trailing_window_ending_yesterday() -> None:
    """Not the current month. A month-shaped default is wrong at a month boundary — see
    the test below — and a trailing window is also what repairs a run of missed days."""
    result, store = run("rollup", TENANT, now=datetime(2026, 9, 20, 2, 0, tzinfo=UTC))
    assert result.code == 0
    days = [day for _, day in store.rollups]
    assert days[-1] == date(2026, 9, 19), "the window must stop at yesterday"
    assert days[0] == date(2026, 9, 19) - timedelta(days=usagectl.ROLLUP_LOOKBACK_DAYS - 1)
    assert len(days) == usagectl.ROLLUP_LOOKBACK_DAYS


def test_the_daily_default_rolls_up_the_last_day_of_the_previous_month() -> None:
    """The bug this default exists to prevent, stated as the case that broke.

    With a current-month default, the run on 1 October finds no closed day in October and
    does nothing, and every later run of October reaches back only to 1 October — so 30
    September is never in any period, gets no ``usage_daily`` row, and every September
    invoice silently undercharges by a day. Roughly 3.3%, exit code 0, no error anywhere.
    """
    result, store = run("rollup", TENANT, now=datetime(2026, 10, 1, 2, 0, tzinfo=UTC))

    assert result.code == 0
    days = [day for _, day in store.rollups]
    assert date(2026, 9, 30) in days
    assert days[-1] == date(2026, 9, 30)


def test_the_daily_default_repairs_a_week_of_missed_runs() -> None:
    """The window is longer than a month on purpose: the rollup is an idempotent
    replacement, so re-doing a day is free, and an outage of the job heals itself on the
    next run rather than needing somebody to notice and pick a range by hand."""
    _, store = run("rollup", TENANT, now=datetime(2026, 10, 8, 2, 0, tzinfo=UTC))
    days = {day for _, day in store.rollups}
    assert {date(2026, 10, day) for day in range(1, 8)} <= days
    assert date(2026, 9, 30) in days


def test_a_month_of_daily_runs_leaves_no_day_unmetered() -> None:
    """The reviewer's simulation, kept as a test: run the prescribed cron across a month
    boundary and assert that every day of September ends up rolled up."""
    store = InMemoryMeteringStore()
    for day in range(1, 4):
        run("rollup", TENANT, store=store, now=datetime(2026, 10, day, 2, 0, tzinfo=UTC))
    rolled = {rolled_day for _, rolled_day in store.rollups}
    september = {date(2026, 9, day) for day in range(1, 31)}
    assert september <= rolled, f"unmetered: {sorted(september - rolled)}"


def test_rollup_can_be_told_to_include_the_day_in_progress() -> None:
    result, store = run(
        "rollup", TENANT, "--include-today", now=datetime(2026, 9, 4, 2, 0, tzinfo=UTC)
    )
    assert result.code == 0
    assert (TENANT, date(2026, 9, 4)) in store.rollups
    assert "(partial)" in result.out


def test_rollup_with_nothing_closed_yet_says_so_and_succeeds() -> None:
    """Exit 0: "no day of this range is over" is not a failure, and a job that alerted on
    it would be turned off. Only reachable with an explicit range now — the daily default
    is a trailing window and always has closed days in it."""
    result, store = run(
        "rollup",
        TENANT,
        "--from",
        "2026-09-04",
        "--to",
        "2026-09-04",
        now=datetime(2026, 9, 4, 1, 0, tzinfo=UTC),
    )
    assert result.code == 0
    assert store.rollups == []
    assert "--include-today" in result.err
    assert result.out == ""


def test_rollup_is_idempotent_from_the_command_line() -> None:
    store = InMemoryMeteringStore()
    seed(store)
    first, _ = run("rollup", TENANT, "--from", "2026-09-03", "--to", "2026-09-03", store=store)
    second, _ = run("rollup", TENANT, "--from", "2026-09-03", "--to", "2026-09-03", store=store)
    assert first.out == second.out
    assert len(store.rows) == 1


# ------------------------------------------------------------------------------- usage


def test_usage_reports_the_three_counts_separately() -> None:
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("usage", TENANT, "--month", "2026-09", store=store)

    assert result.code == 0
    assert "leads ingested:    5" in result.out
    assert "leads assessed:    3" in result.out
    assert "leads billable:    3" in result.out
    assert "pre-filtered:    2" in result.out


def test_usage_json_carries_money_as_strings() -> None:
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("usage", TENANT, "--month", "2026-09", "--json", store=store)

    document = result.json()
    assert document["tenant_id"] == TENANT
    assert document["period"] == {"start": "2026-09-01", "end": "2026-09-30", "days": 30}
    assert document["leads_billable"] == 3
    assert document["cost_usd"] == "0.030000"
    assert isinstance(document["cost_usd"], str)
    assert document["partial"] is False


def test_usage_marks_a_period_that_includes_today() -> None:
    store = InMemoryMeteringStore()
    seed(store, day=date(2026, 9, 4))
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    run("rollup", TENANT, "--include-today", store=store, now=now)

    result, _ = run("usage", TENANT, "--include-today", store=store, now=now)

    assert "(partial)" in result.out


def test_usage_daily_breaks_the_period_down() -> None:
    store = InMemoryMeteringStore()
    seed(store, day=date(2026, 9, 3))
    seed(store, day=date(2026, 9, 4))
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("usage", TENANT, "--month", "2026-09", "--daily", store=store)

    assert "2026-09-03" in result.out
    assert "2026-09-04" in result.out


def test_usage_daily_json_is_a_list_with_one_entry_per_rolled_up_day() -> None:
    """A rollup writes a row for every day it was asked about, including the quiet ones —
    "rolled up, nothing happened" is a different fact from "never rolled up", and only the
    first of those can be billed from."""
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)
    result, _ = run("usage", TENANT, "--month", "2026-09", "--daily", "--json", store=store)
    document = result.json()
    assert isinstance(document, list)
    assert len(document) == 30
    busy = next(row for row in document if row["period"]["start"] == "2026-09-03")
    assert busy["leads_billable"] == 3
    assert document[0]["leads_billable"] == 0


def test_usage_daily_drops_the_day_in_progress_unless_asked() -> None:
    """``--include-today`` used to be accepted and silently ignored here."""
    store = InMemoryMeteringStore()
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    for day in (date(2026, 9, 4), date(2026, 9, 5)):
        seed(store, day=day)
    run("rollup", TENANT, "--include-today", store=store, now=now)

    closed = run("usage", TENANT, "--daily", "--json", store=store, now=now)[0].json()
    live = run("usage", TENANT, "--daily", "--include-today", "--json", store=store, now=now)[
        0
    ].json()

    assert [row["period"]["start"] for row in closed][-1] == "2026-09-04"
    assert [row["period"]["start"] for row in live][-1] == "2026-09-05"
    assert live[-1]["partial"] is True, "a day that can still grow must say so"
    assert all(row["partial"] is False for row in closed)


def test_usage_for_a_month_with_no_rollup_rows_is_zero_and_not_an_error() -> None:
    result, _ = run("usage", TENANT, "--month", "2026-09", "--json")
    assert result.code == 0
    assert result.json()["leads_billable"] == 0


def test_one_tenants_usage_never_includes_anothers() -> None:
    store = InMemoryMeteringStore()
    seed(store, tenant=TENANT)
    seed(store, tenant=OTHER)
    run("rollup", TENANT, "--month", "2026-09", store=store)
    run("rollup", OTHER, "--month", "2026-09", store=store)

    result, _ = run("usage", TENANT, "--month", "2026-09", "--json", store=store)

    assert result.json()["leads_billable"] == 3


# ------------------------------------------------------------------------------- quota


def quota_store(*, used: int, quota: int | None) -> InMemoryMeteringStore:
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=quota))
    for _ in range(used):
        store.add_assessment(
            tenant_id=TENANT, created_at=datetime(2026, 9, 3, 12, tzinfo=UTC), input_tokens=100
        )
    return store


def test_quota_reports_a_tenant_inside_its_plan() -> None:
    store = quota_store(used=10, quota=100)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("quota", TENANT, "--month", "2026-09", store=store)

    assert result.code == 0
    assert "status:   ok" in result.out
    assert result.err == ""


def test_quota_says_plainly_that_nothing_is_blocked() -> None:
    """The one sentence that has to be on the screen when somebody is deciding what to do
    about a customer who is over: invariant 3 is not negotiable."""
    store = quota_store(used=150, quota=100)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("quota", TENANT, "--month", "2026-09", store=store)

    assert result.code == 0, "being over a plan is not a command failure"
    assert "status:   exceeded" in result.out
    assert "nothing has been blocked" in result.err
    assert "still qualified" in result.err


def test_quota_json_says_it_is_not_enforced() -> None:
    store = quota_store(used=150, quota=100)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    document = run("quota", TENANT, "--month", "2026-09", "--json", store=store)[0].json()

    assert document["level"] == "exceeded"
    assert document["enforced"] is False
    assert document["used"] == 150
    assert document["remaining"] == 0
    assert document["fraction"] == "1.5"


def test_quota_renders_an_unlimited_plan_without_a_division() -> None:
    store = quota_store(used=10, quota=None)
    run("rollup", TENANT, "--month", "2026-09", store=store)
    result, _ = run("quota", TENANT, "--month", "2026-09", store=store)
    assert "quota:    unlimited" in result.out


def test_quota_for_an_unknown_tenant_is_a_failure_with_a_reason() -> None:
    result, _ = run("quota", "ghost", "--month", "2026-09")
    assert result.code == usagectl.EXIT_FAILED
    assert "no tenant" in result.err
    assert result.out == ""


def test_quota_sweeps_every_tenant_with_a_plan() -> None:
    """The command to schedule. A per-tenant run reports one customer, which is no use as
    a standing alert across a growing customer list."""
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=100))
    store.given_quota(TenantQuota(tenant_id=OTHER, monthly_lead_quota=1))
    for index in range(5):
        store.add_assessment(
            tenant_id=OTHER,
            created_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
            lead_id=f"o-{index}",
            input_tokens=100,
        )
    run("rollup", OTHER, "--month", "2026-09", store=store)

    result, _ = run("quota", "--all", "--month", "2026-09", store=store)

    assert result.code == 0
    assert result.out.index(OTHER) < result.out.index(TENANT), "worst first"
    assert "exceeded" in result.out
    assert "nothing has been blocked" in result.err


def test_quota_all_json_is_a_list() -> None:
    store = InMemoryMeteringStore()
    store.given_quota(TenantQuota(tenant_id=TENANT, monthly_lead_quota=100))
    result, _ = run("quota", "--all", "--month", "2026-09", "--json", store=store)
    document = result.json()
    assert isinstance(document, list)
    assert document[0]["tenant_id"] == TENANT


def test_quota_needs_a_tenant_or_all_and_not_both() -> None:
    store = quota_store(used=0, quota=100)
    assert run("quota", store=store)[0].code == usagectl.EXIT_INPUT_ERROR
    both = run("quota", TENANT, "--all", store=store)[0]
    assert both.code == usagectl.EXIT_INPUT_ERROR
    assert "not both, not neither" in both.err


def test_a_sweep_with_no_tenant_on_a_plan_says_so() -> None:
    result, _ = run("quota", "--all", "--month", "2026-09")
    assert result.code == 0
    assert "no tenant has an allowance" in result.out


# -------------------------------------------------------------------------- set-quota


def test_set_quota_puts_a_tenant_on_a_plan() -> None:
    store = quota_store(used=90, quota=None)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("set-quota", TENANT, "--quota", "100", store=store)

    assert result.code == 0
    assert "100 billable leads/month" in result.out
    assert "alert at 80%" in result.out
    assert run("quota", TENANT, "--month", "2026-09", store=store)[0].out.count("warning") == 1


def test_set_quota_says_that_it_enforces_nothing() -> None:
    """The sentence has to be on the screen of the person configuring it, not only in the
    documentation they did not open."""
    store = quota_store(used=0, quota=None)
    result, _ = run("set-quota", TENANT, "--quota", "100", store=store)
    assert "no lead is ever refused" in result.err


def test_set_quota_can_take_a_tenant_off_a_plan() -> None:
    store = quota_store(used=500, quota=100)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("set-quota", TENANT, "--unlimited", store=store)

    assert result.code == 0
    assert "unlimited" in result.out


def test_set_quota_takes_a_custom_alert_fraction() -> None:
    store = quota_store(used=0, quota=None)
    result, _ = run("set-quota", TENANT, "--quota", "100", "--alert-fraction", "0.5", store=store)
    assert "alert at 50%" in result.out


def test_set_quota_refuses_a_contradiction() -> None:
    store = quota_store(used=0, quota=None)
    result, _ = run("set-quota", TENANT, "--quota", "100", "--unlimited", store=store)
    assert result.code == usagectl.EXIT_INPUT_ERROR
    assert "not both" in result.err


def test_set_quota_refuses_a_plan_of_zero_with_the_alternative_named() -> None:
    store = quota_store(used=0, quota=None)
    result, _ = run("set-quota", TENANT, "--quota", "0", store=store)
    assert result.code == usagectl.EXIT_FAILED
    assert "suspend" in result.err


# ------------------------------------------------------------------------------ margin


def test_margin_prints_unknown_revenue_as_unknown() -> None:
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("margin", TENANT, "--month", "2026-09", store=store)

    assert result.code == 0
    assert "revenue:           unknown" in result.out
    assert "margin:            unknown" in result.out
    assert "margin %:          unknown" in result.out


def test_margin_labels_the_infrastructure_figure_as_an_allocation() -> None:
    """Wherever this number is shown, the sentence has to be shown with it."""
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("margin", TENANT, "--month", "2026-09", store=store)

    assert "(allocated, not measured)" in result.out
    assert "allocated pro rata by billable leads" in result.err


def test_margin_computes_once_revenue_is_known() -> None:
    store = InMemoryMeteringStore()
    seed(store)
    run("rollup", TENANT, "--month", "2026-09", store=store)

    document = run(
        "margin",
        TENANT,
        "--month",
        "2026-09",
        "--json",
        store=store,
        revenue=StaticRevenue({TENANT: Decimal("200.00")}),
    )[0].json()

    assert document["revenue_usd"] == "200.00"
    assert document["margin_usd"] is not None
    assert Decimal(document["margin_usd"]) == Decimal("200.00") - Decimal(document["cost_usd"])
    assert document["fleet_billable_leads"] == 3


# ------------------------------------------------------------------------- reconcile


def write_invoice(tmp_path: Path, text: str = INVOICE_CSV) -> Path:
    path = tmp_path / "anthropic.csv"
    path.write_text(text, encoding="utf-8")
    return path


def reconcilable_store() -> InMemoryMeteringStore:
    """Two days at $0.030 each, matching :data:`INVOICE_CSV` exactly."""
    store = InMemoryMeteringStore()
    for day in (date(2026, 9, 3), date(2026, 9, 4)):
        for hour in (1, 2):
            store.add_assessment(
                tenant_id=TENANT,
                created_at=datetime(2026, 9, day.day, hour, tzinfo=UTC),
                input_tokens=1_500,
                output_tokens=150,
                cost_usd=Decimal("0.015000"),
            )
    return store


def test_reconcile_exits_zero_when_the_totals_agree(tmp_path: Path) -> None:
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("reconcile", str(write_invoice(tmp_path)), "--month", "2026-09", store=store)

    assert result.code == 0
    assert "within tolerance" in result.out
    assert result.err == ""


def test_reconcile_exits_one_when_the_totals_disagree(tmp_path: Path) -> None:
    """The exit code is the finding, so this can run from a cron and be noticed."""
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)
    invoice = write_invoice(
        tmp_path,
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,3000,300,0,0,0.100000\n"
        "2026-09-04,3000,300,0,0,0.100000\n",
    )

    result, _ = run("reconcile", str(invoice), "--month", "2026-09", store=store)

    assert result.code == usagectl.EXIT_OUT_OF_TOLERANCE
    assert "OUTSIDE tolerance" in result.out
    assert "Do not bill from these figures" in result.err


def test_reconcile_sums_every_tenant_because_that_is_what_anthropic_bills(
    tmp_path: Path,
) -> None:
    """Reconciling one tenant's share against the whole workspace invoice would fail every
    month by construction."""
    store = reconcilable_store()
    store.add_assessment(
        tenant_id=OTHER,
        created_at=datetime(2026, 9, 3, 5, tzinfo=UTC),
        input_tokens=1_000,
        cost_usd=Decimal("0.010000"),
    )
    run("rollup", TENANT, "--month", "2026-09", store=store)
    run("rollup", OTHER, "--month", "2026-09", store=store)
    invoice = write_invoice(
        tmp_path,
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,4000,300,0,0,0.040000\n"
        "2026-09-04,3000,300,0,0,0.030000\n",
    )

    result, _ = run("reconcile", str(invoice), "--month", "2026-09", "--json", store=store)

    assert result.code == 0
    assert result.json()["ours_usd"] == "0.070000"


def test_reconcile_shows_a_day_we_have_no_data_for(tmp_path: Path) -> None:
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)
    invoice = write_invoice(
        tmp_path,
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,3000,300,0,0,0.030000\n"
        "2026-09-04,3000,300,0,0,0.030000\n"
        "2026-09-20,9000,900,0,0,0.090000\n",
    )

    result, _ = run("reconcile", str(invoice), "--month", "2026-09", "--json", store=store)

    days = {row["date"]: row for row in result.json()["days"]}
    assert days["2026-09-20"]["ours_usd"] == "0"
    assert result.code == usagectl.EXIT_OUT_OF_TOLERANCE


def test_reconcile_accepts_a_wider_tolerance_for_one_run(tmp_path: Path) -> None:
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)
    invoice = write_invoice(
        tmp_path,
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,3000,300,0,0,0.033000\n"
        "2026-09-04,3000,300,0,0,0.030000\n",
    )

    tight, _ = run("reconcile", str(invoice), "--month", "2026-09", store=store)
    loose, _ = run(
        "reconcile", str(invoice), "--month", "2026-09", "--tolerance", "0.10", store=store
    )

    assert tight.code == usagectl.EXIT_OUT_OF_TOLERANCE
    assert loose.code == 0


def test_reconcile_calls_out_a_day_that_drifts_far_more_than_the_month(
    tmp_path: Path,
) -> None:
    """The tolerance is measured on the total, so a systematic day-shift — every day wrong
    by a day's spend — cancels to zero across the month and reconciles cleanly. This is
    the only thing that would surface it."""
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)
    invoice = write_invoice(
        tmp_path,
        "date,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,cost_usd\n"
        "2026-09-03,3000,300,0,0,0.060000\n"
        "2026-09-04,3000,300,0,0,0.000001\n",
    )

    result, _ = run("reconcile", str(invoice), "--month", "2026-09", store=store)

    assert "2026-09-03 is out by" in result.err
    assert "2026-09-04 is out by" in result.err


def test_a_quiet_month_that_reconciles_gets_no_day_notes(tmp_path: Path) -> None:
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)
    result, _ = run("reconcile", str(write_invoice(tmp_path)), "--month", "2026-09", store=store)
    assert "is out by" not in result.err


@pytest.mark.parametrize(
    "argv",
    [
        ("reconcile", "--tolerance", "abc"),
        ("set-quota", "--quota", "5", "--alert-fraction", "high"),
    ],
)
def test_a_decimal_argument_that_is_not_a_number_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    """``type=Decimal`` looks like it works and does not.

    A bad value raises ``decimal.InvalidOperation``, which is an ``ArithmeticError`` and
    **not** a ``ValueError``, so argparse does not catch it: the command dies with a
    traceback instead of the usage message every other bad argument gets. Before the fix
    this test failed with ``InvalidOperation`` rather than ``SystemExit``.
    """
    command, *rest = argv
    positional = str(write_invoice(tmp_path)) if command == "reconcile" else TENANT
    with pytest.raises(SystemExit) as caught:
        run(command, positional, *rest)
    assert caught.value.code == usagectl.EXIT_INPUT_ERROR
    assert "not a number" in capsys.readouterr().err


def test_reconcile_refuses_a_csv_with_the_wrong_columns(tmp_path: Path) -> None:
    invoice = write_invoice(tmp_path, "day,model,spend\n2026-09-03,claude-opus-5,1.00\n")
    result, _ = run("reconcile", str(invoice), "--month", "2026-09")
    assert result.code == usagectl.EXIT_FAILED
    assert "input_tokens" in result.err
    assert "model" in result.err


def test_reconcile_names_a_missing_file(tmp_path: Path) -> None:
    result, _ = run("reconcile", str(tmp_path / "nope.csv"), "--month", "2026-09")
    assert result.code == usagectl.EXIT_INPUT_ERROR
    assert "no such file" in result.err


def test_reconcile_reads_a_csv_with_a_byte_order_mark(tmp_path: Path) -> None:
    """Excel writes one, and an operator who opened the export to look at it will save one
    back. Refusing that would be a support ticket a month."""
    path = tmp_path / "bom.csv"
    path.write_text(INVOICE_CSV, encoding="utf-8-sig")
    store = reconcilable_store()
    run("rollup", TENANT, "--month", "2026-09", store=store)

    result, _ = run("reconcile", str(path), "--month", "2026-09", store=store)

    assert result.code == 0


# ------------------------------------------------------------------------------ periods


def test_a_period_defaults_to_the_current_month_in_utc() -> None:
    """From the service's clock, not the host's local date: a command that defaulted from
    the host would ask for a month the service then clipped differently."""
    result, _ = run("usage", TENANT, "--json", now=datetime(2026, 9, 20, 23, 0, tzinfo=UTC))
    assert result.json()["period"]["start"] == "2026-09-01"


def test_an_explicit_range_is_inclusive_at_both_ends() -> None:
    result, _ = run("usage", TENANT, "--from", "2026-09-03", "--to", "2026-09-05", "--json")
    assert result.json()["period"] == {"start": "2026-09-03", "end": "2026-09-05", "days": 3}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("--month", "2026-13"), "month"),
        (("--month", "septober"), "YYYY-MM"),
        (("--from", "2026-09-03"), "--from and --to go together"),
        (("--to", "2026-09-03"), "--from and --to go together"),
        (("--from", "the third", "--to", "2026-09-05"), "YYYY-MM-DD"),
        (("--from", "2026-09-05", "--to", "2026-09-01"), "is after"),
        (("--month", "2026-09", "--from", "2026-09-01", "--to", "2026-09-02"), "not both"),
    ],
)
def test_a_period_that_does_not_parse_is_a_usage_error(
    argv: tuple[str, ...], expected: str
) -> None:
    result, _ = run("usage", TENANT, *argv)
    assert result.code == usagectl.EXIT_INPUT_ERROR
    assert expected in result.err
    assert result.out == ""


# ------------------------------------------------------------------------------ wiring


def test_a_missing_database_url_is_reported_not_raised() -> None:
    out, err = io.StringIO(), io.StringIO()

    def exploding(settings: Settings) -> MeteringService:
        del settings
        raise RuntimeError("DATABASE_URL is not set")

    code = usagectl.main(["usage", TENANT], service_factory=exploding, stdout=out, stderr=err)

    assert code == usagectl.EXIT_INPUT_ERROR
    assert "DATABASE_URL" in err.getvalue()


def test_importing_the_cli_pulls_in_no_database_machinery() -> None:
    """``import leadquali.usagectl`` happens to print a help string; it must not drag in
    SQLAlchemy's engine machinery to do it. The real wiring is inside the factory."""
    import ast

    source = Path(usagectl.__file__).read_text(encoding="utf-8")
    module_scope = {
        node.module
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    leaked = {name for name in module_scope if name.startswith("leadquali.adapters")}
    assert not leaked, f"usagectl imports {sorted(leaked)} at module scope"
