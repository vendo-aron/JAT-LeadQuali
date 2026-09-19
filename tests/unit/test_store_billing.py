"""The billing adapter's offline half: is the SQL well-formed, and does it say the rule?

Docker is not available in every environment this suite runs in, and the #31 and #33
reviews both found a money-critical property asserted *only* by a Docker-gated test — so a
mutation broke it and the whole suite stayed green. Everything here compiles against the
real ``postgresql`` dialect and reads the statement back as text, with no server anywhere,
and it checks the three properties an invoice depends on:

* the webhook insert is ``ON CONFLICT (event_id) DO NOTHING``, not a ``SELECT`` then an
  ``INSERT`` — which is the difference between a Stripe retry being a no-op and being an
  ``IntegrityError`` in the middle of a request;
* the usage insert is ``ON CONFLICT (tenant_id, usage_date) DO NOTHING``, which is what
  stops a customer being billed twice for one day;
* the attempt counter is incremented **from the column**, so two overlapping drains cannot
  both read four and both write five and retry an event for ever.

Plus invariant 4: every tenant-scoped statement carries a tenant predicate.

``tests/integration/test_store_billing.py`` is the behavioural half — that Postgres really
accepts these rows and really refuses the second one. It is the *second* test of each
property, never the only one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.store_billing import PostgresBillingStore
from leadquali.app.billing import BillingStorePort, EventStatus, StripeEvent, UsageReport
from leadquali.app.tenants import TenantStatus

TENANT = "acme-demo"
NOW = dt.datetime(2026, 9, 8, 6, 0, tzinfo=dt.UTC)
DAY = dt.date(2026, 9, 7)

EVENT = StripeEvent(
    event_id="evt_1",
    event_type="invoice.payment_failed",
    payload={"id": "evt_1", "data": {"object": {"customer": "cus_1"}}},
    received_at=NOW,
)


def record(store: PostgresBillingStore, report: UsageReport) -> bool:
    """Record one report, unpacked into the store's own parameters.

    The store takes the row's columns rather than the value object, so that ``tenant_id``
    is a named parameter of the method — which is what invariant 4 asks for and what lets
    the isolation sweep inject a tenant at all. This unpacks it the way
    ``BillingService._record`` does, in one place, so the tests below read as the behaviour
    they are about rather than as four arguments each.
    """
    return store.record_usage_report(
        tenant_id=report.tenant_id,
        usage_date=report.usage_date,
        quantity=report.quantity,
        external_id=report.external_id,
        reported_at=NOW,
    )


class CapturedStatementError(Exception):
    """Carries the statement a method built, instead of executing it."""

    def __init__(self, statement: ClauseElement) -> None:
        super().__init__("captured")
        self.statement = statement


class CapturingSession:
    """A session that records the statement it is given and refuses to run it.

    The adapter builds its statement, opens a session and executes, so intercepting the
    last step is the only way to see the SQL without a server — and it means these tests
    exercise the real code path rather than a copy of it.
    """

    def execute(self, statement: ClauseElement, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise CapturedStatementError(statement)

    def __enter__(self) -> CapturingSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class CapturingSessions(sessionmaker[Session]):
    """A ``sessionmaker`` whose ``begin()`` yields the capturing session."""

    def __init__(self) -> None:
        super().__init__()

    def begin(self) -> Any:
        """Hand out the capturing session instead of a real one."""
        return CapturingSession()


def capture(call: Any) -> ClauseElement:
    """Run ``call`` and return the statement it tried to execute."""
    try:
        call()
    except CapturedStatementError as captured:
        return captured.statement
    raise AssertionError("the method executed nothing")


PG_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]  # untyped in SQLAlchemy


def sql_for(call: Any) -> str:
    """The Postgres SQL one method emits, compiled and lowercased."""
    return str(capture(call).compile(dialect=PG_DIALECT)).lower()


@pytest.fixture
def store() -> Iterator[PostgresBillingStore]:
    yield PostgresBillingStore(CapturingSessions())


def test_the_adapter_satisfies_the_port(store: PostgresBillingStore) -> None:
    assert isinstance(store, BillingStorePort)


# --------------------------------------------------------------- webhook idempotency


def test_the_event_insert_does_nothing_on_a_conflicting_event_id(
    store: PostgresBillingStore,
) -> None:
    """The idempotency acceptance criterion, in the SQL. A ``SELECT`` then an ``INSERT``
    would let two deliveries arriving milliseconds apart both see no row."""
    sql = sql_for(lambda: store.insert_event(event=EVENT))
    assert "insert into stripe_events" in sql
    assert "on conflict (event_id) do nothing" in sql
    assert "do update" not in sql


def test_the_event_insert_returns_whether_it_inserted(store: PostgresBillingStore) -> None:
    """``RETURNING`` is how the caller tells "we stored it" from "Stripe is retrying", and
    a route that could not tell them apart would apply an event twice."""
    sql = sql_for(lambda: store.insert_event(event=EVENT))
    assert "returning stripe_events.event_id" in sql


def test_an_inserted_event_starts_pending_with_no_attempts(store: PostgresBillingStore) -> None:
    statement = capture(lambda: store.insert_event(event=EVENT))
    compiled = statement.compile(dialect=PG_DIALECT)
    assert compiled.params["status"] == EventStatus.PENDING.value
    assert compiled.params["attempts"] == 0


def test_the_route_does_not_attribute_the_event_to_a_tenant(
    store: PostgresBillingStore,
) -> None:
    """The deliberate invariant-4 exception: a webhook is stored before we know whose it
    is. Resolving the customer here would put a second query on the request path the whole
    design exists to keep to one."""
    sql = sql_for(lambda: store.insert_event(event=EVENT))
    assert "tenant_id" not in sql


# ----------------------------------------------------------------------- the drain


def test_the_drain_reads_pending_rows_oldest_first(store: PostgresBillingStore) -> None:
    sql = sql_for(lambda: store.pending_events(limit=10))
    assert "stripe_events.status = " in sql
    assert "order by stripe_events.received_at" in sql
    assert "limit" in sql


def test_the_drain_never_picks_up_a_failed_row(store: PostgresBillingStore) -> None:
    """An event out of attempts is an operator's problem. Retrying it automatically hides
    it rather than fixing it, and it would be retried for ever."""
    statement = capture(lambda: store.pending_events(limit=10))
    compiled = statement.compile(dialect=PG_DIALECT)
    assert EventStatus.PENDING.value in compiled.params.values()
    assert EventStatus.FAILED.value not in compiled.params.values()


def test_the_attempt_counter_is_incremented_from_the_column(
    store: PostgresBillingStore,
) -> None:
    """Not from a value the application read a moment ago. Two overlapping drains would
    each read four and each write five, and the event would be retried for ever while
    looking as though it had failed five times."""
    sql = sql_for(
        lambda: store.mark_event_attempt_failed(
            event_id="evt_1", error="Boom: no", attempted_at=NOW, max_attempts=5
        )
    )
    assert "attempts=(stripe_events.attempts +" in sql
    assert "update stripe_events" in sql


def test_the_failure_update_decides_the_new_status_in_the_same_statement(
    store: PostgresBillingStore,
) -> None:
    """One statement, so the count and the decision cannot disagree under a race."""
    sql = sql_for(
        lambda: store.mark_event_attempt_failed(
            event_id="evt_1", error="Boom: no", attempted_at=NOW, max_attempts=5
        )
    )
    assert "case when" in sql
    assert "returning stripe_events.status" in sql


def test_marking_an_event_processed_clears_the_last_error(
    store: PostgresBillingStore,
) -> None:
    """A retry that succeeds must not leave the error from the attempt before it lying in
    a column an operator reads as current."""
    statement = capture(
        lambda: store.mark_event_processed(event_id="evt_1", processed_at=NOW, tenant_id=TENANT)
    )
    compiled = statement.compile(dialect=PG_DIALECT)
    assert compiled.params["last_error"] is None
    assert compiled.params["status"] == EventStatus.PROCESSED.value


# ------------------------------------------------------- usage reporting idempotency


def test_the_usage_insert_does_nothing_on_a_conflicting_tenant_day(
    store: PostgresBillingStore,
) -> None:
    """The one that stops a customer being billed twice. ``DO UPDATE`` here would make a
    second run overwrite the row and look like a first — which is precisely the state the
    caller uses to decide whether to send anything to Stripe."""
    report = UsageReport(tenant_id=TENANT, usage_date=DAY, quantity=3)
    sql = sql_for(lambda: record(store, report))
    assert "insert into usage_reports" in sql
    assert "on conflict (tenant_id, usage_date) do nothing" in sql
    assert "do update" not in sql
    assert "returning usage_reports.usage_date" in sql


def test_the_recorded_quantity_and_identifier_are_the_reports_own(
    store: PostgresBillingStore,
) -> None:
    report = UsageReport(tenant_id=TENANT, usage_date=DAY, quantity=42)
    compiled = capture(lambda: record(store, report)).compile(dialect=PG_DIALECT)
    assert compiled.params["quantity"] == 42
    assert compiled.params["external_id"] == report.external_id


# --------------------------------------------------------------------- invariant 4


@pytest.mark.parametrize(
    "name",
    [
        "billing_tenant",
        "link_customer",
        "set_subscription",
        "set_status",
        "set_dunning_until",
        "usage_reported",
        "record_usage_report",
    ],
)
def test_every_tenant_scoped_statement_filters_on_the_tenant(
    store: PostgresBillingStore, name: str
) -> None:
    """Invariant 4 as a property of the SQL rather than of a docstring."""
    calls = {
        "billing_tenant": lambda: store.billing_tenant(tenant_id=TENANT),
        "link_customer": lambda: store.link_customer(tenant_id=TENANT, stripe_customer_id="cus_1"),
        "set_subscription": lambda: store.set_subscription(
            tenant_id=TENANT, stripe_subscription_id="sub_1"
        ),
        "set_status": lambda: store.set_status(tenant_id=TENANT, status=TenantStatus.SUSPENDED),
        "set_dunning_until": lambda: store.set_dunning_until(tenant_id=TENANT, until=NOW),
        "usage_reported": lambda: store.usage_reported(tenant_id=TENANT, usage_date=DAY),
        "record_usage_report": lambda: record(
            store, UsageReport(tenant_id=TENANT, usage_date=DAY, quantity=1)
        ),
    }
    sql = sql_for(calls[name])
    assert "tenant_id" in sql or "tenants.id = " in sql


def test_the_billable_tenant_listing_is_not_filtered_by_status(
    store: PostgresBillingStore,
) -> None:
    """A suspended tenant's usage from *before* it was suspended is still owed. A listing
    that skipped them would write off exactly the customers who are not paying."""
    sql = sql_for(store.fleet_billable_tenants)
    where = sql.split("where", 1)[1]
    assert "tenants.stripe_customer_id is not null" in where
    assert "tenants.status" not in where


def test_the_dunning_sweep_only_looks_at_active_tenants(store: PostgresBillingStore) -> None:
    """Re-suspending a suspended tenant would be a second log line, a second metric and a
    second alert about one event."""
    sql = sql_for(lambda: store.fleet_tenants_in_expired_dunning(now=NOW))
    assert "tenants.dunning_until is not null" in sql
    assert "tenants.dunning_until <= " in sql
    assert "tenants.status = " in sql


def test_the_tenant_read_does_not_load_the_rubric(store: PostgresBillingStore) -> None:
    """``icp_config`` is a document billing has no use for, and it would be carried across
    the wire on every webhook the drain attributes."""
    sql = sql_for(lambda: store.billing_tenant(tenant_id=TENANT))
    assert "icp_config" not in sql
