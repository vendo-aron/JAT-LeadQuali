"""The billing domain: webhook idempotency, the lifecycle, dunning, and usage reporting.

Not one test in this file needs Postgres, Docker or a Stripe key. That is deliberate and
it is the lesson of the #31 and #33 reviews: in both, a money- or security-critical
property was asserted only by a Docker-gated ``integration`` test, so mutations that broke
it left the whole suite green. Everything here — that a replayed event changes nothing,
that the quantity we send is the rollup's ``leads_billable`` and not some neighbouring
column, that a failed payment does not suspend anybody for seven days — runs in the
default suite on a laptop with nothing installed.

``tests/integration/test_store_billing.py`` proves the SQL agrees with the in-memory
double. It is the *second* test of each property, never the only one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from leadquali.app.billing import (
    DUNNING_GRACE,
    DUNNING_GRACE_DAYS,
    MAX_EVENT_ATTEMPTS,
    METER_EVENT_MAX_AGE_DAYS,
    BillingService,
    EventStatus,
    SubscriptionState,
    UsageReport,
    UsageReportOutcome,
    usage_external_id,
)
from leadquali.app.metering import BillingPeriod, MeteringService
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
SUBSCRIPTION = "sub_acme"
TODAY = date(2026, 9, 8)
NOW = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)
YESTERDAY = date(2026, 9, 7)


@pytest.fixture
def clock() -> FakeClock:
    """A stopped clock. Billing arithmetic is in days, and a clock that drifts by ten
    milliseconds per read makes ``dunning_until == now + DUNNING_GRACE`` unassertable —
    which would push every dunning test into an inequality and hide an off-by-a-day."""
    return FakeClock(NOW, step_ms=0)


@pytest.fixture
def store() -> InMemoryBillingStore:
    store = InMemoryBillingStore()
    store.given_tenant(TENANT, stripe_customer_id=CUSTOMER, stripe_subscription_id=SUBSCRIPTION)
    return store


@pytest.fixture
def metering_store() -> InMemoryMeteringStore:
    return InMemoryMeteringStore()


@pytest.fixture
def billing() -> RecordingBilling:
    return RecordingBilling()


@pytest.fixture
def service(
    store: InMemoryBillingStore,
    billing: RecordingBilling,
    metering_store: InMemoryMeteringStore,
    clock: FakeClock,
) -> BillingService:
    from leadquali.adapters.revenue_none import UnknownRevenue

    return BillingService(
        store=store,
        billing=billing,
        metering=MeteringService(store=metering_store, clock=clock, revenue=UnknownRevenue()),
        clock=clock,
    )


# ------------------------------------------------------------------ webhook receipt


def test_a_received_event_is_stored_pending_and_nothing_else_happens(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """The route's whole job. Whatever the event says, receiving it must not touch a
    tenant's status: the work happens later, in the drain, where a failure can be retried
    instead of turning into a 500 that makes Stripe resend."""
    event = stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER)
    receipt = service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)

    assert receipt.stored is True
    assert receipt.duplicate is False
    assert [row.status for row in store.events.values()] == [EventStatus.PENDING]
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE


def test_a_replayed_event_is_stored_once_and_has_one_effect(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """The idempotency acceptance criterion. Stripe retries; the second copy must be a
    no-op even before the first has been processed."""
    event = stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER)
    first = service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
    second = service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)

    assert first.stored is True
    assert second.stored is False
    assert second.duplicate is True
    assert len(store.events) == 1
    assert store.inserts == 2, "both attempts reached the store; the store deduplicated"

    service.process_pending()
    service.process_pending()
    assert store.status_writes.count((TENANT, TenantStatus.SUSPENDED)) == 1


def test_a_replay_arriving_while_the_first_copy_is_still_pending_is_a_no_op(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """The window the design brief calls out: the conflict target is the event id, not
    'an event we have already finished with'."""
    event = stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER)
    service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
    assert store.events["evt_1"].status is EventStatus.PENDING

    receipt = service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
    assert receipt.duplicate is True
    assert store.events["evt_1"].status is EventStatus.PENDING
    assert store.events["evt_1"].attempts == 0


def test_the_stored_payload_is_the_verified_body_verbatim(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    event = stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER)
    service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
    assert store.events["evt_1"].payload == event


# --------------------------------------------------------------------- the lifecycle


def _drain(service: BillingService, store: InMemoryBillingStore, event: dict[str, object]) -> None:
    service.receive_event(event_id=str(event["id"]), event_type=str(event["type"]), payload=event)
    service.process_pending()


@pytest.mark.parametrize("state", ["active", "trialing"])
def test_a_live_subscription_activates_the_tenant_and_clears_dunning(
    service: BillingService, store: InMemoryBillingStore, state: str
) -> None:
    store.given_tenant(
        TENANT,
        status=TenantStatus.SUSPENDED,
        stripe_customer_id=CUSTOMER,
        dunning_until=NOW + timedelta(days=2),
    )
    _drain(
        service,
        store,
        stripe_event(
            "evt_1",
            "customer.subscription.updated",
            customer=CUSTOMER,
            subscription=SUBSCRIPTION,
            status=state,
        ),
    )
    tenant = store.tenants[TENANT]
    assert tenant.status is TenantStatus.ACTIVE
    assert tenant.dunning_until is None
    assert tenant.stripe_subscription_id == SUBSCRIPTION


def test_a_subscription_created_event_records_the_subscription_id(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    store.given_tenant(TENANT, stripe_customer_id=CUSTOMER)
    _drain(
        service,
        store,
        stripe_event(
            "evt_1",
            "customer.subscription.created",
            customer=CUSTOMER,
            subscription="sub_new",
            status="active",
        ),
    )
    assert store.tenants[TENANT].stripe_subscription_id == "sub_new"


def test_a_subscription_deleted_event_suspends_the_tenant(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    _drain(
        service,
        store,
        stripe_event(
            "evt_1",
            "customer.subscription.deleted",
            customer=CUSTOMER,
            subscription=SUBSCRIPTION,
            status="canceled",
        ),
    )
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED


def test_a_dead_subscription_status_does_not_activate_anybody(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """``incomplete_expired`` means the first payment never succeeded. Treating every
    ``subscription.updated`` as "active" would give a free account to anyone who started
    a checkout and walked away."""
    store.given_tenant(TENANT, status=TenantStatus.SUSPENDED, stripe_customer_id=CUSTOMER)
    _drain(
        service,
        store,
        stripe_event(
            "evt_1",
            "customer.subscription.updated",
            customer=CUSTOMER,
            subscription=SUBSCRIPTION,
            status="incomplete_expired",
        ),
    )
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED


def test_an_unknown_customer_is_processed_and_left_unattributed(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """Stripe accounts hold customers that are not our tenants. The event is kept — it is
    the only record that it arrived — and no tenant is touched."""
    _drain(
        service,
        store,
        stripe_event("evt_1", "customer.subscription.deleted", customer="cus_stranger"),
    )
    assert store.events["evt_1"].status is EventStatus.PROCESSED
    assert store.events["evt_1"].tenant_id is None
    assert store.status_writes == []


def test_an_event_type_we_do_not_handle_is_marked_processed_not_retried(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """Stripe sends whatever the endpoint subscribes to plus whatever it adds next year.
    Leaving those pending would fill the drain with rows nothing will ever handle."""
    _drain(service, store, stripe_event("evt_1", "charge.succeeded", customer=CUSTOMER))
    assert store.events["evt_1"].status is EventStatus.PROCESSED


# ------------------------------------------------------------------------- dunning


def test_a_failed_payment_starts_a_grace_period_and_does_not_suspend(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """The commercial decision this whole issue turns on: a card that expired on Friday
    must not stop a customer's leads on Friday."""
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    tenant = store.tenants[TENANT]
    assert tenant.status is TenantStatus.ACTIVE
    assert tenant.dunning_until == NOW + DUNNING_GRACE
    assert timedelta(days=DUNNING_GRACE_DAYS) == DUNNING_GRACE
    assert DUNNING_GRACE_DAYS == 7


