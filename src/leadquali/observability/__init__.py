"""Structured logs, trace ids and CloudWatch metrics — the things #29 alarms on.

Plan §8 and issue #21. The field set, the metric names and the alarms #29 should build on
them are written up in ``docs/observability.md``; that file is the contract, and this
package is its implementation. Five modules, each with one job:

* :mod:`~leadquali.observability.context` — the trace id and the ambient identifiers every
  record inherits, on a ``ContextVar``.
* :mod:`~leadquali.observability.logs` — :func:`configure_logging` and the two formatters.
  JSON in every deployed environment, prose locally, the same fields either way.
* :mod:`~leadquali.observability.metrics` — CloudWatch Embedded Metric Format, so a metric
  is a field on a log line rather than an API call in the path of a customer's lead.
* :mod:`~leadquali.observability.pii` — the hash that may be logged, and the redaction net
  for what arrives from outside.
* :mod:`~leadquali.observability.events` — the event catalogue: one function per emitted
  event, so the field set and the metric names are a contract in one file rather than a
  convention spread over six.

**Where this sits in the layering.** ``CLAUDE.md`` puts ``domain`` below ``app`` below
``adapters``/``api``. This package sits beside all of them, like the standard library's
``logging`` that it wraps: it is pure Python, it performs no I/O beyond writing a line to a
stream, it imports no SDK, and it reads value types (``RoutingDecision``, ``CallMetering``)
without ever calling back into the layers that own them. That is what lets the pipeline,
the ingest service and the adapters all emit the same field set — and it is checked, not
asserted: ``tests/unit/test_layering.py`` fails if this package ever imports ``adapters``
or ``api``.
"""

from __future__ import annotations

from leadquali.observability.context import (
    CONTEXT_FIELDS,
    TRACE_ID,
    current_context,
    current_trace_id,
    ensure_trace_id,
    log_context,
    new_trace_id,
)
from leadquali.observability.events import (
    EVENT_ASSESSMENT,
    EVENT_DISPATCH_FAILED,
    EVENT_LEAD_ACCEPTED,
    EVENT_LEAD_DUPLICATE,
    EVENT_LEAD_ROUTED,
    EVENT_LEAD_SUPPRESSED,
    SuppressionCause,
    log_assessment,
    log_dispatch_failed,
    log_lead_accepted,
    log_lead_duplicate,
    log_lead_routed,
    log_lead_suppressed,
    suppression_cause,
)
from leadquali.observability.logs import (
    LOG_FORMAT_HUMAN,
    LOG_FORMAT_JSON,
    SERVICE_NAME,
    HumanLogFormatter,
    JsonLogFormatter,
    configure_logging,
    log_event,
)
from leadquali.observability.metrics import (
    METRIC_NAMESPACE,
    Metric,
    MetricPayload,
    MetricSet,
    Unit,
)
from leadquali.observability.pii import EMAIL_REDACTION, contact_email_hash, redact_emails

__all__ = [
    "CONTEXT_FIELDS",
    "EMAIL_REDACTION",
    "EVENT_ASSESSMENT",
    "EVENT_DISPATCH_FAILED",
    "EVENT_LEAD_ACCEPTED",
    "EVENT_LEAD_DUPLICATE",
    "EVENT_LEAD_ROUTED",
    "EVENT_LEAD_SUPPRESSED",
    "LOG_FORMAT_HUMAN",
    "LOG_FORMAT_JSON",
    "METRIC_NAMESPACE",
    "SERVICE_NAME",
    "TRACE_ID",
    "HumanLogFormatter",
    "JsonLogFormatter",
    "Metric",
    "MetricPayload",
    "MetricSet",
    "SuppressionCause",
    "Unit",
    "configure_logging",
    "contact_email_hash",
    "current_context",
    "current_trace_id",
    "ensure_trace_id",
    "log_assessment",
    "log_context",
    "log_dispatch_failed",
    "log_event",
    "log_lead_accepted",
    "log_lead_duplicate",
    "log_lead_routed",
    "log_lead_suppressed",
    "new_trace_id",
    "redact_emails",
    "suppression_cause",
]
