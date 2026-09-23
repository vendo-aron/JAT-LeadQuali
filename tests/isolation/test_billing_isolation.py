"""#35's exception to invariant 4, held to a boundary instead of taken on trust.

``stripe_events`` is the one table in this schema whose ``tenant_id`` is nullable. The
reason is real: a Stripe webhook names a *customer*, and which of our tenants that is can
only be learned from a database read the verifying route deliberately does not make — so
an event arrives before there is a tenant to scope it to, and refusing to store one we
cannot attribute would mean discarding the only record that it arrived.

An exception with a good reason is still an exception, and the way one stops being a hole
is that somebody can see where it ends. That is what this file is: the five methods on
:class:`~leadquali.adapters.store_billing.PostgresBillingStore` that carry no tenant
predicate are named in ``repositories.py``'s allowlist, and here each one is shown to be
exempt for the stated reason rather than merely listed. The complement is asserted too —
every method that reads or writes a *tenant-owned* row is in the sweep and has a recipe.

It sits beside ``test_metering_isolation.py``, which does the same job for #33's
``fleet_`` reads: per-issue named tests for the cases the generic sweep is told to skip.
Nothing here needs a database.
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Sequence
from typing import Any, Final

import pytest
from sqlalchemy import ClauseElement, Insert, Select, Update

from leadquali.adapters.db_schema import StripeEventRow
from leadquali.adapters.store_billing import PostgresBillingStore
from leadquali.app.billing import EventStatus, StripeEvent
from tests.isolation.repositories import (
    ALLOWLIST,
    FLEET_METHODS,
    RECIPES,
    TENANT_A,
    TENANT_A_UUID,
    TENANT_B,
    TENANT_B_UUID,
    tenant_parameter_of,
)
from tests.isolation.test_repository_isolation import _public_methods, scoping_failure
from tests.sqlcapture import SqlCapture, parameters, sql_text

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)

#: The table the exception is about. Every allowlisted method that issues a statement must
#: touch this one and no other — that is the whole content of "the exception is bounded".
EVENTS_TABLE: Final[str] = "stripe_events"

#: The allowlisted methods that are not constructors, and what each one is called with.
#: Written out rather than synthesised, for the reason ``repositories.py`` gives: arguments
#: guessed from type hints produce a test that passes because the call raised.
EXEMPT_CALLS: Final[dict[str, dict[str, Any]]] = {
    "insert_event": {
        "event": StripeEvent(
            event_id="evt_probe",
            event_type="invoice.payment_failed",
            payload={"id": "evt_probe", "data": {"object": {"customer": "cus_probe"}}},
            received_at=NOW,
        )
    },
    "pending_events": {"limit": 10},
    "mark_event_attempt_failed": {
        "event_id": "evt_probe",
        "error": "RuntimeError: nope",
        "attempted_at": NOW,
        "max_attempts": 5,
    },
    "mark_event_processed": {
        "event_id": "evt_probe",
        "processed_at": NOW,
        "tenant_id": TENANT_B,
    },
    "tenant_for_customer": {"stripe_customer_id": "cus_probe"},
}

#: Constructors, which issue no statement and are exempt everywhere in this suite.
CONSTRUCTORS: Final[frozenset[str]] = frozenset({"from_url", "from_env"})


def capture(method: str, arguments: dict[str, Any]) -> tuple[ClauseElement, ...]:
    """Every statement one method builds, with no database behind it."""
    sql = SqlCapture()
    store = PostgresBillingStore(sql.sessions)
    return sql.run(lambda: getattr(store, method)(**arguments), results=())


def tables_of(statement: ClauseElement) -> set[str]:
    """The table names a statement names, read off its compiled text."""
    text = sql_text(statement)
    return {name for name in ("stripe_events", "tenants", "usage_reports") if name in text}


# ------------------------------------------------------------------ the boundary


def test_the_billing_exception_covers_only_unattributed_events() -> None:
    """The allowlist for this store is exactly the constructors plus five named methods.

    Stated as an equality rather than a subset, so that widening the exception is a diff
    in this file as well as in ``repositories.py``. An exemption that can be added in one
    place is an exemption nobody has to argue for.
    """
    allowed = set(ALLOWLIST[PostgresBillingStore])
    assert allowed == CONSTRUCTORS | set(EXEMPT_CALLS)


@pytest.mark.parametrize("method", sorted(set(EXEMPT_CALLS) - {"tenant_for_customer"}))
def test_every_exempt_method_touches_only_the_event_table(method: str) -> None:
    """The exception is about ``stripe_events``, so a method claiming it may touch nothing
    else.

    ``tenant_for_customer`` is left out of the parametrisation rather than skipped inside
    it: it reads ``tenants`` by design, because its whole job is to *produce* the tenant
    every other billing read is then scoped to, and what makes it safe is its projection
    rather than its tables. ``test_the_customer_lookup_can_only_return_a_slug`` is where it
    is held to that.
    """
    for statement in capture(method, EXEMPT_CALLS[method]):
        assert tables_of(statement) == {EVENTS_TABLE}, sql_text(statement)


def test_every_method_over_a_tenant_owned_table_is_in_the_sweep() -> None:
    """The complement, and the half that matters most.

    An exception that grew to cover ``usage_reports`` or the ``tenants`` billing columns
    would be an exception that had eaten the rule. So: every public method of this store is
    either allowlisted (and therefore shown above to touch only ``stripe_events``), a
    named ``fleet_`` worklist, or has a recipe and is swept like anything else.
    """
    allowed = set(ALLOWLIST[PostgresBillingStore])
    fleet = set(FLEET_METHODS[PostgresBillingStore])
    recipes = set(RECIPES[PostgresBillingStore])
    public = set(_public_methods(PostgresBillingStore))

    assert allowed | fleet | recipes == public
    assert not allowed & recipes, "a method cannot be both exempt and swept"
    assert recipes == {
        "billing_tenant",
        "link_customer",
        "record_usage_report",
        "set_dunning_until",
        "set_status",
        "set_subscription",
        "usage_reported",
    }


def test_every_swept_method_names_its_tenant_in_its_signature() -> None:
    """Invariant 4's own words: ``tenant_id`` on every table *and every repository method*.

    This is the check that made ``record_usage_report`` change shape. It used to take a
    ``UsageReport`` — which carries the tenant as a field — so it named no tenant, the
    enumeration would have let it through as "takes no tenant", and the sweep could not
    have injected one. It now takes the row's columns, and the tenant is a parameter.
    """
    for method in RECIPES[PostgresBillingStore]:
        signature = inspect.signature(getattr(PostgresBillingStore, method)).parameters
        assert tenant_parameter_of(signature) == "tenant_id", method


# ------------------------------------------------- why each exemption is an exemption


def test_the_attribution_write_names_the_tenant_it_writes() -> None:
    """``mark_event_processed`` is the one exempt method that *takes* a tenant.

    It is exempt because its ``tenant_id`` is the value being written, not a filter for
    finding the row: the row is addressed by Stripe's globally unique ``evt_...`` primary
    key, and its ``tenant_id`` is NULL until this statement sets it — so a ``WHERE`` on the
    tenant would match zero rows and the attribution would silently not happen.

    Saying that out loud is the point of this test. The statement is asserted to do exactly
    what the exemption claims: filter on the event id, write the tenant, and carry no
    tenant predicate at all.
    """
    (statement,) = capture("mark_event_processed", EXEMPT_CALLS["mark_event_processed"])
    assert isinstance(statement, Update)
    text = sql_text(statement)

    assert "where stripe_events.event_id = " in text
    assert "tenant_id=" in text.replace(" ", ""), "the tenant is written into the row"
    assert "where" in text and "tenant_id = " not in text.split("where", 1)[1]
    # Bound as the row id, because that is what ``tenants.id`` holds — the port speaks
    # slugs and ``tenant_uuid`` is the single mapping between the two.
    bound = {str(value) for value in parameters(statement).values()}
    assert str(TENANT_B_UUID) in bound
    assert str(TENANT_A_UUID) not in bound and TENANT_A not in bound


def test_a_pending_event_can_only_ever_be_an_unattributed_one() -> None:
    """Why ``pending_events`` has no tenant to be scoped to, proved rather than asserted.

    The claim in the allowlist is structural: the only statement that writes ``tenant_id``
    is the one that also moves the row to ``processed``. So every statement this store
    builds against ``stripe_events`` is checked — if it binds a value for ``tenant_id``, it
    must bind ``processed`` for ``status`` in the same statement. A future method that
    attributed a row and left it pending would break the claim, and it would fail here.
    """
    attributing: list[str] = []
    for method, arguments in EXEMPT_CALLS.items():
        for statement in capture(method, arguments):
            if EVENTS_TABLE not in tables_of(statement):
                continue
            if not isinstance(statement, Insert | Update):
                continue
            bound = parameters(statement)
            if "tenant_id" not in bound:
                continue
            attributing.append(method)
            assert bound.get("status") == EventStatus.PROCESSED.value, (
                f"{method} writes tenant_id without moving the row to processed; pending "
                "rows would then be attributable and the allowlist's reason for exempting "
                "pending_events would no longer hold"
            )
    assert attributing == ["mark_event_processed"], (
        "exactly one statement in this store attributes an event; if that changed, the "
        f"reasoning behind three of its five exemptions changed with it: {attributing}"
    )


def test_the_drain_worklist_reads_one_table_and_no_tenant_row() -> None:
    """``pending_events`` has no join to ``tenants``, and could not usefully have one.

    It once did — a ``LEFT JOIN`` to resolve a slug — and the join was dead by construction,
    because the rows it returns always have ``tenant_id`` NULL (see the test above). It was
    removed when this store went through the sweep. The test is here so it does not come
    back: a join to ``tenants`` on this read would be a second table in a statement with no
    tenant predicate, which is precisely the shape the exemption is *not* for.
    """
    (statement,) = capture("pending_events", EXEMPT_CALLS["pending_events"])
    assert isinstance(statement, Select)
    assert tables_of(statement) == {EVENTS_TABLE}
    assert "join" not in sql_text(statement)


def test_the_customer_lookup_can_only_return_a_slug() -> None:
    """``tenant_for_customer`` is unscoped because it is the question "which tenant?".

    It is handed a Stripe customer id and answers with a slug or ``None``. What makes that
    safe is not a predicate but the projection: it selects one column, so there is no row
    of another tenant's for it to hand back even in principle. If it ever started selecting
    more, that reasoning would stop holding, and this fails.
    """
    (statement,) = capture("tenant_for_customer", EXEMPT_CALLS["tenant_for_customer"])
    assert isinstance(statement, Select)
    assert [str(column.name) for column in statement.selected_columns] == ["slug"]
    assert "where tenants.stripe_customer_id = " in sql_text(statement)


def test_the_insert_writes_no_tenant_at_all() -> None:
    """The webhook route's statement, and the exception at its purest.

    ``insert_event`` runs before anything knows whose event this is. It must not invent a
    tenant, and it must not silently write one — a column left NULL on purpose is a
    different thing from a column nobody thought about, and the difference is visible here.
    """
    (statement,) = capture("insert_event", EXEMPT_CALLS["insert_event"])
    assert isinstance(statement, Insert)
    text = sql_text(statement)
    assert "insert into stripe_events" in text
    assert "tenant_id" not in text
    assert "on conflict (event_id) do nothing" in text


# ------------------------------------------------- the evidence rule can see this table


def test_the_scoping_rule_would_catch_a_dropped_filter_on_the_billing_tables() -> None:
    """A rule that cannot fail on a table proves nothing about the statements over it.

    Both of #35's tenant-owned tables are put through :func:`scoping_failure` with the
    filter removed, and both must be refused — otherwise the seven recipes above are green
    because the rule is blind here rather than because the statements are scoped.
    """
    from sqlalchemy import select, update

    from leadquali.adapters.db_schema import Tenant, UsageReportRecord

    unscoped_read = select(UsageReportRecord.usage_date).where(
        UsageReportRecord.usage_date == dt.date(2026, 9, 3)
    )
    assert scoping_failure(unscoped_read) is not None

    unscoped_write = update(Tenant).values(stripe_customer_id="cus_anyone")
    assert scoping_failure(unscoped_write) is not None

    # And the real ones are accepted, so the rule is not simply refusing everything.
    sql = SqlCapture()
    store = PostgresBillingStore(sql.sessions)
    for statement in sql.run(
        lambda: store.usage_reported(tenant_id=TENANT_B, usage_date=dt.date(2026, 9, 3)),
        results=(),
    ):
        assert scoping_failure(statement) is None, sql_text(statement)


def test_the_event_table_is_the_only_nullable_tenant_column_in_this_store() -> None:
    """The exception, restated where a reader of this file will find it.

    ``tests/unit/test_db_schema.py`` pins the same fact against the whole schema. It is
    repeated here because this is the file somebody opens when they want to know why five
    methods are on an allowlist, and the answer is a property of one column.
    """
    assert StripeEventRow.__table__.c["tenant_id"].nullable is True
    from leadquali.adapters.db_schema import UsageReportRecord

    assert UsageReportRecord.__table__.c["tenant_id"].nullable is False


def test_no_exempt_method_is_also_a_fleet_method() -> None:
    """Two different excuses for the same method would mean neither was examined."""
    assert not set(ALLOWLIST[PostgresBillingStore]) & set(FLEET_METHODS[PostgresBillingStore])


def test_the_fleet_worklists_return_tenants_rather_than_tenant_data() -> None:
    """#33's convention, applied to #35's two worklists.

    What makes a ``fleet_`` method acceptable is that it answers "who should this job
    iterate over?" rather than handing anybody another tenant's figures. Both of these
    return :class:`~leadquali.app.billing.BillingTenant` rows — a slug, a status, the
    Stripe identifiers and the dunning deadline — to a scheduled job with no tenant
    context, and neither is reachable from a request path.
    """
    for method in FLEET_METHODS[PostgresBillingStore]:
        assert method.startswith("fleet_")
        signature = inspect.signature(getattr(PostgresBillingStore, method)).parameters
        assert tenant_parameter_of(signature) is None, method
        annotation = str(inspect.signature(getattr(PostgresBillingStore, method)).return_annotation)
        assert "Sequence[BillingTenant]" in annotation, method


def test_canned_results_are_not_needed_by_any_exempt_method() -> None:
    """Every method here issues exactly one statement, which is worth knowing.

    A second statement would mean a second chance to drop a filter, and the capture above
    would stop at the first one and never see it. If one of these grows a second statement,
    this fails and somebody has to decide whether it needs a recipe rather than an
    exemption.
    """
    for method, arguments in EXEMPT_CALLS.items():
        statements: Sequence[ClauseElement] = capture(method, arguments)
        assert len(statements) == 1, f"{method} now issues {len(statements)} statements"