def test_a_second_failed_payment_does_not_extend_the_grace_period(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    """Stripe's dunning retries an invoice several times. Restarting the clock on each
    retry would make the grace period unbounded, which is the same as never suspending."""
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    first = store.tenants[TENANT].dunning_until
    clock.advance(timedelta(days=3))
    _drain(service, store, stripe_event("evt_2", "invoice.payment_failed", customer=CUSTOMER))
    assert store.tenants[TENANT].dunning_until == first


def test_a_successful_payment_clears_dunning(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    assert store.tenants[TENANT].dunning_until is not None
    _drain(service, store, stripe_event("evt_2", "invoice.payment_succeeded", customer=CUSTOMER))
    assert store.tenants[TENANT].dunning_until is None


def test_suspension_is_reversible_suspend_then_pay_then_active(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    """The acceptance criterion, end to end and offline: a failed payment, a grace period
    that runs out, a sweep that suspends, a payment that succeeds, and a tenant that can
    submit leads again."""
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE

    clock.advance(DUNNING_GRACE + timedelta(seconds=1))
    swept = service.sweep_dunning()
    assert [tenant.tenant_id for tenant in swept] == [TENANT]
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED

    _drain(service, store, stripe_event("evt_2", "invoice.payment_succeeded", customer=CUSTOMER))
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE
    assert store.tenants[TENANT].dunning_until is None


def test_the_sweep_leaves_a_tenant_whose_grace_period_is_still_running(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    clock.advance(DUNNING_GRACE - timedelta(hours=1))
    assert service.sweep_dunning() == []
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE


def test_the_sweep_ignores_a_tenant_with_no_dunning_at_all(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    clock.advance(timedelta(days=90))
    assert service.sweep_dunning() == []
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE


# ------------------------------------------------------------------ failure handling


def test_a_handler_that_raises_leaves_the_row_pending_and_counts_the_attempt(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    store.fail_on_status_write = 1
    _drain(
        service,
        store,
        stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER),
    )
    row = store.events["evt_1"]
    assert row.status is EventStatus.PENDING
    assert row.attempts == 1
    assert row.last_error is not None


def test_the_recorded_error_is_a_class_name_and_a_line_not_a_payload_dump(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """``last_error`` is read by an operator and lives in a table we query. A traceback
    with the event body in it would put a customer's billing details there for ever."""
    store.fail_on_status_write = 1
    _drain(
        service,
        store,
        stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER),
    )
    error = store.events["evt_1"].last_error
    assert error is not None
    assert error.startswith("FakeStoreError")
    assert len(error) <= 200
    assert CUSTOMER not in error


def test_an_event_that_keeps_failing_is_marked_failed_after_the_attempt_cap(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    store.fail_on_status_write = MAX_EVENT_ATTEMPTS + 5
    service.receive_event(
        event_id="evt_1",
        event_type="customer.subscription.deleted",
        payload=stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER),
    )
    for _ in range(MAX_EVENT_ATTEMPTS):
        service.process_pending()
    row = store.events["evt_1"]
    assert row.attempts == MAX_EVENT_ATTEMPTS
    assert row.status is EventStatus.FAILED

    # A failed row is never picked up again; an operator has to look at it.
    summary = service.process_pending()
    assert summary.attempted == 0


def test_a_retry_after_a_transient_failure_succeeds(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    store.fail_on_status_write = 1
    _drain(
        service,
        store,
        stripe_event("evt_1", "customer.subscription.deleted", customer=CUSTOMER),
    )
    service.process_pending()
    assert store.events["evt_1"].status is EventStatus.PROCESSED
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED


def test_the_drain_takes_the_oldest_first(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    for index in range(3):
        service.receive_event(
            event_id=f"evt_{index}",
            event_type="invoice.payment_succeeded",
            payload=stripe_event(f"evt_{index}", "invoice.payment_succeeded", customer=CUSTOMER),
        )
        clock.advance(timedelta(seconds=30))
    service.process_pending(limit=2)
    assert [row.status for row in store.ordered_events()] == [
        EventStatus.PROCESSED,
        EventStatus.PROCESSED,
        EventStatus.PENDING,
    ]


# ----------------------------------------------------- usage reporting reconciliation


def _seed_a_day(metering_store: InMemoryMeteringStore, day: date) -> None:
    """One day with three leads, one of which was assessed three times after a dispatch
    failure — the exact shape that makes ``leads_billable`` differ from ``leads_assessed``.
    """
    noon = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    for index in range(3):
        metering_store.add_lead(tenant_id=TENANT, received_at=noon)
        metering_store.add_assessment(
            tenant_id=TENANT,
            created_at=noon,
            lead_id=f"lead-{index}",
            input_tokens=900,
            output_tokens=120,
            cost_usd=Decimal("0.018"),
        )
    for _ in range(2):
        metering_store.add_assessment(
            tenant_id=TENANT,
            created_at=noon,
            lead_id="lead-0",
            input_tokens=900,
            output_tokens=120,
            cost_usd=Decimal("0.018"),
        )


def test_the_quantity_reported_to_stripe_is_the_rollups_leads_billable(
    service: BillingService,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    """The acceptance criterion "usage reported to Stripe reconciles with the #33 rollup",
    asserted against the rollup itself rather than against a number typed here.

    ``leads_billable`` is ``count(DISTINCT lead_id) FILTER (WHERE input_tokens > 0)`` —
    distinct *leads*, not assessment rows. This day has five assessment attempts over
    three leads because one lead was redelivered twice after a dispatch failure, so any
    implementation that reported attempts would bill the customer 5 for a day they sent 3.
    """
    _seed_a_day(metering_store, YESTERDAY)
    rollup = metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)
    assert rollup.leads_assessed == 5
    assert rollup.leads_billable == 3

    outcome = service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)

    assert outcome.reported is True
    assert len(billing.reports) == 1
    reported = billing.reports[0].report
    assert reported.quantity == rollup.leads_billable
    assert reported.quantity != rollup.leads_assessed
    assert reported.quantity != rollup.leads_ingested or rollup.leads_ingested == 3


def test_the_reported_quantity_equals_the_rollup_read_back_for_the_same_period(
    service: BillingService,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
    clock: FakeClock,
) -> None:
    """Reconciliation stated the way an auditor would: report the day, then read the
    period out of the rollup table and compare. The two numbers come from different code
    paths and must be equal."""
    _seed_a_day(metering_store, YESTERDAY)
    metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)
    service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)

    from leadquali.adapters.revenue_none import UnknownRevenue

    metering = MeteringService(store=metering_store, clock=clock, revenue=UnknownRevenue())
    totals = metering.usage_for_period(tenant_id=TENANT, period=BillingPeriod.of_day(YESTERDAY))
    assert billing.reports[0].report.quantity == totals.leads_billable


def test_a_day_that_is_not_over_is_never_reported(
    service: BillingService, metering_store: InMemoryMeteringStore, billing: RecordingBilling
) -> None:
    """Double-reporting overbills; reporting a partial day *under*-reports and then gets
    re-reported tomorrow, which overbills as well. Neither is allowed."""
    _seed_a_day(metering_store, TODAY)
    metering_store.rollup_day(tenant_id=TENANT, day=TODAY)

    outcome = service.report_usage_for_day(tenant_id=TENANT, usage_date=TODAY)

    assert outcome is UsageReportOutcome.DAY_NOT_CLOSED
    assert billing.reports == []


def test_a_day_already_reported_is_skipped(
    service: BillingService,
    store: InMemoryBillingStore,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    """Mechanism one: our own ``usage_reports`` table, unique on ``(tenant_id, usage_date)``."""
    _seed_a_day(metering_store, YESTERDAY)
    metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)

    first = service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)
    second = service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)

    assert first.reported is True
    assert second.reported is False
    assert second.already_reported is True
    assert len(billing.reports) == 1
    assert len(store.usage_reports) == 1


def test_the_idempotency_handle_is_deterministic_from_the_tenant_and_the_day() -> None:
    """Mechanism two: even if our table were lost, the same day would carry the same
    handle and Stripe would deduplicate it."""
    first = usage_external_id(tenant_id=TENANT, usage_date=YESTERDAY)
    assert first == usage_external_id(tenant_id=TENANT, usage_date=YESTERDAY)
    assert first != usage_external_id(tenant_id=TENANT, usage_date=TODAY)
    assert first != usage_external_id(tenant_id="other", usage_date=YESTERDAY)
    assert first == UsageReport(tenant_id=TENANT, usage_date=YESTERDAY, quantity=3).external_id


def test_a_zero_usage_day_is_recorded_without_calling_stripe(
    service: BillingService,
    store: InMemoryBillingStore,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    """A meter event of zero is noise on an invoice. The day is still written to
    ``usage_reports`` so that tomorrow's job knows it was dealt with."""
    metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)
    outcome = service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)
    assert outcome.reported is False
    assert billing.reports == []
    assert len(store.usage_reports) == 1


def test_a_tenant_with_no_stripe_customer_is_skipped_not_reported(
    service: BillingService, store: InMemoryBillingStore, metering_store: InMemoryMeteringStore
) -> None:
    store.given_tenant("no-billing", stripe_customer_id=None)
    _seed_a_day(metering_store, YESTERDAY)
    metering_store.rollup_day(tenant_id="no-billing", day=YESTERDAY)
    outcome = service.report_usage_for_day(tenant_id="no-billing", usage_date=YESTERDAY)
    assert outcome is UsageReportOutcome.NOT_BILLABLE


def test_a_day_older_than_stripe_accepts_is_refused_rather_than_silently_wrong(
    service: BillingService, metering_store: InMemoryMeteringStore, billing: RecordingBilling
) -> None:
    """Stripe's meter events take a timestamp "within the past 35 calendar days". A
    backfill past that is rejected by the API; better to say so than to send it and record
    the day as reported."""
    old = TODAY - timedelta(days=METER_EVENT_MAX_AGE_DAYS + 1)
    _seed_a_day(metering_store, old)
    metering_store.rollup_day(tenant_id=TENANT, day=old)
    outcome = service.report_usage_for_day(tenant_id=TENANT, usage_date=old)
    assert outcome is UsageReportOutcome.TOO_OLD
    assert billing.reports == []


def test_a_stripe_failure_does_not_record_the_day_as_reported(
    service: BillingService,
    store: InMemoryBillingStore,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    """Under-reporting is recoverable; a day marked reported that Stripe never received is
    not. The write to ``usage_reports`` therefore happens only after Stripe accepts."""
    _seed_a_day(metering_store, YESTERDAY)
    metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)
    billing.fail_times = 1

    with pytest.raises(RuntimeError):
        service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY)
    assert store.usage_reports == {}

    assert service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY).reported is True


