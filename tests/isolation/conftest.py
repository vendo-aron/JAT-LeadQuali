"""Database fixtures for the isolation suite's ``integration`` half.

The same rule as ``tests/integration/conftest.py``: these tests **skip, they never fail**,
when there is no Postgres. Reachability is decided by that package's ``_database_url``,
which is re-exported here so there is one definition of "is there a database, and how do we
say so" rather than two that can disagree.

The throwaway database has a name of its own (``<dbname>_isolation``) rather than reusing
``<dbname>_test``. Both are session-scoped, both create and drop with ``WITH (FORCE)``, and
a shared name would mean one suite dropping the database the other is connected to
depending on collection order — which fails in a way that looks like a tenancy bug and is
not one.

Every test runs inside an outer transaction that is rolled back, with the session factory
joined to it as a savepoint, so the adapters' own ``commit`` calls behave exactly as they do
in production while leaving nothing behind.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.conftest import (
    _database_url,
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

__all__ = ["_database_url", "isolation_db", "isolation_engine", "sessions"]


@pytest.fixture(scope="session")
def isolation_engine(_database_url: URL) -> Iterator[Engine]:
    """A throwaway database with ``alembic upgrade head`` applied, dropped afterwards."""
    name = f"{_database_url.database}_isolation"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def isolation_db(isolation_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with isolation_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(isolation_db: Connection) -> sessionmaker[Session]:
    """A session factory the adapters can be built over, bound to the rolled-back connection.

    ``join_transaction_mode="create_savepoint"`` is what lets the adapters commit for real —
    which they all do, once per method — inside a transaction the fixture will discard. The
    alternative, making the adapters not commit, would mean testing something other than
    the code that ships.
    """
    return sessionmaker(
        bind=isolation_db, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
