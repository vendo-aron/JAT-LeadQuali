"""The ingest use case: persist, screen, enqueue — and never lose a lead doing it.

The service is where invariant 3 is enforced at the edge. A submission the spam filters
catch is still written to ``leads`` and still gets a ``routing_events`` row saying which
filter caught it; a redelivery does not create a second lead; and a lead whose enqueue
failed is not treated as dealt with just because the row exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.app.ingest import (
    QUEUE_MESSAGE_VERSION,
    IngestDisposition,
    IngestRequest,
    IngestService,
    QueuedLead,
)
from leadquali.app.ports import RoutingOutcome
from leadquali.domain.models import Action
from leadquali.domain.spam import SpamPolicy, SpamReason
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import FakeClock, InMemoryLeadStore

TENANT = "acme"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

GOOD = LeadSubmission(
    full_name="Ada Lovelace",
    email="ada@analytical-engines.co.uk",
    company="Analytical Engines",
    message="We get about 400 inbound leads a month and cannot triage them.",
)


def build(
    *, store: InMemoryLeadStore | None = None, queue: InProcessLeadQueue | None = None
) -> tuple[IngestService, InMemoryLeadStore, InProcessLeadQueue]:
    store = store if store is not None else InMemoryLeadStore()
    queue = queue if queue is not None else InProcessLeadQueue()
    service = IngestService(
        store=store,
        queue=queue,
        clock=FakeClock(start=NOW, step_ms=0),
        spam_policy=SpamPolicy(),
    )
    return service, store, queue


def request(
    *,
    submission_id: str = "sub-0001",
    submission: LeadSubmission = GOOD,
    honeypot: str | None = None,
    elapsed_ms: int | None = 9_000,
) -> IngestRequest:
    return IngestRequest(
        tenant_id=TENANT,
        submission_id=submission_id,
        submission=submission,
        honeypot=honeypot,
        elapsed_ms=elapsed_ms,
    )


def test_a_clean_lead_is_persisted_and_enqueued() -> None:
    service, store, queue = build()
    receipt = service.accept(request())

    assert receipt.disposition is IngestDisposition.QUEUED
    assert receipt.is_new_lead is True
    assert receipt.spam_reason is None
    assert receipt.received_at == NOW
    assert store.leads == {(TENANT, "sub-0001"): receipt.lead_id}
    assert [lead.submission_id for lead in queue.pending()] == ["sub-0001"]


def test_the_queued_message_carries_everything_the_worker_needs() -> None:
    service, _, queue = build()
    receipt = service.accept(request())
    queued = queue.pending()[0]

    assert queued.tenant_id == TENANT
    assert queued.lead_id == receipt.lead_id
    assert queued.submission == GOOD
    assert queued.received_at == NOW
    assert queued.source == "web_form"


def test_a_queued_message_round_trips_through_its_json_form() -> None:
    """#26 puts this on SQS; the shape is part of the contract, not an implementation detail."""
    service, _, queue = build()
    service.accept(request())
    message = queue.pending()[0].to_message()

    assert message["version"] == QUEUE_MESSAGE_VERSION
    assert QueuedLead.from_message(message) == queue.pending()[0]


def test_a_malformed_queue_message_is_refused_rather_than_half_read() -> None:
    service, _, queue = build()
    service.accept(request())
    message = queue.pending()[0].to_message()
    message["version"] = QUEUE_MESSAGE_VERSION + 1
    with pytest.raises(ValueError, match="version"):
        QueuedLead.from_message(message)


# ------------------------------------------------------------------------ spam paths


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"honeypot": "http://spam.example"}, SpamReason.HONEYPOT),
        ({"elapsed_ms": 40}, SpamReason.TOO_FAST),
        (
            {"submission": GOOD.model_copy(update={"email": "a@mailinator.com"})},
            SpamReason.FAKE_EMAIL_DOMAIN,
        ),
        ({"submission": GOOD.model_copy(update={"message": ""})}, SpamReason.EMPTY_MESSAGE),
    ],
)
def test_a_caught_submission_is_suppressed_and_never_enqueued(
    kwargs: dict[str, object], reason: SpamReason
) -> None:
    service, _store, queue = build()
    receipt = service.accept(request(**kwargs))  # type: ignore[arg-type]

    assert receipt.disposition is IngestDisposition.SUPPRESSED
    assert receipt.spam_reason is reason
    assert queue.pending() == ()


