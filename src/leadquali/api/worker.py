"""The SQS-triggered qualification worker.

One Lambda invocation carries a batch of leads. Two properties decide whether this is
correct under load, and neither is obvious from the happy path:

* **Per-message failure, not per-batch.** The handler returns a partial-batch response
  (``batchItemFailures``), so one poisoned message does not drag its nine healthy
  neighbours back onto the queue to be retried — and eventually onto the dead-letter
  queue — alongside it. Without this, a single unparseable lead multiplies into ten
  duplicate emails on redelivery.
* **A message that can never succeed must not be retried forever.** A body that is not
  JSON, or that does not match the queue schema, is not going to parse on the fourth
  attempt either. It is logged and *not* reported as a failure, so SQS deletes it — the
  lead is already persisted by ingest, so the record is not lost, and the alternative is
  an infinite redelivery loop billed by the invocation.

Everything else — a database blip, a Claude timeout — *is* reported as a failure, because
those genuinely may succeed on redelivery. That is the whole reason the queue exists.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

from leadquali.app.ingest import QueuedLead
from leadquali.app.qualify import QualificationRequest
from leadquali.observability.logs import configure_logging

# Lambda reuses containers, so this runs once per container rather than per invocation.
# `configure_logging` is idempotent by design (#21) — calling it twice would otherwise
# double every log line and every metric derived from one.
configure_logging()

LOGGER: Final = logging.getLogger(__name__)


def _parse(record: dict[str, Any]) -> QueuedLead | None:
    """Decode one SQS record, or ``None`` if it can never be decoded."""
    try:
        body = json.loads(record["body"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    try:
        return QueuedLead.from_message(body)
    except (KeyError, TypeError, ValueError):
        return None


def handle(event: dict[str, Any], context: object, *, pipeline: Any) -> dict[str, Any]:
    """Process one SQS batch and report which messages failed.

    ``pipeline`` is injected rather than built here so the handler is testable without
    AWS; the module-level entrypoint below wires the real one.
    """
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", ""))
        queued = _parse(record)
        if queued is None:
            # Undecodable: retrying cannot help, and the lead row already exists.
            LOGGER.error(
                "queue.undecodable_message",
                extra={"event": "queue.undecodable_message", "message_id": message_id},
            )
            continue
        try:
            # The pipeline binds the trace id (and tenant/lead) onto the log context
            # itself, so the worker passes it along rather than binding a second time.
            pipeline.qualify(
                QualificationRequest(
                    tenant_id=queued.tenant_id,
                    submission_id=queued.submission_id,
                    submission=queued.submission,
                    source=queued.source,
                    received_at=queued.received_at,
                    trace_id=queued.trace_id,
                )
            )
        except Exception:
            # Transient by assumption: let SQS redeliver this one message.
            LOGGER.exception(
                "queue.qualify_failed",
                extra={"event": "queue.qualify_failed", "message_id": message_id},
            )
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """The SQS entrypoint named by `infra/template.yaml`.

    The pipeline is built lazily and cached on the module, so a container pays for the
    engine, the client and the config load once rather than per invocation - and an
    import of this module (by a test, or by a linter) builds nothing at all.
    """
    global _PIPELINE
    if _PIPELINE is None:  # pragma: no cover - exercised only in a real Lambda
        _PIPELINE = _build_pipeline()
    return handle(event, context, pipeline=_PIPELINE)


_PIPELINE: Any = None


def _build_pipeline() -> Any:  # pragma: no cover - requires AWS and a database
    """Wire the real adapters. Deliberately not run in tests: every line needs a service.

    This is also where the container's secrets are resolved (#28). Each ``require_*``
    below reads Secrets Manager once and the resolver caches the value for
    ``SECRETS_CACHE_TTL_SECONDS``, so a warm container pays nothing per lead and a
    rotation is picked up within the TTL without a redeploy. A secret that cannot be read
    raises here, before the pipeline exists, which is deliberate: the alternative is a
    worker that starts and then escalates every lead it is handed.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.enrich_null import NullEnricher
    from leadquali.adapters.llm_anthropic import AnthropicLeadAssessor, build_anthropic_client
    from leadquali.adapters.notify_ses import SesNotifier
    from leadquali.adapters.store_postgres import PostgresLeadStore, PostgresTenantConfigSource
    from leadquali.app.qualify import QualificationPipeline
    from leadquali.config import get_settings

    settings = get_settings()
    return QualificationPipeline(
        config_source=PostgresTenantConfigSource.from_env(settings),
        assessor=AnthropicLeadAssessor(
            build_anthropic_client(settings.require_anthropic_api_key())
        ),
        store=PostgresLeadStore.from_env(settings),
        notifier=SesNotifier.from_env(settings),
        # NullEnricher, not EmailEnricher: #18's DNS-backed enricher lands on a
        # parallel branch and is not in this one's history. Swap it in when both are on
        # the default branch - and read #18's note first, because a worker in private
        # subnets has no DNS unless #27's VPC provides it.
        enricher=NullEnricher(),
        clock=SystemClock(),
        escalation_destination=os.environ["ESCALATION_DESTINATION"],
    )
