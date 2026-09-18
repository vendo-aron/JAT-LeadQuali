"""The scheduled billing jobs, driven with an injected service and no AWS.

The handlers are thin on purpose, so what is worth testing here is thin too — and it is
exactly the part a Lambda makes hard to see: which day a run bills, what it counts, and
whether a failure is loud. A job that returns a tidy summary having silently done nothing
is the worst possible failure mode for billing, because nothing is alarmed and the money is
simply missing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from leadquali.adapters.revenue_none import UnknownRevenue
from leadquali.api.billing_jobs import (
    drain_events,
    report_usage,
    requested_day,
    sweep_dunning,
)
from leadquali.app.billing import DUNNING_GRACE, BillingService, UsageReportOutcome
from leadquali.app.metering import MeteringService
from leadquali.app.tenants import TenantStatus
from tests.fakes import (
    FakeClock,
    InMemoryBillingStore,
    InMemoryMeteringStore,
    RecordingBilling,
    stripe_event,
)

TENANT = "acme-demo"
CUSTOMER = "cus_acme"
NOW = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
YESTERDAY = date(2026, 9, 7)


class Fixture:
    """A service over in-memory doubles, plus the doubles."""

    def __init__(self) -> None:
        self.clock = FakeClock(NOW, step_ms=0)
        self.store = InMemoryBillingStore()
        self.store.given_tenant(TENANT, stripe_customer_id=CUSTOMER)
        self.billing = RecordingBilling()
        self.metering_store = InMemoryMeteringStore()
        self.service = BillingService(
            store=self.store,
            billing=self.billing,
            metering=MeteringService(
                store=self.metering_store, clock=self.clock, revenue=UnknownRevenue()
            ),
            clock=self.clock,
        )

    def given_usage(
        self, *, tenant_id: str = TENANT, leads: int = 3, day: date = YESTERDAY
    ) -> None:
        moment = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9)
        for index in range(leads):
            self.metering_store.add_lead(tenant_id=tenant_id, received_at=moment)
            self.metering_store.add_assessment(
                tenant_id=tenant_id,
                created_at=moment,
                lead_id=f"{tenant_id}-{day}-{index}",
                input_tokens=800,
                cost_usd=Decimal("0.016"),
            )
        self.metering_store.rollup_day(tenant_id=tenant_id, day=day)


@pytest.fixture
def fixture() -> Fixture:
    return Fixture()


# ------------------------------------------------------------------------- the drain


def test_the_drain_counts_what_it_did(fixture: Fixture) -> None:
    for index in range(2):
        fixture.service.receive_event(
            event_id=f"evt_{index}",
            event_type="invoice.payment_failed",
            payload=stripe_event(f"evt_{index}", "invoice.payment_failed", customer=CUSTOMER),
        )
    assert drain_events(fixture.service) == {
        "attempted": 2,
        "processed": 2,
        "retrying": 0,
        "failed": 0,
    }


def test_the_drain_reports_a_retrying_event_separately_from_a_failed_one(
    fixture: Fixture,
) -> None:
    """A summary that collapsed the two would make a transient blip and a permanently
    stuck event look the same, and only one of them needs a person."""
    fixture.store.fail_on_status_write = 1
    fixture.service.receive_event(
        event_id="evt_1",
        event_type="customer.subscription.deleted",
        payload=stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER),
    )
    assert drain_events(fixture.service)["retrying"] == 1
    assert drain_events(fixture.service)["processed"] == 1


def test_an_empty_drain_is_all_zeroes_rather_than_an_error(fixture: Fixture) -> None:
    assert drain_events(fixture.service)["attempted"] == 0


# ------------------------------------------------------------------- the usage run


def test_a_run_with_no_day_named_bills_yesterday(fixture: Fixture) -> None:
    """Yesterday, because it is the most recent day that is over. Today would under-report
    now and be re-reported tomorrow, which over-reports — and the issue is explicit that
    over-reporting is the worse of the two."""
    assert requested_day({}, service=fixture.service) == YESTERDAY
    assert requested_day(None, service=fixture.service) == YESTERDAY


def test_the_day_comes_from_the_metering_clock_not_the_hosts(fixture: Fixture) -> None:
    """One answer to "what day is it?" across the rollup, the quota check and the billing
    run. A host in a non-UTC timezone must not bill a different day than the rollup
    counted."""
    fixture.clock.advance(timedelta(days=40))
    assert requested_day({}, service=fixture.service) == date(2026, 10, 17)


def test_an_operator_can_name_a_day_to_re_run(fixture: Fixture) -> None:
    assert requested_day({"usage_date": "2026-09-05"}, service=fixture.service) == date(2026, 9, 5)


@pytest.mark.parametrize("value", [5, ["2026-09-05"], {"day": 1}])
def test_a_usage_date_that_is_not_a_string_is_an_error(fixture: Fixture, value: object) -> None:
    """Silently billing yesterday when somebody asked for last Tuesday is the kind of
    helpfulness that ends in a support ticket."""
    with pytest.raises(ValueError, match="usage_date"):
        requested_day({"usage_date": value}, service=fixture.service)


def test_the_usage_run_reports_every_billable_tenant_and_counts_the_outcomes(
    fixture: Fixture,
) -> None:
    fixture.store.given_tenant("second", stripe_customer_id="cus_second")
    fixture.given_usage(leads=3)
    fixture.given_usage(tenant_id="second", leads=1)

    summary = report_usage(fixture.service, usage_date=YESTERDAY)

    assert summary["tenants"] == 2
    assert summary[UsageReportOutcome.REPORTED.value] == 2
    assert {call.report.quantity for call in fixture.billing.reports} == {3, 1}


def test_a_second_run_of_the_same_day_sends_nothing(fixture: Fixture) -> None:
    """The property that keeps an overlapping schedule, a retry and an operator's manual
    re-run from all billing the same day again."""
    fixture.given_usage(leads=4)
    report_usage(fixture.service, usage_date=YESTERDAY)
    summary = report_usage(fixture.service, usage_date=YESTERDAY)

    assert summary[UsageReportOutcome.ALREADY_REPORTED.value] == 1
    assert len(fixture.billing.reports) == 1


def test_one_tenants_failure_does_not_stop_the_rest_of_the_fleet(fixture: Fixture) -> None:
    """A job that aborted on the first bad tenant would silently stop billing every
    customer whose slug sorts after it."""
    fixture.store.given_tenant("second", stripe_customer_id="cus_second")
    fixture.given_usage(leads=2)
    fixture.given_usage(tenant_id="second", leads=5)
    fixture.billing.fail_times = 1

    summary = report_usage(fixture.service, usage_date=YESTERDAY)

    assert summary["tenants"] == 1, "one tenant reported; the other raised and was logged"
    assert len(fixture.billing.reports) == 1
    # The failed tenant's day was not recorded, so tomorrow's run picks it up again.
    assert len(fixture.store.usage_reports) == 1


def test_the_usage_run_never_bills_an_open_day(fixture: Fixture) -> None:
    today = fixture.service.today()
    fixture.given_usage(day=today, leads=9)
    summary = report_usage(fixture.service, usage_date=today)
    assert summary[UsageReportOutcome.DAY_NOT_CLOSED.value] == 1
    assert fixture.billing.reports == []


# ------------------------------------------------------------------------- the sweep


def test_the_sweep_suspends_only_expired_grace_periods(fixture: Fixture) -> None:
    fixture.service.receive_event(
        event_id="evt_1",
        event_type="invoice.payment_failed",
        payload=stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER),
    )
    fixture.service.process_pending()

    assert sweep_dunning(fixture.service) == {"suspended": 0}
    assert fixture.store.tenants[TENANT].status is TenantStatus.ACTIVE

    fixture.clock.advance(DUNNING_GRACE + timedelta(minutes=1))
    assert sweep_dunning(fixture.service) == {"suspended": 1}
    assert fixture.store.tenants[TENANT].status is TenantStatus.SUSPENDED


def test_running_the_sweep_twice_suspends_nobody_twice(fixture: Fixture) -> None:
    fixture.service.receive_event(
        event_id="evt_1",
        event_type="invoice.payment_failed",
        payload=stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER),
    )
    fixture.service.process_pending()
    fixture.clock.advance(DUNNING_GRACE + timedelta(minutes=1))
    assert sweep_dunning(fixture.service) == {"suspended": 1}
    assert sweep_dunning(fixture.service) == {"suspended": 0}
