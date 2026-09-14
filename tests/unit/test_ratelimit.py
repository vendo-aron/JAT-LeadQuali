"""Per-tenant rate limiting: the bucket, the refill, and where the limits come from.

:class:`~leadquali.api.ratelimit.FixedWindowRateLimiter` is exercised through the endpoint
in ``test_api_ingest.py``. What is here is :class:`TenantRateLimiter`, which is the one that
reads a *per-tenant* allowance off the ``tenants`` row, and whose arithmetic is worth
pinning: a tenant quoted "60 a minute, bursts of 10" should get exactly that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from leadquali.api.ratelimit import (
    DEFAULT_TENANT_RATE_LIMIT,
    LIMIT_STALE_RETRY_SECONDS,
    RateLimiterPort,
    TenantRateLimit,
    TenantRateLimiter,
)

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


class StubLimits:
    """A rate-limit source that counts reads, so caching is assertable."""

    def __init__(self, limits: dict[str, TenantRateLimit] | None = None) -> None:
        self.limits = limits if limits is not None else {}
        self.reads = 0

    def rate_limit_for(self, tenant_id: str) -> TenantRateLimit | None:
        self.reads += 1
        return self.limits.get(tenant_id)


def spend(limiter: TenantRateLimiter, *, tenant_id: str, times: int, at: datetime) -> list[bool]:
    """Submit ``times`` requests at one instant and report which were allowed."""
    return [limiter.check(tenant_id=tenant_id, now=at).allowed for _ in range(times)]


# ------------------------------------------------------------------------ the limit


def test_a_rate_limit_needs_a_positive_rate_and_burst() -> None:
    """Either at zero is a customer whose leads silently stop. The column has the same
    CHECK, so this only catches a value that never came from a row."""
    with pytest.raises(ValueError, match="positive rate and burst"):
        TenantRateLimit(per_minute=0, burst=10)
    with pytest.raises(ValueError, match="positive rate and burst"):
        TenantRateLimit(per_minute=60, burst=0)


def test_the_limiter_satisfies_the_port() -> None:
    assert isinstance(TenantRateLimiter(StubLimits()), RateLimiterPort)


# ------------------------------------------------------------------ burst and refill


def test_a_tenant_may_spend_its_whole_burst_at_once() -> None:
    """A marketing email landing at 9am puts several submissions in the same second, and
    refusing those would lose real leads."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=5)})
    limiter = TenantRateLimiter(limits)

    assert spend(limiter, tenant_id="acme", times=5, at=NOW) == [True] * 5
    assert limiter.check(tenant_id="acme", now=NOW).allowed is False


def test_the_bucket_refills_at_the_configured_rate() -> None:
    """60 a minute is one a second: after three seconds, three more requests fit."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=5)})
    limiter = TenantRateLimiter(limits)
    spend(limiter, tenant_id="acme", times=5, at=NOW)

    later = NOW + timedelta(seconds=3)
    assert spend(limiter, tenant_id="acme", times=3, at=later) == [True] * 3
    assert limiter.check(tenant_id="acme", now=later).allowed is False


def test_the_bucket_never_fills_past_its_burst() -> None:
    """An idle hour does not buy an hour's worth of requests in one second."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=5)})
    limiter = TenantRateLimiter(limits)
    assert limiter.check(tenant_id="acme", now=NOW).allowed

    much_later = NOW + timedelta(hours=1)
    allowed = spend(limiter, tenant_id="acme", times=10, at=much_later)
    assert allowed == [True] * 5 + [False] * 5


def test_a_refusal_says_when_to_come_back() -> None:
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=1)})
    limiter = TenantRateLimiter(limits)
    assert limiter.check(tenant_id="acme", now=NOW).allowed

    refused = limiter.check(tenant_id="acme", now=NOW)
    assert refused.allowed is False
    assert refused.retry_after_seconds >= 1


