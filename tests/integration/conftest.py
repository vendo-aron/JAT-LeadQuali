"""Fixtures for the Postgres integration suite.

Everything here is built around one rule: **these tests skip, they never fail, when there
is no database.** The default suite and CI have no Postgres, and a red bar there would say
"the schema is broken" when it only means "Docker is not running". Reachability is decided
once per session by :func:`_database_url`, and every test in this package depends on it.

The suite also refuses to touch the database named in ``DATABASE_URL``. It creates a
throwaway ``<dbname>_test`` alongside it, migrates that, and drops it afterwards — because
``alembic downgrade base`` is one of the things under test, and running that against a
developer's working database would delete their data.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from leadquali.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Connecting to the database you are about to drop and recreate is not possible, so the
# admin connection goes to the always-present maintenance database instead.
MAINTENANCE_DATABASE = "postgres"


def alembic_config() -> Config:
    """An Alembic config with absolute paths, so pytest's cwd cannot change the outcome."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture(scope="session")
def _database_url() -> URL:
    """The configured database URL, or skip the whole package.

    Two distinct reasons to skip, reported separately because they need different fixes:
    ``DATABASE_URL`` was never set, or it was set and nothing is listening.
    """
    configured = Settings().database_url
    if configured is None:
        pytest.skip(
            "DATABASE_URL is not set; start Postgres with `docker compose up -d` and "
            "export it (see docs/local-database.md)."
        )
    url = make_url(configured)
    admin_url = url.set(database=MAINTENANCE_DATABASE)
    engine = create_engine(admin_url, connect_args={"connect_timeout": 3})
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.skip(f"Postgres at {url.render_as_string(hide_password=True)} is unreachable: {exc}")
    finally:
        engine.dispose()
    return url


@contextmanager
def temporary_database(admin_url: URL, name: str) -> Iterator[URL]:
    """Create ``name``, yield a URL pointing at it, and drop it on the way out."""
    admin = create_engine(
        admin_url.set(database=MAINTENANCE_DATABASE), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield admin_url.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def database_url_in_environment(url: URL) -> Iterator[None]:
    """Point ``DATABASE_URL`` at ``url`` for the duration of the block.

    ``migrations/env.py`` reads the URL from :class:`leadquali.config.Settings` rather than
    taking it as an argument — that is the behaviour under test — so the only honest way to
    aim it at the throwaway database is through the environment it actually reads.
    """
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def migrated_engine(_database_url: URL) -> Iterator[Engine]:
    """A throwaway database with ``alembic upgrade head`` applied, dropped afterwards."""
    name = f"{_database_url.database}_test"
    with temporary_database(_database_url, name) as test_url, database_url_in_environment(test_url):
        command.upgrade(alembic_config(), "head")
        engine = create_engine(test_url)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()