def test_a_caught_submission_is_still_recorded_with_its_reason() -> None:
    """Invariant 3: suppression is a decision on the record, never a silent drop."""
    service, store, _queue = build()
    receipt = service.accept(request(honeypot="filled-in-by-a-bot"))

    assert store.leads == {(TENANT, "sub-0001"): receipt.lead_id}
    (event,) = store.routing_events
    assert event.tenant_id == TENANT
    assert event.lead_id == receipt.lead_id
    assert event.action is Action.SUPPRESS
    assert event.outcome is RoutingOutcome.SUPPRESSED
    assert event.destination is None
    assert SpamReason.HONEYPOT.value in event.detail
    assert store.already_routed(tenant_id=TENANT, lead_id=receipt.lead_id) is True


def test_the_recorded_reason_carries_no_contact_details() -> None:
    """Invariant 5: the detail line is read straight into a log."""
    service, store, _ = build()
    service.accept(
        request(submission=GOOD.model_copy(update={"email": "ada.lovelace@mailinator.com"}))
    )
    assert "ada.lovelace" not in store.routing_events[0].detail


def test_a_tenant_whose_form_has_no_message_box_can_allow_empty_free_text() -> None:
    store, queue = InMemoryLeadStore(), InProcessLeadQueue()
    service = IngestService(
        store=store,
        queue=queue,
        clock=FakeClock(start=NOW, step_ms=0),
        spam_policy=SpamPolicy(require_message=False),
    )
    receipt = service.accept(request(submission=GOOD.model_copy(update={"message": None})))
    assert receipt.disposition is IngestDisposition.QUEUED


# ----------------------------------------------------------------------- idempotency


def test_the_same_submission_id_twice_creates_one_lead() -> None:
    service, store, _queue = build()
    first = service.accept(request())
    second = service.accept(request())

    assert second.lead_id == first.lead_id
    assert second.is_new_lead is False
    assert len(store.leads) == 1


def test_a_redelivery_of_an_already_routed_lead_is_not_enqueued_again() -> None:
    service, store, queue = build()
    receipt = service.accept(request())
    store.record_routing_event(
        tenant_id=TENANT,
        lead_id=receipt.lead_id,
        action=Action.EMAIL_SALES,
        destination="sales@acme.test",
        outcome=RoutingOutcome.DISPATCHED,
        provider_message_id="msg-1",
        occurred_at=NOW,
        detail="dispatched",
    )
    queue.drain()

    again = service.accept(request())
    assert again.disposition is IngestDisposition.DUPLICATE
    assert queue.pending() == ()


def test_a_failed_enqueue_is_raised_rather_than_answered_with_202() -> None:
    class BrokenQueue:
        def enqueue(self, lead: QueuedLead) -> str | None:
            raise RuntimeError("queue unavailable")

    store = InMemoryLeadStore()
    service = IngestService(store=store, queue=BrokenQueue(), clock=FakeClock(start=NOW, step_ms=0))
    with pytest.raises(RuntimeError, match="queue unavailable"):
        service.accept(request())
    # The lead is on the record even though nothing consumed it; the retry finds it.
    assert len(store.leads) == 1


def test_a_retry_after_a_failed_enqueue_reaches_the_queue() -> None:
    store = InMemoryLeadStore()

    class OnceBrokenQueue:
        def __init__(self) -> None:
            self.accepted: list[QueuedLead] = []
            self.calls = 0

        def enqueue(self, lead: QueuedLead) -> str | None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("queue unavailable")
            self.accepted.append(lead)
            return None

    queue = OnceBrokenQueue()
    service = IngestService(store=store, queue=queue, clock=FakeClock(start=NOW, step_ms=0))
    with pytest.raises(RuntimeError):
        service.accept(request())

    receipt = service.accept(request())
    assert receipt.disposition is IngestDisposition.QUEUED
    assert receipt.is_new_lead is False
    assert len(store.leads) == 1
    assert [lead.submission_id for lead in queue.accepted] == ["sub-0001"]


def test_a_blank_submission_id_is_refused_before_it_reaches_the_store() -> None:
    service, store, _ = build()
    with pytest.raises(ValueError, match="submission_id"):
        service.accept(request(submission_id="   "))
    assert store.leads == {}
