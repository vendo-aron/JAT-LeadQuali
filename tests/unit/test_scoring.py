"""Behaviour of the deterministic weighted score.

The two properties that matter are stated as properties, not as examples: a maximal
assessment lands on exactly ``MAX_TOTAL_SCORE`` under *any* valid weighting, and a total
is always a value that can be shown to a human and tiered on without a second rounding.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from leadquali.domain.models import MAX_TOTAL_SCORE, DimensionScores
from leadquali.domain.scoring import SCORE_DECIMAL_PLACES, round_score, weighted_total
from leadquali.domain.tenant_config import DIMENSION_MAXIMA, TenantConfig

MINIMAL: dict[str, Any] = {
    "tenant_id": "acme",
    "name": "Acme Corp",
    "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
    "routing_rules": {
        "hot": {"action": "email_sales", "destination": "hot@acme.test"},
        "warm": {"action": "email_sales", "destination": "sales@acme.test"},
        "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
        "disqualified": {"action": "suppress"},
    },
}

NEUTRAL: dict[str, float] = dict.fromkeys(DIMENSION_MAXIMA, 1.0)

#: Weightings a real tenant might plausibly ask for, plus the ones that break naive
#: arithmetic: a single dimension carrying everything, weights that are not representable
#: in binary, and weights eleven orders of magnitude apart.
WEIGHTINGS: dict[str, dict[str, float]] = {
    "neutral": NEUTRAL,
    "authority_heavy": {**NEUTRAL, "authority": 6.0},
    "icp_only": {**dict.fromkeys(DIMENSION_MAXIMA, 0.0), "icp_fit": 1.0},
    "lopsided": {
        "icp_fit": 10.0,
        "intent": 0.01,
        "authority": 0.01,
        "urgency": 0.01,
        "budget_signal": 0.01,
    },
    "inexact_binary": {
        "icp_fit": 0.1,
        "intent": 0.2,
        "authority": 0.3,
        "urgency": 0.4,
        "budget_signal": 0.7,
    },
    "many_orders_of_magnitude": {
        "icp_fit": 1e6,
        "intent": 1e-5,
        "authority": 1e-5,
        "urgency": 1e-5,
        "budget_signal": 1e-5,
    },
    "fractional": {
        "icp_fit": 0.333,
        "intent": 1.7,
        "authority": 2.25,
        "urgency": 0.05,
        "budget_signal": 9.9,
    },
    "two_dimensions": {
        **dict.fromkeys(DIMENSION_MAXIMA, 0.0),
        "icp_fit": 1.0,
        "intent": 1.0,
    },
}


def make_config(**overrides: Any) -> TenantConfig:
    """A valid config with only the named fields changed."""
    return TenantConfig.model_validate({**MINIMAL, **overrides})


def scores(**values: int) -> DimensionScores:
    """Dimension scores, defaulting every dimension not named to zero."""
    return DimensionScores.model_validate({**dict.fromkeys(DIMENSION_MAXIMA, 0), **values})


ZERO = scores()
MAXIMAL = scores(**dict(DIMENSION_MAXIMA))


def plan_formula(cfg: TenantConfig, dimensions: DimensionScores) -> float:
    """The unrounded formula from plan section 5, spelled out independently."""
    dumped = dimensions.model_dump()
    raw = sum(cfg.weights[name] * float(dumped[name]) for name in sorted(DIMENSION_MAXIMA))
    return MAX_TOTAL_SCORE * raw / cfg.max_weighted_raw


# ------------------------------------------------------------------- the two endpoints


@pytest.mark.parametrize("label", sorted(WEIGHTINGS))
def test_an_all_zero_assessment_scores_zero_under_every_weighting(label: str) -> None:
    assert weighted_total(ZERO, make_config(weights=WEIGHTINGS[label])) == 0.0


@pytest.mark.parametrize("label", sorted(WEIGHTINGS))
def test_a_maximal_assessment_lands_on_exactly_100_under_every_weighting(label: str) -> None:
    """The scale has to be exact at the top, or no lead could ever reach a hot threshold
    set at 100 and the number shown to sales would be 99.99999999999999."""
    total = weighted_total(MAXIMAL, make_config(weights=WEIGHTINGS[label]))
    assert total == MAX_TOTAL_SCORE
    assert isinstance(total, float)


def test_neutral_weights_make_the_total_the_raw_sum() -> None:
    """With every multiplier at one the rubric's own 30/25/15/15/15 emphasis is the scale,
    so the total is simply the sum the model handed us."""
    cfg = make_config(weights=NEUTRAL)
    assert weighted_total(scores(icp_fit=20, intent=15, authority=10), cfg) == 45.0
    assert weighted_total(scores(icp_fit=30, intent=25, authority=15), cfg) == 70.0


# ------------------------------------------------------------------------- properties


@pytest.mark.parametrize("label", sorted(WEIGHTINGS))
def test_every_total_stays_on_the_scale_and_is_already_rounded(label: str) -> None:
    """Fuzzed over the whole score space: the result is always something ``RoutingDecision``
    accepts (0-100) and always equal to its own rounding, so tiering and display agree."""
    cfg = make_config(weights=WEIGHTINGS[label])
    rng = random.Random(20260902)
    for _ in range(200):
        sample = scores(**{name: rng.randint(0, top) for name, top in DIMENSION_MAXIMA.items()})
        total = weighted_total(sample, cfg)
        assert 0.0 <= total <= MAX_TOTAL_SCORE
        assert total == round_score(total)
        assert total == pytest.approx(plan_formula(cfg, sample), abs=0.005)


def test_scaling_every_weight_by_the_same_factor_changes_nothing() -> None:
    """Only the *relative* emphasis is policy. A tenant that writes 3/3/3/3/3 instead of
    1/1/1/1/1 has said the same thing and must get the same score, to the last bit."""
    sample = scores(icp_fit=21, intent=9, authority=4, urgency=11, budget_signal=2)
    base = weighted_total(sample, make_config(weights=NEUTRAL))
    for factor in (0.5, 3.0, 1000.0):
        scaled = {name: weight * factor for name, weight in NEUTRAL.items()}
        assert weighted_total(sample, make_config(weights=scaled)) == base


def test_a_zero_weighted_dimension_cannot_move_the_total() -> None:
    cfg = make_config(weights={**NEUTRAL, "budget_signal": 0.0})
    baseline = weighted_total(scores(icp_fit=20, budget_signal=0), cfg)
    assert weighted_total(scores(icp_fit=20, budget_signal=15), cfg) == baseline


def test_a_dominant_weight_makes_one_dimension_carry_the_lead() -> None:
    """A tenant selling to procurement can make authority nearly the whole score, and a
    perfect lead on that one dimension must come out near the top of the scale."""
    cfg = make_config(weights={**dict.fromkeys(DIMENSION_MAXIMA, 0.001), "authority": 100.0})
    authority_only = weighted_total(scores(authority=15), cfg)
    everything_else = weighted_total(
        scores(icp_fit=30, intent=25, urgency=15, budget_signal=15), cfg
    )
    assert authority_only > 99.0
    assert everything_else < 1.0


@pytest.mark.parametrize("dimension", sorted(DIMENSION_MAXIMA))
def test_raising_a_positively_weighted_dimension_never_lowers_the_total(dimension: str) -> None:
    cfg = make_config(weights=WEIGHTINGS["fractional"])
    previous = -1.0
    for value in range(DIMENSION_MAXIMA[dimension] + 1):
        total = weighted_total(scores(**{dimension: value}), cfg)
        assert total > previous
        previous = total


def test_the_same_input_always_scores_the_same() -> None:
    cfg = make_config(weights=WEIGHTINGS["inexact_binary"])
    sample = scores(icp_fit=17, intent=6, authority=15, urgency=1, budget_signal=9)
    assert len({weighted_total(sample, cfg) for _ in range(50)}) == 1


# --------------------------------------------------------------------------- rounding


def test_scores_are_rounded_to_two_decimals() -> None:
    assert SCORE_DECIMAL_PLACES == 2
    cfg = make_config(weights=WEIGHTINGS["icp_only"])
    # 100 * 10/30 = 33.3333...
    assert weighted_total(scores(icp_fit=10), cfg) == 33.33
    # 100 * 20/30 = 66.6666...
    assert weighted_total(scores(icp_fit=20), cfg) == 66.67


def test_round_score_rounds_half_up_on_the_printed_value() -> None:
    """Ties go up, and they go up on the number a person would read.

    ``round(2.675, 2)`` is 2.67 because 2.675 is really 2.67499999...; a salesperson told
    the lead scored 2.675 and then shown 2.67 has been told two different things.
    """
    assert round_score(2.675) == 2.68
    assert round_score(54.545) == 54.55
    assert round_score(0.125) == 0.13
    assert round_score(0.0) == 0.0
    assert round_score(99.999) == 100.0


def test_round_score_leaves_an_already_rounded_value_alone() -> None:
    for value in (0.0, 12.34, 55.0, 79.99, 100.0):
        assert round_score(value) == value
