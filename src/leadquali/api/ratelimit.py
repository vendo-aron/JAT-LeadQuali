"""The rate-limiting seam. A hook here, the real enforcement at the edge.

Plan §8 puts rate limiting on API Gateway usage plans, and that is the right place for it:
enforcement belongs in front of the runtime, where a flood costs no Lambda invocations and
no database connections. #26 configures it. What lives here is the seam that makes the
application testable and runnable without API Gateway — a Protocol the endpoint consults
and a fixed-window implementation for local runs and for the tests.

Keyed by tenant, applied after authentication. An unauthenticated flood is not this
layer's problem: it is refused before any lookup by the signature check, and refused before
*that* by the infrastructure. Counting unverified requests per tenant would be worse than
useless, because the tenant a stranger claims to be is not a fact.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Whether this request may proceed, and when to come back if not."""

    allowed: bool
    retry_after_seconds: int = 0

    @classmethod
    def allow(cls) -> RateLimitDecision:
        """Proceed."""
        return cls(allowed=True)

    @classmethod
    def refuse(cls, retry_after_seconds: int) -> RateLimitDecision:
        """Refuse, and say when the window reopens."""
        return cls(allowed=False, retry_after_seconds=max(1, retry_after_seconds))


@runtime_checkable
class RateLimiterPort(Protocol):
    """Decides whether one authenticated tenant may submit another lead right now."""

    def check(self, *, tenant_id: str, now: datetime) -> RateLimitDecision:
        """Consume one unit of this tenant's allowance and report the outcome."""
        ...


class NoRateLimit:
    """Allows everything. The default, because API Gateway does the real enforcement."""

    def check(self, *, tenant_id: str, now: datetime) -> RateLimitDecision:
        """Always allow."""
        del tenant_id, now
        return RateLimitDecision.allow()


class FixedWindowRateLimiter:
    """At most ``limit`` requests per tenant per ``window_seconds``.

    A sliding window of timestamps rather than a counter reset on the hour, so a burst
    straddling a boundary cannot get twice the allowance. Per-process and in memory, which
    is the honest limit: it protects a laptop and a single container, not a fleet. For a
    fleet, use the edge.
    """

    def __init__(self, *, limit: int, window_seconds: int = 60) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("a rate limit needs a positive allowance and a positive window")
        self._limit = limit
        self._window = window_seconds
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def check(self, *, tenant_id: str, now: datetime) -> RateLimitDecision:
        """Record this request against the tenant's window and report the outcome."""
        stamp = now.timestamp()
        hits = self._hits[tenant_id]
        cutoff = stamp - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            return RateLimitDecision.refuse(int(hits[0] + self._window - stamp) + 1)
        hits.append(stamp)
        return RateLimitDecision.allow()


__all__ = ["FixedWindowRateLimiter", "NoRateLimit", "RateLimitDecision", "RateLimiterPort"]
