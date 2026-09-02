"""Behaviour of ``decide`` — the code half of "the model assesses, code routes".

Written adversarially, because the expensive failure here is silent: a lead that is quietly
dropped generates no alert, no bounce and no complaint, and the deal is simply lost. The
last section is invariant 3 as an executable guarantee.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

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
from leadquali.domain.routing import (
    LOW_CONFIDENCE_NOTE,
    LOW_SCORE_SUPPRESSION_NOTE,
    SPAM_NOTE,
    SYSTEM_FAILURE_BANNER,
    decide,
    system_failure,
)
from leadquali.domain.scoring import weighted_total
from leadquali.domain.tenant_config import DIMENSION_MAXIMA, TenantConfig

EMAIL_EVERYTHING: dict[str, Any] = {
    "hot": {"action": "email_sales", "destination": "hot@acme.test"},
    "warm": {"action": "email_sales", "destination": "sales@acme.test"},
    "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
    "disqualified": {"action": "email_sales", "destination": "audit@acme.test"},
}

#: The shipped default shape: the bottom tier is suppressed by policy.
SUPPRESS_THE_BOTTOM: dict[str, Any] = {**EMAIL_EVERYTHING, "disqualified": {"action": "suppress"}}

MINIMAL: dict[str, Any] = {
    "tenant_id": "acme",
    "name": "Acme Corp",
    "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
    "routing_rules": SUPPRESS_THE_BOTTOM,
}

NEUTRAL: dict[str, float] = dict.fromkeys(DIMENSION_MAXIMA, 1.0)


def make_config(**overrides: Any) -> TenantConfig:
    """A valid config with only the named fields changed. Neutral weights, plan defaults."""
    return TenantConfig.model_validate({**MINIMAL, **overrides})


def scores(**values: int) -> DimensionScores:
    return DimensionScores.model_validate({**dict.fromkeys(DIMENSION_MAXIMA, 0), **values})


def scoring_to(total: int) -> DimensionScores:
    """Dimension scores that sum to ``total``; under neutral weights that *is* the total."""
    remaining = total
    values: dict[str, int] = {}
    for name, top in DIMENSION_MAXIMA.items():
        values[name] = min(top, remaining)
        remaining -= values[name]
    assert remaining == 0, f"{total} is above the rubric's maximum"
    return scores(**values)


def make_assessment(**overrides: Any) -> LeadAssessment:
    base: dict[str, Any] = {
        "dimension_scores": scores(icp_fit=20, intent=15, authority=10, urgency=8, budget_signal=7),
        "extracted": ExtractedFacts.model_validate(dict.fromkeys(ExtractedFacts.model_fields)),
        "reasoning": "Director at a mid-size logistics firm naming a concrete use case.",
        "confidence": 0.82,
        "missing_information": [],
        "suggested_first_question": None,
        "spam_or_test_submission": False,
    }
    return LeadAssessment.model_validate({**base, **overrides})


# ------------------------------------------------------------------- 1. spam suppresses


def test_spam_is_suppressed_as_disqualified_with_no_escalation_reason() -> None:
    decision = decide(make_assessment(spam_or_test_submission=True), make_config())
    assert decision.tier is Tier.DISQUALIFIED
    assert decision.action is Action.SUPPRESS
    assert decision.escalation_reason is None
    assert decision.escalated is False
    assert decision.total_score == 0.0
    assert decision.note == SPAM_NOTE


def test_spam_beats_a_perfect_score() -> None:
    decision = decide(
        make_assessment(
            spam_or_test_submission=True,
            dimension_scores=scores(**dict(DIMENSION_MAXIMA)),
            confidence=1.0,
        ),
        make_config(),
    )
    assert (decision.tier, decision.action) == (Tier.DISQUALIFIED, Action.SUPPRESS)


def test_spam_beats_low_confidence_and_attaches_no_escalation_reason() -> None:
    """The two top branches can fire on the same lead. Spam wins, and it must not carry an
    escalation reason: ``RoutingDecision`` forbids escalating and suppressing at once."""
    decision = decide(
        make_assessment(spam_or_test_submission=True, confidence=0.0),
        make_config(min_confidence=0.9),
    )
    assert decision.action is Action.SUPPRESS
    assert decision.escalation_reason is None


def test_spam_is_suppressed_even_where_the_tenant_routes_the_bottom_tier_to_sales() -> None:
    """Suppression is the one product-level exception to invariant 3, not a tenant knob."""
    decision = decide(
        make_assessment(spam_or_test_submission=True),
        make_config(routing_rules=EMAIL_EVERYTHING),
    )
    assert (decision.tier, decision.action) == (Tier.DISQUALIFIED, Action.SUPPRESS)


# --------------------------------------------------------------- 2. the confidence gate


def test_low_confidence_goes_to_sales_as_warm_and_says_why() -> None:
    assessment = make_assessment(confidence=0.4, dimension_scores=scoring_to(12))
    decision = decide(assessment, make_config(min_confidence=0.6))
    assert decision.tier is Tier.WARM
    assert decision.action is Action.EMAIL_SALES
    assert decision.escalation_reason is EscalationReason.LOW_CONFIDENCE
    assert decision.escalated is True
    assert decision.note == LOW_CONFIDENCE_NOTE
    assert decision.total_score == 12.0


def test_low_confidence_never_disqualifies_however_bad_the_score() -> None:
    """Uncertainty escalates. A lead the model could not read is not a bad lead."""
    decision = decide(
        make_assessment(confidence=0.0, dimension_scores=scores()),
        make_config(min_confidence=0.6),
    )
    assert decision.tier is Tier.WARM
    assert decision.action is not Action.SUPPRESS


def test_low_confidence_also_overrides_a_hot_score() -> None:
    decision = decide(
        make_assessment(confidence=0.1, dimension_scores=scoring_to(95)),
        make_config(min_confidence=0.6),
    )
    assert decision.tier is Tier.WARM
    assert decision.total_score == 95.0


def test_confidence_exactly_at_the_threshold_is_trusted() -> None:
    """The gate is ``confidence < min_confidence``: equality routes on the score.

    Stated as a test because it is the one place a half-open interval is a policy choice —
    a tenant that sets 0.6 means "0.6 is good enough", not "better than 0.6".
    """
    cfg = make_config(min_confidence=0.6)
    at = decide(make_assessment(confidence=0.6, dimension_scores=scoring_to(90)), cfg)
    assert at.tier is Tier.HOT
    assert at.escalation_reason is None

    below = decide(make_assessment(confidence=0.59, dimension_scores=scoring_to(90)), cfg)
    assert below.tier is Tier.WARM
    assert below.escalation_reason is EscalationReason.LOW_CONFIDENCE


def test_a_tenant_can_switch_the_confidence_gate_off() -> None:
    decision = decide(
        make_assessment(confidence=0.0, dimension_scores=scoring_to(90)),
        make_config(min_confidence=0.0),
    )
    assert decision.tier is Tier.HOT
    assert decision.escalation_reason is None


# ------------------------------------------------------------------ 3. scored routing


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (100, Tier.HOT),
        (81, Tier.HOT),
        (80, Tier.HOT),
        (79, Tier.WARM),
        (56, Tier.WARM),
        (55, Tier.WARM),
        (54, Tier.COLD),
        (31, Tier.COLD),
        (30, Tier.COLD),
        (29, Tier.DISQUALIFIED),
        (0, Tier.DISQUALIFIED),
    ],
)
def test_every_default_threshold_is_inclusive_from_both_sides(total: int, expected: Tier) -> None:
    decision = decide(make_assessment(dimension_scores=scoring_to(total)), make_config())
    assert decision.tier is expected
    assert decision.total_score == float(total)


def test_a_total_landing_exactly_on_a_fractional_threshold_tiers_deterministically() -> None:
    """The rounded score is the score that tiers, so a tenant can set a threshold to a value
    a lead can actually hit. Raw arithmetic here is 54.5454...; the reported score is 54.55,
    and a threshold written as 54.55 must be met by it rather than missed by 1e-14."""
    weights = {**dict.fromkeys(DIMENSION_MAXIMA, 0.0), "icp_fit": 1.0, "intent": 1.0}
    cfg = make_config(
        weights=weights,
        thresholds={"hot": 54.55, "warm": 30.0, "cold": 10.0},
    )
    assessment = make_assessment(dimension_scores=scores(icp_fit=30, intent=0))
    assert weighted_total(assessment.dimension_scores, cfg) == 54.55
    assert decide(assessment, cfg).tier is Tier.HOT


def test_the_action_comes_from_the_tenants_routing_table() -> None:
    cfg = make_config(
        routing_rules={
            **EMAIL_EVERYTHING,
            "cold": {"action": "escalate_human", "destination": "queue@acme.test"},
        }
    )
    decision = decide(make_assessment(dimension_scores=scoring_to(40)), cfg)
    assert decision.tier is Tier.COLD
    assert decision.action is Action.ESCALATE_HUMAN
    assert decision.escalation_reason is None


def test_a_tenant_can_move_its_own_boundaries() -> None:
    cfg = make_config(thresholds={"hot": 40.0, "warm": 20.0, "cold": 5.0})
    assert decide(make_assessment(dimension_scores=scoring_to(45)), cfg).tier is Tier.HOT
    assert decide(make_assessment(dimension_scores=scoring_to(21)), cfg).tier is Tier.WARM


def test_a_scored_decision_reports_its_score_in_the_note() -> None:
    decision = decide(make_assessment(dimension_scores=scoring_to(72)), make_config())
    assert "72.00" in decision.note
    assert Tier.WARM.value in decision.note


def test_deciding_twice_gives_the_same_answer() -> None:
    assessment = make_assessment(dimension_scores=scoring_to(63))
    cfg = make_config()
    assert decide(assessment, cfg) == decide(assessment, cfg)


# ----------------------------------------------------- invariant 3, executably guaranteed


def test_a_confidently_low_lead_is_suppressed_when_the_tenant_configured_that() -> None:
    """The ``disqualified`` tier exists to be suppressible, and the table is the tenant's.

    This is a judgement, not a doubt: the model was confident and the score is real, so
    honouring ``action_for`` here is invariant 1, not a breach of invariant 3.
    """
    decision = decide(make_assessment(dimension_scores=scoring_to(4)), make_config())
    assert decision.tier is Tier.DISQUALIFIED
    assert decision.action is Action.SUPPRESS
    assert decision.total_score == 4.0
    assert decision.escalation_reason is None


def test_a_tenant_can_ask_to_see_its_disqualified_leads_instead() -> None:
    """Nothing about suppression is hardcoded: the same lead, a different table."""
    decision = decide(
        make_assessment(dimension_scores=scoring_to(4)),
        make_config(routing_rules=EMAIL_EVERYTHING),
    )
    assert decision.tier is Tier.DISQUALIFIED
    assert decision.action is Action.EMAIL_SALES


def test_the_two_suppressions_are_told_apart_by_their_notes() -> None:
    """Spam and a bad score are different answers to "why did we never contact this lead?",
    and #21 has to be able to group on the difference."""
    spam = decide(make_assessment(spam_or_test_submission=True), make_config())
    scored = decide(make_assessment(dimension_scores=scoring_to(4)), make_config())

    assert spam.action is scored.action is Action.SUPPRESS
    assert spam.note == SPAM_NOTE
    assert scored.note.startswith(LOW_SCORE_SUPPRESSION_NOTE)
    assert "4.00" in scored.note
    assert spam.note != scored.note