def test_a_slow_tenant_waits_proportionally_longer() -> None:
    """Six a minute is one every ten seconds, and the Retry-After has to say so."""
    limits = StubLimits({"slow": TenantRateLimit(per_minute=6, burst=1)})
    limiter = TenantRateLimiter(limits)
    limiter.check(tenant_id="slow", now=NOW)

    refused = limiter.check(tenant_id="slow", now=NOW)
    assert refused.retry_after_seconds == 10


def test_a_clock_that_steps_backwards_cannot_mint_tokens() -> None:
    """NTP moves wall clocks. A negative elapsed time must add nothing to the bucket."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=2)})
    limiter = TenantRateLimiter(limits)
    spend(limiter, tenant_id="acme", times=2, at=NOW)

    assert limiter.check(tenant_id="acme", now=NOW - timedelta(hours=1)).allowed is False


# ---------------------------------------------------------------------- per tenant


def test_one_tenant_cannot_spend_another_tenants_allowance() -> None:
    limits = StubLimits(
        {
            "noisy": TenantRateLimit(per_minute=60, burst=2),
            "quiet": TenantRateLimit(per_minute=60, burst=2),
        }
    )
    limiter = TenantRateLimiter(limits)
    assert spend(limiter, tenant_id="noisy", times=3, at=NOW) == [True, True, False]
    assert spend(limiter, tenant_id="quiet", times=2, at=NOW) == [True, True]


def test_tenants_get_their_own_configured_allowances() -> None:
    """The whole point of putting the limits on the row: a customer on a bigger plan gets
    a bigger bucket, and that is a config write."""
    limits = StubLimits(
        {
            "small": TenantRateLimit(per_minute=60, burst=1),
            "large": TenantRateLimit(per_minute=600, burst=50),
        }
    )
    limiter = TenantRateLimiter(limits)
    assert spend(limiter, tenant_id="small", times=2, at=NOW) == [True, False]
    assert spend(limiter, tenant_id="large", times=50, at=NOW) == [True] * 50


def test_a_tenant_the_source_does_not_know_gets_the_default() -> None:
    """Never "unlimited". Authentication has already refused a request whose tenant does
    not exist, so this is belt and braces — and belt and braces must fail closed."""
    limiter = TenantRateLimiter(StubLimits())
    allowed = spend(limiter, tenant_id="ghost", times=DEFAULT_TENANT_RATE_LIMIT.burst + 1, at=NOW)
    assert allowed[-1] is False


def test_an_explicit_default_is_used_for_an_unknown_tenant() -> None:
    limiter = TenantRateLimiter(StubLimits(), default=TenantRateLimit(per_minute=60, burst=3))
    assert spend(limiter, tenant_id="ghost", times=4, at=NOW) == [True, True, True, False]


# ------------------------------------------------------------------- reading limits


def test_a_tenants_limits_are_not_read_on_every_request() -> None:
    """A per-request read would put a second query on the path of every lead to answer a
    question whose answer changes about once a quarter."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=6000, burst=100)})
    limiter = TenantRateLimiter(limits, cache_seconds=60)
    spend(limiter, tenant_id="acme", times=25, at=NOW)
    assert limits.reads == 1


def test_a_changed_limit_is_picked_up_within_the_cache_window() -> None:
    """Raising a customer's allowance during an incident has to take effect while you are
    still on the call, without a redeploy."""
    limits = StubLimits({"acme": TenantRateLimit(per_minute=60, burst=1)})
    limiter = TenantRateLimiter(limits, cache_seconds=60)
    spend(limiter, tenant_id="acme", times=2, at=NOW)

    limits.limits["acme"] = TenantRateLimit(per_minute=600, burst=20)
    later = NOW + timedelta(seconds=61)
    assert spend(limiter, tenant_id="acme", times=10, at=later) == [True] * 10
    assert limits.reads == 2


