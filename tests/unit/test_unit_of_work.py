"""The ambient transaction, against a real engine rather than a mock.

There is no Postgres in this environment, and "the config write and the audit row are one
transaction" is exactly the kind of claim that a Docker-gated test lets a mutation break
in silence — #31's and #33's reviews both found one. SQLAlchemy over in-memory SQLite is a
real engine with real transactions, so the mechanism itself is checked in the default
suite: two statements issued through :func:`session_scope` inside one
:meth:`PostgresUnitOfWork.atomic` land in one transaction, and a failure between them
leaves neither.

What this cannot check is the Postgres *schema* — that is
``tests/integration/test_config_versions_store.py``, which is marked ``integration`` and
skips here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.unit_of_work import PostgresUnitOfWork, ambient_session, session_scope

_METADATA = MetaData()
_NOTES = Table(
    "notes",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("body", String, nullable=False),
)


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    """A session factory over a single in-memory SQLite database.

    ``StaticPool`` is not needed because the factory hands out sessions from one engine,
    and the engine holds one connection for a ``sqlite://`` URL.
    """
    engine = create_engine("sqlite://")
    _METADATA.create_all(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def write(sessions: sessionmaker[Session], body: str) -> None:
    """One store-style write: resolve a session the way every admin store does."""
    with session_scope(sessions) as session:
        session.execute(insert(_NOTES).values(body=body))


def bodies(sessions: sessionmaker[Session]) -> list[str]:
    with sessions.begin() as session:
        return [row[0] for row in session.execute(select(_NOTES.c.body)).all()]


def test_without_a_unit_of_work_each_write_commits_on_its_own() -> None:
    """The behaviour every store had before this module existed, unchanged."""
    engine = create_engine("sqlite://")
    _METADATA.create_all(engine)
    factory = sessionmaker(engine)

    write(factory, "first")
    with pytest.raises(RuntimeError), session_scope(factory):
        raise RuntimeError("boom")

    assert bodies(factory) == ["first"]
    engine.dispose()


def test_two_writes_inside_one_block_share_a_session(sessions: sessionmaker[Session]) -> None:
    seen = []
    with PostgresUnitOfWork(sessions).atomic():
        with session_scope(sessions) as first:
            seen.append(first)
        with session_scope(sessions) as second:
            seen.append(second)

    assert seen[0] is seen[1], "the second write opened a transaction of its own"


def test_a_completed_block_commits_everything(sessions: sessionmaker[Session]) -> None:
    with PostgresUnitOfWork(sessions).atomic():
        write(sessions, "config")
        write(sessions, "version")

    assert bodies(sessions) == ["config", "version"]


def test_a_failure_between_the_two_writes_leaves_neither(sessions: sessionmaker[Session]) -> None:
    """The property the config editor is built on, stated against a real transaction."""
    with (
        pytest.raises(RuntimeError, match="the version insert failed"),
        PostgresUnitOfWork(sessions).atomic(),
    ):
        write(sessions, "config")
        raise RuntimeError("the version insert failed")

    assert bodies(sessions) == []


def test_the_ambient_session_is_bound_only_inside_the_block(
    sessions: sessionmaker[Session],
) -> None:
    assert ambient_session() is None
    with PostgresUnitOfWork(sessions).atomic():
        assert ambient_session() is not None
    assert ambient_session() is None


def test_the_ambient_session_is_unbound_after_a_failure(sessions: sessionmaker[Session]) -> None:
    """A leaked binding would enrol the next request's writes in a dead transaction."""
    with pytest.raises(RuntimeError), PostgresUnitOfWork(sessions).atomic():
        raise RuntimeError("boom")

    assert ambient_session() is None


def test_nesting_is_refused_rather_than_silently_flattened(
    sessions: sessionmaker[Session],
) -> None:
    unit = PostgresUnitOfWork(sessions)

    with pytest.raises(RuntimeError, match="already open"), unit.atomic(), unit.atomic():
        pass  # pragma: no cover - the inner block never runs


def test_the_repr_does_not_carry_a_connection_string(sessions: sessionmaker[Session]) -> None:
    assert repr(PostgresUnitOfWork(sessions)) == "PostgresUnitOfWork(open=False)"
