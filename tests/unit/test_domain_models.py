"""Behaviour of the domain contract every other module is written against."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from leadquali.domain.models import (
    MAX_TOTAL_SCORE,
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    RoutingDecision,
    Tier,
)

VALID_SCORES: dict[str, Any] = {
    "icp_fit": 20,
    "intent": 15,
    "authority": 10,
    "urgency": 8,
    "budget_signal": 7,
}

DIMENSION_BOUNDS: list[tuple[str, int]] = [
    ("icp_fit", 30),
    ("intent", 25),
    ("authority", 15),
    ("urgency", 15),
    ("budget_signal", 15),
]


def make_scores(**overrides: int) -> DimensionScores:
    return DimensionScores.model_validate({**VALID_SCORES, **overrides})


def make_facts(**overrides: str | None) -> ExtractedFacts:
    base: dict[str, str | None] = {
        "company_name": "Acme Freight",
        "industry": "logistics",
        "company_size_estimate": "50-200",
        "role_seniority": "director",
        "stated_use_case": "route planning",
        "stated_timeline": "this quarter",
    }
    return ExtractedFacts.model_validate({**base, **overrides})


def make_assessment(**overrides: Any) -> LeadAssessment:
    base: dict[str, Any] = {
        "dimension_scores": make_scores(),
        "extracted": make_facts(),
        "reasoning": "Director at a mid-size logistics firm naming a concrete use case.",
        "confidence": 0.82,
        "missing_information": ["budget"],
        "suggested_first_question": "What does routing cost you per week today?",
        "spam_or_test_submission": False,
    }
    return LeadAssessment.model_validate({**base, **overrides})


# --------------------------------------------------------------------------- Tier


def test_tier_values_are_the_wire_strings() -> None:
    assert [t.value for t in Tier] == ["hot", "warm", "cold", "disqualified"]
    assert Tier("hot") is Tier.HOT
    assert f"{Tier.WARM}" == "warm"


def test_tier_rank_orders_by_business_value_not_alphabetically() -> None:
    ordered = sorted(Tier, key=lambda t: t.rank)
    assert ordered == [Tier.DISQUALIFIED, Tier.COLD, Tier.WARM, Tier.HOT]
    assert Tier.HOT.rank > Tier.WARM.rank > Tier.COLD.rank > Tier.DISQUALIFIED.rank


def test_string_ordering_on_tier_is_the_trap_rank_exists_to_avoid() -> None:
    """Pin the footgun: ``<`` on a ``StrEnum`` ranks ``cold`` above ``hot``."""
    assert Tier.COLD < Tier.HOT  # lexicographic, and exactly backwards
    assert Tier.COLD.rank < Tier.HOT.rank


def test_every_tier_has_a_distinct_rank() -> None:
    ranks = [t.rank for t in Tier]
    assert sorted(ranks) == [0, 1, 2, 3]


# ------------------------------------------------------------------- enums / actions


def test_action_covers_notify_escalate_and_suppress() -> None:
    assert {a.value for a in Action} >= {"email_sales", "escalate_human", "suppress"}


def test_escalation_reasons_cover_every_non_answer_path() -> None:
    assert {r.value for r in EscalationReason} == {
        "low_confidence",
        "model_refusal",
        "parse_error",
        "api_error",
        "timeout",
    }


# ------------------------------------------------------------------- DimensionScores


@pytest.mark.parametrize(("field", "maximum"), DIMENSION_BOUNDS)
def test_dimension_accepts_both_ends_of_its_range(field: str, maximum: int) -> None:
    assert getattr(make_scores(**{field: 0}), field) == 0
    assert getattr(make_scores(**{field: maximum}), field) == maximum


@pytest.mark.parametrize(("field", "maximum"), DIMENSION_BOUNDS)
def test_dimension_rejects_one_over_its_maximum(field: str, maximum: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_scores(**{field: maximum + 1})
    assert excinfo.value.errors()[0]["type"] == "less_than_equal"


@pytest.mark.parametrize(("field", "_maximum"), DIMENSION_BOUNDS)
def test_dimension_rejects_negative(field: str, _maximum: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_scores(**{field: -1})
    assert excinfo.value.errors()[0]["type"] == "greater_than_equal"


def test_dimension_bounds_are_the_ones_the_rubric_promises() -> None:
    """The per-dimension ceilings sum to the 0-100 scale the thresholds are stated on."""
    assert sum(maximum for _, maximum in DIMENSION_BOUNDS) == MAX_TOTAL_SCORE


def test_scores_are_required_not_defaulted() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DimensionScores.model_validate({"icp_fit": 10})
    assert {e["type"] for e in excinfo.value.errors()} == {"missing"}


def test_scores_coerce_a_numeric_string_and_a_whole_float() -> None:
    """Lax validation: the wire is JSON, and a model that emits "30" is still right."""
    assert DimensionScores.model_validate({**VALID_SCORES, "icp_fit": "30"}).icp_fit == 30
    assert DimensionScores.model_validate({**VALID_SCORES, "icp_fit": 12.0}).icp_fit == 12


def test_scores_reject_a_fractional_float_and_none() -> None:
    for bad in (12.5, None, "high"):
        with pytest.raises(ValidationError):
            DimensionScores.model_validate({**VALID_SCORES, "icp_fit": bad})


def test_scores_reject_an_unknown_field() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DimensionScores.model_validate({**VALID_SCORES, "vibes": 5})
    assert excinfo.value.errors()[0]["type"] == "extra_forbidden"


# --------------------------------------------------------------------- ExtractedFacts


def test_every_extracted_fact_may_be_null() -> None:
    facts = ExtractedFacts.model_validate(dict.fromkeys(ExtractedFacts.model_fields))
    assert all(getattr(facts, name) is None for name in ExtractedFacts.model_fields)


def test_extracted_facts_are_required_even_though_nullable() -> None:
    """An explicit null is a signal; a missing key is a bug we want to hear about."""
    with pytest.raises(ValidationError) as excinfo:
        ExtractedFacts.model_validate({"company_name": "Acme"})
    assert {e["type"] for e in excinfo.value.errors()} == {"missing"}
    assert len(excinfo.value.errors()) == len(ExtractedFacts.model_fields) - 1


# --------------------------------------------------------------------- LeadAssessment


def test_assessment_accepts_a_complete_sparse_lead() -> None:
    assessment = make_assessment(
        extracted=make_facts(company_name=None, stated_timeline=None),
        missing_information=[],
        suggested_first_question=None,
    )
    assert assessment.extracted.company_name is None
    assert assessment.missing_information == []
    assert assessment.suggested_first_question is None


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_confidence_accepts_the_closed_unit_interval(confidence: float) -> None:
    assert make_assessment(confidence=confidence).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 2.0, -1.0])
def test_confidence_rejects_anything_outside_it(confidence: float) -> None:
    with pytest.raises(ValidationError):
        make_assessment(confidence=confidence)


def test_reasoning_must_not_be_empty() -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_assessment(reasoning="")
    assert excinfo.value.errors()[0]["type"] == "string_too_short"


def test_assessment_is_frozen() -> None:
    assessment = make_assessment()
    with pytest.raises(ValidationError) as excinfo:
        assessment.confidence = 1.0
    assert excinfo.value.errors()[0]["type"] == "frozen_instance"


def test_nested_models_are_frozen_too() -> None:
    scores = make_scores()
    with pytest.raises(ValidationError):
        scores.icp_fit = 0


def test_assessment_round_trips_through_json() -> None:
    assessment = make_assessment(extracted=make_facts(industry=None), suggested_first_question=None)
    restored = LeadAssessment.model_validate_json(assessment.model_dump_json())
    assert restored == assessment
    assert restored.extracted.industry is None


def test_assessment_round_trip_preserves_every_field() -> None:
    assessment = make_assessment()
    assert LeadAssessment.model_validate(assessment.model_dump()) == assessment


# --------------------------------------------------------------------- RoutingDecision


def test_routing_decision_defaults_to_no_score_and_no_note() -> None:
    decision = RoutingDecision(tier=Tier.DISQUALIFIED, action=Action.SUPPRESS)
    assert decision.total_score == 0.0
    assert decision.note == ""
    assert decision.escalation_reason is None
    assert decision.escalated is False


def test_routing_decision_records_why_it_escalated() -> None:
    decision = RoutingDecision(
        tier=Tier.WARM,
        action=Action.ESCALATE_HUMAN,
        total_score=61.5,
        note="low model confidence - human review",
        escalation_reason=EscalationReason.LOW_CONFIDENCE,
    )
    assert decision.escalated is True
    assert decision.escalation_reason is EscalationReason.LOW_CONFIDENCE
    assert decision.tier.rank > Tier.COLD.rank


def test_an_escalation_can_never_suppress_the_lead() -> None:
    """Invariant 3 encoded in the type: uncertainty escalates, it never bins."""
    with pytest.raises(ValidationError) as excinfo:
        RoutingDecision(
            tier=Tier.DISQUALIFIED,
            action=Action.SUPPRESS,
            escalation_reason=EscalationReason.API_ERROR,
        )
    assert "cannot suppress" in str(excinfo.value)


@pytest.mark.parametrize("total", [0.0, 55.0, MAX_TOTAL_SCORE])
def test_total_score_accepts_the_whole_scale(total: float) -> None:
    decision = RoutingDecision(tier=Tier.COLD, action=Action.EMAIL_SALES, total_score=total)
    assert decision.total_score == total


@pytest.mark.parametrize("total", [-0.1, MAX_TOTAL_SCORE + 0.1])
def test_total_score_rejects_anything_off_the_scale(total: float) -> None:
    with pytest.raises(ValidationError):
        RoutingDecision(tier=Tier.HOT, action=Action.EMAIL_SALES, total_score=total)


def test_routing_decision_is_frozen_and_round_trips() -> None:
    decision = RoutingDecision(
        tier=Tier.HOT,
        action=Action.EMAIL_SALES,
        total_score=88.0,
        note="strong fit, named timeline",
    )
    with pytest.raises(ValidationError):
        decision.tier = Tier.COLD
    assert RoutingDecision.model_validate_json(decision.model_dump_json()) == decision


def test_routing_decision_serialises_enums_as_their_wire_strings() -> None:
    payload = RoutingDecision(
        tier=Tier.HOT,
        action=Action.EMAIL_SALES,
        escalation_reason=None,
    ).model_dump(mode="json")
    assert payload["tier"] == "hot"
    assert payload["action"] == "email_sales"
    assert payload["escalation_reason"] is None
