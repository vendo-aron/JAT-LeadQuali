"""The real schema, on in-memory SQLite, so adapter SQL can be *run* in the default suite.

Why this exists
---------------

``tests/integration`` needs Postgres and skips without Docker, which is most of the time.
That is fine for what genuinely needs Postgres — a query plan, a ``jsonb`` operator, an
advisory lock — and it was quietly disastrous for everything else: three separate mutations
each deleting a tenant predicate from ``store_admin.py`` left all 2184 unit tests green,
because the only tests that would have executed those statements were skipped, and the
unit-level guard grepped the source for ``tenant_uuid(tenant_slug)`` — which survives every
one of those mutations, since it asserts a variable is *computed* and not that it is
*used*.

``tests/unit/test_unit_of_work.py`` already proved that SQLAlchemy over ``sqlite://`` is a
real engine running real statements against real transactions. This module extends that to
the real tables, so a tenant predicate is checked by **seeding two tenants and finding that
the other one's rows never come back** — which no amount of source-reading can fake.

What is faithful, and what is not
---------------------------------

The tables are **derived from** :data:`leadquali.adapters.db_schema.Base.metadata` by
``Table.to_metadata``, not retyped. So the column names, the nullability, the keys and the
foreign keys are the shipping schema's: rename a column and every test built on this fails,
which is the property that makes it worth doing at all.

Three Postgres-only things are translated rather than reproduced, and each is something the
statements under test do not depend on:

* ``jsonb`` becomes ``JSON``, ``uuid`` becomes ``CHAR(36)``, ``timestamptz`` becomes
  ``TIMESTAMP``. SQLAlchemy's own type processors do the round-tripping either way.
* ``CHECK`` constraints are dropped: ``slug ~ '...'`` is a POSIX regex SQLite cannot parse.
  The vocabularies and ranges they enforce are asserted against the real metadata in
  ``tests/unit/test_db_schema.py`` and exercised for real in ``tests/integration``.
* The server defaults are rewritten — ``gen_random_uuid()`` to ``lower(hex(randomblob(16)))``
  (which produces exactly the 32-character hex form SQLAlchemy's ``UUID`` type stores),
  ``now()`` to ``CURRENT_TIMESTAMP``, and a Postgres cast such as ``'[]'::jsonb`` to the
  literal in front of it. Literal defaults are left alone.

**This is not a substitute for the integration suite.** It cannot check a query plan, it
does not enforce the CHECK constraints, and SQLite's ``ON CONFLICT DO NOTHING`` is not
Postgres's ``ON CONFLICT ON CONSTRAINT ... DO NOTHING`` — close enough that idempotency is
observable here, not close enough that it proves the named constraint exists. What it is
for is the class of defect that was getting through: a predicate, a filter or a clause that
was deleted and that nothing in the default suite ever executed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    Engine,
    FetchedValue,
    MetaData,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import TIMESTAMP as PG_TIMESTAMP
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Base

__all__ = ["shadow_metadata", "sqlite_sessions"]


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_: JSONB, compiler: object, **kw: object) -> str:
    """``jsonb`` has no SQLite equivalent; ``JSON`` round-trips through the same processor."""
    del type_, compiler, kw
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_as_char(type_: UUID[uuid.UUID], compiler: object, **kw: object) -> str:
    """SQLAlchemy stores a UUID as its 32-character hex form when the backend has no type."""
    del type_, compiler, kw
    return "CHAR(36)"


@compiles(PG_TIMESTAMP, "sqlite")
def _timestamptz_as_timestamp(type_: PG_TIMESTAMP, compiler: object, **kw: object) -> str:
    """SQLite has no timezone-aware type. Every value written here is already UTC."""
    del type_, compiler, kw
    return "TIMESTAMP"


#: What each Postgres server default becomes. ``gen_random_uuid()``'s replacement produces
#: exactly the form SQLAlchemy's ``UUID`` type reads back, so a server-generated primary key
#: still comes out of a ``RETURNING`` clause as a ``uuid.UUID``.
_SERVER_DEFAULTS: dict[str, str] = {
    "gen_random_uuid()": "lower(hex(randomblob(16)))",
    "now()": "CURRENT_TIMESTAMP",
}


def shadow_metadata() -> MetaData:
    """The shipping schema's tables, translated for SQLite.

    Derived from :data:`~leadquali.adapters.db_schema.Base.metadata` rather than retyped,
    so a column renamed in the real schema breaks every test built on this.
    """
    shadow = MetaData()
    for table in Base.metadata.sorted_tables:
        copy: Table = table.to_metadata(shadow)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.discard(constraint)
        for column in copy.columns:
            column.server_default = _translated_default(column.server_default)
    return shadow


def _translated_default(server_default: FetchedValue | None) -> FetchedValue | None:
    """Rewrite a Postgres server default, or leave a literal one alone."""
    argument = getattr(server_default, "arg", None)
    if argument is None:
        return None
    rendered = str(argument)
    replacement = _SERVER_DEFAULTS.get(rendered)
    if replacement is not None:
        return DefaultClause(text(replacement))
    if "::" in rendered:
        # A Postgres cast, e.g. ``'[]'::jsonb``. SQLite has no cast syntax at all and the
        # literal in front of it is what the column actually defaults to.
        return DefaultClause(text(rendered.split("::", 1)[0]))
    # A literal such as ``'active'`` or ``0``: SQLite parses those unchanged.
    return server_default


@contextmanager
def sqlite_sessions() -> Iterator[sessionmaker[Session]]:
    """A session factory over one in-memory database with the shadow schema created.

    Yields the factory the adapters take, so the class under test is constructed exactly
    as production constructs it — ``PostgresAdminQueryStore(sessions)`` and nothing else.
    """
    engine: Engine = create_engine("sqlite://")
    # Without this SQLite does not enforce the composite (tenant_id, lead_id) foreign keys,
    # and a test could seed a row that the real schema would have refused.
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys = ON"))
        connection.commit()
    shadow_metadata().create_all(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()
