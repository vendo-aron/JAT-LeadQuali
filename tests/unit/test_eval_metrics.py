"""The eval maths, against hand-computed fixtures.

These are the numbers a prompt change is judged by, so the arithmetic is pinned rather
than trusted. Every expectation here is computed by hand in the test name or the comment
next to it — a metric implementation checked against itself proves nothing.

The edge cases are the point. A precision of "0.0" when nothing was predicted hot is a
lie that reads as a catastrophe; a recall of "1.0" over an empty denominator is a lie that
reads as perfection. Both must come out *undefined*, carrying the reason, and both are
asserted below.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from leadquali.domain.models import EscalationReason, Tier
from tests.evals import Provenance
from tests.evals.metrics import (
    CONTACTABLE_TIERS,
    FINDING_ABOVE_CEILING,
    FINDING_GENUINE_LEAD_DESTROYED,
    FINDING_SCORED_HOT,
    CaseResult,
    ConfusionMatrix,
    Ratio,
    compute_segment,
    compute_stability,
    is_adjacent,
    is_contactable,
    percentile_ms,
    security_findings,
    synthetic_caveat,
)


def _case(
    case_id: str,
    expected: Tier,
    predicted: Tier,
    *,
    lower: Tier | None = None,
    upper: Tier | None = None,
    assessed: bool = True,
    escalation_reason: EscalationReason | None = None,
    cost: str = "0.02",
    latency_ms: int = 1000,
    provenance: Provenance = Provenance.SYNTHETIC,
    hard_case: bool = False,
    injection_case_id: str | None = None,
) -> CaseResult:
    """One case result, with everything irrelevant to the metric under test defaulted."""
    return CaseResult(
        case_id=case_id,
        provenance=provenance,
        expected_tier=expected,
        lower_bound=lower or expected,
        upper_bound=upper or expected,
        predicted_tier=predicted,
        assessed=assessed,
        escalation_reason=escalation_reason,
        total_score=42.0,
        confidence=0.9 if assessed else None,
        hard_case=hard_case,
        injection_case_id=injection_case_id,
        cost_usd=Decimal(cost),
        latency_ms=latency_ms,
    )


# ------------------------------------------------------------------------------ Ratio


def test_ratio_reports_a_fraction_and_its_counts() -> None:
    ratio = Ratio(3, 4)
    assert ratio.defined
    assert ratio.value == 0.75
    assert "3/4" in ratio.text


def test_ratio_over_an_empty_denominator_is_undefined_not_zero() -> None:
    ratio = Ratio(0, 0, "no lead was predicted hot")
    assert not ratio.defined
    assert ratio.value is None
    assert "undefined" in ratio.text
    assert "no lead was predicted hot" in ratio.text


def test_ratio_refuses_a_numerator_larger_than_its_denominator() -> None:
    """A metric that cannot be true is a bug, and it must not reach a report."""
    with pytest.raises(ValueError, match="numerator"):
        Ratio(5, 4)


def test_ratio_json_carries_the_counts_so_a_diff_shows_which_half_moved() -> None:
    assert Ratio(3, 4).as_json() == {"numerator": 3, "denominator": 4, "value": 0.75}
    empty = Ratio(0, 0, "nothing to divide").as_json()
    assert empty["value"] is None
    assert empty["undefined_reason"] == "nothing to divide"


# ---------------------------------------------------------------------- tier ordering


def test_contactable_is_hot_and_warm_by_rank() -> None:
    assert frozenset({Tier.HOT, Tier.WARM}) == CONTACTABLE_TIERS
    assert is_contactable(Tier.HOT)
    assert is_contactable(Tier.WARM)
    assert not is_contactable(Tier.COLD)
    assert not is_contactable(Tier.DISQUALIFIED)


@pytest.mark.parametrize(
    ("expected", "predicted", "adjacent"),
    [
        (Tier.HOT, Tier.HOT, True),
        (Tier.HOT, Tier.WARM, True),
        (Tier.WARM, Tier.HOT, True),
        # rank distance 2. String comparison would call "cold" < "hot" and get this wrong.
        (Tier.COLD, Tier.HOT, False),
        (Tier.HOT, Tier.COLD, False),
        (Tier.DISQUALIFIED, Tier.COLD, True),
        (Tier.DISQUALIFIED, Tier.HOT, False),
    ],
)
def test_adjacency_is_computed_from_rank_not_from_the_string(
    expected: Tier, predicted: Tier, adjacent: bool
) -> None:
    assert is_adjacent(expected, predicted) is adjacent


def test_the_string_ordering_this_module_refuses_to_use_really_is_wrong() -> None:
    """The trap #7 added `rank` for: lexicographically, `cold` outranks `hot`."""
    assert Tier.COLD < Tier.HOT  # string ordering, and business-nonsense
    assert Tier.COLD.rank < Tier.HOT.rank


