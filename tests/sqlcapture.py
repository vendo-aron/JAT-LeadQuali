"""Run a repository method without a database and keep the SQL it tried to execute.

Docker is not available in every environment this suite runs in, so a property proved
only by an ``integration``-marked test is a property that is not proved at all: it skips,
the bar stays green, and the filter it was watching can be deleted without anybody
noticing. That has already happened twice in this repository — once to the cross-tenant
filter in the Postgres store, once to the billing query's result mapping — which is why
the mechanism that catches it lives here, in one place, rather than being reinvented per
adapter.

The idea is #33's, generalised. An adapter builds its statement, opens a session and
executes; the only way to get at the SQL without a server is to let it do all three and
intercept the last. So :class:`CapturingSessions` stands in for the ``sessionmaker``, hands
out a session whose ``execute`` records the statement, and — once it has run out of the
canned results it was given — raises :class:`CapturedStatementsError` to stop the method before
it tries to make sense of a row that does not exist. The test then compiles what it caught
against the real ``postgresql`` dialect, which is what makes "there is a tenant predicate
in the ``WHERE`` clause" an assertion about the SQL Postgres would receive rather than
about the Python that built it.

A method that issues several statements is why the results are a *queue* rather than a
single value: feed it one fewer result than it has statements and every one of them is
recorded, with the last raising. Feeding results is deliberately explicit — a harness that
synthesised plausible rows would produce a test that passes because the method errored,
which is the failure mode this whole file exists to avoid.

Nothing here is specific to tenancy. What counts as evidence that a statement is scoped to
one tenant belongs to ``tests/isolation/test_repository_isolation.py``, which is the only
thing that has an opinion about it.
"""

from __future__ import annotations

import contextlib
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Final

from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "PG_DIALECT",
    "CannedResult",
    "CapturedStatementsError",
    "CapturingSessions",
    "SqlCapture",
    "compiled",
    "parameters",
    "sql_text",
]

#: The dialect every statement here is compiled against. Built once because SQLAlchemy's
#: dialect constructor carries no annotations of its own, so the ignore belongs in one
#: place rather than on every call site.
PG_DIALECT: Final[Any] = postgresql.dialect()  # type: ignore[no-untyped-call]  # untyped upstream


class CapturedStatementsError(Exception):
    """Carries the statements a method built, instead of letting it run the last one."""

    def __init__(self, statements: Sequence[ClauseElement]) -> None:
        super().__init__(f"captured {len(statements)} statement(s)")
        self.statements: tuple[ClauseElement, ...] = tuple(statements)


class CannedResult:
    """One row handed back to an adapter so it can reach its *next* statement.

    Deliberately minimal and deliberately explicit. It answers the six accessors the
    adapters in this repository actually call and nothing else, so a method that starts
    consuming its results some other way fails here with an ``AttributeError`` naming the
    accessor rather than silently receiving something plausible.
    """

    def __init__(self, row: Sequence[Any] | None = None) -> None:
        self.row = row

    def one(self) -> Sequence[Any]:
        """The single row. Mirrors ``Result.one``."""
        if self.row is None:
            raise AssertionError("the method expected a row; give its recipe a CannedResult(row=…)")
        return self.row

    def one_or_none(self) -> Sequence[Any] | None:
        """The single row, or ``None``."""
        return self.row

    def first(self) -> Sequence[Any] | None:
        """The first row, or ``None``."""
        return self.row

    def all(self) -> list[Sequence[Any]]:
        """Every row, which is at most one here."""
        return [] if self.row is None else [self.row]

    def scalar_one(self) -> Any:
        """The first column of the single row."""
        return self.one()[0]

    def scalar_one_or_none(self) -> Any:
        """The first column of the single row, or ``None``."""
        return None if self.row is None else self.row[0]


class CapturingSession:
    """A session that records what it is asked to execute and refuses to run it."""

    def __init__(self, statements: list[ClauseElement], results: deque[CannedResult]) -> None:
        self._statements = statements
        self._results = results

    def execute(self, statement: ClauseElement, *args: Any, **kwargs: Any) -> CannedResult:
        """Record ``statement``; answer from the queue, or stop the method here."""
        del args, kwargs
        self._statements.append(statement)
        if self._results:
            return self._results.popleft()
        raise CapturedStatementsError(self._statements)

    def __enter__(self) -> CapturingSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class CapturingSessions(sessionmaker[Session]):
    """A ``sessionmaker`` whose ``begin()`` yields the capturing session.

    A subclass rather than a duck type because the adapters are annotated as taking a
    ``sessionmaker[Session]``, and a test that had to lie to mypy about that would be a
    test that stops matching the code it is watching.
    """

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[ClauseElement] = []
        self.results: deque[CannedResult] = deque()

    def reset(self, results: Iterable[CannedResult] = ()) -> None:
        """Forget the previous call and arm the queue for the next one."""
        self.statements.clear()
        self.results = deque(results)

    def begin(self) -> Any:
        """Hand out the capturing session instead of a real one."""
        return CapturingSession(self.statements, self.results)


class SqlCapture:
    """Owns one :class:`CapturingSessions` and the store built over it.

    The store has to be constructed *before* the call is made, so the canned results
    cannot be a constructor argument; they are armed per call by :meth:`run`.
    """

    def __init__(self) -> None:
        self.sessions = CapturingSessions()

    def run(
        self, call: Callable[[], Any], *, results: Iterable[CannedResult] = ()
    ) -> tuple[ClauseElement, ...]:
        """Call ``call`` and return every statement it tried to execute, in order.

        Give it one fewer :class:`CannedResult` than the method has statements: the last
        one then finds an empty queue and stops the method before it interprets a row it
        was never given. Any exception other than that stop propagates — a recipe with the
        wrong number of results is a broken test and must look like one.
        """
        self.sessions.reset(results)
        with contextlib.suppress(CapturedStatementsError):
            call()
        statements = tuple(self.sessions.statements)
        assert statements, "the method executed no statement at all"
        return statements


def compiled(statement: ClauseElement) -> Any:
    """Compile one statement against Postgres. Raises for a construct Postgres cannot run."""
    return statement.compile(dialect=PG_DIALECT)


def sql_text(statement: ClauseElement) -> str:
    """The Postgres SQL one statement emits, compiled and lowercased."""
    return str(compiled(statement)).lower()


def parameters(statement: ClauseElement) -> dict[str, Any]:
    """The bound parameters Postgres would receive with this statement."""
    values: dict[str, Any] = dict(compiled(statement).params)
    return values
