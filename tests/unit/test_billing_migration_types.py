"""#35's migration and its models, compared without a database.

``tests/integration/test_migrations.py`` already proves the two agree — it runs
``alembic upgrade head`` and asks autogenerate for a diff — and that test is the real one.
It is also skipped everywhere there is no Postgres, which is CI and every developer who has
not started Docker, so a migration that declares ``sa.Integer()`` where the model says
``Text`` can be committed, reviewed and merged with a green suite.

This file closes that window for the two tables #35 adds, the same way
``test_migration_types.py`` does for #33's: it runs the migration's ``upgrade()`` against a
recording stand-in for ``alembic.op`` and compares the DDL it asks for, column by column,
compiled for Postgres.

It also pins the three things about this revision that are decisions rather than types: the
``down_revision`` (which #36 forces somebody to re-point when the branches are stacked), the
``ON DELETE`` behaviour of each foreign key, and the partial predicate on the drain's index.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.base import PGDialect

from leadquali.adapters.db_schema import metadata

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "migrations" / "versions" / "20260908_1100_b7e14c9d82a3_stripe_billing.py"

#: The head #35 chains off. Stated here as a literal so that re-pointing it when #36's
#: branch is stacked is a change somebody has to make on purpose, in two places, and not
#: something that drifts.
EXPECTED_DOWN_REVISION = "b6d2e94f7a13"

DIALECT = PGDialect()  # type: ignore[no-untyped-call]  # untyped in SQLAlchemy


class RecordingOp:
    """Stands in for ``alembic.op``, recording the DDL a migration asks for.

    Only the operations this migration uses are implemented. Anything else raising
    ``AttributeError`` is the right outcome: a migration that grew a new operation should
    fail this test until somebody has decided whether it needs checking too.
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, sa.Column[Any]]] = {}
        self.added: dict[tuple[str, str], sa.Column[Any]] = {}
        self.primary_keys: dict[str, tuple[str, ...]] = {}
        self.foreign_keys: dict[str, sa.ForeignKeyConstraint] = {}
        self.checks: dict[tuple[str, str], str] = {}
        self.uniques: dict[str, tuple[str, ...]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}

    def create_table(self, name: str, *elements: Any, **kwargs: Any) -> None:
        del kwargs
        self.tables[name] = {
            element.name: element for element in elements if isinstance(element, sa.Column)
        }
        for element in elements:
            if isinstance(element, sa.PrimaryKeyConstraint):
                self.primary_keys[name] = tuple(
                    str(column) if isinstance(column, str) else str(column.name)
                    for column in element._pending_colargs
                    if column is not None
                )
            elif isinstance(element, sa.ForeignKeyConstraint):
                self.foreign_keys[name] = element
            elif isinstance(element, sa.CheckConstraint):
                self.checks[(name, str(element.name))] = str(element.sqltext)
            elif isinstance(element, sa.UniqueConstraint):
                self.uniques[str(element.name)] = tuple(
                    str(column) if isinstance(column, str) else str(column.name)
                    for column in element._pending_colargs
                    if column is not None
                )

    def add_column(self, table: str, column: sa.Column[Any], **kwargs: Any) -> None:
        del kwargs
        self.added[(table, column.name)] = column

    def create_index(self, name: str, table: str, columns: list[str], **kwargs: Any) -> None:
        self.indexes[name] = {"table": table, "columns": tuple(columns), **kwargs}

    def create_unique_constraint(self, name: str, table: str, columns: list[str]) -> None:
        self.uniques[name] = tuple(columns)

    def f(self, name: str) -> str:
        """``op.f`` marks a name as already conventional; here it is the name itself."""
        return name


