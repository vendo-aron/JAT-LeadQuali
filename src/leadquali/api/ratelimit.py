"""Rate limiting: a per-tenant allowance here, a stage-wide backstop at the edge.

Two layers, and they answer different questions.

**The edge** (``infra/template.yaml``'s stage-level ``MethodSettings`` throttle) bounds the
absolute worst case: a flood is refused in front of the runtime, costing no Lambda
invocations and no database connections. It is account-wide and knows nothing about tenants.

**This module** is the per-tenant allowance, applied after authentication. Plan §8 called
for an API Gateway *usage plan* per tenant; #31 deliberately does not build that, for three
reasons written down in ``docs/tenant-onboarding.md``: a usage plan is keyed by an API
Gateway API key, which is a second credential the customer would have to embed in their
page alongside the one we actually authenticate on; the default account quota of 300 usage
plans caps the customer count at a number the business plans to exceed; and provisioning one
is a control-plane call in the middle of onboarding, with its own throttles and its own way
to fail half way through creating a customer. So the limits live on the tenant row and
:class:`TenantRateLimiter` enforces them.

Keyed by tenant, applied after authentication. An unauthenticated flood is not this
layer's problem: it is refused before any lookup by the signature check, and refused before
*that* by the infrastructure. Counting unverified requests per tenant would be worse than
useless, because the tenant a stranger claims to be is not a fact.

**The honest limit of a per-process bucket.** Every implementation here counts inside one
Python process. Under N warm Lambda containers a tenant can therefore get up to N times its
configured allowance, and the stage throttle is what bounds the total. That is a real
weakness and it is accepted rather than hidden: the alternative is a round trip to Redis or
DynamoDB on the path of every lead, which costs more latency than the limit saves, and the
limit exists to stop a runaway integration rather than a determined attacker.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Final, Protocol, runtime_checkable

from leadquali.app.tenants import TenantRateLimit
from leadquali.observability.logs import log_event

LOGGER: Final = logging.getLogger(__name__)


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


#: What a tenant gets when the store has no row for it. Matches the column defaults in
#: ``db_schema`` so that "unconfigured" and "configured with the defaults" behave the same.
DEFAULT_TENANT_RATE_LIMIT: Final[TenantRateLimit] = TenantRateLimit(per_minute=60, burst=10)

#: How long a tenant's limits are reused before they are read again. A minute, so raising a
#: customer's allowance during an incident takes effect while you are still on the call,
#: and so a flood costs one row read a minute rather than one per request.
DEFAULT_LIMIT_CACHE_SECONDS: Final[int] = 60

#: How long a limit that could not be refreshed is served before the source is tried again.
#:
#: The same shape as ``adapters/secrets_manager.STALE_RETRY_SECONDS``, and for the same
#: reason: without it, a source that is failing is retried on *every* request, which turns
#: a struggling database into a struggling database being hammered by the whole fleet, and
#: writes one log line per lead while it happens.
LIMIT_STALE_RETRY_SECONDS: Final[int] = 5


@runtime_checkable
class TenantRateLimitSource(Protocol):
    """Where a tenant's configured allowance comes from — the ``tenants`` row, in practice."""

    def rate_limit_for(self, tenant_id: str) -> TenantRateLimit | None:
        """Return this tenant's allowance, or ``None`` if there is no such tenant."""
        ...


