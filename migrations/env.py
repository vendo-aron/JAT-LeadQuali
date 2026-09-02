"""Alembic runtime environment.

The database URL is **never** written here or in ``alembic.ini``. It is read from
:class:`leadquali.config.Settings`, i.e. from ``DATABASE_URL`` in the environment (or a
local ``.env``), so there is exactly one way to point a process at a database and no
connection string can be committed by accident. ``alembic upgrade head`` against the wrong
database is then an environment mistake, visible in the environment, rather than a file
someone forgot to edit.

The autogenerate target is :data:`leadquali.adapters.db_schema.metadata`. That is what makes
``alembic revision --autogenerate`` produce an empty diff when the migrations and the models
agree — the check that keeps a hand-edited migration from drifting away from the schema the
application actually reads and writes.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from leadquali.adapters.db_schema import metadata
from leadquali.config import Settings

config = context.config

# Honour the logging setup in alembic.ini so a migration run is legible in CI output.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Every table this project owns lives in one MetaData; nothing else is migrated.
target_metadata = metadata

# `alembic upgrade head` must not silently target a database the operator did not choose.
# Settings.require_database_url() raises a named error when DATABASE_URL is unset.
_DATABASE_URL = Settings().require_database_url()


def _configure(**kwargs: Any) -> None:
    """Apply the options shared by the offline and online paths.

    ``compare_type`` is on so that widening a column in the models is reported as a diff
    rather than silently ignored. ``compare_server_default`` is deliberately left off:
    Postgres rewrites defaults on read (``'active'`` comes back as
    ``'active'::character varying``), which makes it a generator of phantom diffs rather
    than a safety net.
    """
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
        # Constraint and index names come from the metadata's naming convention, so
        # autogenerate can render — and downgrade can drop — every one of them by name.
        render_as_batch=False,
        include_schemas=False,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``).

    Used to hand a reviewable script to a DBA for a production change window.
    """
    _configure(url=_DATABASE_URL, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run the migrations in a transaction.

    Postgres has transactional DDL, so a failing migration rolls back completely instead of
    leaving the schema halfway between two revisions.
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _DATABASE_URL
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
