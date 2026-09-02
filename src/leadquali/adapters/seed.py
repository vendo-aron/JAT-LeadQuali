"""Seed the default internal tenant.

Issue #15 asks for "a seed script inserting the default internal tenant". This module is
the implementation; ``scripts/seed.py`` is the command-line wrapper documented in
``docs/local-database.md``, and the step that follows ``alembic upgrade head``.

It exists because ``tenants.icp_config`` has no server default (invariant 1: the rubric is
tenant configuration, and a tenant with no rubric is a tenant every config load rejects).
A freshly migrated database therefore has no tenant at all, and nothing can be ingested
until one exists. Seeding is not a convenience — it is the second half of "create the
database".

Cross-branch dependency (read this before changing the validation below)
-----------------------------------------------------------------------
The authoritative rubric model, ``TenantConfig``, and the file this script reads,
``tenants/default.json``, both belong to issue #8, which is not merged with this branch
yet. This module therefore **does not import** ``leadquali.domain.tenant_config`` — the
import would not resolve here and mypy would fail on it.

What it does instead is deliberately shallow: it reads the JSON document, checks that the
handful of keys the rubric is made of are present and of roughly the right kind, and stores
the document verbatim in ``icp_config``. It is a *smoke test for an obviously wrong file*,
not a schema validator, and it is written to stay that way. A second, independent copy of
the rubric's validation rules living here would drift away from ``TenantConfig`` within a
release, and the copy that disagreed would be the one that let a bad config into the
database.

Once #8 and #15 are both on the default branch, tighten this in one commit:
:func:`load_tenant_document` should call ``TenantConfig.model_validate(document)`` and let
the model be the only validator, and :data:`REQUIRED_CONFIG_KEYS` together with
:func:`_check_shape` should be deleted rather than kept in sync.
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

from sqlalchemy import Connection, create_engine, select

from leadquali.adapters.db_schema import Tenant, metadata
from leadquali.config import Settings

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TENANT_SLUG",
    "SeedError",
    "SeedResult",
    "load_tenant_document",
    "main",
    "seed_tenant",
    "tenant_id_for",
]

TENANT_ID_NAMESPACE: Final = uuid.UUID("c0ee1346-dad8-59ff-9326-afab90a0f177")
"""UUID5 namespace for tenant slugs. Fixed forever: it is what makes seeding idempotent."""

DEFAULT_TENANT_SLUG: Final = "default"
"""The slug of the internal tenant, matching ``tenant_id`` in ``tenants/default.json``."""

REQUIRED_CONFIG_KEYS: Final[tuple[str, ...]] = (
    "icp_description",
    "weights",
    "thresholds",
    "min_confidence",
    "routing_rules",
    "prompt_version",
)
"""The rubric keys #8's ``TenantConfig`` is built from. See the module docstring: this is a
presence check, not a schema — ``TenantConfig`` is the validator once it is available."""

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


def tenant_id_for(slug: str) -> uuid.UUID:
    """Return the stable tenant UUID for ``slug``.

    Derived rather than random so that seeding is genuinely idempotent: re-running the
    script updates the tenant it created last time instead of adding a second one, and the
    default tenant has the same id in every developer's database and in every environment,
    which makes a fixture or a support query portable.
    """
    return uuid.uuid5(TENANT_ID_NAMESPACE, slug)


def _check_shape(document: Mapping[str, Any]) -> None:
    """Reject a file that is obviously not a tenant config. See the module docstring."""
    missing = [key for key in ("name", *REQUIRED_CONFIG_KEYS) if key not in document]
    if missing:
        raise SeedError(f"tenant config is missing required key(s): {', '.join(sorted(missing))}")

    for key in ("name", "icp_description", "prompt_version"):
        value = document[key]
        if not isinstance(value, str) or not value.strip():
            raise SeedError(f"tenant config key {key!r} must be a non-empty string")

    for key in ("weights", "thresholds", "routing_rules"):
        if not isinstance(document[key], dict):
            raise SeedError(f"tenant config key {key!r} must be an object")

    confidence = document["min_confidence"]
    # bool is an int in Python, and `True` is not a confidence.
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise SeedError("tenant config key 'min_confidence' must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise SeedError(
            f"tenant config key 'min_confidence' must be between 0 and 1, got {confidence}"
        )


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

    _check_shape(document)
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
        connection.execute(table.insert().values(id=tenant_id, name=name, icp_config=config))
        return SeedResult(tenant_id=tenant_id, created=True)

    connection.execute(
        table.update().where(table.c.id == tenant_id).values(name=name, icp_config=config)
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