# ----------------------------------------------------------------- the four metrics


def test_exact_and_adjacent_tier_accuracy_over_a_hand_counted_set() -> None:
    # 5 cases: 3 exact (a, b, e); d is adjacent (warm vs hot); c is two ranks out.
    results = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.WARM, Tier.WARM),
        _case("c", Tier.COLD, Tier.HOT),
        _case("d", Tier.HOT, Tier.WARM),
        _case("e", Tier.DISQUALIFIED, Tier.DISQUALIFIED),
    ]
    segment = compute_segment("all", results)
    assert segment.exact_tier_accuracy == Ratio(3, 5)
    assert segment.exact_tier_accuracy.value == 0.6
    assert segment.adjacent_tier_accuracy == Ratio(4, 5)
    assert segment.adjacent_tier_accuracy.value == 0.8


def test_precision_on_hot_is_over_the_leads_called_hot() -> None:
    # 4 predicted hot; the human agreed with 3 of them. Two other cases are not counted.
    results = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.HOT, Tier.HOT),
        _case("c", Tier.HOT, Tier.HOT),
        _case("d", Tier.COLD, Tier.HOT),
        _case("e", Tier.WARM, Tier.WARM),
        _case("f", Tier.HOT, Tier.COLD),
    ]
    segment = compute_segment("all", results)
    assert segment.precision_on_hot == Ratio(3, 4)
    assert segment.precision_on_hot.value == 0.75


def test_precision_on_hot_is_undefined_when_nothing_was_predicted_hot() -> None:
    """Zero hot predictions is not zero precision - there is nothing to be right about."""
    results = [
        _case("a", Tier.HOT, Tier.WARM),
        _case("b", Tier.WARM, Tier.COLD),
    ]
    segment = compute_segment("all", results)
    precision = segment.precision_on_hot
    assert not precision.defined
    assert precision.value is None
    assert precision.denominator == 0
    assert "hot" in precision.text


def test_precision_within_band_credits_a_label_that_permits_hot() -> None:
    """A label of `warm` with a ceiling of `hot` is not a wrong hot prediction."""
    results = [_case("a", Tier.WARM, Tier.HOT, upper=Tier.HOT)]
    segment = compute_segment("all", results)
    assert segment.precision_on_hot == Ratio(0, 1)
    assert segment.precision_on_hot_within_band == Ratio(1, 1)


def test_recall_on_contactable_is_the_false_disqualification_rate() -> None:
    # 4 cases labeled hot or warm; 3 of them were surfaced. `c` is the money case.
    results = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.WARM, Tier.HOT),
        _case("c", Tier.HOT, Tier.DISQUALIFIED),
        _case("d", Tier.WARM, Tier.WARM),
        _case("e", Tier.COLD, Tier.COLD),
    ]
    segment = compute_segment("all", results)
    assert segment.recall_on_contactable == Ratio(3, 4)
    assert segment.recall_on_contactable.value == 0.75
    assert segment.false_disqualified_case_ids == ("c",)


def test_recall_is_undefined_when_no_case_should_have_been_contacted() -> None:
    results = [
        _case("a", Tier.COLD, Tier.COLD),
        _case("b", Tier.DISQUALIFIED, Tier.COLD),
    ]
    segment = compute_segment("all", results)
    recall = segment.recall_on_contactable
    assert not recall.defined
    assert recall.denominator == 0
    assert "hot or warm" in recall.text
    assert segment.false_disqualified_case_ids == ()


def test_false_disqualified_ids_are_sorted_so_two_runs_diff_cleanly() -> None:
    results = [
        _case("zebra", Tier.HOT, Tier.COLD),
        _case("alpha", Tier.WARM, Tier.DISQUALIFIED),
    ]
    segment = compute_segment("all", results)
    assert segment.false_disqualified_case_ids == ("alpha", "zebra")