PATHOLOGICAL: dict[str, Any] = {
    "hot": {"action": "suppress"},
    "warm": {"action": "suppress"},
    "cold": {"action": "suppress"},
    "disqualified": {"action": "suppress"},
}

CONFIGS: dict[str, TenantConfig] = {
    "default_shape": make_config(),
    "emails_everything": make_config(routing_rules=EMAIL_EVERYTHING),
    "suppresses_every_tier": make_config(routing_rules=PATHOLOGICAL),
    "generous_gate": make_config(min_confidence=0.99),
    "no_gate": make_config(min_confidence=0.0),
    "lopsided_weights": make_config(
        weights={**dict.fromkeys(DIMENSION_MAXIMA, 0.01), "authority": 50.0}
    ),
    "narrow_bands": make_config(thresholds={"hot": 99.0, "warm": 98.0, "cold": 97.0}),
    "wide_open": make_config(thresholds={"hot": 3.0, "warm": 2.0, "cold": 1.0}),
}


def _sample_assessments(seed: int, count: int) -> list[LeadAssessment]:
    rng = random.Random(seed)
    samples: list[LeadAssessment] = []
    for _ in range(count):
        samples.append(
            make_assessment(
                dimension_scores=scores(
                    **{name: rng.randint(0, top) for name, top in DIMENSION_MAXIMA.items()}
                ),
                confidence=rng.choice([0.0, 0.59, 0.6, 0.61, 0.99, 1.0, rng.random()]),
            )
        )
    return samples


