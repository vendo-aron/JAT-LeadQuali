"""SQS producer for the ingest path.

The queue is what lets the form post return in under 200 ms while a Claude call that
takes several seconds happens somewhere else. It is also where the never-drop-a-lead
invariant is enforced by infrastructure rather than by code: a message that the worker
fails to process is redelivered, and after ``maxReceiveCount`` attempts it lands on a
dead-letter queue that an alarm watches, instead of vanishing.

This adapter is deliberately thin. The message body is exactly what
:meth:`leadquali.app.ingest.QueuedLead.to_message` produces, unchanged — the queue is a
transport, and a transport that reshapes its payload becomes a second schema nobody
versioned.
"""

from __future__ import annotations

import json
from typing import Any, Final

import boto3

from leadquali.app.ingest import QueuedLead

#: Sent as the SQS message group id when the queue is FIFO. Ordering between leads does
#: not matter — two leads are independent — so every lead is its own group, which lets
#: SQS parallelise fully instead of serialising the whole queue behind one slow lead.
GROUP_ID_PREFIX: Final[str] = "lead"


class SqsLeadQueue:
    """A :class:`~leadquali.app.ingest.LeadQueuePort` backed by an SQS queue."""

    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    @classmethod
    def from_env(cls, queue_url: str, *, region_name: str | None = None) -> SqsLeadQueue:
        """Build a producer against ``queue_url``.

        The client is created here rather than at import time: a Lambda cold start should
        pay for it once, but an import should never make a network-capable object as a
        side effect of being imported by a test.
        """
        return cls(boto3.client("sqs", region_name=region_name), queue_url)

    def enqueue(self, queued: QueuedLead) -> None:
        """Publish one lead. Raises on failure so the caller can decide.

        Deliberately does not swallow errors: the ingest endpoint has already persisted
        the lead, so a failed enqueue must surface as a non-2xx rather than a 202 that
        quietly means nothing happened. A 202 the website believes and a lead that never
        gets qualified is the worst of both worlds.
        """
        body = json.dumps(queued.to_message(), separators=(",", ":"), sort_keys=True)
        self._client.send_message(QueueUrl=self._queue_url, MessageBody=body)