def test_reporting_every_tenant_covers_each_one_once(
    service: BillingService,
    store: InMemoryBillingStore,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    store.given_tenant("second", stripe_customer_id="cus_second")
    for tenant in (TENANT, "second"):
        metering_store.add_lead(tenant_id=tenant, received_at=datetime(2026, 9, 7, 9, tzinfo=UTC))
        metering_store.add_assessment(
            tenant_id=tenant,
            created_at=datetime(2026, 9, 7, 9, tzinfo=UTC),
            lead_id=f"{tenant}-1",
            input_tokens=100,
        )
        metering_store.rollup_day(tenant_id=tenant, day=YESTERDAY)

    results = service.report_usage_for_all(usage_date=YESTERDAY)

    assert sorted(results) == ["acme-demo", "second"]
    assert {call.report.tenant_id for call in billing.reports} == {TENANT, "second"}
    assert all(call.report.quantity == 1 for call in billing.reports)


def test_a_suspended_tenant_is_still_billed_for_what_it_used(
    service: BillingService,
    store: InMemoryBillingStore,
    metering_store: InMemoryMeteringStore,
    billing: RecordingBilling,
) -> None:
    """Suspension stops new leads. It does not retroactively make the leads already
    assessed free, and a usage job that skipped suspended tenants would quietly write off
    the usage of exactly the customers who are not paying."""
    store.given_tenant(TENANT, status=TenantStatus.SUSPENDED, stripe_customer_id=CUSTOMER)
    _seed_a_day(metering_store, YESTERDAY)
    metering_store.rollup_day(tenant_id=TENANT, day=YESTERDAY)
    assert service.report_usage_for_day(tenant_id=TENANT, usage_date=YESTERDAY).reported is True
    assert billing.reports[0].report.quantity == 3


# ---------------------------------------------------------------------- subscription


def test_the_subscription_states_that_keep_a_tenant_serving_are_the_three_live_ones() -> None:
    """Stripe's vocabulary, read off the installed SDK's ``Subscription.status`` literal.
    ``past_due`` keeps serving because that is what the grace period is *for*."""
    live = {state for state in SubscriptionState if state.keeps_service}
    assert live == {
        SubscriptionState.ACTIVE,
        SubscriptionState.TRIALING,
        SubscriptionState.PAST_DUE,
    }


def test_an_unknown_subscription_status_is_not_treated_as_live() -> None:
    assert SubscriptionState.parse("something_new") is None
    assert SubscriptionState.parse("active") is SubscriptionState.ACTIVE


def test_the_portal_url_comes_from_the_adapter_for_the_tenants_own_customer(
    service: BillingService, billing: RecordingBilling
) -> None:
    url = service.portal_url(tenant_id=TENANT, return_url="https://acme.example/billing")
    assert url == billing.portal_url
    assert billing.portal_calls == [(TENANT, CUSTOMER, "https://acme.example/billing")]


def test_a_tenant_with_no_stripe_customer_cannot_open_a_portal(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    store.given_tenant("no-billing", stripe_customer_id=None)
    from leadquali.app.billing import BillingError

    with pytest.raises(BillingError):
        service.portal_url(tenant_id="no-billing", return_url="https://x.example/")


def test_the_final_invoice_of_a_cancelled_subscription_does_not_resurrect_the_tenant(
    service: BillingService, store: InMemoryBillingStore
) -> None:
    """Cancelling produces one more invoice — for the usage up to the cancellation — and
    the customer pays it. Treating that payment as "they are back" would silently
    re-activate an account both parties agreed to close."""
    _drain(
        service,
        store,
        stripe_event(
            "evt_1",
            "customer.subscription.deleted",
            customer=CUSTOMER,
            subscription=SUBSCRIPTION,
            status="canceled",
        ),
    )
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED
    assert store.tenants[TENANT].stripe_subscription_id is None

    _drain(service, store, stripe_event("evt_2", "invoice.paid", customer=CUSTOMER))

    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED
    assert store.events["evt_2"].status is EventStatus.PROCESSED


def test_a_dunning_suspension_is_still_reversible_because_the_subscription_remains(
    service: BillingService, store: InMemoryBillingStore, clock: FakeClock
) -> None:
    """The other side of the test above, and the acceptance criterion: a tenant suspended
    for non-payment keeps its subscription, so a later payment brings it back."""
    _drain(service, store, stripe_event("evt_1", "invoice.payment_failed", customer=CUSTOMER))
    clock.advance(DUNNING_GRACE + timedelta(seconds=1))
    service.sweep_dunning()
    assert store.tenants[TENANT].status is TenantStatus.SUSPENDED
    assert store.tenants[TENANT].stripe_subscription_id == SUBSCRIPTION

    _drain(service, store, stripe_event("evt_2", "invoice.payment_succeeded", customer=CUSTOMER))
    assert store.tenants[TENANT].status is TenantStatus.ACTIVE
