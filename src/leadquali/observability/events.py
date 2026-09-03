"""The event catalogue: every log line and every metric this system emits, in one file.

There is one function per event, and no caller anywhere builds a field dict by hand. That
is the point of the module: the field set and the metric set are a published contract —
#29 writes alarms against these metric names, #33 meters ``CostUsd`` for billing, and a
Logs Insights query saved in a runbook filters on these field names — and a contract
scattered across six call sites is a contract that drifts. Adding a field here is a
deliberate change to a documented surface; adding one at a call site would not be.

The functions take a ``logger`` so the ``logger`` field still names the module the event
came from rather than this one, and they take ``tenant_id`` and ``lead_id`` explicitly —
the first because it is a metric dimension, the second because it is only known halfway
through the pipeline and threading it through beats re-binding the context mid-method.
Everything else rides on :func:`~leadquali.observability.context.log_context`: ``trace_id``
and ``submission_id`` are bound once by whoever owns the lead and appear on every record,
including the ones written by SQLAlchemy and botocore underneath.

Nothing here takes a ``LeadSubmission``, an address or a note written by a lead. The
signatures are the enforcement: a function that cannot be passed PII cannot log it.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import StrEnum
from typing import Final

from leadquali.app.assessment_result import CallMetering
from leadquali.domain.models import Action, RoutingDecision, Tier
from leadquali.domain.routing import LOW_SCORE_SUPPRESSION_NOTE, SPAM_NOTE
from leadquali.observability.logs import log_event
from leadquali.observability.metrics import (
    ASSESSMENT_FAILURES,
    ASSESSMENTS,
    CACHE_CREATION_TOKENS,
    CACHE_READ_TOKENS,
    COST_USD,
    DIM_DISPOSITION,
    DIM_ESCALATION_REASON,
    DIM_SPAM_REASON,
    DIM_SUPPRESSION_CAUSE,
    DIM_TENANT,
    DIM_TIER,
    DISPATCH_FAILURES,
    DISPATCHES,
    DUPLICATES,
    ENRICHMENT_UNAVAILABLE,
    ESCALATIONS,
    FALLBACK_DESTINATIONS,
    INGEST_SUPPRESSIONS,
    INGESTED_LEADS,
    INPUT_TOKENS,
    MODEL_LATENCY_MS,
    OUTPUT_TOKENS,
    PIPELINE_LATENCY_MS,
    SUPPRESSIONS,
    Metric,
    MetricPayload,
    MetricSet,
    Unit,
)
from leadquali.observability.pii import contact_email_hash

#: A submission was accepted at the public edge and queued, suppressed or recognised as a
#: redelivery. The first line of every lead's journey and where its trace id is minted.
EVENT_LEAD_ACCEPTED: Final[str] = "lead.accepted"

#: One assessment finished — successfully or not — and a routing decision was reached.
#: This is the per-assessment metrics line: tokens, cost, latency, tier, provenance.
EVENT_ASSESSMENT: Final[str] = "assessment.completed"

#: A person was notified.
EVENT_LEAD_ROUTED: Final[str] = "lead.routed"

#: A lead was recorded and deliberately not contacted, with the cause.
EVENT_LEAD_SUPPRESSED: Final[str] = "lead.suppressed"

#: A redelivery of a lead already routed. Nothing was done, on purpose.
EVENT_LEAD_DUPLICATE: Final[str] = "lead.duplicate"

#: Dispatch raised. The lead is still on the queue and will be retried.
EVENT_DISPATCH_FAILED: Final[str] = "lead.dispatch_failed"


class SuppressionCause(StrEnum):
    """Why a lead was never contacted. The two answers are not interchangeable.

    A rise in :attr:`SPAM` is a bot campaign, and a rise in :attr:`BELOW_THRESHOLD` is a
    rubric or a form change — the first is somebody else's problem and the second is ours.
    #52 gave the two suppressions distinguishable notes so this distinction could survive
    into a metric dimension; :func:`suppression_cause` is where that is cashed in.
    """

    SPAM = "spam"
    BELOW_THRESHOLD = "below_threshold"
    UNKNOWN = "unknown"
    """A suppression reached by a path that did not exist when this was written. Emitted
    rather than guessed at: a metric that quietly attributes a new cause to an old bucket
    is worse than one that says it does not know."""


def suppression_cause(decision: RoutingDecision) -> SuppressionCause:
    """Classify a suppression by the note ``domain.routing`` stamped on it."""
    if decision.note.startswith(SPAM_NOTE):
        return SuppressionCause.SPAM
    if decision.note.startswith(LOW_SCORE_SUPPRESSION_NOTE):
        return SuppressionCause.BELOW_THRESHOLD
    return SuppressionCause.UNKNOWN


def log_lead_accepted(
    logger: logging.Logger,
    *,
    tenant_id: str,
    lead_id: str,
    disposition: str,
    source: str,
    is_new_lead: bool,
    email: str | None,
    spam_reason: str | None,
) -> None:
    """The edge accepted one submission.

    Args:
        email: the contact address, hashed here and never emitted. Taking the address and
            hashing it in one place is what keeps every call site from having to remember;
            the parameter exists so no caller is tempted to pass the address as a field.
    """
    sets = [
        MetricSet((DIM_TENANT, DIM_DISPOSITION), (Metric(INGESTED_LEADS, 1),)),
    ]
    dimensions = {DIM_TENANT: tenant_id, DIM_DISPOSITION: disposition}
    if spam_reason is not None:
        dimensions[DIM_SPAM_REASON] = spam_reason
        sets.append(MetricSet((DIM_TENANT, DIM_SPAM_REASON), (Metric(INGEST_SUPPRESSIONS, 1),)))

    log_event(
        logger,
        EVENT_LEAD_ACCEPTED,
        lead_id=lead_id,
        disposition=disposition,
        source=source,
        is_new_lead=is_new_lead,
        contact_email_hash=contact_email_hash(email),
        spam_reason=spam_reason,
        metrics=MetricPayload(dimensions=dimensions, metric_sets=tuple(sets)),
    )


def log_assessment(
    logger: logging.Logger,
    *,
    tenant_id: str,
    lead_id: str,
    decision: RoutingDecision,
    metering: CallMetering | None,
    assessed: bool,
    confidence: float | None,
    enrichment_available: bool,
) -> None:
    """One assessment and the decision it produced — the per-assessment metrics line.

    Emitted for *every* decision, successful or not, before the suppress/dispatch branch,
    which is what makes ``Assessments`` by ``Tier`` a complete tier distribution rather
    than a distribution over the leads that happened to be emailed.

    ``metering`` is emitted exactly as :mod:`leadquali.adapters.llm_anthropic` computed it.
    Nothing here recomputes a token count or a cost: two definitions of what a lead cost
    is a billing dispute with a customer.

    Args:
        assessed: whether the model returned a judgement. ``False`` still produces a full
            line — a refusal is billed, and a failure that is not counted is an outage
            nobody is paged for.
        confidence: the model's own confidence, or ``None`` when there is no assessment.
    """
    reason = decision.escalation_reason
    dimensions = {DIM_TENANT: tenant_id, DIM_TIER: decision.tier.value}
    totals: list[Metric] = [
        Metric(ASSESSMENTS, 1),
        Metric(ASSESSMENT_FAILURES, 0 if assessed else 1),
        Metric(ENRICHMENT_UNAVAILABLE, 0 if enrichment_available else 1),
    ]
    if metering is not None:
        totals.extend(
            (
                Metric(INPUT_TOKENS, metering.input_tokens),
                Metric(OUTPUT_TOKENS, metering.output_tokens),
                Metric(CACHE_READ_TOKENS, metering.cache_read_tokens),
                Metric(CACHE_CREATION_TOKENS, metering.cache_creation_tokens),
                Metric(COST_USD, metering.cost_usd, Unit.NONE),
                Metric(MODEL_LATENCY_MS, metering.latency_ms, Unit.MILLISECONDS),
            )
        )
    sets = [
        MetricSet((DIM_TENANT, DIM_TIER), (Metric(ASSESSMENTS, 1),)),
        MetricSet((DIM_TENANT,), tuple(totals)),
    ]
    if reason is not None:
        dimensions[DIM_ESCALATION_REASON] = reason.value
        sets.append(MetricSet((DIM_TENANT, DIM_ESCALATION_REASON), (Metric(ESCALATIONS, 1),)))

    log_event(
        logger,
        EVENT_ASSESSMENT,
        lead_id=lead_id,
        assessed=assessed,
        tier=decision.tier.value,
        action=decision.action.value,
        total_score=decision.total_score,
        confidence=confidence,
        escalation_reason=reason.value if reason is not None else None,
        enrichment_available=enrichment_available,
        fields=_metering_fields(metering),
        metrics=MetricPayload(dimensions=dimensions, metric_sets=tuple(sets)),
    )


def log_lead_routed(
    logger: logging.Logger,
    *,
    tenant_id: str,
    lead_id: str,
    tier: Tier,
    action: Action,
    destination: str,
    used_fallback_destination: bool,
    provider_message_id: str | None,
    latency_ms: int,
) -> None:
    """A person was notified.

    ``destination`` is hashed on the way in for the same reason a contact address is: it is
    an individual salesperson's mailbox, and an operator needs to tell two destinations
    apart far more often than they need to read one.
    """
    log_event(
        logger,
        EVENT_LEAD_ROUTED,
        lead_id=lead_id,
        tier=tier.value,
        action=action.value,
        destination_hash=contact_email_hash(destination),
        used_fallback_destination=used_fallback_destination,
        provider_message_id=provider_message_id,
        latency_ms=latency_ms,
        metrics=MetricPayload(
            dimensions={DIM_TENANT: tenant_id, DIM_TIER: tier.value},
            metric_sets=(
                MetricSet((DIM_TENANT, DIM_TIER), (Metric(DISPATCHES, 1),)),
                MetricSet(
                    (DIM_TENANT,),
                    (
                        Metric(DISPATCHES, 1),
                        Metric(FALLBACK_DESTINATIONS, 1 if used_fallback_destination else 0),
                        Metric(PIPELINE_LATENCY_MS, latency_ms, Unit.MILLISECONDS),
                    ),
                ),
            ),
        ),
    )


def log_lead_suppressed(
    logger: logging.Logger,
    *,
    tenant_id: str,
    lead_id: str,
    decision: RoutingDecision,
    latency_ms: int,
) -> None:
    """A lead was recorded and deliberately not contacted."""
    cause = suppression_cause(decision)
    log_event(
        logger,
        EVENT_LEAD_SUPPRESSED,
        lead_id=lead_id,
        tier=decision.tier.value,
        suppression_cause=cause.value,
        total_score=decision.total_score,
        latency_ms=latency_ms,
        metrics=MetricPayload(
            dimensions={DIM_TENANT: tenant_id, DIM_SUPPRESSION_CAUSE: cause.value},
            metric_sets=(
                MetricSet((DIM_TENANT, DIM_SUPPRESSION_CAUSE), (Metric(SUPPRESSIONS, 1),)),
                MetricSet(
                    (DIM_TENANT,), (Metric(PIPELINE_LATENCY_MS, latency_ms, Unit.MILLISECONDS),)
                ),
            ),
        ),
    )


def log_lead_duplicate(
    logger: logging.Logger, *, tenant_id: str, lead_id: str, latency_ms: int
) -> None:
    """A redelivery of a lead that was already routed. No model call, no email, no row."""
    log_event(
        logger,
        EVENT_LEAD_DUPLICATE,
        lead_id=lead_id,
        latency_ms=latency_ms,
        metrics=MetricPayload(
            dimensions={DIM_TENANT: tenant_id},
            metric_sets=(
                MetricSet(
                    (DIM_TENANT,),
                    (
                        Metric(DUPLICATES, 1),
                        Metric(PIPELINE_LATENCY_MS, latency_ms, Unit.MILLISECONDS),
                    ),
                ),
            ),
        ),
    )


def log_dispatch_failed(
    logger: logging.Logger,
    *,
    tenant_id: str,
    lead_id: str,
    tier: Tier,
    destination: str,
    error: BaseException,
) -> None:
    """Dispatch raised; the lead stays on the queue.

    Logged at ``ERROR`` with the traceback attached, because this is the line an operator
    is looking at when the DLQ alarm fires. The traceback goes through the formatter's
    redaction and carries no locals — see
    :meth:`~leadquali.observability.logs._BaseFormatter._exception`.
    """
    log_event(
        logger,
        EVENT_DISPATCH_FAILED,
        level=logging.ERROR,
        lead_id=lead_id,
        exc_info=error,
        tier=tier.value,
        destination_hash=contact_email_hash(destination),
        error_type=type(error).__name__,
        metrics=MetricPayload(
            dimensions={DIM_TENANT: tenant_id},
            metric_sets=(MetricSet((DIM_TENANT,), (Metric(DISPATCH_FAILURES, 1),)),),
        ),
    )


def _metering_fields(metering: CallMetering | None) -> dict[str, str | int | Decimal | None]:
    """The metering, flattened into log fields under the names the plan uses."""
    if metering is None:
        return {}
    return {
        "model_id": metering.model_id,
        "prompt_version": metering.prompt_version,
        "effort": metering.effort,
        "input_tokens": metering.input_tokens,
        "output_tokens": metering.output_tokens,
        "cache_read_tokens": metering.cache_read_tokens,
        "cache_creation_tokens": metering.cache_creation_tokens,
        "cost_usd": metering.cost_usd,
        "model_latency_ms": metering.latency_ms,
    }


__all__ = [
    "EVENT_ASSESSMENT",
    "EVENT_DISPATCH_FAILED",
    "EVENT_LEAD_ACCEPTED",
    "EVENT_LEAD_DUPLICATE",
    "EVENT_LEAD_ROUTED",
    "EVENT_LEAD_SUPPRESSED",
    "SuppressionCause",
    "log_assessment",
    "log_dispatch_failed",
    "log_lead_accepted",
    "log_lead_duplicate",
    "log_lead_routed",
    "log_lead_suppressed",
    "suppression_cause",
]