def test_cost_and_latency_come_straight_from_the_metering() -> None:
    results = [
        _case("a", Tier.HOT, Tier.HOT, cost="0.0100", latency_ms=1000),
        _case("b", Tier.WARM, Tier.WARM, cost="0.0200", latency_ms=9000),
        _case("c", Tier.COLD, Tier.COLD, cost="0.0300", latency_ms=3000),
        _case("d", Tier.COLD, Tier.COLD, cost="0.0400", latency_ms=2000),
    ]
    segment = compute_segment("all", results)
    assert segment.total_cost_usd == Decimal("0.1000")
    assert segment.cost_usd_per_lead == Decimal("0.025")
    # Nearest-rank p95 over 4 values: ceil(0.95 * 4) = 4, so the 4th smallest.
    assert segment.latency.p95_ms == 9000
    assert segment.latency.p50_ms == 2000
    assert segment.latency.max_ms == 9000


@pytest.mark.parametrize(
    ("values", "quantile", "expected"),
    [
        ([], 0.95, None),
        ([7], 0.95, 7),
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95, 10),
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5, 5),
        ([5, 1, 3], 0.95, 5),
    ],
)
def test_percentile_is_nearest_rank_and_order_independent(
    values: list[int], quantile: float, expected: int | None
) -> None:
    assert percentile_ms(values, quantile) == expected


# --------------------------------------------------------------------- the edge cases


def test_a_single_case_produces_defined_metrics() -> None:
    segment = compute_segment("all", [_case("only", Tier.HOT, Tier.HOT, latency_ms=1234)])
    assert segment.cases == 1
    assert segment.exact_tier_accuracy == Ratio(1, 1)
    assert segment.precision_on_hot == Ratio(1, 1)
    assert segment.recall_on_contactable == Ratio(1, 1)
    assert segment.latency.p95_ms == 1234
    assert segment.cost_usd_per_lead == Decimal("0.02")


def test_an_empty_segment_reports_undefined_everywhere_and_never_divides_by_zero() -> None:
    """An empty segment is normal: there are no real cases in the seed set yet."""
    segment = compute_segment("real", [])
    assert segment.cases == 0
    for ratio in (
        segment.exact_tier_accuracy,
        segment.adjacent_tier_accuracy,
        segment.within_band_accuracy,
        segment.precision_on_hot,
        segment.recall_on_contactable,
    ):
        assert not ratio.defined
    assert segment.total_cost_usd == Decimal(0)
    assert segment.cost_usd_per_lead is None
    assert segment.latency.p95_ms is None
    assert segment.caveat


# --------------------------------------------------------------- failures are escalations


def test_a_failed_case_is_counted_as_an_escalation_and_still_scored() -> None:
    """`system_failure` routes to WARM, so a failure is a prediction like any other."""
    results = [
        _case("ok", Tier.HOT, Tier.HOT),
        _case(
            "broke",
            Tier.COLD,
            Tier.WARM,
            assessed=False,
            escalation_reason=EscalationReason.API_ERROR,
            cost="0",
        ),
    ]
    segment = compute_segment("all", results)
    assert segment.cases == 2
    assert segment.assessed == 1
    assert segment.failures == 1
    assert segment.escalations == 1
    assert segment.escalations_by_reason == {"api_error": 1}
    # It counts against accuracy rather than vanishing from the denominator.
    assert segment.exact_tier_accuracy == Ratio(1, 2)


def test_a_low_confidence_escalation_is_an_escalation_but_not_a_failure() -> None:
    results = [
        _case(
            "unsure",
            Tier.WARM,
            Tier.WARM,
            escalation_reason=EscalationReason.LOW_CONFIDENCE,
        )
    ]
    segment = compute_segment("all", results)
    assert segment.failures == 0
    assert segment.escalations == 1
    assert segment.escalations_by_reason == {"low_confidence": 1}


def test_escalation_reasons_are_ordered_so_the_json_diffs_cleanly() -> None:
    results = [
        _case(
            "t", Tier.WARM, Tier.WARM, assessed=False, escalation_reason=EscalationReason.TIMEOUT
        ),
        _case(
            "a",
            Tier.WARM,
            Tier.WARM,
            assessed=False,
            escalation_reason=EscalationReason.API_ERROR,
        ),
        _case(
            "p",
            Tier.WARM,
            Tier.WARM,
            assessed=False,
            escalation_reason=EscalationReason.PARSE_ERROR,
        ),
    ]
    segment = compute_segment("all", results)
    assert list(segment.escalations_by_reason) == ["api_error", "parse_error", "timeout"]


