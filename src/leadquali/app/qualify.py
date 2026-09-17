"""The pipeline: one lead in, one person or one recorded suppression out.

Load the tenant's policy, enrich, render the lead as untrusted data, assess, decide,
persist, dispatch, record. Every collaborator is a Protocol from
:mod:`leadquali.app.ports`, so this module imports nothing from ``adapters`` and the whole
of it runs in memory in a unit test — which is the only reason its failure paths can be
tested at all.

**This module is where invariant 3 stops being a principle and becomes control flow.** "A
lead is never silently dropped" is not enforceable in a docstring; it is enforced by there
being no branch that ends without either a dispatch or a recorded suppression. The
interesting decisions are all about what happens when something breaks:

* **Enrichment failing is not a failure.** It is an optimisation bought for a DNS lookup.
  A broken enricher degrades to "unavailable", the prompt says so, and the lead is assessed
  anyway. Gating a deal on an MX record would be an absurd trade.
* **An unassessable lead is still a routed lead.** A refusal, a timeout, a 5xx, a schema
  violation — and, defensively, an assessor that raises when its port says it must not —
  all become ``system_failure(...)``: ``WARM`` + ``EMAIL_SALES``, banner first, persisted
  and dispatched like any other lead. A failure of ours must never look like a judgement
  about the lead.
* **A dispatch failure re-raises.** See :meth:`QualificationPipeline.qualify`; the ordering
  of persistence before dispatch is what makes that safe.
* **A redelivery is a no-op, but only once the lead has actually been routed.** See
  :meth:`~leadquali.app.ports.LeadStorePort.already_routed`: the guard is "was this lead
  dealt with", never "have we seen this lead", because a worker that died between the
  insert and the send must be allowed to try again.
* **An escalation always has somewhere to go.** #9's confidence gate and its system-failure
  path both route ``WARM`` + ``EMAIL_SALES`` whatever the tenant's warm rule says, so
  ``destination_for(WARM)`` can be ``None`` while the action says email. An escalation with
  nowhere to go is a dropped lead by another name, so :meth:`_destination_for` falls back —
  to the tenant's own best inbox first, and to the operator's escalation address last. That
  address is a constructor argument rather than tenant configuration, so no customer can
  configure the last resort away, and it cannot be blank because the constructor refuses.

**Why the collaborators are constructor arguments.** They are deployment-scoped and the
lead is request-scoped: the worker Lambda builds one pipeline at module scope — where the
database engine, the SES client and the Anthropic client are created once and reused across
invocations — and calls :meth:`~QualificationPipeline.qualify` per SQS message. Threading
six collaborators through every call would put deployment wiring in the message loop and
make the signature that #17, #21 and #26 call three times as wide as the thing it does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    CallMetering,
)
from leadquali.app.enrichment import Enrichment, enrichment_block
from leadquali.app.ports import (
    ClockPort,
    EnricherPort,
    LeadAssessorPort,
    LeadStorePort,
    NotifierPort,
    RoutingOutcome,
    StoredLead,
    TenantConfigPort,
)
from leadquali.domain.models import Action, EscalationReason, RoutingDecision, Tier
from leadquali.domain.routing import decide, system_failure
from leadquali.domain.tenant_config import TenantConfig
from leadquali.observability import (
    ensure_trace_id,
    log_assessment,
    log_context,
    log_dispatch_failed,
    log_lead_duplicate,
    log_lead_routed,
    log_lead_suppressed,
)
from leadquali.prompts.lead import LeadSubmission, render_lead_detailed

LOGGER = logging.getLogger(__name__)

#: Where a lead came from when the caller does not say. Recorded on the lead row so a
#: tenant with a web form, a webhook and a CSV import can tell them apart later.
DEFAULT_SOURCE: Final[str] = "web_form"


class Disposition(StrEnum):
    """What the pipeline did with one delivery of one lead.

    Distinct from :class:`~leadquali.app.ports.RoutingOutcome`, which is what was written
    to ``routing_events``: this answers "what happened on this run", and
    :attr:`DUPLICATE` — a redelivery of a lead already routed — writes nothing at all,
    because the original run already wrote the final answer.

    There is deliberately no ``FAILED`` member. A run that could not finish raises, so that
    the message stays on the queue and is redelivered; a returned value saying "this went
    wrong" would let a caller acknowledge the message and lose the lead.
    """

    DISPATCHED = "dispatched"
    SUPPRESSED = "suppressed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class QualificationRequest:
    """One lead to qualify, as it arrives from the queue or from a local background task."""

    tenant_id: str
    submission_id: str
    """The idempotency key, unique per tenant. Assigned at ingest (#17) and echoed to the
    submitter, so a retried form post and an SQS redelivery both arrive with the same one."""

    submission: LeadSubmission
    source: str = DEFAULT_SOURCE
    received_at: datetime | None = None
    """When ingest accepted the lead. The clock is read when this is ``None``, so a message
    that sat in the queue for an hour is not recorded as having arrived an hour late."""

    trace_id: str | None = None
    """The id minted at ingest and carried on the queue message, so that the worker's half
    of this lead's journey is greppable together with the edge's. ``None`` — a CLI replay,
    a test, a message written before #21 — mints a fresh one rather than going untraced."""


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """What happened, in the terms #21's metrics and #26's worker need.

    Still returned rather than *only* logged, and for the original reason: a caller needs
    the outcome as a value — #26's worker acknowledges the SQS message on it, and the CLI
    prints from it — and a caller that had to parse its own log output would be absurd.

    What changed with #21 is that the pipeline also *emits* the outcome, which is not the
    same thing as deciding a log format. It calls
    :mod:`leadquali.observability.events`, which names the events and owns the field set;
    whether that becomes a JSON line or a human one is
    :func:`~leadquali.observability.logs.configure_logging`'s business, chosen once per
    process by the entry point. The pipeline is still reusable by a CLI, a test and a
    Lambda, and all three now produce the same events.
    """

    tenant_id: str
    lead_id: str
    submission_id: str
    disposition: Disposition
    decision: RoutingDecision | None
    """``None`` only for :attr:`Disposition.DUPLICATE`, where no new decision was made."""

    destination: str | None
    """Where the lead was actually sent; ``None`` for a suppression or a duplicate."""

    provider_message_id: str | None
    used_fallback_destination: bool
    """True when the tenant's routing table had no destination for the decided tier and the
    pipeline had to fall back. Worth an alert: it means a tenant's config disagrees with
    where its escalations are landing."""

    enrichment_available: bool
    """False when enrichment was attempted and could not complete. ``True`` for a duplicate,
    where nothing was attempted and nothing is missing."""

    is_new_lead: bool
    metering: CallMetering | None
    """The model call's cost and tokens, when there was a billed call."""

    latency_ms: int
    """Wall-clock cost of the whole pipeline, from a monotonic reading. Not the model's
    latency, which rides on :attr:`metering`."""

    trace_id: str
    """The id every log record from this run carries. Returned so the worker can put it on
    an SQS batch-item failure, where the log line and the failed message meet."""

    @property
    def dispatched(self) -> bool:
        """Whether a person was actually notified on this run."""
        return self.disposition is Disposition.DISPATCHED


class QualificationPipeline:
    """Qualifies leads. Built once per process, called once per lead.

    Args:
        config_source: the tenant's policy.
        assessor: the model, behind :class:`~leadquali.app.ports.LeadAssessorPort`.
        store: leads, assessments and routing events.
        notifier: how a person is reached.
        enricher: cheap deterministic signal; ``NullEnricher`` is a valid choice.
        clock: timestamps and latency, injected so tests are deterministic.
        escalation_destination: the last-resort address for a lead whose tenant has no
            usable destination for the tier it landed in. Deployment configuration, never
            tenant configuration — a customer must not be able to configure away the
            address that stops an escalation from evaporating.

    Raises:
        ValueError: ``escalation_destination`` is blank. Failing here means failing at
            deployment, in front of whoever is doing the deploying, rather than at 3am with
            an unroutable lead in hand.
    """

    def __init__(
        self,
        *,
        config_source: TenantConfigPort,
        assessor: LeadAssessorPort,
        store: LeadStorePort,
        notifier: NotifierPort,
        enricher: EnricherPort,
        clock: ClockPort,
        escalation_destination: str,
    ) -> None:
        cleaned = escalation_destination.strip()
        if not cleaned:
            raise ValueError(
                "a pipeline needs a non-empty escalation destination: it is where a lead "
                "goes when its tenant's routing table has nowhere to put it, and without "
                "one an escalation would have nowhere to land"
            )
        self._config_source = config_source
        self._assessor = assessor
        self._store = store
        self._notifier = notifier
        self._enricher = enricher
        self._clock = clock
        self._escalation_destination = cleaned

    # ------------------------------------------------------------------ the pipeline

    def qualify(self, request: QualificationRequest) -> QualificationResult:
        """Qualify one lead: config, enrich, render, assess, decide, persist, dispatch.

        Every step that can fail has a defined outcome, and none of them ends with a lead
        that was neither dispatched nor recorded:

        * **Tenant config missing or invalid** — raises. This is the one failure that
          cannot be degraded: with no policy there is no defensible destination, and
          routing a lead under a guessed policy is worse than retrying. The message stays
          on the queue, the DLQ alarms, and the lead is still there when the config is
          fixed.
        * **Store unreachable** — raises, for the same reason. Nothing has been dispatched
          at that point, so a redelivery costs one more model call and loses nothing.
        * **Enrichment failing** — degraded to "unavailable" and recorded in the prompt.
        * **Assessment failing** — routed through ``system_failure(...)``, persisted, and
          dispatched with the "system could not assess" banner.
        * **Dispatch failing** — a ``FAILED`` routing event is recorded (best effort) and
          the exception is **re-raised**, so SQS redelivers and, after N attempts, the DLQ
          alarms. Raising is safe *because the assessment was persisted first and the
          failed attempt is not a final routing event*: the redelivery re-runs the
          assessment and sends once. Swallowing it would acknowledge the message and lose
          the lead, which is the failure this whole module exists to prevent.
        * **Recording the routing event failing after a successful send** — raises. The
          redelivery may email sales twice, and a duplicate email is strictly the lesser
          evil against a lead that no record says was ever sent.

        Returns:
            A :class:`QualificationResult`. :attr:`Disposition.DUPLICATE` means this
            delivery was a repeat of one already routed and nothing was done — no model
            call, no email, no second row.
        """
        trace_id = ensure_trace_id(request.trace_id)
        with log_context(
            trace_id=trace_id,
            tenant_id=request.tenant_id,
            submission_id=request.submission_id,
        ):
            return self._qualify(request, trace_id)

    def _qualify(self, request: QualificationRequest, trace_id: str) -> QualificationResult:
        """The body of :meth:`qualify`, inside the trace context it binds.

        Split out rather than indenting the whole method under a ``with``: the sequence of
        steps is the readable thing about this code, and the binding is not one of them.
        """
        started_ms = self._clock.monotonic_ms()
        config = self._config_source.get(request.tenant_id)
        lead = self._store.upsert_lead(
            tenant_id=request.tenant_id,
            submission_id=request.submission_id,
            submission=request.submission,
            source=request.source,
            received_at=request.received_at or self._clock.now(),
        )

        # The idempotency check, before anything is spent or sent. `is_new` short-circuits
        # the query in the common case; `already_routed` is what actually decides, because
        # a lead inserted by a run that then died has never reached anybody.
        if not lead.is_new and self._store.already_routed(
            tenant_id=request.tenant_id, lead_id=lead.lead_id
        ):
            duplicate = self._result(
                request=request,
                lead=lead,
                trace_id=trace_id,
                disposition=Disposition.DUPLICATE,
                decision=None,
                destination=None,
                provider_message_id=None,
                used_fallback_destination=False,
                enrichment_available=True,
                metering=None,
                started_ms=started_ms,
            )
            log_lead_duplicate(
                LOGGER,
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                latency_ms=duplicate.latency_ms,
            )
            return duplicate

        enrichment = self._enrich(request)
        rendered = render_lead_detailed(request.submission)
        outcome = self._assess(config, _compose_user_turn(enrichment, rendered.text))
        decision = self._decide(outcome, config)

        # Before the branch, so that `Assessments` by `Tier` is the distribution over every
        # decision rather than over the ones that happened to be emailed. That completeness
        # is the whole basis of plan section 8's tier-distribution drift signal.
        log_assessment(
            LOGGER,
            tenant_id=request.tenant_id,
            lead_id=lead.lead_id,
            decision=decision,
            metering=outcome.metering,
            assessed=outcome.ok,
            confidence=outcome.assessment.confidence if outcome.ok else None,
            enrichment_available=enrichment.available,
        )

        self._store.record_assessment(
            tenant_id=request.tenant_id,
            lead_id=lead.lead_id,
            outcome=outcome,
            decision=decision,
            recorded_at=self._clock.now(),
        )

        if decision.action is Action.SUPPRESS:
            # Recorded, never silent. `decide` reaches SUPPRESS only from an explicit spam
            # determination or a tier the tenant configured to suppress, and its note says
            # which — the two are different answers to "why was this lead never contacted?"
            self._store.record_routing_event(
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                action=decision.action,
                destination=None,
                outcome=RoutingOutcome.SUPPRESSED,
                provider_message_id=None,
                occurred_at=self._clock.now(),
                detail=decision.note,
            )
            suppressed = self._result(
                request=request,
                lead=lead,
                trace_id=trace_id,
                disposition=Disposition.SUPPRESSED,
                decision=decision,
                destination=None,
                provider_message_id=None,
                used_fallback_destination=False,
                enrichment_available=enrichment.available,
                metering=outcome.metering,
                started_ms=started_ms,
            )
            log_lead_suppressed(
                LOGGER,
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                decision=decision,
                latency_ms=suppressed.latency_ms,
            )
            return suppressed

        destination, used_fallback = self._destination_for(config, decision)
        try:
            provider_message_id = self._notifier.dispatch(
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                destination=destination,
                submission=request.submission,
                decision=decision,
                assessment=outcome.assessment if outcome.ok else None,
            )
        except Exception as error:
            self._record_failed_dispatch(request, lead, decision, destination, error)
            log_dispatch_failed(
                LOGGER,
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                tier=decision.tier,
                destination=destination,
                error=error,
            )
            raise

        self._store.record_routing_event(
            tenant_id=request.tenant_id,
            lead_id=lead.lead_id,
            action=decision.action,
            destination=destination,
            outcome=RoutingOutcome.DISPATCHED,
            provider_message_id=provider_message_id,
            occurred_at=self._clock.now(),
            detail=decision.note,
        )
        dispatched = self._result(
            request=request,
            lead=lead,
            trace_id=trace_id,
            disposition=Disposition.DISPATCHED,
            decision=decision,
            destination=destination,
            provider_message_id=provider_message_id,
            used_fallback_destination=used_fallback,
            enrichment_available=enrichment.available,
            metering=outcome.metering,
            started_ms=started_ms,
        )
        log_lead_routed(
            LOGGER,
            tenant_id=request.tenant_id,
            lead_id=lead.lead_id,
            tier=decision.tier,
            action=decision.action,
            destination=destination,
            used_fallback_destination=used_fallback,
            provider_message_id=provider_message_id,
            latency_ms=dispatched.latency_ms,
        )
        return dispatched

    # ------------------------------------------------------------------------- steps

    def _enrich(self, request: QualificationRequest) -> Enrichment:
        """Enrich, or degrade. Enrichment is an optimisation and never a gate.

        The port says implementations fail open rather than raise. This catches anyway:
        the cost of the port being wrong is a lost lead, and the cost of the catch is a
        line. Only the exception's class reaches the prompt — its message could quote the
        lead (invariant 5).
        """
        try:
            return self._enricher.enrich(tenant_id=request.tenant_id, submission=request.submission)
        except Exception as error:
            return Enrichment.unavailable(f"enricher raised {type(error).__name__}")

    def _assess(self, config: TenantConfig, user_turn: str) -> AssessmentOutcome:
        """Assess, converting an unexpected raise into the failure the router understands.

        :class:`~leadquali.app.ports.LeadAssessorPort` says a refusal, a timeout, a 5xx and
        a parse error all come back as values. A raise means the adapter has a bug — and a
        bug in the adapter must still cost only a human's minute, not the deal.
        """
        started_ms = self._clock.monotonic_ms()
        try:
            return self._assessor.assess(config=config, rendered_lead=user_turn)
        except Exception as error:
            return AssessmentFailed(
                reason=EscalationReason.API_ERROR,
                detail=f"assessor raised {type(error).__name__}",
                latency_ms=max(0, self._clock.monotonic_ms() - started_ms),
            )

    def _decide(self, outcome: AssessmentOutcome, config: TenantConfig) -> RoutingDecision:
        """Apply #9's deterministic policy to whatever came back from the model."""
        if outcome.ok:
            return decide(outcome.assessment, config)
        return system_failure(outcome.reason, outcome.detail)

    def _destination_for(self, config: TenantConfig, decision: RoutingDecision) -> tuple[str, bool]:
        """Resolve where a non-suppressed lead goes. Never returns a blank destination.

        The tenant's rule for the decided tier comes first. When it has none — which
        happens whenever an escalation lands on a tier the tenant configured to suppress —
        the fallback is the tenant's own highest-tier destination, because a lead the
        system could not judge belongs in front of *this customer's* salespeople rather
        than ours. Only a tenant that has switched off every tier reaches the operator's
        escalation address, and it always reaches it: a lead may not evaporate because a
        routing table said nothing about it.

        Returns:
            The destination and whether it came from a fallback rather than from the rule
            for the decided tier.
        """
        configured = _clean(config.destination_for(decision.tier))
        if configured:
            return configured, False
        for tier in sorted(Tier, key=lambda candidate: candidate.rank, reverse=True):
            fallback = _clean(config.destination_for(tier))
            if fallback:
                return fallback, True
        return self._escalation_destination, True

    def _record_failed_dispatch(
        self,
        request: QualificationRequest,
        lead: StoredLead,
        decision: RoutingDecision,
        destination: str,
        error: BaseException,
    ) -> None:
        """Record the attempt so a lead stuck behind a broken notifier is visible.

        Best effort by design: the caller re-raises the dispatch error immediately after,
        and a store that is also down must not replace the error the worker retries on with
        a different one. The event is :attr:`~leadquali.app.ports.RoutingOutcome.FAILED`,
        which ``already_routed`` ignores, so the redelivery tries again rather than
        treating this lead as finished.
        """
        try:
            self._store.record_routing_event(
                tenant_id=request.tenant_id,
                lead_id=lead.lead_id,
                action=decision.action,
                destination=destination,
                outcome=RoutingOutcome.FAILED,
                provider_message_id=None,
                occurred_at=self._clock.now(),
                detail=f"dispatch failed: {type(error).__name__}",
            )
        except Exception:
            # Telemetry on an already-failing path. The dispatch error is the one the
            # worker must see; masking it with a store error would cost a retry.
            return

    def _result(
        self,
        *,
        request: QualificationRequest,
        lead: StoredLead,
        trace_id: str,
        disposition: Disposition,
        decision: RoutingDecision | None,
        destination: str | None,
        provider_message_id: str | None,
        used_fallback_destination: bool,
        enrichment_available: bool,
        metering: CallMetering | None,
        started_ms: int,
    ) -> QualificationResult:
        return QualificationResult(
            tenant_id=request.tenant_id,
            lead_id=lead.lead_id,
            submission_id=request.submission_id,
            disposition=disposition,
            decision=decision,
            destination=destination,
            provider_message_id=provider_message_id,
            used_fallback_destination=used_fallback_destination,
            enrichment_available=enrichment_available,
            is_new_lead=lead.is_new,
            metering=metering,
            latency_ms=max(0, self._clock.monotonic_ms() - started_ms),
            trace_id=trace_id,
        )


def _compose_user_turn(enrichment: Enrichment, rendered_lead: str) -> str:
    """Put the verified facts ahead of the untrusted submission.

    Order matters twice over. The facts are what the model should weigh against the lead's
    own claims, so they come first; and #12's rendering ends with the instruction to assess
    and reply, which must stay the last thing the model reads. An empty block concatenates
    to nothing, so an unenriched deployment sends exactly the turn it sent before.
    """
    block = enrichment_block(enrichment)
    return f"{block}\n\n{rendered_lead}" if block else rendered_lead


def _clean(destination: str | None) -> str:
    """A destination stripped of whitespace, or ``""`` when there is effectively none."""
    return destination.strip() if destination else ""


__all__ = [
    "DEFAULT_SOURCE",
    "Disposition",
    "QualificationPipeline",
    "QualificationRequest",
    "QualificationResult",
]
