"""Seeding the default tenant against a real Postgres.

``tests/unit/test_seed.py`` covers reading and checking the config file. These cover the
half that needs a server: that the row lands, that it satisfies the NOT NULL rubric the
schema now insists on, and that running the script twice leaves one tenant rather than two.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, func, select

from leadquali.adapters.db_schema import metadata
from leadquali.adapters.seed import seed_tenant, tenant_id_for
from tests.integration.conftest import database_url_in_environment
from tests.unit.test_seed import A_TENANT_DOCUMENT

pytestmark = pytest.mark.integration

TENANTS = metadata.tables["tenants"]


def _count(db: Connection) -> int:
    total: int = db.execute(select(func.count()).select_from(TENANTS)).scalar_one()
    return total


def test_seeding_creates_the_default_tenant_with_its_rubric(db: Connection) -> None:
    """The step that follows ``alembic upgrade head``: without it there is no tenant, and
    ``icp_config`` has no default, so nothing can be ingested at all."""
    result = seed_tenant(db, A_TENANT_DOCUMENT)

    assert result.created is True
    assert result.tenant_id == tenant_id_for("default")

    row = db.execute(
        select(TENANTS.c.name, TENANTS.c.icp_config, TENANTS.c.status).where(
            TENANTS.c.id == result.tenant_id
        )
    ).one()
    assert row.name == A_TENANT_DOCUMENT["name"]
    assert row.status == "active"
    # Stored whole, so #16's loader can hand the column straight to TenantConfig.
    assert row.icp_config == A_TENANT_DOCUMENT
    assert row.icp_config["thresholds"]["hot"] == 80.0


def test_seeding_twice_updates_the_same_tenant(db: Connection) -> None:
    """Idempotent, because a seed script is run again every time someone rebuilds a local
    database — and a second default tenant would be a silent multi-tenancy bug."""
    first = seed_tenant(db, A_TENANT_DOCUMENT)
    assert _count(db) == 1

    revised: dict[str, Any] = {
        **A_TENANT_DOCUMENT,
        "name": "JAT-LeadQuali (renamed)",
        "min_confidence": 0.75,
    }
    second = seed_tenant(db, revised)

    assert second.created is False
    assert second.tenant_id == first.tenant_id
    assert _count(db) == 1

    row = db.execute(
        select(TENANTS.c.name, TENANTS.c.icp_config).where(TENANTS.c.id == first.tenant_id)
    ).one()
    assert row.name == "JAT-LeadQuali (renamed)"
    assert row.icp_config["min_confidence"] == 0.75


def test_a_second_slug_is_a_second_tenant(db: Connection) -> None:
    """The script is not hardcoded to one customer; the slug is what identifies the row."""
    seed_tenant(db, A_TENANT_DOCUMENT)
    seed_tenant(db, {**A_TENANT_DOCUMENT, "tenant_id": "acme", "name": "Acme"})

    assert _count(db) == 2


def test_the_documented_command_seeds_a_freshly_migrated_database(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    """End to end through ``main()``, the way docs/local-database.md tells a developer to
    run it — including reading the database URL from the environment rather than a flag."""
    from leadquali.adapters.seed import main

    config_path = tmp_path / "default.json"
    config_path.write_text(json.dumps(A_TENANT_DOCUMENT), encoding="utf-8")

    with database_url_in_environment(migrated_engine.url):
        exit_code = main(["--config", str(config_path)])
    assert exit_code == 0

    with migrated_engine.begin() as connection:
        try:
            tenant_id = tenant_id_for("default")
            stored = connection.execute(
                select(TENANTS.c.icp_config).where(TENANTS.c.id == tenant_id)
            ).scalar_one()
            assert stored == A_TENANT_DOCUMENT
        finally:
            # This test commits, unlike the rolled-back `db` fixture, so it cleans up after
            # itself rather than leaving a row the other tests would count.
            connection.execute(TENANTS.delete().where(TENANTS.c.id == tenant_id))