def test_suppression_has_exactly_two_doors_and_no_others() -> None:
    """Invariant 3, as a property over the whole reachable input space.

    Every tenant shape, every corner of the score space, every position around the
    confidence gate. A decision may only suppress if the model called it spam, or if the
    lead was confidently scored into a tier this tenant configured to suppress. Any third
    route to ``SUPPRESS`` is a lead lost with no alert, no bounce and no complaint.
    """
    for cfg in CONFIGS.values():
        for assessment in _sample_assessments(seed=20260902, count=250):
            decision = decide(assessment, cfg)
            assert 0.0 <= decision.total_score <= MAX_TOTAL_SCORE
            if decision.action is not Action.SUPPRESS:
                continue
            spam = assessment.spam_or_test_submission
            confidently_scored_into_a_suppressed_tier = (
                assessment.confidence >= cfg.min_confidence
                and cfg.action_for(cfg.tier_for(weighted_total(assessment.dimension_scores, cfg)))
                is Action.SUPPRESS
            )
            assert spam or confidently_scored_into_a_suppressed_tier, (cfg.tenant_id, decision)
            assert decision.escalation_reason is None, (cfg.tenant_id, decision)


def test_both_doors_to_suppression_actually_open() -> None:
    """The property above is only worth something if neither door is unreachable."""
    spam = decide(make_assessment(spam_or_test_submission=True), make_config())
    scored = decide(make_assessment(dimension_scores=scoring_to(4)), make_config())
    assert spam.action is Action.SUPPRESS
    assert scored.action is Action.SUPPRESS


