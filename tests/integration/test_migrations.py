"""Migrations against a real Postgres.

The offline tests in ``tests/unit/test_db_schema.py`` prove the *models* say the right
thing. These prove the *migration* says the same thing, and that Postgres agrees — the two
can only be checked apart from each other by connecting to a real server.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine import URL

from leadquali.adapters.db_schema import metadata
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {"tenants", "leads", "assessments", "routing_events", "feedback"}


def test_upgrade_head_creates_every_planned_table(migrated_engine: Engine) -> None:
    present = set(inspect(migrated_engine).get_table_names())
    assert present >= EXPECTED_TABLES
    # Alembic's bookkeeping table, and nothing else the models did not ask for.
    assert present - EXPECTED_TABLES == {"alembic_version"}


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_column_the_models_declare_exists_in_the_database(
    migrated_engine: Engine, table_name: str
) -> None:
    """Column-for-column, including nullability — not just "the table is there"."""
    inspector = inspect(migrated_engine)
    actual = {column["name"]: column for column in inspector.get_columns(table_name)}
    expected = metadata.tables[table_name].c

    assert set(actual) == {column.name for column in expected}
    for column in expected:
        assert actual[column.name]["nullable"] == column.nullable, (
            f"{table_name}.{column.name} nullability differs from the model"
        )


def test_autogenerate_against_head_produces_an_empty_diff(migrated_engine: Engine) -> None:
    """The check that stops the migration and the models from drifting apart.

    A migration that no longer describes the models is worse than no migration: the
    application reads columns the database does not have, and only production finds out.
    """
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": False},
        )
        diff = compare_metadata(context, metadata)
    assert diff == [], f"models and migrations have drifted: {diff}"


def test_unique_and_foreign_key_constraints_survive_the_migration(migrated_engine: Engine) -> None:
    inspector = inspect(migrated_engine)

    unique_columns = {tuple(c["column_names"]) for c in inspector.get_unique_constraints("leads")}
    # The idempotency key...
    assert ("tenant_id", "submission_id") in unique_columns
    # ...and the target the composite ownership keys below need to exist at all.
    assert ("tenant_id", "id") in unique_columns

    lead_fks = {
        (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for fk in inspector.get_foreign_keys("leads")
    }
    assert (("tenant_id",), "tenants", ("id",)) in lead_fks, "leads lost its tenant FK"

    for table_name in EXPECTED_TABLES - {"tenants", "leads"}:
        referred = {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table_name)
        }
        # One composite key, not two independent ones — Postgres, not just the models,
        # has to be the thing that refuses a row whose tenant does not own its lead.
        assert (
            ("tenant_id", "lead_id"),
            "leads",
            ("tenant_id", "id"),
        ) in referred, f"{table_name} lost its composite tenant/lead ownership FK"


def test_the_deletion_policy_survives_the_migration(migrated_engine: Engine) -> None:
    """CASCADE from a lead, RESTRICT from a tenant — asserted against the server, because
    an ``ondelete`` that only exists in the models protects nothing."""
    inspector = inspect(migrated_engine)

    (tenant_fk,) = [
        fk for fk in inspector.get_foreign_keys("leads") if fk["referred_table"] == "tenants"
    ]
    assert tenant_fk["options"].get("ondelete") == "RESTRICT"

    for table_name in EXPECTED_TABLES - {"tenants", "leads"}:
        (lead_fk,) = inspector.get_foreign_keys(table_name)
        assert lead_fk["options"].get("ondelete") == "CASCADE"


def test_downgrade_base_then_upgrade_head_round_trips(_database_url: URL) -> None:
    """A downgrade nobody has run is a downgrade that does not work.

    This runs in its own throwaway database so that dropping every table cannot disturb the
    session-scoped one the other tests share.
    """
    name = f"{_database_url.database}_roundtrip_{uuid.uuid4().hex[:8]}"
    config = alembic_config()
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(config, "head")
        command.downgrade(config, "base")

        engine = create_engine(url)
        try:
            after_downgrade = set(inspect(engine).get_table_names())
            # Every table this project owns is gone; only Alembic's own ledger remains.
            assert after_downgrade & EXPECTED_TABLES == set()

            command.upgrade(config, "head")
            assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES

            # And the rebuilt schema still matches the models exactly.
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={"compare_type": True, "compare_server_default": False},
                )
                assert compare_metadata(context, metadata) == []
        finally:
            engine.dispose()
