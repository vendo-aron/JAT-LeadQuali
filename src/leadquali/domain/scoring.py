"""The lead's number: weighted, normalised, rounded once, deterministic forever.

This module is the arithmetic half of invariant 2 — the model assesses, code routes. The
model hands us five bounded judgments; this turns them into one comparable number on a
fixed 0-100 scale, using nothing but the tenant's own weights.

Two decisions are worth reading before you change anything here.

**The scale is normalised, not accumulated.** A total is
``MAX_TOTAL_SCORE * Σ(weight_d * score_d) / cfg.max_weighted_raw``. Dividing by the largest
weighted raw score the tenant's weights *can* produce is what makes a threshold portable:
"hot >= 80" means the same thing to a tenant that weights everything at 1.0 and to one that
multiplies authority by fifty, and a maximal assessment lands on exactly 100.0 for both. If
totals were merely accumulated, every reweighting would silently move every tier boundary
and the tenant would have to re-derive its thresholds by hand.

**Rounding happens exactly once, here, to two decimals, half-up.** The rounded value is
what gets stored, what gets shown to a salesperson, and — crucially — what gets tiered, so
the number in the email is provably the number that chose the tier. Anything else invites
the failure this module exists to prevent: a lead displayed as 55.00 against a warm
threshold of 55.0 but filed as cold because the underlying double was 54.99999999999999.
Half-up on the *printed* value (via :class:`~decimal.Decimal` on ``repr``) rather than
:func:`round`'s round-half-to-even on the *binary* value, because a person reading 2.675
expects 2.68 and cannot see that the double is really 2.67499999999999982236431605997495.

Pure functions over frozen values: no I/O, no clock, no global state. The same assessment
and the same config produce the same float on every machine, forever, which is what makes
the eval harness (#23) and the audit trail worth anything.

Boundary with #8: :class:`~leadquali.domain.tenant_config.TenantConfig` owns the policy
*data* — weights, maxima, thresholds — and the pure lookups over it. This module owns the
policy *computation* and never reaches for a number that is not in the config.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from leadquali.domain.models import MAX_TOTAL_SCORE, DimensionScores
from leadquali.domain.tenant_config import DIMENSION_MAXIMA, TenantConfig

#: Decimal places every total is rounded to. Two, because that is enough to keep a
#: threshold expressible at the granularity a tenant would ever tune it, and few enough
#: that the stored number and the displayed number are the same string.
SCORE_DECIMAL_PLACES: Final[int] = 2

_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-SCORE_DECIMAL_PLACES)

#: Iterated in sorted order so the floating-point summation is identical on every run and
#: every host, independent of dict insertion order in a config file.
_DIMENSIONS: Final[tuple[str, ...]] = tuple(sorted(DIMENSION_MAXIMA))


def round_score(value: float) -> float:
    """Round a score to :data:`SCORE_DECIMAL_PLACES` places, ties away from zero.

    The rounding is applied to ``repr(value)`` — the shortest decimal string that round-trips
    to the same double — so the result matches what a human reading the number would expect:
    ``round_score(2.675) == 2.68``, where ``round(2.675, 2)`` is ``2.67``.

    Args:
        value: A finite score. Infinities and NaN are a bug upstream, not a score.

    Returns:
        The rounded value as a float. Already-rounded values are returned unchanged.

    Raises:
        ValueError: ``value`` is not finite.
    """
    if not math.isfinite(value):
        raise ValueError(f"a score must be a finite number, got {value!r}")
    return float(Decimal(repr(value)).quantize(_QUANTUM, rounding=ROUND_HALF_UP))


def weighted_total(scores: DimensionScores, cfg: TenantConfig) -> float:
    """The lead's total on the 0-100 scale, under this tenant's weights.

    ``MAX_TOTAL_SCORE * Σ(weight_d * score_d) / cfg.max_weighted_raw``, rounded once by
    :func:`round_score`. The denominator is positive for every config that validates (#8
    rejects an all-zero weighting), so there is no division to guard here.

    Summation uses :func:`math.fsum` over the dimensions in sorted order: the result is the
    correctly-rounded sum regardless of how wildly a tenant's weights differ in magnitude,
    so a tenant weighting authority at 1e6 and the rest at 1e-5 still gets a stable number
    rather than one that depends on the order keys happened to land in a JSON file.

    Args:
        scores: The model's per-dimension judgment.
        cfg: The tenant whose weights and scale apply.

    Returns:
        A float in ``[0.0, MAX_TOTAL_SCORE]``, rounded to :data:`SCORE_DECIMAL_PLACES`
        places. An all-zero assessment scores ``0.0`` and a maximal one scores exactly
        ``MAX_TOTAL_SCORE`` under any valid weighting.
    """
    dumped = scores.model_dump()
    raw = math.fsum(cfg.weight_for(name) * int(dumped[name]) for name in _DIMENSIONS)
    normalised = MAX_TOTAL_SCORE * raw / cfg.max_weighted_raw
    # Clamped, not asserted: the arithmetic can land a maximal assessment a single ulp above
    # the top of the scale, and RoutingDecision rejects anything above MAX_TOTAL_SCORE.
    return min(max(round_score(normalised), 0.0), MAX_TOTAL_SCORE)


__all__ = ["SCORE_DECIMAL_PLACES", "round_score", "weighted_total"]
