"""Protocol interfaces for everything outside the domain.

Each port is the narrowest contract the application layer needs, stated where the
application layer lives. Adapters implement them structurally — no base class, no
registration — so ``domain`` and ``app`` never import an adapter, and swapping a Phase 1
file loader for the Phase 5 Postgres one is a wiring change at the entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from leadquali.app.assessment_result import AssessmentOutcome
from leadquali.app.enrichment import Enrichment
from leadquali.domain.models import Action, LeadAssessment, RoutingDecision
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts.lead import LeadSubmission


@runtime_checkable
class TenantConfigPort(Protocol):
    """Source of validated tenant configuration.

    Phase 1 reads ``tenants/*.json``; P5.1 reads the ``tenants.icp_config`` jsonb column.
    Callers see no difference: either way they get a fully validated
    :class:`~leadquali.domain.tenant_config.TenantConfig` or an exception. There is no
    partially-valid config and no silent fallback to defaults — a tenant whose policy
    cannot be loaded must not have its leads routed by someone else's policy.
    """

    def get(self, tenant_id: str) -> TenantConfig:
        """Return the configuration for ``tenant_id``.

        Raises:
            TenantNotFoundError: no configuration exists for this tenant.
            TenantConfigError: a configuration exists but is invalid or unreadable.
        """
        ...


@runtime_checkable
class LeadAssessorPort(Protocol):
    """Turns one rendered lead into a judgment, or into the reason there isn't one.

    The whole point of this port is that ``app/qualify.py`` (#14) never sees the Anthropic
    SDK: it holds a ``LeadAssessorPort``, gets an
    :data:`~leadquali.app.assessment_result.AssessmentOutcome`, and routes on it. Swapping
    the model, the provider, or a recorded-response double for the eval harness is a wiring
    change at the entrypoint.

    Implementations **do not raise** for the failure modes of talking to a model. A refusal,
    a timeout, a rate limit, a 5xx and a schema violation all come back as
    :class:`~leadquali.app.assessment_result.AssessmentFailed` carrying the matching
    :class:`~leadquali.domain.models.EscalationReason`, because invariant 3 says every one
    of them has to reach a human rather than becoming a stack trace or, far worse, a low
    score. Only a programming error should ever escape.

    ``effort`` is deliberately absent from this signature: it is a property of the
    configured assessor, not of a lead, so #24 sweeps it by constructing assessors rather
    than by threading a parameter through the pipeline.
    """

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        """Assess one lead against one tenant's profile.

        Args:
            config: the tenant whose ICP and prompt version this call is made under.
            rendered_lead: the lead as the user turn — already rendered and wrapped in
                untrusted-data delimiters by #12. Implementations send it verbatim as a
                user message and never as a system block: it is attacker-controlled text
                from a public form, and it must stay outside the cached prefix.

        Returns:
            :class:`~leadquali.app.assessment_result.AssessmentSucceeded` with a validated
            assessment and its metering, or
            :class:`~leadquali.app.assessment_result.AssessmentFailed` with the reason.
        """
        ...


class RoutingOutcome(StrEnum):
    """What one ``routing_events`` row says happened to one lead.

    Three values, and the difference between them is what makes at-least-once delivery
    safe. :attr:`DISPATCHED` and :attr:`SUPPRESSED` are *final answers*: the lead has been
    dealt with, and a redelivery of the same SQS message must do nothing at all.
    :attr:`FAILED` is deliberately not final — it records that a send was attempted and did
    not happen, which the operator needs to see, and it must never make
    :meth:`LeadStorePort.already_routed` true. If it did, one transient SES outage would
    convert a retryable failure into a permanently lost lead, which is invariant 3 broken
    by the very mechanism meant to protect it.
    """

    DISPATCHED = "dispatched"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StoredLead:
    """The identity of a persisted lead, and whether this delivery is the one that made it."""

    lead_id: str
    """Store-assigned id, used for every later write about this lead."""

    is_new: bool
    """``False`` when ``(tenant_id, submission_id)`` already existed.

    Useful for metrics and logs, and **not** the idempotency signal: a worker that died
    between the insert and the send leaves a lead that is not new and has still never
    reached anybody. Ask :meth:`LeadStorePort.already_routed` before deciding to skip.
    """


@runtime_checkable
class LeadStorePort(Protocol):
    """Everything the pipeline persists, tenant-scoped on every call.

    Four methods, in the order the pipeline calls them: put the lead on the record, ask
    whether it has already been dealt with, record what the model said, record where the
    lead went. ``tenant_id`` is on all four even where ``lead_id`` alone would find the row
    (plan §4): retrofitting multi-tenancy is a rewrite and an unused parameter is free, and
    an adapter that filters on it cannot serve tenant B's lead to tenant A by mistake.

    Implementations raise on failure — a store that cannot write is not a degraded mode,
    it is a reason to leave the message on the queue and let it be redelivered.
    """

    def upsert_lead(
        self,
        *,
        tenant_id: str,
        submission_id: str,
        submission: LeadSubmission,
        source: str,
        received_at: datetime,
    ) -> StoredLead:
        """Insert this lead, or return the existing one for the same submission id.

        ``(tenant_id, submission_id)`` is unique (``uq_leads_tenant_id_submission_id`` in
        #15's schema), so this is an upsert rather than an insert: SQS is at-least-once and
        a second delivery must not raise, must not create a second row, and must come back
        carrying the first row's ``lead_id``.
        """
        ...

    def already_routed(self, *, tenant_id: str, lead_id: str) -> bool:
        """Whether this lead has a *final* routing event — dispatched or suppressed.

        The idempotency check, and the one question worth asking before spending money on
        a model call or sending a second email to sales. It deliberately ignores
        :attr:`RoutingOutcome.FAILED` rows: a lead whose only history is a failed send has
        not reached anyone, and the redelivery that gets it there is the whole point of the
        queue.
        """
        ...

    def record_assessment(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        outcome: AssessmentOutcome,
        decision: RoutingDecision,
        recorded_at: datetime,
    ) -> None:
        """Record what the model said and what policy concluded from it.

        Takes the whole :data:`~leadquali.app.assessment_result.AssessmentOutcome`, not an
        assessment, because a failed attempt is also a row: a refusal that was billed is
        money the business must be able to see, and a run of ``API_ERROR`` is the signal
        that wakes a different person than a run of ``LOW_CONFIDENCE``. The scored columns
        are null for a failure; ``decision`` is always present, because there is always a
        decision.
        """
        ...

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
        """Record where the lead went, or why it did not go anywhere.

        Called on every terminal path, including suppression — "we never contacted this
        lead, and here is which of the two suppressions it was" is an answer the business
        needs — and on a failed send, so a lead stuck behind a broken notifier is visible
        rather than merely absent.

        ``destination`` is ``None`` only for a suppression. ``provider_message_id`` is the
        notifier's receipt when there is one, and ``None`` otherwise. ``detail`` is one
        short PII-free line: the decision's note, or the failure's class (invariant 5).
        """
        ...


@runtime_checkable
class NotifierPort(Protocol):
    """Puts a routed lead in front of a person, however that tenant's people are reached.

    Email today (#19, SES), a HubSpot pipeline or a Slack channel for customer #2 — the
    pipeline neither knows nor cares, which is the whole argument for the ports/adapters
    split. ``destination`` is resolved from the tenant's routing table by the caller and is
    always a non-empty string: choosing where a lead goes is policy, and policy is not the
    notifier's decision to make.
    """

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
        """Deliver one routed lead. Returns the provider's message id, if it gives one.

        Args:
            tenant_id: whose lead this is; also selects the sender identity in #19.
            lead_id: recorded on the routing event and bound into the feedback links.
            destination: the resolved sales inbox, queue or pipeline id. Never blank.
            submission: the lead itself, so the recipient can reply to a human being.
            decision: tier, score, note and escalation reason. The note carries the
                ``"system could not assess"`` banner and the low-confidence wording
                verbatim, so the message renders them rather than inventing its own.
            assessment: the model's judgment, or ``None`` when there is none. ``None`` is
                the system-failure path and it is not an error: the lead is emailed
                unqualified, banner first, for a person to qualify by hand.

        Raises:
            Exception: any delivery failure. The pipeline records the attempt and
                re-raises so the queue redelivers; swallowing it would lose the lead.
        """
        ...


@runtime_checkable
class EnricherPort(Protocol):
    """Cheap deterministic signal about a lead, bought without spending tokens.

    #18 implements it against the email domain: corporate vs free-mail vs disposable, a
    role address, an MX lookup. ``adapters/enrich_null.py`` implements it by doing nothing,
    which is a complete and shippable configuration.

    **Fail open, always.** An implementation that cannot finish returns
    :meth:`~leadquali.app.enrichment.Enrichment.unavailable` rather than raising;
    enrichment improves a judgment, it never gates one, and a DNS timeout must not cost a
    deal. The pipeline defends against a raise anyway — it does not stake a lead on an
    adapter honouring a docstring — but an implementation that raises is a bug.
    """

    def enrich(self, *, tenant_id: str, submission: LeadSubmission) -> Enrichment:
        """Look up whatever is cheaply knowable about this lead. Never raises."""
        ...


@runtime_checkable
class ClockPort(Protocol):
    """Time, injected rather than ambient.

    Two readings, for two different jobs. :meth:`now` stamps rows — it is wall time, it is
    timezone-aware UTC, and it may jump backwards when NTP corrects the host.
    :meth:`monotonic_ms` measures durations — it never jumps, and only differences between
    two readings mean anything. Using wall time for a latency is how a p99 metric ends up
    with negative values twice a year.

    It is a port so that tests get deterministic timestamps and latencies with no sleeping,
    and so that a replay tool can re-run a lead with the timestamps it originally had.
    """

    def now(self) -> datetime:
        """The current wall-clock time, timezone-aware and in UTC."""
        ...

    def monotonic_ms(self) -> int:
        """A monotonic millisecond counter. Only differences are meaningful."""
        ...


__all__ = [
    "ClockPort",
    "EnricherPort",
    "LeadAssessorPort",
    "LeadStorePort",
    "NotifierPort",
    "RoutingOutcome",
    "StoredLead",
    "TenantConfigPort",
]
