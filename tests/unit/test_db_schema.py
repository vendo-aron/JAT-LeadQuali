"""Offline schema invariants, asserted from SQLAlchemy metadata alone.

These run without Docker and without a database. They exist so that the default test
suite still proves the product invariants that the schema is responsible for: multi-tenancy
on every table (CLAUDE.md invariant 4), the idempotency key on ``leads``, and the absence of
any raw-email column (CLAUDE.md invariant 5).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from leadquali.adapters.db_schema import (
    Assessment,
    Base,
    Feedback,
    Lead,
    RoutingEvent,
    Tenant,
)

EXPECTED_TABLES = {"tenants", "leads", "assessments", "routing_events", "feedback"}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def test_metadata_declares_exactly_the_five_planned_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_model_classes_map_to_the_expected_table_names() -> None:
    """The names #16 imports. Renaming one of these is a breaking change for that issue."""
    for model, table_name in (
        (Tenant, "tenants"),
        (Lead, "leads"),
        (Assessment, "assessments"),
        (RoutingEvent, "routing_events"),
        (Feedback, "feedback"),
    ):
        assert model.__tablename__ == table_name
        # The class and the metadata entry are one object, so a repository written against
        # either sees the same columns.
        assert model.__table__ is _table(table_name)


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_carries_a_tenant_id(table_name: str) -> None:
    """Invariant 4: ``tenant_id`` on every table, from the first migration."""
    table = _table(table_name)
    if table_name == "tenants":
        # The tenant table is its own tenant scope: its primary key *is* the tenant id.
        assert "id" in table.c
        return
    column = table.c["tenant_id"]
    assert not column.nullable, f"{table_name}.tenant_id must be NOT NULL"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES - {"tenants"}))
def test_every_tenant_id_has_a_foreign_key_to_tenants(table_name: str) -> None:
    targets = {fk.column.table.name for fk in _table(table_name).c["tenant_id"].foreign_keys}
    assert targets == {"tenants"}


def test_leads_has_the_tenant_scoped_submission_idempotency_key() -> None:
    """The unique key that stops SQS at-least-once delivery from emailing sales twice."""
    unique_column_sets = {
        tuple(c.name for c in constraint.columns)
        for constraint in _table("leads").constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "submission_id") in unique_column_sets


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_no_table_stores_a_raw_contact_email(table_name: str) -> None:
    """Invariant 5: only ``contact_email_hash``; raw PII lives solely in ``raw_payload``."""
    for column in _table(table_name).c:
        assert column.name != "contact_email"
        assert "email" not in column.name or column.name == "contact_email_hash"


def test_leads_hashes_the_contact_email() -> None:
    assert "contact_email_hash" in _table("leads").c
    assert "raw_payload" in _table("leads").c
    assert isinstance(_table("leads").c["raw_payload"].type, JSONB)


def test_assessments_records_everything_the_feedback_loop_and_billing_need() -> None:
    columns = set(_table("assessments").c.keys())
    assert {
        "model_id",
        "prompt_version",
        "effort",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "latency_ms",
        "confidence",
        "dimension_scores",
        "extracted",
        "missing_information",
        "reasoning",
        "tier",
        "total_score",
    } <= columns


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("tenants", "created_at"),
        ("leads", "received_at"),
        ("leads", "created_at"),
        ("assessments", "created_at"),
        ("routing_events", "created_at"),
        ("feedback", "created_at"),
    ],
)
def test_timestamps_are_timezone_aware_with_a_server_default(
    table_name: str, column_name: str
) -> None:
    column = _table(table_name).c[column_name]
    assert isinstance(column.type, TIMESTAMP)
    assert column.type.timezone is True, f"{table_name}.{column_name} must be timestamptz"
    assert column.server_default is not None


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_primary_keys_are_uuids_generated_by_the_server(table_name: str) -> None:
    primary_key = list(_table(table_name).primary_key.columns)
    assert [c.name for c in primary_key] == ["id"]
    assert primary_key[0].server_default is not None


def test_indexes_cover_the_queries_the_product_actually_runs() -> None:
    index_columns = {
        table.name: {tuple(c.name for c in index.columns) for index in table.indexes}
        for table in Base.metadata.tables.values()
    }
    # Per-tenant recent-leads listing.
    assert ("tenant_id", "received_at") in index_columns["leads"]
    # "Every lead scored hot last month the rep marked bad, grouped by industry".
    assert ("tenant_id", "tier", "created_at") in index_columns["assessments"]
    assert ("tenant_id", "verdict", "created_at") in index_columns["feedback"]
    # Join paths from a lead to its history.
    assert ("lead_id",) in index_columns["feedback"]
    assert ("lead_id",) in index_columns["routing_events"]
    assert ("lead_id",) in index_columns["assessments"]


def test_child_rows_are_deleted_with_their_lead() -> None:
    for table_name in ("assessments", "routing_events", "feedback"):
        for fk in _table(table_name).c["lead_id"].foreign_keys:
            assert fk.ondelete == "CASCADE"


def test_constraint_names_are_deterministic() -> None:
    """A naming convention is what lets Alembic autogenerate and downgrade stay stable."""
    convention = Base.metadata.naming_convention
    assert {"ix", "uq", "ck", "fk", "pk"} <= set(convention)
