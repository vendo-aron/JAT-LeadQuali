"""Capture what the process actually writes, and hand it back as parsed JSON.

Shared because #17's endpoint tests, #21's pipeline tests and anything after them all need
the same thing, and because the way *not* to do it is tempting: ``caplog`` gives you
``LogRecord`` objects, and asserting on those passes happily on a record the formatter
would go on to render as unparseable output, or with a field the formatter drops. Every
assertion in this suite goes through the real handler, the real formatter and
``json.loads``, so a test proves what a log aggregator would receive.

The root logger is restored on the way out. ``configure_logging`` takes it over by design
(a Lambda arrives with a handler already installed), and pytest keeps its own capture
handlers there.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from leadquali.config import Environment, LogLevel, Settings
from leadquali.observability import LOG_FORMAT_JSON, configure_logging


@dataclass(frozen=True, slots=True)
class LogCapture:
    """Everything emitted inside a :func:`capture_json_logs` block."""

    buffer: io.StringIO

    @property
    def text(self) -> str:
        """The raw output, for "this string appears nowhere" assertions."""
        return self.buffer.getvalue()

    def records(self) -> list[dict[str, Any]]:
        """Every line, parsed. Fails the test if any line is not a JSON object."""
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    def events(self, event: str) -> list[dict[str, Any]]:
        """Every record for one event name, in order."""
        return [record for record in self.records() if record.get("event") == event]

    def one(self, event: str) -> dict[str, Any]:
        """The single record for one event name.

        Raises:
            AssertionError: the event was emitted no times or more than once. Both are
                bugs worth failing on — a metric emitted twice is a metric that double
                counts, which is how a dashboard lies without anybody noticing.
        """
        found = self.events(event)
        assert len(found) == 1, f"expected exactly one {event!r}, got {len(found)}"
        return found[0]


@contextmanager
def capture_json_logs(level: LogLevel = "DEBUG") -> Iterator[LogCapture]:
    """Configure real JSON logging into a buffer for the duration of the block."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    previous_level = root.level
    buffer = io.StringIO()
    try:
        configure_logging(
            Settings(env=Environment.PROD, log_level=level),
            stream=buffer,
            log_format=LOG_FORMAT_JSON,
        )
        yield LogCapture(buffer=buffer)
    finally:
        for installed in list(root.handlers):
            root.removeHandler(installed)
        for original in handlers:
            root.addHandler(original)
        root.setLevel(previous_level)


__all__ = ["LogCapture", "capture_json_logs"]