def load_migration() -> ModuleType:
    """Import the migration module from its path — ``migrations`` is not a package."""
    spec = importlib.util.spec_from_file_location("stripe_billing_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def applied() -> Iterator[RecordingOp]:
    """The DDL ``upgrade()`` asks for, recorded."""
    module = load_migration()
    recorder = RecordingOp()
    original = getattr(module, "op")  # noqa: B009  # a module attribute set at import
    setattr(module, "op", recorder)  # noqa: B010
    try:
        module.upgrade()
        yield recorder
    finally:
        setattr(module, "op", original)  # noqa: B010


def declared_type(column: sa.Column[Any]) -> str:
    return column.type.compile(dialect=DIALECT)


def model_type(table: str, column: str) -> str:
    return metadata.tables[table].c[column].type.compile(dialect=DIALECT)


def test_the_migration_was_actually_run(applied: RecordingOp) -> None:
    """Guards against the recorder silently intercepting nothing."""
    assert set(applied.tables) == {"stripe_events", "usage_reports"}
    assert len(applied.tables["stripe_events"]) == 9
    assert len(applied.tables["usage_reports"]) == 5


@pytest.mark.parametrize("column", sorted(metadata.tables["stripe_events"].c.keys()))
def test_every_stripe_events_column_matches_the_model(applied: RecordingOp, column: str) -> None:
    declared = applied.tables["stripe_events"][column]
    assert declared_type(declared) == model_type("stripe_events", column), column
    assert declared.nullable == metadata.tables["stripe_events"].c[column].nullable, column


@pytest.mark.parametrize("column", sorted(metadata.tables["usage_reports"].c.keys()))
def test_every_usage_reports_column_matches_the_model(applied: RecordingOp, column: str) -> None:
    declared = applied.tables["usage_reports"][column]
    assert declared_type(declared) == model_type("usage_reports", column), column
    assert declared.nullable == metadata.tables["usage_reports"].c[column].nullable, column


@pytest.mark.parametrize(
    "column", ["stripe_customer_id", "stripe_subscription_id", "dunning_until"]
)
def test_the_tenant_link_columns_match_the_model(applied: RecordingOp, column: str) -> None:
    declared = applied.added[("tenants", column)]
    assert declared_type(declared) == model_type("tenants", column), column
    assert declared.nullable is True, "every existing tenant comes out of this unlinked"


# ------------------------------------------------------------------- the decisions


def test_the_revision_chains_off_the_current_head() -> None:
    """#36 adds a migration off the same head, so one of the two will be re-pointed when
    the branches are stacked. Keeping the value here as well means that is a two-line
    change somebody makes deliberately rather than a silent branch in the chain."""
    module = load_migration()
    assert module.revision == "b7e14c9d82a3"
    assert module.down_revision == EXPECTED_DOWN_REVISION


def test_the_event_id_is_the_primary_key(applied: RecordingOp) -> None:
    """Webhook idempotency in the schema: Stripe's own id is the conflict target."""
    assert applied.primary_keys["stripe_events"] == ("event_id",)


def test_a_usage_report_is_keyed_by_the_tenant_and_the_day(applied: RecordingOp) -> None:
    """The constraint that makes double-billing impossible rather than merely unlikely."""
    assert applied.primary_keys["usage_reports"] == ("tenant_id", "usage_date")


def test_an_event_survives_the_deletion_of_its_tenant(applied: RecordingOp) -> None:
    """``SET NULL``: the row is the record that Stripe told us something, and that record
    outlives the account it was about."""
    assert applied.foreign_keys["stripe_events"].ondelete == "SET NULL"


def test_a_usage_report_blocks_the_deletion_of_its_tenant(applied: RecordingOp) -> None:
    """``RESTRICT``, unlike the derived rollup beside it. Nothing here is recomputable: it
    records something we told a payment processor about a customer's money, and a tenant
    deleted out from under it would let a re-created tenant report the same days again."""
    assert applied.foreign_keys["usage_reports"].ondelete == "RESTRICT"


def test_the_drain_index_is_partial(applied: RecordingOp) -> None:
    """An index over every webhook ever received grows forever while the part anybody
    reads stays the size of the backlog."""
    index = applied.indexes["ix_stripe_events_pending"]
    assert index["columns"] == ("received_at",)
    assert "status = 'pending'" in str(index["postgresql_where"])


def test_the_tenant_read_index_exists_because_the_tenant_column_is_nullable(
    applied: RecordingOp,
) -> None:
    """The invariant-4 exception costs an index: every read that *is* about a tenant has
    to filter on the column, and a nullable column with no index makes that a table scan."""
    index = applied.indexes["ix_stripe_events_tenant_id_received_at"]
    assert index["columns"] == ("tenant_id", "received_at")


def test_a_stripe_customer_belongs_to_exactly_one_tenant(applied: RecordingOp) -> None:
    """Cross-billing, made impossible by the database rather than by a code review."""
    assert applied.uniques["uq_tenants_stripe_customer_id"] == ("stripe_customer_id",)
    assert applied.uniques["uq_tenants_stripe_subscription_id"] == ("stripe_subscription_id",)


def test_the_external_id_carries_its_own_unique_constraint(applied: RecordingOp) -> None:
    """A second, cheap guard on the same fact the primary key states — and the one that
    would catch a bug in the *derivation*, where the primary key only catches one at the
    call site."""
    assert applied.uniques["uq_usage_reports_external_id"] == ("external_id",)


def test_the_status_vocabulary_matches_the_application(applied: RecordingOp) -> None:
    from leadquali.adapters.db_schema import STRIPE_EVENT_STATUSES
    from leadquali.app.billing import EventStatus

    condition = applied.checks[("stripe_events", "ck_stripe_events_status_known")]
    for status in STRIPE_EVENT_STATUSES:
        assert f"'{status}'" in condition
    assert set(STRIPE_EVENT_STATUSES) == {status.value for status in EventStatus}