GATED = sorted(label for label, cfg in CONFIGS.items() if cfg.min_confidence > 0.0)


@pytest.mark.parametrize("label", GATED)
def test_the_confidence_gate_can_never_suppress_or_disqualify_at_any_score(label: str) -> None:
    """Doubt is not a judgement. At every reachable total, under every tenant shape —
    including one that asks for every single tier to be suppressed — a lead the model was
    not confident about reaches a person."""
    cfg = CONFIGS[label]
    for total in range(int(MAX_TOTAL_SCORE) + 1):
        decision = decide(make_assessment(confidence=0.0, dimension_scores=scoring_to(total)), cfg)
        assert decision.action is not Action.SUPPRESS, (label, total, decision)
        assert decision.tier is not Tier.DISQUALIFIED, (label, total, decision)
        assert decision.action is Action.EMAIL_SALES
        assert decision.escalation_reason is EscalationReason.LOW_CONFIDENCE


@pytest.mark.parametrize("reason", list(EscalationReason))
def test_no_system_failure_can_suppress_or_disqualify(reason: EscalationReason) -> None:
    """The other escalation path, held to the same guarantee. A failure of ours must never
    look like a judgement about the lead, and must never be a dead end."""
    decision = system_failure(reason)
    assert decision.action is not Action.SUPPRESS
    assert decision.tier is not Tier.DISQUALIFIED
    assert decision.tier.rank >= Tier.WARM.rank
    assert decision.escalated is True


def test_an_explicit_spam_flag_always_suppresses_whatever_else_is_true() -> None:
    for cfg in CONFIGS.values():
        for assessment in _sample_assessments(seed=7, count=50):
            decision = decide(assessment.model_copy(update={"spam_or_test_submission": True}), cfg)
            assert decision.action is Action.SUPPRESS
            assert decision.tier is Tier.DISQUALIFIED


# ------------------------------------------------------------------- 4. system failures


@pytest.mark.parametrize(
    "reason",
    [
        EscalationReason.MODEL_REFUSAL,
        EscalationReason.PARSE_ERROR,
        EscalationReason.API_ERROR,
        EscalationReason.TIMEOUT,
    ],
)
def test_a_system_failure_reaches_sales_unqualified_and_never_disqualified(
    reason: EscalationReason,
) -> None:
    decision = system_failure(reason)
    assert isinstance(decision, RoutingDecision)
    assert decision.tier is Tier.WARM
    assert decision.action is Action.EMAIL_SALES
    assert decision.escalation_reason is reason
    assert decision.escalated is True
    assert decision.total_score == 0.0
    assert decision.note.startswith(SYSTEM_FAILURE_BANNER)


def test_a_system_failure_names_the_reason_in_words_a_person_can_act_on() -> None:
    notes = {system_failure(reason).note for reason in EscalationReason}
    assert len(notes) == len(EscalationReason), "each reason needs its own explanation"
    assert all(note.startswith(SYSTEM_FAILURE_BANNER) for note in notes)


def test_a_system_failure_can_carry_an_operator_detail() -> None:
    decision = system_failure(EscalationReason.API_ERROR, detail="overloaded_error after 3 tries")
    assert decision.note.startswith(SYSTEM_FAILURE_BANNER)
    assert "overloaded_error after 3 tries" in decision.note


def test_a_system_failure_detail_is_normalised_not_pasted() -> None:
    """The note goes into an email. A stray newline from an exception message must not."""
    decision = system_failure(EscalationReason.TIMEOUT, detail="  read timeout\n\nafter 30s  ")
    assert "\n" not in decision.note
    assert "read timeout after 30s" in decision.note


def test_system_failures_are_deterministic() -> None:
    assert system_failure(EscalationReason.TIMEOUT) == system_failure(EscalationReason.TIMEOUT)
