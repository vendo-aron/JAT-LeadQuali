"""Accepting a lead from the public form: persist it, screen it, hand it on.

This is everything the ingest endpoint does that is not HTTP, kept out of the route so it
can be unit tested without a client and reused unchanged by #26's Lambda. It is also where
the edge honours the invariants:

* **Nothing is dropped (invariant 3).** The lead row is written *before* any judgement is
  made about the submission, so a spam catch is a ``routing_events`` row saying which
  filter fired — never an early ``return``. The one thing that stops here is a submission
  an explicit deterministic filter caught, and even that is on the record.
* **Nothing slow happens (plan §3).** No model, no enrichment, no email: two or three short
  statements and a queue write. The whole point of the 202 is that the expensive work
  happens after the browser has been answered.
* **A redelivery costs one lead, not two.** ``(tenant_id, submission_id)`` is the
  idempotency key and the store upserts on it.

The subtle case is a retry after a *failed* enqueue. The lead row exists, so the upsert
says "not new" — and stopping there would leave a lead that is persisted, unqueued and
invisible, which is invariant 3 broken by the mechanism meant to protect it. So "not new"
is not the signal to stop; :meth:`~leadquali.app.ports.LeadStorePort.already_routed` is.
A lead nobody has finished with is enqueued again, and the worker's own idempotency check
makes the redundant delivery a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.ports import ClockPort, LeadStorePort, RoutingOutcome
from leadquali.domain.models import Action
from leadquali.domain.spam import DEFAULT_SPAM_POLICY, SpamPolicy, SpamReason, screen
from leadquali.prompts.lead import LeadSubmission

LOGGER = logging.getLogger(__name__)

#: Where a lead came from when the caller does not say. Matches ``app.qualify``'s default
#: so a lead ingested here and one replayed by the CLI are recorded the same way.
DEFAULT_SOURCE: Final[str] = "web_form"

#: Version stamped on every queue message. #26 puts these on SQS, where a message written
#: by yesterday's deploy is read by today's worker; an unrecognised version is refused
#: rather than partially understood.
QUEUE_MESSAGE_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class QueuedLead:
    """One lead handed from ingest to the qualification worker.

    The queue message contract, in one place, because two services depend on it: ingest
    writes it and #26's worker reads it. It carries the whole submission rather than just
    an id so the worker needs no read-back — and so a lead cannot be processed before its
    own row is visible to another connection.
    """

    tenant_id: str
    lead_id: str
    submission_id: str
    submission: LeadSubmission
    source: str
    received_at: datetime

    def to_message(self) -> dict[str, Any]:
        """The JSON-safe form that goes on the wire (an SQS message body in #26)."""
        return {
            "version": QUEUE_MESSAGE_VERSION,
            "tenant_id": self.tenant_id,
            "lead_id": self.lead_id,
            "submission_id": self.submission_id,
            "source": self.source,
            "received_at": self.received_at.isoformat(),
            "submission": self.submission.model_dump(mode="json"),
        }

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> QueuedLead:
        """Rebuild a queued lead from :meth:`to_message`.

        Raises:
            ValueError: the message is not a version this code understands, or a required
                field is missing. Refusing it sends the message to the DLQ, where a person
                sees it; guessing at it would qualify a lead against the wrong data.
        """
        version = message.get("version")
        if version != QUEUE_MESSAGE_VERSION:
            raise ValueError(
                f"queue message version {version!r} is not supported "
                f"(this worker reads version {QUEUE_MESSAGE_VERSION})"
            )
        try:
            return cls(
                tenant_id=str(message["tenant_id"]),
                lead_id=str(message["lead_id"]),
                submission_id=str(message["submission_id"]),
                submission=LeadSubmission.model_validate(message["submission"]),
                source=str(message["source"]),
                received_at=datetime.fromisoformat(str(message["received_at"])),
            )
        except KeyError as error:
            raise ValueError(f"queue message is missing {error.args[0]!r}") from None


@runtime_checkable
class LeadQueuePort(Protocol):
    """Where an accepted lead goes to be qualified later.

    One method, because that is the whole contract the edge needs: hand over a lead and
    return once it is durably somebody else's problem. #26 implements it over SQS; the
    in-process implementation in ``adapters/queue_inprocess.py`` implements it for local
    development, where there is no AWS.

    Implementations **raise** if the lead was not accepted. Returning quietly would let the
    endpoint answer 202 for a lead that no worker will ever see, which is exactly the
    silent drop invariant 3 forbids.
    """

    def enqueue(self, lead: QueuedLead) -> str | None:
        """Hand one lead to the worker. Returns the provider's message id, if any."""
        ...


class IngestDisposition(StrEnum):
    """What ingest did with one submission.

    Never returned to the caller. A bot must not be able to read its own filter results
    off the response — that is free tuning feedback — so the HTTP answer is the same 202
    for all three. This is what the logs, the metrics and the tests read.
    """

    QUEUED = "queued"
    SUPPRESSED = "suppressed"
    """A deterministic pre-filter caught it. Recorded, with the reason, and not enqueued."""

    DUPLICATE = "duplicate"
    """The lead already exists and has already been dealt with. Nothing further to do."""


@dataclass(frozen=True, slots=True)
class IngestRequest:
    """One validated form post, in the terms the application layer works in.

    Deliberately not the wire model: ``api/schemas.py`` owns what HTTP accepts and maps it
    onto this. The two change for different reasons — a new form field is a wire change,
    a new pre-filter signal is a change here.
    """

    tenant_id: str
    submission_id: str
    submission: LeadSubmission
    source: str = DEFAULT_SOURCE
    honeypot: str | None = None
    """The hidden field's value. Non-blank means a script filled the form."""

    elapsed_ms: int | None = None
    """Client-reported milliseconds from form render to submit; ``None`` if not reported."""


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    """What happened to one submission, for the response, the log and the tests."""

    tenant_id: str
    submission_id: str
    lead_id: str
    disposition: IngestDisposition
    received_at: datetime
    is_new_lead: bool
    spam_reason: SpamReason | None = None


class IngestService:
    """Accepts leads at the public edge. Built once per process, called once per post.

    Args:
        store: where the raw lead and its suppression are recorded.
        queue: where an accepted lead goes to be qualified.
        clock: injected so ``received_at`` is deterministic under test.
        spam_policy: the deterministic pre-filter thresholds and lists.
        source: recorded on the lead row; distinguishes the web form from a later
            webhook or CSV import.
    """

    def __init__(
        self,
        *,
        store: LeadStorePort,
        queue: LeadQueuePort,
        clock: ClockPort,
        spam_policy: SpamPolicy = DEFAULT_SPAM_POLICY,
        source: str = DEFAULT_SOURCE,
    ) -> None:
        self._store = store
        self._queue = queue
        self._clock = clock
        self._spam_policy = spam_policy
        self._source = source

    def accept(self, request: IngestRequest) -> IngestReceipt:
        """Persist, screen and enqueue one submission.

        The order is the plan's order and it matters: the lead is on the record before any
        decision is taken about it, so every path from here — accepted, suppressed,
        duplicate, or an exception on the way to the queue — leaves a row a person can
        find.

        Raises:
            ValueError: the submission id is blank. It is the idempotency key, and a blank
                one would make every post the same lead.
            Exception: whatever the store or the queue raises. The caller answers 5xx and
                the form retries with the same ``submission_id``, which is why the retry
                path is the tested one.
        """
        submission_id = request.submission_id.strip()
        if not submission_id:
            raise ValueError("submission_id must not be blank; it is the idempotency key")

        received_at = self._clock.now()
        stored = self._store.upsert_lead(
            tenant_id=request.tenant_id,
            submission_id=submission_id,
            submission=request.submission,
            source=request.source or self._source,
            received_at=received_at,
        )

        if not stored.is_new and self._store.already_routed(
            tenant_id=request.tenant_id, lead_id=stored.lead_id
        ):
            return self._receipt(
                request,
                submission_id,
                stored.lead_id,
                IngestDisposition.DUPLICATE,
                received_at,
                is_new=False,
            )

        verdict = screen(
            submission=request.submission,
            honeypot=request.honeypot,
            elapsed_ms=request.elapsed_ms,
            policy=self._spam_policy,
        )
        if verdict.reason is not None:
            self._suppress(request, stored.lead_id, verdict.reason, verdict.detail, received_at)
            return self._receipt(
                request,
                submission_id,
                stored.lead_id,
                IngestDisposition.SUPPRESSED,
                received_at,
                is_new=stored.is_new,
                spam_reason=verdict.reason,
            )

        self._queue.enqueue(
            QueuedLead(
                tenant_id=request.tenant_id,
                lead_id=stored.lead_id,
                submission_id=submission_id,
                submission=request.submission,
                source=request.source or self._source,
                received_at=received_at,
            )
        )
        return self._receipt(
            request,
            submission_id,
            stored.lead_id,
            IngestDisposition.QUEUED,
            received_at,
            is_new=stored.is_new,
        )

    def _suppress(
        self,
        request: IngestRequest,
        lead_id: str,
        reason: SpamReason,
        detail: str,
        occurred_at: datetime,
    ) -> None:
        """Record the suppression. The lead stops here, and the row says why."""
        self._store.record_routing_event(
            tenant_id=request.tenant_id,
            lead_id=lead_id,
            action=Action.SUPPRESS,
            destination=None,
            outcome=RoutingOutcome.SUPPRESSED,
            provider_message_id=None,
            occurred_at=occurred_at,
            detail=f"pre-filter {reason.value}: {detail}",
        )

    @staticmethod
    def _receipt(
        request: IngestRequest,
        submission_id: str,
        lead_id: str,
        disposition: IngestDisposition,
        received_at: datetime,
        *,
        is_new: bool,
        spam_reason: SpamReason | None = None,
    ) -> IngestReceipt:
        return IngestReceipt(
            tenant_id=request.tenant_id,
            submission_id=submission_id,
            lead_id=lead_id,
            disposition=disposition,
            received_at=received_at,
            is_new_lead=is_new,
            spam_reason=spam_reason,
        )


__all__ = [
    "DEFAULT_SOURCE",
    "QUEUE_MESSAGE_VERSION",
    "IngestDisposition",
    "IngestReceipt",
    "IngestRequest",
    "IngestService",
    "LeadQueuePort",
    "QueuedLead",
]
