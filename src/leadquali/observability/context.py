"""The ambient fields every log record inherits, and the trace id at the head of them.

A lead's journey crosses four modules that know nothing about each other: the ingest
endpoint, the queue, the pipeline, and the store and notifier adapters underneath it. The
acceptance criterion for #21 is that the whole journey is reconstructable *by trace id
alone*, which means the id has to be on records written by code that was never handed it.

Threading a ``trace_id`` parameter into every adapter method would have put an
observability concern into six port signatures — and it would still have missed the log
lines inside SQLAlchemy and botocore. A :class:`~contextvars.ContextVar` carries it
instead: whoever owns the lead binds it once, and
:class:`~leadquali.observability.logs.JsonLogFormatter` reads it off the context on every
record, from any logger, at any depth.

``ContextVar`` rather than a thread local because it is correct in all three places this
code runs: a synchronous worker thread, FastAPI's event loop (where one thread serves many
requests concurrently, and a thread local would leak one lead's id onto another's log
line), and ``asyncio`` tasks, which inherit a copy of the context at creation. The copy is
also why binding inside a task cannot corrupt its parent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Final

#: The context field that identifies one lead's journey.
TRACE_ID: Final[str] = "trace_id"

#: The other identifiers worth carrying ambiently. Kept as a documented tuple because the
#: field set is a contract: #29 alarms on these names and Logs Insights queries filter on
#: them, so a rename is a breaking change rather than a tidy-up.
CONTEXT_FIELDS: Final[tuple[str, ...]] = (TRACE_ID, "tenant_id", "lead_id", "submission_id")

#: The default, shared by every context that has bound nothing. Genuinely immutable rather
#: than a bare ``{}``: a mutable default on a ``ContextVar`` is one accidental ``update()``
#: away from leaking one lead's identifiers into every other context in the process.
_NO_CONTEXT: Final[Mapping[str, str]] = MappingProxyType({})

_CONTEXT: ContextVar[Mapping[str, str]] = ContextVar("leadquali_log_context", default=_NO_CONTEXT)


def new_trace_id() -> str:
    """A fresh trace id: 32 lowercase hex characters, unique per lead.

    Hex without dashes because the id is grepped and pasted far more often than it is
    parsed — ``fields @message | filter trace_id = "..."`` in Logs Insights, or a bare
    ``grep`` over a downloaded log — and a format with no punctuation survives both a
    double-click and a shell without quoting.
    """
    return uuid.uuid4().hex


def current_context() -> Mapping[str, str]:
    """The fields currently bound, or an empty mapping. Never ``None``."""
    return _CONTEXT.get()


def current_trace_id() -> str | None:
    """The trace id bound to this context, if any."""
    return _CONTEXT.get().get(TRACE_ID)


def ensure_trace_id(candidate: str | None = None) -> str:
    """Return the trace id to use: the candidate, the bound one, or a new one.

    The precedence is the propagation rule in one function. A lead that arrives carrying an
    id keeps it — that is what makes the ingest half and the worker half of the journey the
    same journey. A lead that arrives without one (a queue message written before this
    change shipped, a CLI replay, a direct call from a test) gets a new id rather than none,
    because a journey under a fresh id is still greppable and an unset field is not.

    Args:
        candidate: an id supplied by the caller, e.g. off a queue message. Blank counts as
            absent, since a serialised empty string is indistinguishable from a missing key
            to everyone downstream.
    """
    if candidate is not None and candidate.strip():
        return candidate.strip()
    bound = current_trace_id()
    return bound if bound else new_trace_id()


@contextmanager
def log_context(**fields: str | None) -> Iterator[Mapping[str, str]]:
    """Bind fields onto every log record emitted inside the block, then restore.

    Nested blocks extend rather than replace, so the pipeline can add ``lead_id`` to the
    ``trace_id`` and ``tenant_id`` the worker already bound. ``None`` values are ignored
    rather than bound as ``"None"``, which keeps call sites free of conditionals.

    Restoration goes through the ``ContextVar`` token rather than by re-binding the old
    mapping, so an exception inside the block cannot leave one lead's identifiers attached
    to the next one.

    Yields:
        The merged mapping now in force.
    """
    merged = {**_CONTEXT.get(), **{key: value for key, value in fields.items() if value}}
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


__all__ = [
    "CONTEXT_FIELDS",
    "TRACE_ID",
    "current_context",
    "current_trace_id",
    "ensure_trace_id",
    "log_context",
    "new_trace_id",
]
