"""The metering adapter's offline half: is the SQL well-formed, and does it say the rule?

Docker is not available in every environment this suite runs in, so
``tests/integration/test_metering_postgres.py`` skips and this file carries the weight of
"the statements are at least correct SQL". Every statement is compiled against the real
``postgresql`` dialect — which catches a construct SQLAlchemy cannot render for Postgres,
a column that does not exist and an ``ON CONFLICT`` target that is not a constraint — and
then read back as text to check the three things the money depends on:

* every tenant-scoped statement carries a tenant predicate (invariant 4),
* the rollup is a full ``ON CONFLICT DO UPDATE`` replacement rather than an increment,
* the billable filter is ``input_tokens > 0``, the same rule
  :func:`~leadquali.app.metering.is_billable` states in Python.

The behavioural half — that Postgres accepts these rows and that the sums are right — is
the integration file.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters import metering_postgres
from leadquali.adapters.metering_postgres import PostgresMeteringStore, _day_bounds
from leadquali.app.metering import BillingPeriod, MeteringStorePort

MODULE_PATH = Path(metering_postgres.__file__)

TENANT = "acme"
DAY = dt.date(2026, 9, 3)
SEPTEMBER = BillingPeriod.of_month(2026, 9)


class CapturedStatementError(Exception):
    """Carries the statement a method built, instead of executing it."""

    def __init__(self, statement: ClauseElement) -> None:
        super().__init__("captured")
        self.statement = statement


class CapturingSession:
    """A session that records the statement it is given and refuses to run it.

    The adapter builds its statement, opens a session and executes — so the only way to
    get at the SQL without a server is to let it do all three and intercept the last. That
    also means these tests exercise the real code path rather than a copy of it.
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


#: The dialect every statement here is compiled against. Built once because SQLAlchemy's
#: dialect constructor carries no annotations of its own, so the ignore belongs in one
#: place rather than on every call site.
PG_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]  # untyped in SQLAlchemy


def sql_for(call: Any) -> str:
    """The Postgres SQL one method emits, compiled and lowercased."""
    return str(capture(call).compile(dialect=PG_DIALECT)).lower()


@pytest.fixture
def store() -> Iterator[PostgresMeteringStore]:
    yield PostgresMeteringStore(CapturingSessions())


# ------------------------------------------------------------------- the day boundary


def test_a_day_is_the_utc_calendar_day() -> None:
    start, end = _day_bounds(DAY)
    assert start == dt.datetime(2026, 9, 3, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 9, 4, tzinfo=dt.UTC)


def test_the_day_range_is_half_open() -> None:
    """Midnight belongs to the day that starts, not to the one that ends — otherwise a
    lead at exactly 00:00:00 is billed twice or not at all."""
    _, end = _day_bounds(DAY)
    next_start, _ = _day_bounds(DAY + dt.timedelta(days=1))
    assert end == next_start


# --------------------------------------------------------------------------- the SQL