# ----------------------------------------------------------------------- within-band


def test_within_band_accuracy_honours_the_label_bounds() -> None:
    results = [
        # Labeled cold, band cold..warm: a warm prediction is inside the band, not exact.
        _case("banded", Tier.COLD, Tier.WARM, upper=Tier.WARM),
        # Labeled cold with the same band: hot is outside it.
        _case("outside", Tier.COLD, Tier.HOT, upper=Tier.WARM),
    ]
    segment = compute_segment("all", results)
    assert segment.exact_tier_accuracy == Ratio(0, 2)
    assert segment.within_band_accuracy == Ratio(1, 2)


# ------------------------------------------------------------------- confusion matrix


def test_confusion_matrix_covers_every_tier_in_rank_order() -> None:
    results = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.HOT, Tier.WARM),
        _case("c", Tier.WARM, Tier.WARM),
        _case("d", Tier.COLD, Tier.DISQUALIFIED),
    ]
    matrix = ConfusionMatrix.of(results)
    assert matrix.total == 4
    assert matrix.counts[Tier.HOT][Tier.HOT] == 1
    assert matrix.counts[Tier.HOT][Tier.WARM] == 1
    assert matrix.counts[Tier.COLD][Tier.DISQUALIFIED] == 1
    # Every tier is present as a row and a column, even with no cases, so the shape of
    # the JSON does not change when the golden set grows a tier.
    assert list(matrix.counts) == [Tier.HOT, Tier.WARM, Tier.COLD, Tier.DISQUALIFIED]
    for row in matrix.counts.values():
        assert list(row) == [Tier.HOT, Tier.WARM, Tier.COLD, Tier.DISQUALIFIED]
    assert matrix.counts[Tier.DISQUALIFIED][Tier.HOT] == 0


def test_confusion_matrix_json_is_keyed_by_tier_name() -> None:
    matrix = ConfusionMatrix.of([_case("a", Tier.HOT, Tier.WARM)])
    payload = matrix.as_json()
    assert payload["hot"]["warm"] == 1
    assert payload["hot"]["hot"] == 0
    assert list(payload) == ["hot", "warm", "cold", "disqualified"]


def test_an_empty_confusion_matrix_is_all_zeroes() -> None:
    matrix = ConfusionMatrix.of([])
    assert matrix.total == 0
    assert all(count == 0 for row in matrix.counts.values() for count in row.values())


# ------------------------------------------------------------------------- the caveat


def test_the_caveat_names_the_synthetic_share_of_the_segment() -> None:
    all_synthetic = synthetic_caveat([_case("a", Tier.HOT, Tier.HOT)])
    assert "self-consistency" in all_synthetic
    assert "synthetic" in all_synthetic

    mixed = synthetic_caveat(
        [
            _case("a", Tier.HOT, Tier.HOT),
            _case("b", Tier.HOT, Tier.HOT, provenance=Provenance.REAL),
        ]
    )
    assert "1 of 2" in mixed
    assert "self-consistency" in mixed


def test_every_segment_carries_the_caveat_next_to_its_numbers() -> None:
    segment = compute_segment("hard_cases", [_case("a", Tier.HOT, Tier.HOT, hard_case=True)])
    assert "self-consistency" in segment.caveat


# ----------------------------------------------------------------------- nondeterminism


def test_stability_is_unmeasured_from_a_single_repeat() -> None:
    """One run cannot say anything about run-to-run variation, and must not pretend to."""
    assert compute_stability([[_case("a", Tier.HOT, Tier.HOT)]]) is None


def test_stability_counts_the_cases_whose_tier_moved_between_repeats() -> None:
    first = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.WARM, Tier.WARM),
        _case("c", Tier.COLD, Tier.COLD),
    ]
    second = [
        _case("a", Tier.HOT, Tier.HOT),
        _case("b", Tier.WARM, Tier.HOT),  # moved
        _case("c", Tier.COLD, Tier.COLD),
    ]
    stability = compute_stability([first, second])
    assert stability is not None
    assert stability.repeats == 2
    assert stability.tier_stability == Ratio(2, 3)
    assert stability.unstable_case_ids == ("b",)
    exact = stability.metric_spreads["exact_tier_accuracy"]
    # 3/3 in the first repeat, 2/3 in the second.
    assert exact.minimum == pytest.approx(2 / 3)
    assert exact.maximum == 1.0
    assert exact.spread == pytest.approx(1 / 3)


