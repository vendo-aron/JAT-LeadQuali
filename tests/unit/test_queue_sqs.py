"""The SQS producer and the worker's consumer, round-tripped against a fake queue.

The property that matters is that the message shape is *unchanged* by the transport: what
`QueuedLead.to_message()` produces is what `from_message()` gets back. A queue that
reshapes its payload becomes a second schema nobody versioned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from leadquali.adapters.queue_sqs import SqsLeadQueue
from leadquali.api import worker
from leadquali.app.ingest import QueuedLead
from leadquali.prompts.lead import LeadSubmission

QUEUE_NAME = "leadquali-test-leads"


def _queued(**overrides: Any) -> QueuedLead:
    defaults: dict[str, Any] = {
        "tenant_id": "acme",
        "lead_id": "11111111-1111-1111-1111-111111111111",
        "submission_id": "sub-00000001",
        "submission": LeadSubmission(full_name="Jane", email="jane@acme.test", message="hi"),
        "source": "web_form",
        "received_at": datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        "trace_id": "trace-abc",
    }
    defaults.update(overrides)
    return QueuedLead(**defaults)


@pytest.fixture
def sqs_queue() -> Any:
    with mock_aws():
        client = boto3.client("sqs", region_name="eu-west-1")
        url = client.create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
        yield client, url


def test_a_lead_round_trips_through_the_queue_unchanged(sqs_queue: Any) -> None:
    client, url = sqs_queue
    original = _queued()

    SqsLeadQueue(client, url).enqueue(original)

    received = client.receive_message(QueueUrl=url, MaxNumberOfMessages=1)["Messages"][0]
    restored = QueuedLead.from_message(json.loads(received["Body"]))

    assert restored.tenant_id == original.tenant_id
    assert restored.submission_id == original.submission_id
    assert restored.trace_id == original.trace_id
    assert restored.submission.message == original.submission.message


def test_the_body_is_deterministic_for_the_same_lead(sqs_queue: Any) -> None:
    """Two identical leads produce identical bytes, which makes a redelivery diffable."""
    client, url = sqs_queue
    queue = SqsLeadQueue(client, url)
    queue.enqueue(_queued())
    queue.enqueue(_queued())

    bodies = [
        message["Body"]
        for message in client.receive_message(QueueUrl=url, MaxNumberOfMessages=2)["Messages"]
    ]
    assert bodies[0] == bodies[1]


def test_a_send_failure_is_raised_not_swallowed(sqs_queue: Any) -> None:
    """Ingest has already persisted the lead; a silent enqueue failure would return a
    202 the website believes about a lead that never gets qualified."""
    client, _ = sqs_queue
    queue = SqsLeadQueue(client, "https://sqs.eu-west-1.amazonaws.com/000000000000/nope")
    with pytest.raises(ClientError):
        queue.enqueue(_queued())


# --------------------------------------------------------------------------- the consumer


class _RecordingPipeline:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.seen: list[str] = []
        self._fail_on = fail_on or set()

    def qualify(self, request: Any) -> None:
        self.seen.append(request.submission_id)
        if request.submission_id in self._fail_on:
            raise RuntimeError("database unavailable")


def _record(queued: QueuedLead, message_id: str) -> dict[str, Any]:
    return {"messageId": message_id, "body": json.dumps(queued.to_message())}


def test_a_healthy_batch_reports_no_failures() -> None:
    pipeline = _RecordingPipeline()
    event = {
        "Records": [
            _record(_queued(submission_id="sub-00000001"), "m1"),
            _record(_queued(submission_id="sub-00000002"), "m2"),
        ]
    }
    result = worker.handle(event, None, pipeline=pipeline)

    assert result == {"batchItemFailures": []}
    assert pipeline.seen == ["sub-00000001", "sub-00000002"]


def test_one_failing_lead_does_not_drag_its_neighbours_back(caplog: Any) -> None:
    """The whole reason for ReportBatchItemFailures: without it, nine healthy leads are
    redelivered alongside the one that failed - and eventually emailed twice."""
    pipeline = _RecordingPipeline(fail_on={"sub-00000002"})
    event = {
        "Records": [
            _record(_queued(submission_id="sub-00000001"), "m1"),
            _record(_queued(submission_id="sub-00000002"), "m2"),
            _record(_queued(submission_id="sub-00000003"), "m3"),
        ]
    }
    result = worker.handle(event, None, pipeline=pipeline)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert pipeline.seen == ["sub-00000001", "sub-00000002", "sub-00000003"], (
        "a failure must not abort the rest of the batch"
    )


@pytest.mark.parametrize(
    "body",
    ["not json at all", '"a bare string"', "[]", '{"version": 1}', "{}"],
)
def test_an_undecodable_message_is_dropped_rather_than_retried_forever(body: str) -> None:
    """It will not parse on the fourth attempt either, and ingest already stored the lead.

    Reporting it as a failure would loop it until the DLQ, billed per invocation, for a
    record that is already safe in Postgres.
    """
    pipeline = _RecordingPipeline()
    result = worker.handle(
        {"Records": [{"messageId": "m1", "body": body}]}, None, pipeline=pipeline
    )

    assert result == {"batchItemFailures": []}
    assert pipeline.seen == []


def test_a_message_without_a_trace_id_is_still_processed() -> None:
    """#21 made the trace id additive and optional in both directions on purpose: an old
    message in flight during a rolling deploy must not be refused."""
    message = _queued().to_message()
    message.pop("trace_id", None)
    pipeline = _RecordingPipeline()

    result = worker.handle(
        {"Records": [{"messageId": "m1", "body": json.dumps(message)}]},
        None,
        pipeline=pipeline,
    )

    assert result == {"batchItemFailures": []}
    assert pipeline.seen == ["sub-00000001"]


def test_an_empty_batch_is_not_an_error() -> None:
    assert worker.handle({"Records": []}, None, pipeline=_RecordingPipeline()) == {
        "batchItemFailures": []
    }
