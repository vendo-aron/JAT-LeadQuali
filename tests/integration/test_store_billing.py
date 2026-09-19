"""The billing store against a real Postgres.

``tests/unit/test_store_billing.py`` proves the statements are well-formed Postgres and say
the rule. This file is the half that only a server can settle: that the database really
refuses the second copy of an event, really refuses the second report of a day, and really
refuses two tenants sharing one Stripe customer.

It is deliberately the **second** test of each of those properties. The #31 and #33 reviews
both found a money-critical property asserted only here, in a file that skips wherever
Docker is absent — which is CI and most laptops — so a mutation that broke it left the suite
green. Nothing in this file is the only test of anything.

Skipped, never failed, when there is no database — see ``conftest.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, delete, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from leadquali.adapters.db_schema import StripeEventRow, Tenant, UsageReportRecord
from leadquali.adapters.seed import seed_tenant
from leadquali.adapters.store_billing import PostgresBillingStore
from leadquali.app.billing import (
    MAX_EVENT_ATTEMPTS,
    EventStatus,
    StripeEvent,
    UsageReport,
)
from leadquali.app.tenants import TenantStatus
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

TENANT_A = "billing-tenant-a"
TENANT_B = "billing-tenant-b"
DAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def billing_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own, because these tests commit."""
    name = f"{_database_url.database}_billing_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def billing_engine(billing_database: URL) -> Iterator[Engine]:
    engine = create_engine(billing_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def seeded_tenants(billing_engine: Engine) -> tuple[str, str]:
    """Two committed tenant rows. Half of what is under test is what one cannot do to the
    other's money."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    with billing_engine.begin() as connection:
        for slug in (TENANT_A, TENANT_B):
            seed_tenant(connection, {**document, "tenant_id": slug, "name": f"Tenant {slug}"})
    return (TENANT_A, TENANT_B)


@pytest.fixture
def store(
    billing_engine: Engine, seeded_tenants: tuple[str, str]
) -> Iterator[PostgresBillingStore]:
    """A store over a clean pair of billing tables."""
    sessions = sessionmaker(bind=billing_engine, expire_on_commit=False)
    with billing_engine.begin() as connection:
        connection.execute(delete(UsageReportRecord))
        connection.execute(delete(StripeEventRow))
        connection.execute(
            update(Tenant).values(
                stripe_customer_id=None,
                stripe_subscription_id=None,
                dunning_until=None,
                status=TenantStatus.ACTIVE.value,
            )
        )
    yield PostgresBillingStore(sessions)


def event(event_id: str, *, received_at: datetime = NOW) -> StripeEvent:
    return StripeEvent(
        event_id=event_id,
        event_type="invoice.payment_failed",
        payload={"id": event_id, "data": {"object": {"customer": "cus_a"}}},
        received_at=received_at,
    )


# ------------------------------------------------------------- the webhook inbox


def test_the_same_event_is_stored_once(store: PostgresBillingStore) -> None:
    """The idempotency acceptance criterion, against the database that enforces it."""
    assert store.insert_event(event=event("evt_1")) is True
    assert store.insert_event(event=event("evt_1")) is False
    assert len(store.pending_events(limit=10)) == 1


def test_a_replay_is_refused_while_the_first_copy_is_still_pending(
    store: PostgresBillingStore,
) -> None:
    store.insert_event(event=event("evt_1"))
    assert store.pending_events(limit=10)[0].status is EventStatus.PENDING
    assert store.insert_event(event=event("evt_1")) is False


def test_the_payload_round_trips_verbatim(store: PostgresBillingStore) -> None:
    payload = {"id": "evt_1", "nested": {"list": [1, 2, {"deep": True}]}, "unicode": "café"}
    store.insert_event(
        event=StripeEvent(
            event_id="evt_1", event_type="invoice.paid", payload=payload, received_at=NOW
        )
    )
    assert store.pending_events(limit=1)[0].payload == payload


def test_the_drain_returns_events_oldest_first(store: PostgresBillingStore) -> None:
    for index in range(3):
        store.insert_event(event=event(f"evt_{index}", received_at=NOW + timedelta(seconds=index)))
    assert [row.event_id for row in store.pending_events(limit=10)] == [
        "evt_0",
        "evt_1",
        "evt_2",
    ]
    assert [row.event_id for row in store.pending_events(limit=2)] == ["evt_0", "evt_1"]


def test_a_processed_event_leaves_the_queue_and_carries_its_tenant(
    store: PostgresBillingStore,
) -> None:
    store.insert_event(event=event("evt_1"))
    store.mark_event_processed(event_id="evt_1", processed_at=NOW, tenant_id=TENANT_A)
    assert store.pending_events(limit=10) == []


def test_attempts_accumulate_until_the_cap_and_then_the_row_is_failed(
    store: PostgresBillingStore,
) -> None:
    """The count and the decision are one ``UPDATE``, so this is also what proves the
    ``CASE`` in that statement compares against the value it is about to store."""
    store.insert_event(event=event("evt_1"))
    for attempt in range(1, MAX_EVENT_ATTEMPTS):
        status = store.mark_event_attempt_failed(
            event_id="evt_1",
            error="RuntimeError: nope",
            attempted_at=NOW,
            max_attempts=MAX_EVENT_ATTEMPTS,
        )
        assert status is EventStatus.PENDING, attempt
        assert store.pending_events(limit=10)[0].attempts == attempt

    final = store.mark_event_attempt_failed(
        event_id="evt_1",
        error="RuntimeError: nope",
        attempted_at=NOW,
        max_attempts=MAX_EVENT_ATTEMPTS,
    )
    assert final is EventStatus.FAILED
    assert store.pending_events(limit=10) == [], "a failed row is never picked up again"


def test_a_status_the_vocabulary_does_not_know_is_refused_by_the_database(
    billing_engine: Engine, store: PostgresBillingStore
) -> None:
    store.insert_event(event=event("evt_1"))
    with pytest.raises(IntegrityError), billing_engine.begin() as connection:
        connection.execute(
            update(StripeEventRow)
            .where(StripeEventRow.event_id == "evt_1")
            .values(status="whatever")
        )


def test_deleting_a_tenant_does_not_delete_its_billing_history(
    billing_engine: Engine, store: PostgresBillingStore
) -> None:
    """``ON DELETE SET NULL``: the event survives the tenant, because it is the record that
    Stripe told us something and that record outlives the account."""
    store.insert_event(event=event("evt_1"))
    store.link_customer(tenant_id=TENANT_B, stripe_customer_id="cus_b")
    store.mark_event_processed(event_id="evt_1", processed_at=NOW, tenant_id=TENANT_B)
    with billing_engine.begin() as connection:
        connection.execute(delete(Tenant).where(Tenant.slug == TENANT_B))
        row = connection.execute(select(StripeEventRow.event_id, StripeEventRow.tenant_id)).one()
    assert row.event_id == "evt_1"
    assert row.tenant_id is None
    # Put it back; the tenant fixtures are session-scoped.
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    with billing_engine.begin() as connection:
        seed_tenant(connection, {**document, "tenant_id": TENANT_B, "name": "Tenant B"})


# ------------------------------------------------------------- the usage ledger


def test_a_day_can_only_be_reported_once(store: PostgresBillingStore) -> None:
    """The property that stops a customer being billed twice, proved against the
    constraint that enforces it rather than against a dict."""
    report = UsageReport(tenant_id=TENANT_A, usage_date=DAY, quantity=3)
    assert store.record_usage_report(report=report, reported_at=NOW) is True
    assert store.record_usage_report(report=report, reported_at=NOW) is False
    assert store.usage_reported(tenant_id=TENANT_A, usage_date=DAY) is True


def test_a_second_run_with_a_different_quantity_still_records_nothing(
    billing_engine: Engine, store: PostgresBillingStore
) -> None:
    """``DO NOTHING``, not ``DO UPDATE``. A recount that produced a different number must
    not silently overwrite what we already told Stripe — the two would then disagree and
    only the invoice would know."""
    store.record_usage_report(
        report=UsageReport(tenant_id=TENANT_A, usage_date=DAY, quantity=3), reported_at=NOW
    )
    store.record_usage_report(
        report=UsageReport(tenant_id=TENANT_A, usage_date=DAY, quantity=99), reported_at=NOW
    )
    with billing_engine.begin() as connection:
        assert connection.execute(select(UsageReportRecord.quantity)).scalar_one() == 3


def test_one_tenants_report_does_not_block_anothers(store: PostgresBillingStore) -> None:
    store.record_usage_report(
        report=UsageReport(tenant_id=TENANT_A, usage_date=DAY, quantity=3), reported_at=NOW
    )
    assert (
        store.record_usage_report(
            report=UsageReport(tenant_id=TENANT_B, usage_date=DAY, quantity=5), reported_at=NOW
        )
        is True
    )
    assert store.usage_reported(tenant_id=TENANT_B, usage_date=DAY) is True


def test_a_tenant_with_usage_reports_cannot_be_deleted_by_accident(
    billing_engine: Engine, store: PostgresBillingStore
) -> None:
    """``ON DELETE RESTRICT``, unlike the derived rollup beside it. #37's purge has to
    delete these deliberately, which is the point: this is a record of something we told a
    payment processor about a customer's money."""
    store.record_usage_report(
        report=UsageReport(tenant_id=TENANT_A, usage_date=DAY, quantity=3), reported_at=NOW
    )
    with pytest.raises(IntegrityError), billing_engine.begin() as connection:
        connection.execute(delete(Tenant).where(Tenant.slug == TENANT_A))


# ----------------------------------------------------------------- the tenant link


def test_two_tenants_cannot_share_one_stripe_customer(store: PostgresBillingStore) -> None:
    """Cross-billing made impossible rather than merely unlikely. An application check
    could be bypassed by a second container; a unique constraint cannot."""
    store.link_customer(tenant_id=TENANT_A, stripe_customer_id="cus_shared")
    with pytest.raises(IntegrityError):
        store.link_customer(tenant_id=TENANT_B, stripe_customer_id="cus_shared")


def test_two_tenants_cannot_share_one_subscription(store: PostgresBillingStore) -> None:
    store.set_subscription(tenant_id=TENANT_A, stripe_subscription_id="sub_shared")
    with pytest.raises(IntegrityError):
        store.set_subscription(tenant_id=TENANT_B, stripe_subscription_id="sub_shared")


def test_any_number_of_tenants_may_have_no_customer(store: PostgresBillingStore) -> None:
    """Postgres treats NULLs as distinct, which is exactly what a nullable unique column
    has to mean here: "not on a plan" is not a collision."""
    assert store.billable_tenants() == []
    assert store.tenant_for_customer(stripe_customer_id="cus_missing") is None


def test_a_customer_resolves_back_to_its_tenant(store: PostgresBillingStore) -> None:
    store.link_customer(tenant_id=TENANT_A, stripe_customer_id="cus_a")
    assert store.tenant_for_customer(stripe_customer_id="cus_a") == TENANT_A
    assert [tenant.tenant_id for tenant in store.billable_tenants()] == [TENANT_A]


def test_the_dunning_sweep_finds_only_expired_active_tenants(
    store: PostgresBillingStore,
) -> None:
    store.link_customer(tenant_id=TENANT_A, stripe_customer_id="cus_a")
    store.link_customer(tenant_id=TENANT_B, stripe_customer_id="cus_b")
    store.set_dunning_until(tenant_id=TENANT_A, until=NOW - timedelta(seconds=1))
    store.set_dunning_until(tenant_id=TENANT_B, until=NOW + timedelta(days=1))

    assert [t.tenant_id for t in store.tenants_in_expired_dunning(now=NOW)] == [TENANT_A]

    store.set_status(tenant_id=TENANT_A, status=TenantStatus.SUSPENDED)
    assert store.tenants_in_expired_dunning(now=NOW) == []


def test_the_billing_columns_round_trip(store: PostgresBillingStore) -> None:
    store.link_customer(tenant_id=TENANT_A, stripe_customer_id="cus_a")
    store.set_subscription(tenant_id=TENANT_A, stripe_subscription_id="sub_a")
    store.set_dunning_until(tenant_id=TENANT_A, until=NOW)
    store.set_status(tenant_id=TENANT_A, status=TenantStatus.SUSPENDED)

    tenant = store.billing_tenant(tenant_id=TENANT_A)
    assert tenant is not None
    assert tenant.stripe_customer_id == "cus_a"
    assert tenant.stripe_subscription_id == "sub_a"
    assert tenant.dunning_until == NOW
    assert tenant.status is TenantStatus.SUSPENDED

    store.set_dunning_until(tenant_id=TENANT_A, until=None)
    cleared = store.billing_tenant(tenant_id=TENANT_A)
    assert cleared is not None and cleared.dunning_until is None


def test_an_unknown_tenant_is_none_rather_than_an_error(store: PostgresBillingStore) -> None:
    assert store.billing_tenant(tenant_id="no-such-tenant") is None
