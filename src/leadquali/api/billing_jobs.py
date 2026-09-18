"""The three scheduled billing jobs, as Lambda entrypoints. Thin, like every ``api`` module.

Nothing here decides anything. Each handler builds the wiring (or is handed it, in a test),
calls one method on :class:`~leadquali.app.billing.BillingService`, logs what happened and
returns a small JSON-shaped summary. The decisions — what is billable, when a grace period
starts, when an event has failed for the last time — all live in ``app/billing.py``, which
is testable without AWS, without Postgres and without a Stripe key.

**``process_events``**, every minute. Drains ``stripe_events`` oldest-first. A minute is a
deliberate choice, not a default: it is the only latency the "200 fast, process later" split
costs, and it buys no second queue, no fan-out and no ordering problem. A minute before a
``payment_failed`` starts a grace period measured in days is not a number anybody will ever
notice.

**``report_usage``**, daily. Reports **yesterday** — the most recent *closed* day — for every
tenant with a Stripe customer. Yesterday rather than today because a partial day
under-reports now and is re-reported tomorrow, which over-reports; and the service asks
:class:`~leadquali.app.metering.MeteringService` for the day using its *default*
``closed_days_only``, so a future change to that default cannot start invoicing an open day
from here.

**``sweep_dunning``**, daily. Suspends tenants whose grace period has run out. Scheduled
rather than event-driven because what has to happen is the *absence* of a payment for seven
days, and an absence does not arrive as a webhook.

All three are idempotent, which is what makes running them twice — a retry, an overlapping
schedule, an operator invoking one by hand during an incident — safe. The event drain is
idempotent on Stripe's event id, the usage job on ``(tenant_id, usage_date)``, and the sweep
because suspending a suspended tenant is a no-op.

Errors are **raised**, not swallowed. A Lambda that returns 200 having done nothing is a
failed billing run nobody is paged about; a Lambda that raises shows up in #29's error-rate
alarm. The one exception is inside ``report_usage_for_all``, where one tenant's failure is
logged and the rest of the fleet is still billed — a job that aborted on the first bad
tenant would silently stop billing every customer whose slug sorts after it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Final

from leadquali.app.billing import DEFAULT_DRAIN_LIMIT, BillingService
from leadquali.observability import log_event
from leadquali.observability.logs import configure_logging

# Lambda reuses containers, so this runs once per container rather than per invocation.
# `configure_logging` is idempotent by design (#21); calling it twice would otherwise
# double every log line and every metric derived from one.
configure_logging()

LOGGER: Final = logging.getLogger(__name__)

__all__ = [
    "drain_events",
    "process_events_handler",
    "report_usage",
    "report_usage_handler",
    "requested_day",
    "sweep_dunning",
    "sweep_dunning_handler",
]


def drain_events(service: BillingService, *, limit: int = DEFAULT_DRAIN_LIMIT) -> dict[str, int]:
    """Apply pending Stripe events and return the counts, for the log and the metric."""
    summary = service.process_pending(limit=limit)
    log_event(
        LOGGER,
        "billing.drain",
        level=logging.ERROR if summary.failed else logging.INFO,
        attempted=summary.attempted,
        processed=summary.processed,
        retrying=summary.retrying,
        failed=summary.failed,
    )
    return {
        "attempted": summary.attempted,
        "processed": summary.processed,
        "retrying": summary.retrying,
        "failed": summary.failed,
    }


def report_usage(service: BillingService, *, usage_date: date) -> dict[str, int]:
    """Report one closed day for every billable tenant, and count the outcomes.

    The outcomes are counted by name rather than collapsed into "how many worked", because
    "already reported" is routine and "this day is not over" is a scheduling bug, and a
    summary that could not tell them apart would make the second invisible.
    """
    outcomes = service.report_usage_for_all(usage_date=usage_date)
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome.value] = counts.get(outcome.value, 0) + 1
    log_event(
        LOGGER,
        "billing.usage_run",
        usage_date=usage_date.isoformat(),
        tenants=len(outcomes),
        # Through ``fields`` rather than as keyword arguments: the keys are outcome names
        # decided at runtime, and splatting them would let a future outcome called
        # ``level`` or ``message`` collide with one of log_event's own parameters.
        fields=counts,
    )
    return {"tenants": len(outcomes), **counts}


def sweep_dunning(service: BillingService) -> dict[str, int]:
    """Suspend tenants whose grace period has run out, and count them."""
    suspended = service.sweep_dunning()
    return {"suspended": len(suspended)}


def _service() -> BillingService:  # pragma: no cover - requires AWS and a database
    """The production wiring, built per cold start.

    Reuses :func:`leadquali.api.webhooks.build_billing_deps` rather than assembling a second
    copy: the scheduled jobs and the webhook endpoint must agree about which Stripe account,
    which meter and which database they are talking to, and two wiring functions is how they
    stop agreeing.
    """
    from leadquali.api.webhooks import build_billing_deps

    return build_billing_deps().service


def process_events_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """EventBridge entrypoint: ``leadquali.api.billing_jobs.process_events_handler``."""
    del event, context
    return drain_events(_service())


def report_usage_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """EventBridge entrypoint: ``leadquali.api.billing_jobs.report_usage_handler``.

    Bills **yesterday**, in UTC, for every tenant. An operator re-running a missed day
    passes ``{"usage_date": "2026-09-05"}`` — which is safe to do at any time, because a day
    already in ``usage_reports`` is skipped rather than sent again.
    """
    del context
    service = _service()
    return report_usage(service, usage_date=requested_day(event, service=service))


def sweep_dunning_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """EventBridge entrypoint: ``leadquali.api.billing_jobs.sweep_dunning_handler``."""
    del event, context
    return sweep_dunning(_service())


def requested_day(event: object, *, service: BillingService) -> date:
    """The day an invocation should bill: the one it names, or yesterday.

    "Yesterday" comes from the service's own clock rather than from ``date.today()`` so
    that the job and the metering rollup can never disagree about what day it is — a host
    in a non-UTC timezone would otherwise bill a different day than the rollup counted.

    An explicit ``usage_date`` is how an operator re-runs a day the schedule missed, and it
    is safe to pass at any time: a day already in ``usage_reports`` is skipped rather than
    sent again.

    Raises:
        ValueError: ``usage_date`` is present and is not an ISO date. Raised rather than
            ignored, because silently billing yesterday when somebody asked for last
            Tuesday is the kind of helpfulness that ends in a support ticket.
    """
    requested = event.get("usage_date") if isinstance(event, dict) else None
    if requested is None:
        return service.today() - timedelta(days=1)
    if not isinstance(requested, str):
        raise ValueError(f"usage_date must be an ISO date string, not {type(requested).__name__}")
    return date.fromisoformat(requested)