def test_the_limiter_is_bounded_in_the_number_of_tenants_it_remembers() -> None:
    """The tenant is authenticated by the time this runs, so the map cannot be grown by a
    stranger — but a long-lived container must still not grow without limit."""
    limits = StubLimits()
    limiter = TenantRateLimiter(limits, max_tenants=4)
    for index in range(50):
        limiter.check(tenant_id=f"tenant-{index}", now=NOW)
    # The earliest tenants have been evicted, which at worst hands them a fresh bucket.
    assert spend(limiter, tenant_id="tenant-0", times=1, at=NOW) == [True]


def test_a_negative_cache_window_is_refused() -> None:
    with pytest.raises(ValueError, match="cache_seconds"):
        TenantRateLimiter(StubLimits(), cache_seconds=-1)


def test_a_limiter_that_remembers_no_tenants_is_refused() -> None:
    with pytest.raises(ValueError, match="max_tenants"):
        TenantRateLimiter(StubLimits(), max_tenants=0)


# ------------------------------------------------------- when the source cannot answer


class BrokenLimits:
    """A source that fails after the first read, the way a database blips."""

    def __init__(self, limit: TenantRateLimit | None) -> None:
        self.limit = limit
        self.reads = 0
        self.failing = False

    def rate_limit_for(self, tenant_id: str) -> TenantRateLimit | None:
        del tenant_id
        self.reads += 1
        if self.failing:
            raise RuntimeError("connection reset by peer")
        return self.limit


def test_a_source_that_raises_does_not_turn_an_authenticated_lead_into_a_500() -> None:
    """The limiter runs *after* authentication, so its caller is a paying customer with a
    valid key. A database blip reading a number that changes once a quarter must not be the
    thing that loses their lead (invariant 3)."""
    limits = BrokenLimits(None)
    limits.failing = True
    limiter = TenantRateLimiter(limits, default=TenantRateLimit(per_minute=60, burst=2))

    assert spend(limiter, tenant_id="acme", times=3, at=NOW) == [True, True, False]


def test_a_failed_refresh_keeps_serving_the_limit_it_already_had() -> None:
    """Better than the default: the value we hold is almost certainly still right."""
    limits = BrokenLimits(TenantRateLimit(per_minute=600, burst=20))
    limiter = TenantRateLimiter(limits, cache_seconds=60)
    assert spend(limiter, tenant_id="acme", times=20, at=NOW) == [True] * 20

    limits.failing = True
    later = NOW + timedelta(seconds=61)
    assert spend(limiter, tenant_id="acme", times=20, at=later) == [True] * 20
    assert limits.reads == 2


def test_a_failing_source_is_not_retried_on_every_single_request() -> None:
    """A struggling database must not be hammered by the whole fleet, and an operator must
    not get one log line per lead while it recovers."""
    limits = BrokenLimits(TenantRateLimit(per_minute=6000, burst=100))
    limiter = TenantRateLimiter(limits, cache_seconds=60)
    limiter.check(tenant_id="acme", now=NOW)

    limits.failing = True
    later = NOW + timedelta(seconds=61)
    spend(limiter, tenant_id="acme", times=50, at=later)
    assert limits.reads == 2, "the failing source was retried more than once"

    # ...and it *is* retried, once the short stale window has passed.
    spend(
        limiter, tenant_id="acme", times=1, at=later + timedelta(seconds=LIMIT_STALE_RETRY_SECONDS)
    )
    assert limits.reads == 3


def test_a_source_that_recovers_is_read_again() -> None:
    """The failure is not sticky: nothing about it is remembered beyond the short retry."""
    limits = BrokenLimits(TenantRateLimit(per_minute=60, burst=1))
    limiter = TenantRateLimiter(limits, cache_seconds=0)
    limits.failing = True
    assert limiter.check(tenant_id="acme", now=NOW).allowed
    assert limits.reads == 1

    limits.failing = False
    limits.limit = TenantRateLimit(per_minute=600, burst=30)
    later = NOW + timedelta(seconds=10)
    assert spend(limiter, tenant_id="acme", times=25, at=later) == [True] * 25
    assert limits.reads > 1
