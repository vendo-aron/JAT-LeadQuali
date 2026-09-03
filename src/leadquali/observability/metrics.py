"""Metrics that ride on the log line: CloudWatch Embedded Metric Format.

**Why EMF and not ``PutMetricData``.** The alternative is an API call per lead from inside
the worker, and it is worse on every axis that matters here. It costs 20-80 ms of Lambda
wall time that the customer's lead is waiting on; it costs money per call on top of the
metric itself; it needs ``cloudwatch:PutMetricData`` on a role that otherwise touches only
SQS, SES and Postgres; and — the part that decides it — it *can fail*, which means either
swallowing the failure (an alarm that silently stops being fed is worse than no alarm) or
failing a lead because telemetry was unavailable. EMF has none of that: the metric is a
field on a log line the process was writing anyway, CloudWatch Logs extracts it
server-side, and the numbers stay queryable in Logs Insights as raw fields even where no
metric was ever created. Emitting costs one ``json.dumps`` and cannot fail.

**It degrades honestly off AWS.** The document is ordinary JSON. Locally, and in the tests,
the same call renders as a readable ``Assessments=1 CostUsd=0.0213`` line
(:func:`to_text`) and nothing is lost but the server-side aggregation.

**The shape.** One log line carries a ``_aws.CloudWatchMetrics`` list of *directives*, each
naming a namespace, a dimension set and the metrics published under it; the dimension and
metric values themselves sit at the root of the same object. Publishing one metric under
two dimension sets — ``Assessments`` by ``(TenantId, Tier)`` for the drift signal and by
``(TenantId)`` for the total — is two directives reading one root value, which is why the
tier breakdown costs no extra emission.

**Cardinality is a bill.** Every (metric, dimension-value) combination is a custom metric,
charged monthly. The directives here are deliberately narrow: no dimension takes an
unbounded value, ``lead_id`` and ``trace_id`` stay ordinary log fields (queryable in
Logs Insights, free) rather than dimensions, and the per-tenant total is around twenty
metrics. Adding a dimension is a pricing decision, not a formatting one.

Names are ``PascalCase`` because CloudWatch's console, the ``aws cloudwatch`` CLI and every
alarm definition show them verbatim — and because it keeps them from ever colliding with
the ``snake_case`` log field set they share a line with.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

#: The CloudWatch namespace every metric below is published into. #29 alarms are written
#: against this string; changing it orphans every alarm and every dashboard.
METRIC_NAMESPACE: Final[str] = "LeadQuali"

# --------------------------------------------------------------------------- dimensions

#: Present on every directive. Metrics are per tenant first, because "is this broken" is
#: almost always "is this broken for one customer".
DIM_TENANT: Final[str] = "TenantId"

#: The decided tier. The tier-distribution drift signal (plan §8) is this dimension on
#: :data:`ASSESSMENTS`, and nothing else is needed to compute it.
DIM_TIER: Final[str] = "Tier"

#: Why a lead went to a human: ``low_confidence``, ``model_refusal``, ``parse_error``,
#: ``api_error``, ``timeout``. Five values; a rise in each wakes a different person.
DIM_ESCALATION_REASON: Final[str] = "EscalationReason"

#: Why a lead was never contacted: ``spam`` or ``below_threshold``. #52 gave the two
#: suppressions distinguishable notes precisely so this dimension could exist.
DIM_SUPPRESSION_CAUSE: Final[str] = "SuppressionCause"

#: Which deterministic pre-filter caught a submission at the edge.
DIM_SPAM_REASON: Final[str] = "SpamReason"

#: What ingest did with a submission: ``queued``, ``suppressed``, ``duplicate``.
DIM_DISPOSITION: Final[str] = "Disposition"

# ------------------------------------------------------------------------ metric names

#: Assessments completed, whether or not the model answered. The tier breakdown of this
#: metric is the drift signal.
ASSESSMENTS: Final[str] = "Assessments"

#: Assessments where no judgement was obtained (refusal, timeout, 5xx, parse error). The
#: lead was still routed to a human — this counts *our* failures, not lost leads.
ASSESSMENT_FAILURES: Final[str] = "AssessmentFailures"

#: Leads that reached a human because the system was unsure or broken, by reason.
ESCALATIONS: Final[str] = "Escalations"

INPUT_TOKENS: Final[str] = "InputTokens"
OUTPUT_TOKENS: Final[str] = "OutputTokens"
CACHE_READ_TOKENS: Final[str] = "CacheReadTokens"
CACHE_CREATION_TOKENS: Final[str] = "CacheCreationTokens"

#: Dollars for one model call. Summed over a day, this is the token-spend alarm and #33's
#: billing meter; averaged over a day it is cost per lead, without opening the database.
COST_USD: Final[str] = "CostUsd"

#: The model call, retries included. Distinct from :data:`PIPELINE_LATENCY_MS`, which is
#: the whole lead: when p99 rises, the gap between them says whether it is Anthropic or us.
MODEL_LATENCY_MS: Final[str] = "ModelLatencyMs"

#: Wall clock for one lead through the pipeline: config, enrich, assess, persist, dispatch.
PIPELINE_LATENCY_MS: Final[str] = "PipelineLatencyMs"

#: Leads assessed without enrichment because the enricher failed (#18). Not an error — the
#: lead was assessed anyway — but a sustained rise means every lead is being judged on less.
ENRICHMENT_UNAVAILABLE: Final[str] = "EnrichmentUnavailable"

#: Leads a person was actually notified about.
DISPATCHES: Final[str] = "Dispatches"

#: Dispatch attempts that raised. These are redelivered by SQS and land in the DLQ if they
#: keep failing, so this metric is the early warning the DLQ alarm confirms.
DISPATCH_FAILURES: Final[str] = "DispatchFailures"

#: Dispatches that had to fall back because the tenant's routing table had no destination
#: for the decided tier. A configuration bug, visible before a customer reports it.
FALLBACK_DESTINATIONS: Final[str] = "FallbackDestinations"

#: Leads recorded and never contacted, by cause.
SUPPRESSIONS: Final[str] = "Suppressions"

#: Redeliveries of a lead already routed. Normal at a low rate (SQS is at-least-once);
#: a spike means the idempotency guard is doing work something else should have done.
DUPLICATES: Final[str] = "Duplicates"

#: Submissions accepted at the public edge, by disposition.
INGESTED_LEADS: Final[str] = "IngestedLeads"

#: Submissions the deterministic pre-filters stopped before any model call, by filter.
INGEST_SUPPRESSIONS: Final[str] = "IngestSuppressions"


class Unit(StrEnum):
    """The CloudWatch units this system uses. Not the full list — the full list is noise.

    :attr:`NONE` is CloudWatch's own name for "a number with no unit", and it is what money
    gets: there is no currency unit, and mislabelling dollars as ``Count`` makes a
    dashboard read "21.3 milli" at a glance.
    """

    COUNT = "Count"
    MILLISECONDS = "Milliseconds"
    NONE = "None"


@dataclass(frozen=True, slots=True)
class Metric:
    """One named number to publish."""

    name: str
    value: int | float | Decimal
    unit: Unit = Unit.COUNT


@dataclass(frozen=True, slots=True)
class MetricSet:
    """The metrics published under one dimension set.

    An empty ``dimensions`` tuple is legal EMF and means "publish this against the
    namespace with no dimensions at all"; nothing here uses it, because a metric with no
    tenant on it cannot answer the only question anybody asks of it.
    """

    dimensions: tuple[str, ...]
    metrics: tuple[Metric, ...]


@dataclass(frozen=True, slots=True)
class MetricPayload:
    """Everything one log line publishes: the dimension values and the directives.

    There is no separate "properties" bag. The formatter merges this document into the log
    record it rides on, so the record's own fields — ``trace_id``, ``lead_id``,
    ``model_id``, ``prompt_version`` — are already at the root of the same object, free to
    query in Logs Insights and impossible to get out of step with the metrics beside them.
    """

    dimensions: Mapping[str, str]
    metric_sets: tuple[MetricSet, ...]


def to_emf(
    payload: MetricPayload, *, timestamp_ms: int, namespace: str = METRIC_NAMESPACE
) -> dict[str, Any]:
    """Render ``payload`` as an EMF document.

    Args:
        payload: the dimension values and directives to publish.
        timestamp_ms: the event time in epoch milliseconds. Supplied by the formatter from
            the log record's own creation time, so the metric is stamped when the thing
            happened rather than when it was serialised.
        namespace: the CloudWatch namespace. Overridable for tests only.

    Returns:
        A JSON-safe ``dict`` whose ``_aws`` key CloudWatch Logs recognises, with every
        dimension value and metric value at the root.

    Raises:
        ValueError: a directive names a dimension the payload has no value for, or a
            dimension value is blank, or two directives disagree about a metric's value.
            All three produce a document CloudWatch silently drops the metrics from, and a
            metric that silently stops arriving is exactly the failure mode observability
            exists to prevent — so it fails here, loudly, in a test.
    """
    root: dict[str, Any] = {}
    directives: list[dict[str, Any]] = []

    for metric_set in payload.metric_sets:
        for name in metric_set.dimensions:
            value = payload.dimensions.get(name)
            if value is None or not str(value).strip():
                raise ValueError(
                    f"metric dimension {name!r} has no value; CloudWatch would drop "
                    f"{[metric.name for metric in metric_set.metrics]}"
                )
            root[name] = str(value)
        for metric in metric_set.metrics:
            number = _as_number(metric.value)
            existing = root.get(metric.name)
            if existing is not None and existing != number:
                raise ValueError(
                    f"metric {metric.name!r} is published twice on one line with different "
                    f"values ({existing!r} and {number!r}); EMF reads one root value"
                )
            root[metric.name] = number
        directives.append(
            {
                "Namespace": namespace,
                "Dimensions": [list(metric_set.dimensions)],
                "Metrics": [
                    {"Name": metric.name, "Unit": metric.unit.value}
                    for metric in metric_set.metrics
                ],
            }
        )

    root["_aws"] = {"Timestamp": timestamp_ms, "CloudWatchMetrics": directives}
    return root


def to_text(payload: MetricPayload) -> str:
    """Render ``payload`` for a human: ``Assessments=1 CostUsd=0.0213``.

    Used by the local formatter. Dimensions are left out — they are already on the line as
    ``tenant_id`` and ``tier`` — so what is left is the numbers somebody is watching.
    """
    seen: dict[str, int | float] = {}
    for metric_set in payload.metric_sets:
        for metric in metric_set.metrics:
            seen.setdefault(metric.name, _as_number(metric.value))
    return " ".join(f"{name}={_trim(value)}" for name, value in seen.items())


def _as_number(value: int | float | Decimal) -> int | float:
    """A JSON number. ``Decimal`` serialises as a *string* by default, and a metric that
    arrives as ``"0.0213"`` is not a metric — CloudWatch drops the whole directive."""
    return float(value) if isinstance(value, Decimal) else value


def _trim(value: int | float) -> str:
    return str(value) if isinstance(value, int) else f"{value:g}"


__all__ = [
    "ASSESSMENTS",
    "ASSESSMENT_FAILURES",
    "CACHE_CREATION_TOKENS",
    "CACHE_READ_TOKENS",
    "COST_USD",
    "DIM_DISPOSITION",
    "DIM_ESCALATION_REASON",
    "DIM_SPAM_REASON",
    "DIM_SUPPRESSION_CAUSE",
    "DIM_TENANT",
    "DIM_TIER",
    "DISPATCHES",
    "DISPATCH_FAILURES",
    "DUPLICATES",
    "ENRICHMENT_UNAVAILABLE",
    "ESCALATIONS",
    "FALLBACK_DESTINATIONS",
    "INGESTED_LEADS",
    "INGEST_SUPPRESSIONS",
    "INPUT_TOKENS",
    "METRIC_NAMESPACE",
    "MODEL_LATENCY_MS",
    "OUTPUT_TOKENS",
    "PIPELINE_LATENCY_MS",
    "SUPPRESSIONS",
    "Metric",
    "MetricPayload",
    "MetricSet",
    "Unit",
    "to_emf",
    "to_text",
]