def test_the_rollup_compiles_against_postgres(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "insert into usage_daily" in sql
    assert "from assessments" in sql
    assert "from leads" in sql


def test_the_rollup_replaces_the_whole_row_rather_than_incrementing(
    store: PostgresMeteringStore,
) -> None:
    """The idempotency guarantee, read off the SQL: every counter is set to the freshly
    computed ``excluded`` value, and nothing anywhere adds to what is already stored."""
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "on conflict (tenant_id, usage_date) do update set" in sql
    for column in (
        "leads_ingested",
        "leads_assessed",
        "leads_billable",
        "assessments_failed",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "computed_at",
    ):
        assert f"{column} = excluded.{column}" in sql
    assert "usage_daily.leads_billable +" not in sql
    assert "+ excluded" not in sql


def test_the_rollup_returns_the_row_it_wrote(store: PostgresMeteringStore) -> None:
    """``RETURNING`` rather than trusting the values sent: a CHECK constraint or a type
    coercion must not be able to make the reported totals disagree with the stored ones."""
    assert "returning usage_daily.tenant_id" in sql_for(
        lambda: store.rollup_day(tenant_id=TENANT, day=DAY)
    )


def test_the_billable_filter_is_the_documented_rule(store: PostgresMeteringStore) -> None:
    """``input_tokens > 0``, the SQL half of
    :func:`~leadquali.app.metering.is_billable`. If these two ever disagree, a customer is
    charged for something the documentation says is free."""
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "count(*) filter (where assessments.input_tokens > " in sql


def test_a_failed_assessment_is_counted_by_status(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "count(*) filter (where assessments.status = " in sql


def test_leads_are_counted_by_when_they_arrived(store: PostgresMeteringStore) -> None:
    """``received_at``, not ``created_at``: a customer's "leads on 3 September" means the
    ones they sent that day, not the ones a retried worker happened to write that day."""
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "leads.received_at >=" in sql
    assert "leads.created_at" not in sql


def test_the_day_predicate_is_a_range_and_not_a_function_on_the_column(
    store: PostgresMeteringStore,
) -> None:
    """``date_trunc(created_at)`` would be the same days and could not use
    ``ix_assessments_tenant_id_created_at``; a range over the column can."""
    sql = sql_for(lambda: store.rollup_day(tenant_id=TENANT, day=DAY))
    assert "date_trunc" not in sql
    assert "assessments.created_at >=" in sql
    assert "assessments.created_at <" in sql


def test_reading_a_period_never_touches_the_assessments_table(
    store: PostgresMeteringStore,
) -> None:
    """The whole reason ``usage_daily`` exists, asserted rather than assumed: a billing
    read costs the same in month one and in year three."""
    sql = sql_for(lambda: store.usage_for_period(tenant_id=TENANT, period=SEPTEMBER))
    # The only table named anywhere in the statement, FROM or JOIN, is the rollup. The
    # string "assessments" still appears, as the column `assessments_failed` — which is
    # why this checks the tables rather than searching for the word.
    assert re.findall(r"(?:from|join)\s+(\w+)", sql) == ["usage_daily"]


def test_reading_a_period_sums_every_column_once(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.usage_for_period(tenant_id=TENANT, period=SEPTEMBER))
    for column in ("leads_billable", "input_tokens", "cost_usd"):
        assert f"sum(usage_daily.{column})" in sql
    assert "max(usage_daily.computed_at)" in sql


def test_the_daily_listing_is_ordered_by_day(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.daily_usage(tenant_id=TENANT, period=SEPTEMBER))
    assert "order by usage_daily.usage_date" in sql


def test_writing_a_quota_touches_one_tenant_and_stamps_it(
    store: PostgresMeteringStore,
) -> None:
    """A plan change is an administrative write like any other, and "when did this
    customer's plan change?" is the first question after a surprising invoice."""
    sql = sql_for(
        lambda: store.set_quota(
            tenant_id=TENANT, monthly_lead_quota=100, alert_fraction=Decimal("0.80")
        )
    )
    assert "update tenants set" in sql
    assert "monthly_lead_quota=" in sql
    assert "quota_alert_fraction=" in sql
    assert "updated_at=now()" in sql
    assert "where tenants.id = " in sql


def test_the_quota_read_is_one_row_of_the_tenants_table(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.quota_for(tenant_id=TENANT))
    assert "tenants.monthly_lead_quota" in sql
    assert "tenants.quota_alert_fraction" in sql
    assert "tenants.id = " in sql


def test_the_fleet_allocation_groups_by_tenant(store: PostgresMeteringStore) -> None:
    """Fleet-wide, and keyed by slug: a total with no tenant attached to it is exactly
    what the tenant-scoping rule exists to prevent, so the fleet queries return the
    breakdown rather than the sum."""
    sql = sql_for(lambda: store.fleet_billable_leads(period=SEPTEMBER))
    assert "group by tenants.slug" in sql
    assert "join tenants" in sql


def test_the_fleet_spend_groups_by_day(store: PostgresMeteringStore) -> None:
    sql = sql_for(lambda: store.fleet_daily_spend(period=SEPTEMBER))
    assert "group by usage_daily.usage_date" in sql
    assert "sum(usage_daily.cost_usd)" in sql


# ------------------------------------------------------------------ tenant scoping


@pytest.mark.parametrize(
    "method",
    [
        "rollup_day",
        "usage_for_period",
        "daily_usage",
        "quota_for",
        "set_quota",
    ],
)
def test_every_tenant_scoped_statement_filters_on_the_tenant(
    store: PostgresMeteringStore, method: str
) -> None:
    """Invariant 4, checked on the emitted SQL rather than on the signature.

    A method that takes ``tenant_id`` and forgets to put it in the ``WHERE`` clause would
    pass a signature check and hand one customer another customer's usage.
    """
    arguments: dict[str, Any] = {"tenant_id": TENANT}
    if method == "rollup_day":
        arguments["day"] = DAY
    elif method in {"usage_for_period", "daily_usage"}:
        arguments["period"] = SEPTEMBER
    elif method == "set_quota":
        arguments |= {"monthly_lead_quota": 100, "alert_fraction": Decimal("0.80")}
    sql = sql_for(lambda: getattr(store, method)(**arguments))
    assert "tenant_id = " in sql or "tenants.id = " in sql, sql


def test_no_store_method_is_reachable_without_a_tenant_or_a_fleet_name() -> None:
    """The enumeration, so that a convenience getter added next year has to choose: name
    the tenant, or say ``fleet_`` and return the breakdown."""
    methods = {
        name: member
        for name, member in inspect.getmembers(PostgresMeteringStore, inspect.isfunction)
        if not name.startswith(("_", "from_"))
    }
    assert set(methods) == {
        "rollup_day",
        "usage_for_period",
        "daily_usage",
        "quota_for",
        "set_quota",
        "fleet_billable_leads",
        "fleet_daily_spend",
    }
    for name, method in methods.items():
        parameters = inspect.signature(method).parameters
        if name.startswith("fleet_"):
            assert "tenant_id" not in parameters
            continue
        assert parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_the_adapter_satisfies_the_port() -> None:
    """The Protocol is ``runtime_checkable``, so this is a real check of the method set."""
    assert isinstance(PostgresMeteringStore(CapturingSessions()), MeteringStorePort)


# ----------------------------------------------------------- structural properties


def test_no_sql_is_assembled_from_strings() -> None:
    """Every statement is a Core construct, so dates and tenant ids travel as bound
    parameters. There is no textual fragment in this module at all — unlike
    ``store_postgres.py``, which has the two ``(xmax = 0)`` idioms."""
    module = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    textual = {"text", "literal_column", "column", "table"}
    for call in (node for node in ast.walk(module) if isinstance(node, ast.Call)):
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        assert name not in textual, f"line {call.lineno}: {name}() builds SQL from text"


def test_importing_the_module_creates_no_engine() -> None:
    """The same rule ``store_postgres.py`` is held to: this module is imported by a CLI
    that may be run to print a help string, and an engine at import time would open a
    socket to do it."""
    module = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

    def calls(node: ast.AST) -> list[ast.Call]:
        found: list[ast.Call] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(child, ast.Call):
                found.append(child)
            found.extend(calls(child))
        return found

    for call in calls(module):
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        assert name not in {"create_engine", "sessionmaker"}, f"line {call.lineno}"


def test_a_bigint_sum_comes_back_as_an_integer() -> None:
    """``SUM`` over ``bigint`` returns ``numeric`` in Postgres, so a token total arrives as
    a ``Decimal``. Left as one, a JSON dump of a usage report would carry ``"4200"`` for
    some fields and ``4200`` for others depending on which path produced them."""
    assert metering_postgres._as_int(Decimal("4200")) == 4200
    assert metering_postgres._as_int(None) == 0
    assert metering_postgres._as_decimal(None) == Decimal(0)
    assert metering_postgres._as_decimal(Decimal("0.054")) == Decimal("0.054")
