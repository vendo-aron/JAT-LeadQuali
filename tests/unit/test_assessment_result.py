"""The port's result type: a closed union, immutable, with no third state.

The adapter hands #14 one of exactly two things. Making that a union of two frozen
dataclasses rather than an "assessment or ``None``, plus maybe an error" tuple is what
stops the caller from forgetting the failure branch — there is no attribute to read on a
failure that would give it a score.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from leadquali.app.assessment_result import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
)
from leadquali.domain.models import (
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
)


def metering() -> CallMetering:
    return CallMetering(
        model_id="claude-opus-5",
        prompt_version="rubric_v1",
        effort="medium",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_creation_tokens=0,
        cost_usd=Decimal("0.001"),
        latency_ms=42,
    )


def assessment() -> LeadAssessment:
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=1, intent=1, authority=1, urgency=1, budget_signal=1
        ),
        extracted=ExtractedFacts(
            company_name=None,
            industry=None,
            company_size_estimate=None,
            role_seniority=None,
            stated_use_case=None,
            stated_timeline=None,
        ),
        reasoning="Thin submission.",
        confidence=0.4,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=False,
    )


def test_the_two_outcomes_are_discriminated_by_ok() -> None:
    success: AssessmentOutcome = AssessmentSucceeded(assessment=assessment(), metering=metering())
    failure: AssessmentOutcome = AssessmentFailed(
        reason=EscalationReason.TIMEOUT, detail="took too long", latency_ms=30_000
    )
    assert success.ok is True
    assert failure.ok is False


def test_a_failure_has_no_assessment_to_misread() -> None:
    failure = AssessmentFailed(reason=EscalationReason.API_ERROR, detail="503", latency_ms=12)
    assert not hasattr(failure, "assessment")
    assert failure.metering is None


def test_outcomes_are_frozen() -> None:
    """A metering row is a record of one instant; it is a value, not a workspace."""
    success = AssessmentSucceeded(assessment=assessment(), metering=metering())
    with pytest.raises(dataclasses.FrozenInstanceError):
        success.metering = metering()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        success.metering.latency_ms = 0  # type: ignore[misc]


def test_low_confidence_is_not_an_adapter_failure_reason() -> None:
    """``low_confidence`` is #9's judgement on a *successful* assessment, never a failure."""
    adapter_reasons = frozenset(EscalationReason) - {EscalationReason.LOW_CONFIDENCE}
    assert adapter_reasons == {
        EscalationReason.MODEL_REFUSAL,
        EscalationReason.PARSE_ERROR,
        EscalationReason.API_ERROR,
        EscalationReason.TIMEOUT,
    }


def test_effort_levels_are_the_five_the_model_accepts() -> None:
    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
    assert DEFAULT_EFFORT in EFFORT_LEVELS
