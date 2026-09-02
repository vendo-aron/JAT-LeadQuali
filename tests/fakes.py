"""In-memory doubles for every port the pipeline touches.

They live outside ``tests/unit`` because #16, #17, #19 and #21 all need the same ones: a
worker test, an ingest test and a notifier test each want a store that behaves like the
real one without a database, and three private copies would drift the first time a port
changed. Nothing here talks to a network, a disk or a clock.

Each double can be told to fail — ``InMemoryLeadStore(fail_on={"record_assessment"})``,
``RecordingNotifier(fail_times=1)`` — because the interesting half of the pipeline is what
happens when a collaborator is broken, and a test that can only exercise the happy path
proves nothing about invariant 3.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
)
from leadquali.app.enrichment import Enrichment
from leadquali.app.ports import RoutingOutcome, StoredLead
from leadquali.domain.models import Action, LeadAssessment, RoutingDecision
from leadquali.domain.tenant_config import TenantConfig, TenantNotFoundError
from leadquali.prompts.lead import LeadSubmission


class FakeStoreError(RuntimeError):
    """The store is unreachable. Stands in for a psycopg ``OperationalError``."""


class FakeNotifierError(RuntimeError):
    """The notifier refused the message. Stands in for a botocore ``ClientError``."""


class FakeAssessorError(RuntimeError):
    """The assessor raised, which its port says it must not. Tested precisely for that."""


class FakeEnricherError(RuntimeError):
    """Enrichment blew up. Must never cost the lead."""


@dataclass(frozen=True, slots=True)
class RecordedAssessment:
    """One ``record_assessment`` call."""

    tenant_id: str
    lead_id: str
    outcome: AssessmentOutcome
    decision: RoutingDecision
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RecordedRoutingEvent:
    """One ``record_routing_event`` call."""

    tenant_id: str
    lead_id: str
    action: Action
    destination: str | None
    outcome: RoutingOutcome
    provider_message_id: str | None
    occurred_at: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class RecordedDispatch:
    """One ``dispatch`` call."""

    tenant_id: str
    lead_id: str
    destination: str
    submission: LeadSubmission
    decision: RoutingDecision
    assessment: LeadAssessment | None


class InMemoryLeadStore:
    """A ``LeadStorePort`` with the real one's uniqueness behaviour and none of its I/O.

    ``(tenant_id, submission_id)`` is unique, exactly as ``uq_leads_tenant_id_submission_id``
    makes it in #15's schema, so replaying a lead returns the same ``lead_id`` with
    ``is_new=False``.
    """

    def __init__(self, *, fail_on: Iterable[str] = ()) -> None:
        self.fail_on = set(fail_on)
        self.leads: dict[tuple[str, str], str] = {}
        self.payloads: dict[str, LeadSubmission] = {}
        self.assessments: list[RecordedAssessment] = []
        self.routing_events: list[RecordedRoutingEvent] = []
        self._next_id = 1

    def _guard(self, method: str) -> None:
        if method in self.fail_on:
            raise FakeStoreError(f"store unavailable during {method}")

    def upsert_lead(
        self,
        *,
        tenant_id: str,
        submission_id: str,
        submission: LeadSubmission,
        source: str,
        received_at: datetime,
    ) -> StoredLead:
        self._guard("upsert_lead")
        del source, received_at
        key = (tenant_id, submission_id)
        existing = self.leads.get(key)
        if existing is not None:
            return StoredLead(lead_id=existing, is_new=False)
        lead_id = f"lead-{self._next_id:04d}"
        self._next_id += 1
        self.leads[key] = lead_id
        self.payloads[lead_id] = submission
        return StoredLead(lead_id=lead_id, is_new=True)

    def already_routed(self, *, tenant_id: str, lead_id: str) -> bool:
        self._guard("already_routed")
        return any(
            event.tenant_id == tenant_id
            and event.lead_id == lead_id
            and event.outcome is not RoutingOutcome.FAILED
            for event in self.routing_events
        )

    def record_assessment(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        outcome: AssessmentOutcome,
        decision: RoutingDecision,
        recorded_at: datetime,
    ) -> None:
        self._guard("record_assessment")
        self.assessments.append(
            RecordedAssessment(
                tenant_id=tenant_id,
                lead_id=lead_id,
                outcome=outcome,
                decision=decision,
                recorded_at=recorded_at,
            )
        )

    def record_routing_event(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        action: Action,
        destination: str | None,
        outcome: RoutingOutcome,
        provider_message_id: str | None,
        occurred_at: datetime,
        detail: str,
    ) -> None:
        self._guard("record_routing_event")
        self.routing_events.append(
            RecordedRoutingEvent(
                tenant_id=tenant_id,
                lead_id=lead_id,
                action=action,
                destination=destination,
                outcome=outcome,
                provider_message_id=provider_message_id,
                occurred_at=occurred_at,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------ assertions

    def terminal_events(self, lead_id: str) -> list[RecordedRoutingEvent]:
        """Events that count as "this lead has been dealt with"."""
        return [
            event
            for event in self.routing_events
            if event.lead_id == lead_id and event.outcome is not RoutingOutcome.FAILED
        ]


class RecordingNotifier:
    """A ``NotifierPort`` that remembers what it was asked to send.

    ``fail_times`` makes the first N attempts raise, which is how a transient SES outage
    followed by an SQS redelivery is simulated.
    """

    def __init__(self, *, fail_times: int = 0, message_id: str | None = "provider-msg-1") -> None:
        self.fail_times = fail_times
        self.message_id = message_id
        self.dispatches: list[RecordedDispatch] = []
        self.attempts = 0

    def dispatch(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        destination: str,
        submission: LeadSubmission,
        decision: RoutingDecision,
        assessment: LeadAssessment | None,
    ) -> str | None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise FakeNotifierError("provider rejected the message")
        self.dispatches.append(
            RecordedDispatch(
                tenant_id=tenant_id,
                lead_id=lead_id,
                destination=destination,
                submission=submission,
                decision=decision,
                assessment=assessment,
            )
        )
        return self.message_id


class ScriptedAssessor:
    """A ``LeadAssessorPort`` returning canned outcomes and remembering its prompts.

    ``raises`` covers the case the port forbids but a buggy adapter can still produce: the
    pipeline must survive an assessor that throws, because the alternative is a lost lead.
    """

    def __init__(
        self,
        outcomes: AssessmentOutcome | Sequence[AssessmentOutcome],
        *,
        raises: Exception | None = None,
    ) -> None:
        if isinstance(outcomes, AssessmentSucceeded | AssessmentFailed):
            self.outcomes: list[AssessmentOutcome] = [outcomes]
        else:
            self.outcomes = list(outcomes)
        self.raises = raises
        self.prompts: list[str] = []
        self.configs: list[TenantConfig] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        self.prompts.append(rendered_lead)
        self.configs.append(config)
        if self.raises is not None:
            raise self.raises
        index = min(len(self.prompts) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


class StaticConfigSource:
    """A ``TenantConfigPort`` backed by a dict."""

    def __init__(self, configs: Mapping[str, TenantConfig]) -> None:
        self.configs = dict(configs)
        self.calls: list[str] = []

    def get(self, tenant_id: str) -> TenantConfig:
        self.calls.append(tenant_id)
        try:
            return self.configs[tenant_id]
        except KeyError:
            raise TenantNotFoundError(f"tenant '{tenant_id}': no configuration") from None


class StaticEnricher:
    """An ``EnricherPort`` that always returns the same enrichment, or always raises."""

    def __init__(self, enrichment: Enrichment | None = None, *, raises: Exception | None = None):
        self.enrichment = enrichment if enrichment is not None else Enrichment.none()
        self.raises = raises
        self.calls: list[str] = []

    def enrich(self, *, tenant_id: str, submission: LeadSubmission) -> Enrichment:
        del submission
        self.calls.append(tenant_id)
        if self.raises is not None:
            raise self.raises
        return self.enrichment


@dataclass
class FakeClock:
    """A ``ClockPort`` that advances by a fixed step on every read.

    Wall time and the monotonic counter move together so a test can assert on both an
    ordering of timestamps and a latency without any real waiting.
    """

    start: datetime = field(default_factory=lambda: datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    step_ms: int = 10
    ticks: int = 0

    def now(self) -> datetime:
        value = self.start + timedelta(milliseconds=self.step_ms * self.ticks)
        self.ticks += 1
        return value

    def monotonic_ms(self) -> int:
        value = self.step_ms * self.ticks
        self.ticks += 1
        return value
