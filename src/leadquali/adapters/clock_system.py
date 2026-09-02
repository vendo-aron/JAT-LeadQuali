"""The real clock, behind :class:`~leadquali.app.ports.ClockPort`.

The host's clock is an external system like any other — it is shared, it is not under our
control, and it moves when NTP says so — which is why the pipeline reads time through a
port instead of calling :func:`datetime.now` inline. The payoff is that #14's tests assert
on timestamps and latencies without sleeping, and that a replay tool can re-run a lead with
the timestamps it originally had.

Two readings, because they answer different questions. :meth:`SystemClock.now` is wall
time, stamped on rows so a human can correlate them with an email or a support ticket; it
can jump backwards. :meth:`SystemClock.monotonic_ms` measures elapsed time and cannot jump,
which is what a latency metric needs — computing a duration from two wall-clock readings is
how a p99 ends up negative twice a year.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

#: Nanoseconds per millisecond. ``time.monotonic_ns`` is integer nanoseconds, so the
#: conversion is exact and carries none of the float drift ``time.monotonic`` accumulates.
_NS_PER_MS: int = 1_000_000


class SystemClock:
    """Wall time from the host, elapsed time from its monotonic counter."""

    def now(self) -> datetime:
        """The current time, timezone-aware and in UTC.

        Aware and UTC without exception: a naive datetime reaching a database column is how
        a deployment in one region and a laptop in another silently disagree about when a
        lead arrived.
        """
        return datetime.now(UTC)

    def monotonic_ms(self) -> int:
        """A monotonic millisecond counter. Only differences between readings mean anything."""
        return time.monotonic_ns() // _NS_PER_MS


__all__ = ["SystemClock"]
