"""Seed the default internal tenant.

Issue #15 asks for "a seed script inserting the default internal tenant". This module is
the implementation; ``scripts/seed.py`` is the command-line wrapper documented in
``docs/local-database.md``, and the step that follows ``alembic upgrade head``.

It exists because ``tenants.icp_config`` has no server default (invariant 1: the rubric is
tenant configuration, and a tenant with no rubric is a tenant every config load rejects).
A freshly migrated database therefore has no tenant at all, and nothing can be ingested
until one exists. Seeding is not a convenience — it is the second half of "create the
database".

One validator, not two
----------------------
:func:`load_tenant_document` validates with ``TenantConfig`` and with nothing else. It
used to do a shallow key-presence check instead, because #8 (which owns ``TenantConfig``
and ``tenants/default.json``) was not on this branch; that check is gone, along with the
``REQUIRED_CONFIG_KEYS`` list it read from. A second, independent copy of the rubric's
validation rules living here would drift away from ``TenantConfig`` within a release, and
the copy that disagreed would be the one that let a bad config into the database.

So a config that would break scoring — a gap between two tier bands, a weight for a
dimension the model does not score, a tier with no routing rule — is refused by the seed
script for exactly the same reason and with exactly the same message as it would be
refused at load time by the worker. The same rule applies to the admin path
(``leadquali.app.tenants.TenantService``), which validates through the same model.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Connection, create_engine, func, select

from leadquali.adapters.db_schema import Tenant, metadata
from leadquali.app.tenant_ids import TENANT_ID_NAMESPACE, tenant_id_for
from leadquali.config import Settings
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TENANT_SLUG",
    "TENANT_ID_NAMESPACE",
    "SeedError",
    "SeedResult",
    "load_tenant_document",
    "main",
    "seed_tenant",
    "tenant_id_for",
]

DEFAULT_TENANT_SLUG: Final = "default"
"""The slug of the internal tenant, matching ``tenant_id`` in ``tenants/default.json``."""

DEFAULT_CONFIG_PATH: Final = Path(__file__).resolve().parents[3] / "tenants" / "default.json"
"""``<repo>/tenants/default.json``, for running the script from a checkout. Only a default;
``--config`` overrides it, and the file ships with #8."""


class SeedError(Exception):
    """A seed run that cannot proceed, with an operator-readable reason."""


@dataclass(frozen=True)
class SeedResult:
    """Outcome of a seed run."""

    tenant_id: uuid.UUID
    """The tenant row's primary key."""

    created: bool
    """True if the row was inserted, False if an existing row was updated in place."""


def load_tenant_document(path: Path) -> dict[str, Any]:
    """Read and structurally check the tenant config at ``path``.

    Raises :class:`SeedError` — never a bare ``FileNotFoundError`` or ``JSONDecodeError`` —
    so the operator is told which file was wrong and what was wrong with it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SeedError(
            f"tenant config file not found: {path}\n"
            "This file ships with issue #8 (TenantConfig). If it is not in your checkout "
            "yet, pass an explicit path with --config."
        ) from exc
    except OSError as exc:
        raise SeedError(f"tenant config file could not be read: {path} ({exc})") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SeedError(f"tenant config file is not valid JSON: {path} ({exc})") from exc

    if not isinstance(document, dict):
        raise SeedError(
            f"tenant config file must contain a JSON object, got {type(document).__name__}: {path}"
        )

    try:
        TenantConfig.from_dict(document)
    except TenantConfigError as exc:
        raise SeedError(f"tenant config file is not a valid rubric: {path} ({exc})") from exc
    return document


def seed_tenant(connection: Connection, document: Mapping[str, Any]) -> SeedResult:
    """Upsert the tenant described by ``document`` and return what happened.

    The whole document is stored in ``icp_config``, including its ``tenant_id`` and ``name``
    keys, so that #16's config loader can hand the column straight to ``TenantConfig``
    without reassembling it from parts.
    """
    slug = str(document.get("tenant_id", DEFAULT_TENANT_SLUG))
    tenant_id = tenant_id_for(slug)
    name = str(document["name"])
    config = dict(document)

    # Via `metadata` rather than `Tenant.__table__` so it types as a Table; they are the
    # same object, which tests/unit/test_db_schema.py asserts.
    table = metadata.tables[Tenant.__tablename__]
    existing = connection.execute(
        select(table.c.id).where(table.c.id == tenant_id)
    ).scalar_one_or_none()

    if existing is None:
        connection.execute(
            table.insert().values(id=tenant_id, slug=slug, name=name, icp_config=config)
        )
        return SeedResult(tenant_id=tenant_id, created=True)

    # `slug` is written on the update path too: the id is derived from the slug, so the two
    # can never disagree here, and a row seeded before the column existed is repaired by the
    # next seed run rather than staying half-migrated.
    connection.execute(
        table.update()
        .where(table.c.id == tenant_id)
        .values(slug=slug, name=name, icp_config=config, updated_at=func.now())
    )
    return SeedResult(tenant_id=tenant_id, created=False)


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="seed.py",
        description="Insert or update the default internal tenant and its rubric.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Tenant config JSON to seed from (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL. Defaults to DATABASE_URL, the same source Alembic reads.",
    )
    args = parser.parse_args(argv)

    try:
        document = load_tenant_document(args.config)
        # Settings.require_database_url() raises RuntimeError with its own clear message.
        url = args.database_url or Settings().require_database_url()
    except (SeedError, RuntimeError) as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            result = seed_tenant(connection, document)
    finally:
        engine.dispose()

    verb = "created" if result.created else "updated"
    print(f"seed: {verb} tenant {result.tenant_id} ({document['name']})")
    return 0
