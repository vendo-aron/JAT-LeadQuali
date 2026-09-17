"""The migration and the models, compared without a database.

``tests/integration/test_migrations.py`` already proves the two agree — by running
``alembic upgrade head`` and asking autogenerate for a diff — and that test is the real
one. It is also skipped everywhere there is no Postgres, which is CI and every developer
who has not started Docker, so a migration that declares ``sa.Integer()`` where the model
says ``BigInteger`` can be committed, reviewed and merged with a green suite. It would
then silently truncate a busy tenant's token counters at 2^31 in production and nowhere
else.

This file closes that window for the tables #33 added, cheaply: it runs the migration's
``upgrade()`` against a recording stand-in for ``alembic.op``, and compares the column
types it declares against ``db_schema`` column by column, compiled for Postgres so that
``Numeric(14, 6)`` and ``Numeric(12, 6)`` are different answers.

It deliberately does **not** try to be a general autogenerate substitute: it checks the
columns this issue introduced, which is what it can do honestly without a server.
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
MIGRATION = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "20260905_1000_a3f5c2b81d47_usage_rollup_and_tenant_quota.py"
)

#: Compiled against the same dialect the migration will run on, so the comparison is of
#: the DDL Postgres would receive rather than of two Python objects that happen to differ.
DIALECT = PGDialect()  # type: ignore[no-untyped-call]  # untyped in SQLAlchemy


class RecordingOp:
    """Stands in for ``alembic.op``, recording the DDL a migration asks for.

    Only the four operations this migration uses are implemented. Anything else raising
    ``AttributeError`` is the right outcome: a migration that grew a new operation should
    fail this test until somebody has decided whether it needs checking too.
    """

    def __init__(self) -> None:
        #: ``table name -> {column name -> Column}``, from ``create_table``.
        self.tables: dict[str, dict[str, sa.Column[Any]]] = {}
        #: ``(table, column name) -> Column``, from ``add_column``.
        self.added: dict[tuple[str, str], sa.Column[Any]] = {}
        #: ``(table, constraint name) -> condition``.
        self.checks: dict[tuple[str, str], str] = {}
        #: ``table -> the primary key columns it declares``.
        self.primary_keys: dict[str, tuple[str, ...]] = {}

    def create_table(self, name: str, *elements: Any, **kwargs: Any) -> None:
        del kwargs
        self.tables[name] = {
            element.name: element for element in elements if isinstance(element, sa.Column)
        }
        for element in elements:
            if isinstance(element, sa.PrimaryKeyConstraint):
                # Declared as a table-level constraint rather than `primary_key=True` on
                # the columns, which is the only way to say "composite" in a migration.
                # The column names the constraint was built with, before it is attached
                # to a Table and can resolve them into Column objects.
                self.primary_keys[name] = tuple(
                    str(column) if isinstance(column, str) else str(column.name)
                    for column in element._pending_colargs
                    if column is not None
                )

    def add_column(self, table: str, column: sa.Column[Any], **kwargs: Any) -> None:
        del kwargs
        self.added[(table, column.name)] = column

    def create_check_constraint(self, name: str, table: str, condition: str, **kw: Any) -> None:
        del kw
        self.checks[(table, name)] = condition

    def f(self, name: str) -> str:
        """``op.f`` marks a name as already conventional; here it is the name itself."""
        return name


def load_migration() -> ModuleType:
    """Import the migration module from its path — ``migrations`` is not a package."""
    spec = importlib.util.spec_from_file_location("usage_rollup_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def applied() -> Iterator[RecordingOp]:
    """The DDL ``upgrade()`` asks for, recorded.

    The migration holds a reference to the ``alembic.op`` module object (``from alembic
    import op``), so replacing that name on the module it imported is what puts the
    recorder in front of it.
    """
    module = load_migration()
    recorder = RecordingOp()
    original = getattr(module, "op")  # noqa: B009  # mypy cannot see a module attribute set at import
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
    assert "usage_daily" in applied.tables
    assert len(applied.tables["usage_daily"]) == 12


@pytest.mark.parametrize("column", sorted(metadata.tables["usage_daily"].c.keys()))
def test_every_usage_daily_column_matches_the_model(applied: RecordingOp, column: str) -> None:
    """Type for type, including precision and scale.

    The case this exists for: narrowing ``input_tokens`` to ``sa.Integer()`` in the
    migration while the model says ``BigInteger``. Every unit test passes, the application
    reads and writes happily, and a tenant's token counter wraps at two billion.
    """
    declared = applied.tables["usage_daily"][column]
    assert declared_type(declared) == model_type("usage_daily", column), column
    assert declared.nullable == metadata.tables["usage_daily"].c[column].nullable, column


def test_the_rollup_primary_key_is_the_natural_key(applied: RecordingOp) -> None:
    """A surrogate id here would let two rows for one tenant-day coexist while every
    billing read summed both — and it is also the upsert's conflict target."""
    declared = applied.primary_keys["usage_daily"]
    assert declared == ("tenant_id", "usage_date")
    assert declared == tuple(column.name for column in metadata.tables["usage_daily"].primary_key)


@pytest.mark.parametrize("column", ["monthly_lead_quota", "quota_alert_fraction"])
def test_the_quota_columns_match_the_model(applied: RecordingOp, column: str) -> None:
    declared = applied.added[("tenants", column)]
    assert declared_type(declared) == model_type("tenants", column), column
    assert declared.nullable == metadata.tables["tenants"].c[column].nullable, column


def test_the_quota_bounds_are_enforced_by_the_migration_too(applied: RecordingOp) -> None:
    """The CHECKs exist in the models; a database only has the ones the migration made."""
    assert (
        "monthly_lead_quota IS NULL OR monthly_lead_quota > 0"
        in applied.checks[("tenants", "ck_tenants_monthly_lead_quota_is_positive")]
    )
    assert (
        "quota_alert_fraction > 0 AND quota_alert_fraction <= 1"
        in applied.checks[("tenants", "ck_tenants_quota_alert_fraction_is_a_fraction")]
    )


def test_the_revision_chains_off_the_previous_head() -> None:
    """A migration whose ``down_revision`` drifts is a branch nobody asked for."""
    module = load_migration()
    assert module.revision == "a3f5c2b81d47"
    assert module.down_revision == "7c1f2ad50b93"