class TenantRateLimiter:
    """A token bucket per tenant, filled at that tenant's configured rate.

    ``rate_limit_per_minute`` is the sustained rate and ``rate_limit_burst`` is the bucket's
    capacity, so a tenant may spend a burst all at once — a marketing email landing at 9am
    puts several submissions in the same second, and refusing those would lose real leads —
    and then may only submit as fast as the bucket refills.

    A token bucket rather than :class:`FixedWindowRateLimiter`'s sliding window because the
    two numbers a customer is quoted ("60 a minute, bursts of 10") map onto it exactly, and
    because it needs two floats per tenant rather than a deque of timestamps.

    Limits are read through ``source`` and cached for ``cache_seconds``: a per-request read
    would put a second query on the path of every lead to answer a question whose answer
    changes about once a quarter. Buckets themselves are never evicted by time; the map is
    capped at ``max_tenants`` and the least recently used tenant is dropped, which at worst
    grants that tenant a fresh full bucket.

    Args:
        source: Where allowances come from.
        default: What to use for a tenant the source does not know.
        cache_seconds: How long an allowance is reused.
        max_tenants: How many buckets to keep.
    """

    def __init__(
        self,
        source: TenantRateLimitSource,
        *,
        default: TenantRateLimit = DEFAULT_TENANT_RATE_LIMIT,
        cache_seconds: int = DEFAULT_LIMIT_CACHE_SECONDS,
        max_tenants: int = 4_096,
    ) -> None:
        if cache_seconds < 0:
            raise ValueError(f"cache_seconds must not be negative, got {cache_seconds}")
        if max_tenants < 1:
            raise ValueError(f"max_tenants must be positive, got {max_tenants}")
        self._source = source
        self._default = default
        self._cache_seconds = cache_seconds
        self._max_tenants = max_tenants
        #: ``tenant_id -> (limit, the moment it stops being served without a refetch)``.
        self._limits: OrderedDict[str, tuple[TenantRateLimit, float]] = OrderedDict()
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def check(self, *, tenant_id: str, now: datetime) -> RateLimitDecision:
        """Spend one token of this tenant's allowance and report the outcome."""
        stamp = now.timestamp()
        limit = self._limit_for(tenant_id, stamp)
        per_second = limit.per_minute / 60.0
        capacity = float(limit.burst)

        tokens, last = self._buckets.get(tenant_id, (capacity, stamp))
        # max(0.0, ...) so a clock that steps backwards cannot mint tokens.
        tokens = min(capacity, tokens + max(0.0, stamp - last) * per_second)
        if tokens < 1.0:
            self._buckets[tenant_id] = (tokens, stamp)
            self._buckets.move_to_end(tenant_id)
            return RateLimitDecision.refuse(ceil((1.0 - tokens) / per_second))
        self._buckets[tenant_id] = (tokens - 1.0, stamp)
        self._buckets.move_to_end(tenant_id)
        while len(self._buckets) > self._max_tenants:
            self._buckets.popitem(last=False)
        return RateLimitDecision.allow()

    def _limit_for(self, tenant_id: str, stamp: float) -> TenantRateLimit:
        cached = self._limits.get(tenant_id)
        if cached is not None and stamp < cached[1]:
            return cached[0]
        try:
            limit = self._source.rate_limit_for(tenant_id) or self._default
        except Exception:
            # Broad, and deliberately so: the source is a Protocol over a database, and
            # what it raises is the adapter's business. The point is that this code runs
            # *after* authentication, so its caller is a paying customer holding a valid
            # key — a blip reading a number that changes once a quarter must not become a
            # 500 on their lead. A browser form that gets a 500 does not retry, and the
            # lead is simply gone (invariant 3).
            #
            # Serve the last value we had, or the default, and re-stamp so the failing
            # source is retried in LIMIT_STALE_RETRY_SECONDS rather than on every single
            # request — otherwise a struggling database gets hammered by the whole fleet.
            log_event(
                LOGGER,
                "ratelimit.limits_unavailable",
                level=logging.WARNING,
                tenant_id=tenant_id,
            )
            served = cached[0] if cached is not None else self._default
            self._remember(
                tenant_id, served, stamp + min(LIMIT_STALE_RETRY_SECONDS, self._cache_seconds)
            )
            return served
        self._remember(tenant_id, limit, stamp + self._cache_seconds)
        return limit

    def _remember(self, tenant_id: str, limit: TenantRateLimit, valid_until: float) -> None:
        self._limits[tenant_id] = (limit, valid_until)
        self._limits.move_to_end(tenant_id)
        while len(self._limits) > self._max_tenants:
            self._limits.popitem(last=False)


__all__ = [
    "DEFAULT_LIMIT_CACHE_SECONDS",
    "DEFAULT_TENANT_RATE_LIMIT",
    "LIMIT_STALE_RETRY_SECONDS",
    "FixedWindowRateLimiter",
    "NoRateLimit",
    "RateLimitDecision",
    "RateLimiterPort",
    "TenantRateLimit",
    "TenantRateLimitSource",
    "TenantRateLimiter",
]
