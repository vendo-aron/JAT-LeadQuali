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
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.ports import ClockPort, LeadStorePort, RoutingOutcome
from leadquali.domain.models import Action
from leadquali.domain.spam import DEFAULT_SPAM_POLICY, SpamPolicy, SpamReason, screen
from leadquali.observability import ensure_trace_id, log_context, log_lead_accepted, new_trace_id
from leadquali.prompts.lead import LeadSubmission

LOGGER = logging.getLogger(__name__)

#: Where a lead came from when the caller does not say. Matches ``app.qualify``'s default
#: so a lead ingested here and one replayed by the CLI are recorded the same way.
DEFAULT_SOURCE: Final[str] = "web_form"

#: Version stamped on every queue message. #26 puts these on SQS, where a message written
#: by yesterday's deploy is read by today's worker; an unrecognised version is refused
#: rather than partially understood.
#:
#: Still ``1`` after #21 added ``trace_id``, and deliberately so. A version bump would have
#: been a breaking change in the middle of a rolling deploy: the old worker refuses any
#: version it does not know, so every message the new ingest wrote while both were running
#: would have gone to the DLQ. The field is *additive and optional in both directions*
#: instead — an old worker reads the keys it knows and ignores ``trace_id``; a new worker
#: reading an old message finds none and mints one (see :meth:`QueuedLead.from_message`).
#: Neither side can be broken by the other's deploy order, which is the only property that
#: makes a version number worth keeping for the change that does need it.
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
    trace_id: str = field(default_factory=new_trace_id)
    """The id that makes this lead's ingest half and its worker half the same journey.
    Defaulted rather than required so that a caller who has not thought about tracing still
    produces a traceable lead — an unset trace id is the one value that helps nobody."""

    def to_message(self) -> dict[str, Any]:
        """The JSON-safe form that goes on the wire (an SQS message body in #26)."""
        return {
            "version": QUEUE_MESSAGE_VERSION,
            "tenant_id": self.tenant_id,
            "lead_id": self.lead_id,
            "submission_id": self.submission_id,
            "source": self.source,
            "received_at": self.received_at.isoformat(),
            "trace_id": self.trace_id,
            "submission": self.submission.model_dump(mode="json"),
        }

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> QueuedLead:
        """Rebuild a queued lead from :meth:`to_message`.

        A message written before ``trace_id`` existed — one already on the queue when this
        shipped — is not an error and is not refused: it loads with a freshly minted id, so
        the worker half of its journey is still greppable under a single id and only the
        ingest half sits under none. Refusing it would have meant sending real leads to the
        DLQ to protect a log field, which is the wrong trade by a wide margin.

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
                trace_id=ensure_trace_id(_optional_str(message.get("trace_id"))),
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

    trace_id: str | None = None
    """The id to trace this lead by, when the caller already has one — the HTTP layer mints
    it so that a request rejected before this point still logs under an id. ``None`` means
    "mint one here", which is what a CLI replay or a test gets."""


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    """What happened to one submission, for the response, the log and the tests."""

    tenant_id: str
    trace_id: str
    """Echoed back so the caller can put it on its own log line — and, in #26, on the SQS
    message and the HTTP response header — without re-deriving it."""

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

        This is also where a lead's trace id is minted (or adopted from the caller) and
        bound to the logging context, so that everything written from here down — the
        store's own lines, the queue's, and the exception if one escapes — carries it. The
        binding is a wrapper rather than a ``with`` inside the body so that the body reads
        as the sequence of steps it is.

        Raises:
            ValueError: the submission id is blank. It is the idempotency key, and a blank
                one would make every post the same lead.
            Exception: whatever the store or the queue raises. The caller answers 5xx and
                the form retries with the same ``submission_id``, which is why the retry
                path is the tested one.
        """
        trace_id = ensure_trace_id(request.trace_id)
        with log_context(
            trace_id=trace_id, tenant_id=request.tenant_id, submission_id=request.submission_id
        ):
            return self._accept(request, trace_id)

    def _accept(self, request: IngestRequest, trace_id: str) -> IngestReceipt:
        """The body of :meth:`accept`, inside the trace context it binds."""
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
            return self._announce(
                self._receipt(
                    request,
                    trace_id,
                    submission_id,
                    stored.lead_id,
                    IngestDisposition.DUPLICATE,
                    received_at,
                    is_new=False,
                ),
                request,
            )

        verdict = screen(
            submission=request.submission,
            honeypot=request.honeypot,
            elapsed_ms=request.elapsed_ms,
            policy=self._spam_policy,
        )
        if verdict.reason is not None:
            self._suppress(request, stored.lead_id, verdict.reason, verdict.detail, received_at)
            return self._announce(
                self._receipt(
                    request,
                    trace_id,
                    submission_id,
                    stored.lead_id,
                    IngestDisposition.SUPPRESSED,
                    received_at,
                    is_new=stored.is_new,
                    spam_reason=verdict.reason,
                ),
                request,
            )

        self._queue.enqueue(
            QueuedLead(
                tenant_id=request.tenant_id,
                lead_id=stored.lead_id,
                submission_id=submission_id,
                submission=request.submission,
                source=request.source or self._source,
                received_at=received_at,
                trace_id=trace_id,
            )
        )
        return self._announce(
            self._receipt(
                request,
                trace_id,
                submission_id,
                stored.lead_id,
                IngestDisposition.QUEUED,
                received_at,
                is_new=stored.is_new,
            ),
            request,
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
    def _announce(receipt: IngestReceipt, request: IngestRequest) -> IngestReceipt:
        """Emit the first line of this lead's journey, then hand the receipt back.

        Emitted here rather than in the HTTP handler so that every caller of this service
        — the endpoint, #26's producer, a replay script — produces the same event with the
        same fields. The submission goes no further than
        :func:`~leadquali.observability.pii.contact_email_hash`.
        """
        log_lead_accepted(
            LOGGER,
            tenant_id=receipt.tenant_id,
            lead_id=receipt.lead_id,
            disposition=receipt.disposition.value,
            source=request.source,
            is_new_lead=receipt.is_new_lead,
            email=request.submission.email,
            spam_reason=receipt.spam_reason.value if receipt.spam_reason is not None else None,
        )
        return receipt

    @staticmethod
    def _receipt(
        request: IngestRequest,
        trace_id: str,
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
            trace_id=trace_id,
            submission_id=submission_id,
            lead_id=lead_id,
            disposition=disposition,
            received_at=received_at,
            is_new_lead=is_new,
            spam_reason=spam_reason,
        )


def _optional_str(value: Any) -> str | None:
    """A trimmed string, or ``None`` for a key the message did not carry."""
    return None if value is None else str(value)


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