def test_stability_over_identical_repeats_reports_no_spread() -> None:
    run = [_case("a", Tier.HOT, Tier.HOT), _case("b", Tier.COLD, Tier.COLD)]
    stability = compute_stability([list(run), list(run)])
    assert stability is not None
    assert stability.tier_stability == Ratio(2, 2)
    assert stability.unstable_case_ids == ()
    assert stability.metric_spreads["exact_tier_accuracy"].spread == 0.0


def test_stability_refuses_repeats_over_different_case_sets() -> None:
    """Comparing two runs of different sets would produce a meaningless stability figure."""
    with pytest.raises(ValueError, match="same cases"):
        compute_stability([[_case("a", Tier.HOT, Tier.HOT)], [_case("b", Tier.HOT, Tier.HOT)]])


# ------------------------------------------------------------------ security findings


def _injection(
    case_id: str,
    predicted: Tier,
    *,
    expected: Tier = Tier.DISQUALIFIED,
    lower: Tier | None = None,
    upper: Tier | None = None,
    assessed: bool = True,
) -> CaseResult:
    return _case(
        case_id,
        expected,
        predicted,
        lower=lower,
        upper=upper,
        assessed=assessed,
        injection_case_id="direct_override",
    )


def test_an_injection_case_scored_hot_is_a_finding_not_a_metric() -> None:
    findings = security_findings([_injection("attack", Tier.HOT, upper=Tier.COLD)])
    assert len(findings) == 1
    assert findings[0].case_id == "attack"
    assert FINDING_SCORED_HOT in findings[0].kinds
    assert FINDING_ABOVE_CEILING in findings[0].kinds
    assert "hot" in findings[0].summary


def test_an_injection_case_at_its_declared_ceiling_is_not_a_finding() -> None:
    assert security_findings([_injection("attack", Tier.COLD, upper=Tier.COLD)]) == ()


def test_an_injection_case_above_its_ceiling_is_a_finding_even_below_hot() -> None:
    findings = security_findings([_injection("attack", Tier.WARM, upper=Tier.COLD)])
    assert findings[0].kinds == (FINDING_ABOVE_CEILING,)


def test_destroying_a_genuine_lead_that_carries_a_payload_is_also_a_finding() -> None:
    """The failure mode a blunt keyword filter causes, and the reason #12 has that case."""
    findings = security_findings(
        [
            _injection(
                "genuine_with_payload",
                Tier.DISQUALIFIED,
                expected=Tier.WARM,
                lower=Tier.COLD,
                upper=Tier.WARM,
            )
        ]
    )
    assert findings[0].kinds == (FINDING_GENUINE_LEAD_DESTROYED,)
    assert "cost a real lead" in findings[0].summary


def test_an_unassessed_injection_case_is_not_reported_as_a_successful_attack() -> None:
    """`system_failure` routes to WARM. That is our outage, not the attacker winning."""
    assert (
        security_findings([_injection("attack", Tier.WARM, upper=Tier.COLD, assessed=False)]) == ()
    )


def test_an_ordinary_lead_scored_hot_is_never_a_security_finding() -> None:
    assert security_findings([_case("ordinary", Tier.COLD, Tier.HOT)]) == ()


def test_findings_are_sorted_by_case_id() -> None:
    findings = security_findings(
        [
            _injection("zulu", Tier.HOT, upper=Tier.COLD),
            _injection("alpha", Tier.HOT, upper=Tier.COLD),
        ]
    )
    assert [finding.case_id for finding in findings] == ["alpha", "zulu"]


def test_a_finding_serialises_with_its_band_and_its_explanation() -> None:
    payload = security_findings([_injection("attack", Tier.HOT, upper=Tier.COLD)])[0].as_json()
    assert payload["case_id"] == "attack"
    assert payload["injection_case_id"] == "direct_override"
    assert payload["predicted_tier"] == "hot"
    assert payload["declared_band"] == {"lower": "disqualified", "upper": "cold"}
    assert payload["summary"]
