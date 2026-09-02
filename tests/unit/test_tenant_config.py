"""Behaviour of the tenant rubric-as-data model.

The failure paths carry most of the weight here. A tenant config that is wrong is wrong at
load time, in a message that names the tenant and the problem — never at 3am on a live lead.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from pydantic import ValidationError

from leadquali.domain.models import (
    MAX_TOTAL_SCORE,
    Action,
    DimensionScores,
    Tier,
)
from leadquali.domain.tenant_config import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_PROMPT_VERSION,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    DIMENSION_MAXIMA,
    DIMENSION_NAMES,
    RoutingRule,
    TenantConfig,
    TenantConfigError,
    TierThresholds,
)

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


def make_config(**overrides: Any) -> TenantConfig:
    """A valid config with only the named fields changed."""
    return TenantConfig.model_validate({**MINIMAL, **overrides})


def invalid(**overrides: Any) -> str:
    """Validate a broken config and return the rendered error message."""
    with pytest.raises((ValidationError, TenantConfigError)) as excinfo:
        TenantConfig.model_validate({**MINIMAL, **overrides})
    return str(excinfo.value)


def total_for(cfg: TenantConfig, scores: DimensionScores) -> float:
    """The normalised total #9's ``weighted_total`` will compute, spelled out here.

    This test module deliberately does not import #9: it asserts the *data* half of the
    contract — weights, maxima and ``max_weighted_raw`` — is enough to compute a total that
    lands on the 0-100 scale.
    """
    raw = sum(cfg.weights[name] * float(getattr(scores, name)) for name in sorted(DIMENSION_NAMES))
    return MAX_TOTAL_SCORE * raw / cfg.max_weighted_raw


# --------------------------------------------------------------------------- defaults


def test_dimensions_are_derived_from_the_assessment_schema() -> None:
    assert frozenset(DimensionScores.model_fields) == DIMENSION_NAMES
    assert DIMENSION_MAXIMA == {
        "icp_fit": 30,
        "intent": 25,
        "authority": 15,
        "urgency": 15,
        "budget_signal": 15,
    }


def test_documented_defaults_match_the_plan() -> None:
    assert (DEFAULT_THRESHOLDS.hot, DEFAULT_THRESHOLDS.warm, DEFAULT_THRESHOLDS.cold) == (
        80.0,
        55.0,
        30.0,
    )
    assert set(DEFAULT_WEIGHTS) == DIMENSION_NAMES
    assert 0.0 <= DEFAULT_MIN_CONFIDENCE <= 1.0


def test_a_new_tenant_inherits_the_defaults() -> None:
    cfg = make_config()
    assert cfg.thresholds == DEFAULT_THRESHOLDS
    assert cfg.weights == dict(DEFAULT_WEIGHTS)
    assert cfg.min_confidence == DEFAULT_MIN_CONFIDENCE
    assert cfg.prompt_version == DEFAULT_PROMPT_VERSION


def test_default_weights_put_a_maximal_assessment_exactly_on_the_scale_top() -> None:
    cfg = make_config()
    assert cfg.max_weighted_raw == MAX_TOTAL_SCORE
    maximal = DimensionScores.model_validate(DIMENSION_MAXIMA)
    assert total_for(cfg, maximal) == MAX_TOTAL_SCORE


@pytest.mark.parametrize(
    "weights",
    [
        {"icp_fit": 2.0, "intent": 1.0, "authority": 1.0, "urgency": 1.0, "budget_signal": 1.0},
        {"icp_fit": 0.0, "intent": 4.0, "authority": 0.0, "urgency": 0.0, "budget_signal": 0.5},
        dict.fromkeys(DIMENSION_NAMES, 0.25),
    ],
)
def test_normalisation_keeps_any_reweighting_inside_the_scale(weights: dict[str, float]) -> None:
    """Invariant behind ``RoutingDecision.total_score``: 0 <= total <= MAX_TOTAL_SCORE."""
    cfg = make_config(weights=weights)
    maximal = DimensionScores.model_validate(DIMENSION_MAXIMA)
    zero = DimensionScores.model_validate(dict.fromkeys(DIMENSION_NAMES, 0))
    assert total_for(cfg, maximal) == pytest.approx(MAX_TOTAL_SCORE)
    assert total_for(cfg, zero) == 0.0
    # ...and the hot threshold therefore stays reachable whatever the weights are.
    assert cfg.tier_for(total_for(cfg, maximal)) is Tier.HOT


# --------------------------------------------------------------------------- tier_for


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (100.0, Tier.HOT),
        (80.0, Tier.HOT),
        (79.999, Tier.WARM),
        (55.0, Tier.WARM),
        (54.999, Tier.COLD),
        (30.0, Tier.COLD),
        (29.999, Tier.DISQUALIFIED),
        (0.0, Tier.DISQUALIFIED),
    ],
)
def test_tier_for_is_inclusive_at_every_lower_bound(total: float, expected: Tier) -> None:
    assert make_config().tier_for(total) is expected


def test_tier_for_follows_tenant_thresholds_not_the_defaults() -> None:
    strict = make_config(thresholds={"hot": 90.0, "warm": 70.0, "cold": 50.0})
    assert strict.tier_for(80.0) is Tier.WARM
    assert make_config().tier_for(80.0) is Tier.HOT


@pytest.mark.parametrize("total", [-0.001, 100.001, math.nan, math.inf])
def test_tier_for_rejects_a_score_off_the_scale(total: float) -> None:
    with pytest.raises(ValueError, match="acme"):
        make_config().tier_for(total)


# --------------------------------------------------------------------------- routing


def test_action_and_destination_come_from_the_routing_rules() -> None:
    cfg = make_config()
    assert cfg.action_for(Tier.HOT) is Action.EMAIL_SALES
    assert cfg.destination_for(Tier.HOT) == "hot@acme.test"
    assert cfg.action_for(Tier.DISQUALIFIED) is Action.SUPPRESS
    assert cfg.destination_for(Tier.DISQUALIFIED) is None
    assert cfg.rule_for(Tier.WARM) == RoutingRule(
        action=Action.EMAIL_SALES, destination="sales@acme.test"
    )


def test_routing_is_repointed_by_configuration_alone() -> None:
    rules = {**MINIMAL["routing_rules"], "hot": {"action": "escalate_human", "destination": "vp"}}
    cfg = make_config(routing_rules=rules)
    assert cfg.action_for(Tier.HOT) is Action.ESCALATE_HUMAN
    assert cfg.destination_for(Tier.HOT) == "vp"


def test_two_configs_give_two_tiers_for_one_assessment() -> None:
    """The acceptance criterion: a rubric change is a config write, not a deploy."""
    scores = DimensionScores(icp_fit=24, intent=20, authority=12, urgency=10, budget_signal=10)
    lenient = make_config(thresholds={"hot": 70.0, "warm": 40.0, "cold": 20.0})
    strict = make_config(thresholds={"hot": 90.0, "warm": 80.0, "cold": 60.0})
    assert lenient.tier_for(total_for(lenient, scores)) is Tier.HOT
    assert strict.tier_for(total_for(strict, scores)) is Tier.COLD


# --------------------------------------------------------------- validation: thresholds


@pytest.mark.parametrize(
    ("thresholds", "problem"),
    [
        ({"hot": 55.0, "warm": 55.0, "cold": 30.0}, "hot"),
        ({"hot": 50.0, "warm": 55.0, "cold": 30.0}, "hot"),
        ({"hot": 80.0, "warm": 30.0, "cold": 30.0}, "warm"),
        ({"hot": 80.0, "warm": 20.0, "cold": 30.0}, "warm"),
    ],
)
def test_overlapping_thresholds_are_rejected(thresholds: dict[str, float], problem: str) -> None:
    message = invalid(thresholds=thresholds)
    assert problem in message


@pytest.mark.parametrize(
    "thresholds",
    [
        {"hot": 80.0, "warm": 55.0, "cold": -1.0},
        {"hot": 120.0, "warm": 55.0, "cold": 30.0},
        {"hot": math.nan, "warm": 55.0, "cold": 30.0},
    ],
)
def test_thresholds_must_stay_on_the_scale(thresholds: dict[str, float]) -> None:
    assert invalid(thresholds=thresholds)


def test_thresholds_carve_the_scale_without_gaps_by_construction() -> None:
    """Every point on the scale belongs to exactly one tier."""
    cfg = make_config()
    tiers = {cfg.tier_for(total / 10) for total in range(0, 1001)}
    assert tiers == set(Tier)


# ----------------------------------------------------------------- validation: weights


def test_a_missing_dimension_weight_is_rejected_by_name() -> None:
    weights = {name: 1.0 for name in DIMENSION_NAMES if name != "urgency"}
    message = invalid(weights=weights)
    assert "urgency" in message
    assert "acme" in message


def test_an_unknown_dimension_weight_is_rejected_by_name() -> None:
    weights = {**dict(DEFAULT_WEIGHTS), "vibes": 3.0}
    message = invalid(weights=weights)
    assert "vibes" in message
    assert "acme" in message


def test_a_negative_weight_is_rejected() -> None:
    weights = {**dict(DEFAULT_WEIGHTS), "intent": -1.0}
    assert "intent" in invalid(weights=weights)


def test_all_zero_weights_are_rejected_because_normalisation_is_undefined() -> None:
    message = invalid(weights=dict.fromkeys(DIMENSION_NAMES, 0.0))
    assert "acme" in message


# ---------------------------------------------------------- validation: min_confidence


@pytest.mark.parametrize("value", [-0.01, 1.01, 2.0])
def test_min_confidence_must_be_a_probability(value: float) -> None:
    assert invalid(min_confidence=value)


def test_min_confidence_accepts_both_ends_of_the_range() -> None:
    assert make_config(min_confidence=0.0).min_confidence == 0.0
    assert make_config(min_confidence=1.0).min_confidence == 1.0


# ----------------------------------------------------------- validation: routing rules


def test_a_tier_with_no_routing_rule_is_rejected_by_name() -> None:
    rules = {k: v for k, v in MINIMAL["routing_rules"].items() if k != "cold"}
    message = invalid(routing_rules=rules)
    assert "cold" in message
    assert "acme" in message


def test_an_unknown_tier_in_the_routing_rules_is_rejected() -> None:
    rules = {**MINIMAL["routing_rules"], "lukewarm": {"action": "email_sales", "destination": "x"}}
    assert invalid(routing_rules=rules)


def test_a_delivering_action_without_a_destination_is_rejected() -> None:
    rules = {**MINIMAL["routing_rules"], "warm": {"action": "email_sales"}}
    assert "destination" in invalid(routing_rules=rules)


def test_a_blank_destination_is_rejected() -> None:
    rules = {**MINIMAL["routing_rules"], "warm": {"action": "escalate_human", "destination": "  "}}
    assert "destination" in invalid(routing_rules=rules)


def test_suppression_must_not_carry_a_destination() -> None:
    rules = {**MINIMAL["routing_rules"], "disqualified": {"action": "suppress", "destination": "x"}}
    assert "destination" in invalid(routing_rules=rules)


# ------------------------------------------------------------- validation: identity etc


@pytest.mark.parametrize("tenant_id", ["", "  ", "Acme Corp", "../etc/passwd", "a" * 100])
def test_tenant_id_must_be_a_safe_slug(tenant_id: str) -> None:
    assert invalid(tenant_id=tenant_id)


def test_an_empty_icp_description_is_rejected() -> None:
    assert invalid(icp_description="   ")


def test_unknown_config_keys_are_rejected_rather_than_silently_ignored() -> None:
    assert "threshold" in invalid(threshold=80)


def test_from_dict_names_the_tenant_even_for_a_field_level_failure() -> None:
    broken = {**MINIMAL, "min_confidence": 4.0}
    with pytest.raises(TenantConfigError) as excinfo:
        TenantConfig.from_dict(broken)
    message = str(excinfo.value)
    assert "acme" in message
    assert "min_confidence" in message


def test_from_dict_names_the_tenant_as_unknown_when_the_id_itself_is_missing() -> None:
    broken = {k: v for k, v in MINIMAL.items() if k != "tenant_id"}
    with pytest.raises(TenantConfigError, match="unknown"):
        TenantConfig.from_dict(broken)


def test_from_dict_rejects_a_non_object_document() -> None:
    with pytest.raises(TenantConfigError):
        TenantConfig.from_dict([1, 2, 3])


# ------------------------------------------------------------------------ round-trip


def test_python_round_trip_is_lossless() -> None:
    cfg = make_config(weights={**dict(DEFAULT_WEIGHTS), "intent": 2.5}, min_confidence=0.42)
    assert TenantConfig.model_validate(cfg.model_dump()) == cfg


def test_jsonb_round_trip_is_lossless() -> None:
    """The config lives in a ``jsonb`` column: JSON is its storage form, not an export."""
    cfg = make_config(thresholds={"hot": 88.0, "warm": 60.0, "cold": 25.0}, prompt_version="v9")
    document = json.dumps(cfg.model_dump(mode="json"))
    assert TenantConfig.model_validate(json.loads(document)) == cfg


def test_json_dump_uses_plain_strings_for_enum_keys() -> None:
    dumped = make_config().model_dump(mode="json")
    assert set(dumped["routing_rules"]) == {"hot", "warm", "cold", "disqualified"}
    assert all(isinstance(key, str) for key in dumped["routing_rules"])


def test_a_config_is_immutable() -> None:
    with pytest.raises(ValidationError):
        make_config().min_confidence = 0.9


# ------------------------------------------------------------------------- icp_block


def test_icp_block_is_byte_stable_across_calls() -> None:
    cfg = make_config()
    assert cfg.icp_block().encode() == cfg.icp_block().encode()


def test_icp_block_is_byte_stable_across_independent_loads() -> None:
    """Two loads of the same config must produce the same cacheable prompt prefix."""
    first = TenantConfig.model_validate(json.loads(json.dumps(MINIMAL)))
    shuffled = {key: MINIMAL[key] for key in reversed(list(MINIMAL))}
    shuffled["routing_rules"] = {
        key: MINIMAL["routing_rules"][key] for key in reversed(list(MINIMAL["routing_rules"]))
    }
    second = TenantConfig.model_validate(json.loads(json.dumps(shuffled)))
    assert first.icp_block().encode() == second.icp_block().encode()


def test_icp_block_ignores_weight_insertion_order() -> None:
    forwards = make_config(weights={name: 1.5 for name in sorted(DIMENSION_NAMES)})
    backwards = make_config(weights={name: 1.5 for name in sorted(DIMENSION_NAMES, reverse=True)})
    assert forwards.icp_block() == backwards.icp_block()


def test_icp_block_normalises_line_endings_and_trailing_space() -> None:
    """A config edited on Windows must not silently invalidate the prompt cache."""
    text = "Line one.\nLine two."
    windows = make_config(icp_description="  Line one.  \r\nLine two.\r\n\r\n")
    assert make_config(icp_description=text).icp_block() == windows.icp_block()


def test_icp_block_carries_the_tenant_text_and_relative_emphasis() -> None:
    cfg = make_config(icp_description="Mid-market logistics operators in the EU.")
    block = cfg.icp_block()
    assert "Mid-market logistics operators in the EU." in block
    assert "Acme Corp" in block
    for name in DIMENSION_NAMES:
        assert name in block


def test_icp_block_leaks_no_routing_policy_to_the_model() -> None:
    """Invariant 2: the model assesses. Thresholds and destinations are code's business."""
    cfg = make_config()
    block = cfg.icp_block()
    for secret in ("80", "55", "30", "hot@acme.test", "suppress", "disqualified", "0.6"):
        assert secret not in block


def test_icp_block_changes_when_the_pinned_rubric_changes() -> None:
    assert make_config().icp_block() != make_config(prompt_version="rubric_v2").icp_block()


def test_icp_block_ends_without_trailing_whitespace() -> None:
    block = make_config().icp_block()
    assert block == block.rstrip()
    assert "\r" not in block
    assert not any(line != line.rstrip() for line in block.split("\n"))


# --------------------------------------------------------------------- tier thresholds


def test_tier_thresholds_are_independently_validatable() -> None:
    assert TierThresholds(hot=70.0, warm=40.0, cold=10.0).hot == 70.0
    with pytest.raises(ValidationError):
        TierThresholds(hot=10.0, warm=40.0, cold=70.0)
