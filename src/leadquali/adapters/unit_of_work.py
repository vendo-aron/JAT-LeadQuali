"""One transaction spanning two stores, without handing a session to the application layer.

The config editor (#36) has to write ``tenants.icp_config`` and a ``tenant_config_versions``
row **atomically**: a saved rubric with no audit row must be impossible, not merely
unlikely. Both writes go through their own store — the config through
:meth:`~leadquali.app.tenants.TenantService.update_config`, which is the only writer of
that column — and each store, left alone, opens its own transaction per call.

So the transaction is made ambient rather than passed. :meth:`PostgresUnitOfWork.atomic`
opens one session, binds it to a :class:`~contextvars.ContextVar` for the duration of the
block, and every store that resolves its session through :func:`session_scope` joins that
one instead of starting its own. Outside such a block nothing changes: ``session_scope``
falls straight through to ``sessions.begin()``, which is exactly what the stores did
before.

Why a ContextVar and not a parameter
------------------------------------

The alternative is threading a ``session`` argument through
:class:`~leadquali.app.tenants.TenantService` and its Protocol, which would put SQLAlchemy
in the application layer's vocabulary — the thing ``CLAUDE.md``'s layering rule exists to
prevent — and would widen a port used by a CLI, a worker and a test for the benefit of one
caller. A ``ContextVar`` is also correct under asyncio and under threads, where a module
global would not be: each task and each thread gets its own binding, so two admin requests
being served concurrently cannot land in each other's transaction.

**Blocks do not nest.** :meth:`PostgresUnitOfWork.atomic` refuses to open a second one,
because the obvious implementation of nesting — joining the outer transaction — makes the
inner block's ``with`` say "committed" when nothing has been committed yet, and the
alternative, a savepoint, is a different guarantee wearing the same name. Nothing in the
admin needs it.

``tests/unit/test_unit_of_work.py`` asserts the whole of this against a real SQLAlchemy
engine over in-memory SQLite, so "these two writes are one transaction" is checked in the
default suite rather than only where Docker is available.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

__all__ = ["PostgresUnitOfWork", "ambient_session", "session_scope"]

_AMBIENT: Final[ContextVar[Session | None]] = ContextVar("leadquali_ambient_session", default=None)


def ambient_session() -> Session | None:
    """The session bound by an enclosing :meth:`PostgresUnitOfWork.atomic`, if any."""
    return _AMBIENT.get()


@contextmanager
def session_scope(sessions: sessionmaker[Session]) -> Iterator[Session]:
    """Yield the ambient session, or open a transaction of this call's own.

    Every store method that may be called inside a unit of work resolves its session this
    way. The ambient case deliberately does **not** commit on exit: the block that bound
    the session owns the commit, and a store method that committed half way through would
    turn "both writes or neither" into "the first write, then maybe the second".

    Args:
        sessions: The factory to use when there is no ambient transaction.

    Yields:
        The session to execute against.
    """
    joined = _AMBIENT.get()
    if joined is not None:
        yield joined
        return
    with sessions.begin() as session:
        yield session


class PostgresUnitOfWork:
    """Runs a block of store calls in one transaction.

    Implements :class:`~leadquali.app.config_versions.UnitOfWorkPort`.

    Args:
        sessions: The session factory; see
            :func:`leadquali.adapters.store_postgres.session_factory`.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresUnitOfWork:
        """A unit of work over the memoised engine for ``url``."""
        from leadquali.adapters.store_postgres import session_factory

        return cls(session_factory(url))

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Open one transaction and bind it for the duration of the block.

        Everything inside that goes through :func:`session_scope` joins it, so the block
        commits as a whole or rolls back as a whole.

        Raises:
            RuntimeError: a unit of work is already open on this context. Nesting is
                refused rather than silently flattened — an inner ``with`` that returns
                without having committed anything is a guarantee that reads as true and is
                not.
        """
        if _AMBIENT.get() is not None:
            raise RuntimeError(
                "a unit of work is already open on this context; nesting is not supported "
                "because the inner block would appear to commit while the outer one had "
                "not. Restructure the call so there is one atomic() around both writes"
            )
        with self._sessions.begin() as session:
            token = _AMBIENT.set(session)
            try:
                yield
            finally:
                _AMBIENT.reset(token)

    def __repr__(self) -> str:
        """Render the shape, never the connection string."""
        return f"PostgresUnitOfWork(open={_AMBIENT.get() is not None})"
