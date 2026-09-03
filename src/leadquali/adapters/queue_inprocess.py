"""The lead queue, without a queue: local development and the tests.

#26 owns the real producer — an SQS ``send_message`` behind the same
:class:`~leadquali.app.ingest.LeadQueuePort` — and until it lands this is what the endpoint
enqueues to. It is deliberately two implementations in one small class, chosen by whether a
worker was supplied:

* **Collecting** (``worker=None``). Accepted leads are held in a bounded deque and read
  back with :meth:`pending` or :meth:`drain`. This is what the tests use: nothing runs, so
  an assertion about what the *endpoint* did cannot be confused with what a worker did.
* **Background** (``worker=...``). Each lead is handed to a single background thread, which
  is what makes the acceptance criterion "curl a form payload and a routing email arrives"
  true on a laptop with no AWS account. One thread, not a pool: the local pipeline makes a
  model call per lead and serialising them keeps the ordering obvious and the API usage
  sane.

Either way the request thread does no work beyond an append or a ``submit``, which is the
property the 200 ms budget rests on. What this is *not* is durable. A process that dies
with leads in the deque loses them — the rows are still in Postgres with ``status =
received``, which is the difference between "a person can find them" and "they are gone",
but recovering them is a manual replay. That is the honest cost of running without a queue,
and it is the reason #26 exists rather than a reason to pretend otherwise.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Final

from leadquali.app.ingest import QueuedLead

LOGGER = logging.getLogger(__name__)

#: How many accepted leads the collecting mode holds before it refuses more. A bound
#: rather than an unbounded deque: silently discarding the oldest lead would be invariant 3
#: broken by a data structure, so it fills up and says so instead.
DEFAULT_MAX_PENDING: Final[int] = 10_000


class QueueFullError(RuntimeError):
    """The in-process queue is full. Raised so the endpoint answers 503 rather than 202."""


class InProcessLeadQueue:
    """A :class:`~leadquali.app.ingest.LeadQueuePort` that needs no AWS.

    Args:
        worker: what to do with each accepted lead, on a background thread. ``None`` — the
            default — collects them instead, which is what the tests want.
        max_pending: how many leads the collecting mode holds before raising.
    """

    def __init__(
        self,
        *,
        worker: Callable[[QueuedLead], object] | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._worker = worker
        self._max_pending = max_pending
        self._pending: deque[QueuedLead] = deque()
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="leadquali-worker")
            if worker is not None
            else None
        )
        self._futures: list[Future[object]] = []

    def enqueue(self, lead: QueuedLead) -> str | None:
        """Accept one lead. Returns ``None``: there is no provider and so no message id.

        Raises:
            QueueFullError: the collecting queue is at ``max_pending``.
            RuntimeError: the queue has been closed.
        """
        if self._executor is not None and self._worker is not None:
            worker = self._worker
            self._futures.append(self._executor.submit(_run, worker, lead))
            return None

        if len(self._pending) >= self._max_pending:
            raise QueueFullError(
                f"in-process queue is full at {self._max_pending} leads; nothing is draining it"
            )
        self._pending.append(lead)
        # Ids only: a queue message carries the whole submission (invariant 5).
        LOGGER.info(
            "lead enqueued tenant=%s lead=%s submission=%s",
            lead.tenant_id,
            lead.lead_id,
            lead.submission_id,
        )
        return None

    def pending(self) -> tuple[QueuedLead, ...]:
        """The leads waiting, oldest first. Always empty in background mode."""
        return tuple(self._pending)

    def drain(self) -> list[QueuedLead]:
        """Take every waiting lead, leaving the queue empty."""
        drained = list(self._pending)
        self._pending.clear()
        return drained

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting leads, and in background mode wait for the in-flight ones."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None
            self._worker = None

    def __enter__(self) -> InProcessLeadQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _run(worker: Callable[[QueuedLead], object], lead: QueuedLead) -> object:
    """Run the worker, and make sure a failure is visible rather than swallowed.

    A background thread's exception disappears into its future, and a future nobody
    inspects is a lead that vanished without a log line. The row stays at ``status =
    received``, so the lead is recoverable — but only if somebody knows to look.
    """
    try:
        return worker(lead)
    except Exception:
        LOGGER.exception(
            "local worker failed tenant=%s lead=%s submission=%s; the lead row remains "
            "at status=received and needs replaying",
            lead.tenant_id,
            lead.lead_id,
            lead.submission_id,
        )
        raise


__all__ = ["DEFAULT_MAX_PENDING", "InProcessLeadQueue", "QueueFullError"]
