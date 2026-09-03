"""The stand-in queue: collecting for tests, one background thread for local runs.

The property worth proving is that ``enqueue`` returns without doing the work — that is
what the 202's latency budget rests on — and that a worker blowing up on the background
thread neither loses the lead silently nor reaches back into the caller.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

import pytest

from leadquali.adapters.queue_inprocess import InProcessLeadQueue, QueueFullError
from leadquali.app.ingest import QueuedLead
from leadquali.prompts.lead import LeadSubmission

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def lead(index: int = 1) -> QueuedLead:
    return QueuedLead(
        tenant_id="acme",
        lead_id=f"lead-{index:04d}",
        submission_id=f"sub-{index:04d}",
        submission=LeadSubmission(email="ada@analytical-engines.co.uk", message="hello"),
        source="web_form",
        received_at=NOW,
    )


def test_collecting_mode_holds_leads_in_order() -> None:
    queue = InProcessLeadQueue()
    assert queue.enqueue(lead(1)) is None
    queue.enqueue(lead(2))
    assert [item.submission_id for item in queue.pending()] == ["sub-0001", "sub-0002"]
    assert [item.submission_id for item in queue.drain()] == ["sub-0001", "sub-0002"]
    assert queue.pending() == ()


def test_a_full_queue_refuses_rather_than_discarding_the_oldest_lead() -> None:
    """Dropping to make room would be invariant 3 broken by a data structure."""
    queue = InProcessLeadQueue(max_pending=2)
    queue.enqueue(lead(1))
    queue.enqueue(lead(2))
    with pytest.raises(QueueFullError):
        queue.enqueue(lead(3))
    assert len(queue.pending()) == 2


def test_background_mode_runs_the_worker_off_the_calling_thread() -> None:
    seen: list[tuple[str, int]] = []
    started = threading.Event()
    release = threading.Event()

    def worker(item: QueuedLead) -> None:
        started.set()
        release.wait(timeout=5)
        seen.append((item.submission_id, threading.get_ident()))

    with InProcessLeadQueue(worker=worker) as queue:
        queue.enqueue(lead(1))
        assert started.wait(timeout=5)
        # The worker is still blocked and enqueue has already returned: nothing about the
        # caller's latency depends on how long qualification takes.
        assert seen == []
        release.set()

    assert [submission_id for submission_id, _ in seen] == ["sub-0001"]
    assert seen[0][1] != threading.get_ident()
    assert queue.pending() == ()


def test_a_worker_failure_is_logged_and_does_not_reach_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def worker(item: QueuedLead) -> None:
        raise RuntimeError("qualification exploded")

    with (
        caplog.at_level(logging.ERROR, logger="leadquali.adapters.queue_inprocess"),
        InProcessLeadQueue(worker=worker) as queue,
    ):
        queue.enqueue(lead(7))

    assert "lead-0007" in caplog.text
    assert "status=received" in caplog.text


def test_closing_twice_is_harmless() -> None:
    queue = InProcessLeadQueue(worker=lambda item: None)
    queue.close()
    queue.close()
