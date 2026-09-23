"""The scheduled retention job's Lambda entrypoint.

EventBridge fires this once a day (``infra/template.yaml``). It runs both retention tiers
for every tenant and returns what it deleted, per tenant and per class of record, which
becomes the invocation's result in CloudWatch as well as the ``retention.purged`` log line
each tenant's run emits.

**It applies.** A scheduled job that dry-ran would be a cron entry that does nothing, which
is worse than no job at all: it would look like compliance. That is the one place in this
codebase where the default flips, and it is why it is written out here rather than left to
:meth:`~leadquali.app.retention.RetentionService.purge_all`'s signature.

There is no ``--tenant`` equivalent and no event payload is read. A schedule that could be
pointed at one tenant by editing an EventBridge rule is a schedule that can be pointed at
the wrong one; the by-hand path is ``python -m leadquali.retentionctl``, where a person is
watching.

Failure is loud and total: an exception propagates, the invocation fails, and #29's Lambda
error alarm fires. A retention job that swallowed an error would report success while
personal data stayed past its window, which is the failure mode this whole issue exists to
prevent.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from leadquali.app.retention import DEFAULT_BATCH_SIZE, PurgedRecords, RetentionService
from leadquali.observability.logs import configure_logging, log_event

configure_logging()

LOGGER: Final = logging.getLogger(__name__)

#: Emitted once per scheduled run, whatever it found. Distinct from ``retention.purged``,
#: which is per tenant and only when something was destroyed: this one says the schedule
#: fired at all, so a silent job is distinguishable from a job that had nothing to do.
EVENT_RETENTION_RUN: Final[str] = "retention.run"


def build_service() -> RetentionService:
    """Wire the real store and the system clock.

    Imported inside the function so that a cold start pays for SQLAlchemy's engine
    machinery once, when the handler runs, rather than at module import.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.retention_postgres import PostgresRetentionStore

    return RetentionService(store=PostgresRetentionStore.from_env(), clock=SystemClock())


def run_retention(
    service: RetentionService | None = None, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict[str, Any]:
    """Run both tiers for every tenant and summarise what went.

    Args:
        service: the service to run. ``None`` builds the real one.
        batch_size: rows per transaction. The job loops until it drains.

    Returns:
        ``{"tenants": n, "counts": {...}, "purged": [{"tenant_id": …, "counts": {…}}, …]}``
        — totals for a dashboard and the per-tenant breakdown for an operator.
    """
    resolved = service if service is not None else build_service()
    reports = resolved.purge_all(batch_size=batch_size, dry_run=False)
    totals = {kind.value: sum(report.count(kind) for report in reports) for kind in PurgedRecords}
    return {
        "tenants": len(reports),
        "counts": totals,
        "purged": [
            {
                "tenant_id": report.tenant_id,
                "counts": {kind.value: report.count(kind) for kind in PurgedRecords},
            }
            for report in reports
            if report.changed
        ],
    }


def lambda_handler(
    event: dict[str, Any], context: object, *, service: RetentionService | None = None
) -> dict[str, Any]:
    """Entrypoint named by ``infra/template.yaml``. The event is deliberately ignored.

    Nothing about which tenants are purged, or by how much, may be set from outside: an
    EventBridge rule's input is a JSON blob in a console that nobody reviews, and this
    function deletes customer data.

    Args:
        event: the EventBridge event. Read by nothing, on purpose.
        context: the Lambda context. Likewise.
        service: the service to run, for the tests. ``None`` — which is what Lambda passes,
            since it calls this with two positional arguments — builds the real one.
    """
    del event, context
    summary = run_retention(service)
    log_event(
        LOGGER,
        EVENT_RETENTION_RUN,
        tenants=summary["tenants"],
        **{f"total_{name}": value for name, value in summary["counts"].items()},
    )
    return summary


__all__ = ["EVENT_RETENTION_RUN", "build_service", "lambda_handler", "run_retention"]
