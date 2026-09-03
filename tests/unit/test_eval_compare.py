"""Comparing two saved eval results: the maths, the noise floor, and the diff CLI.

Everything here is offline and free. The diff tool exists precisely so that "did last
Tuesday's prompt change make things worse?" is answerable from artifacts already paid for,
so a test that needed a key would defeat the point of the module it tests.

The tests are grouped by the mistake they prevent:

1. **Reading two incomparable files as one experiment.** A schema-version mismatch is
   refused outright; a case present in one run and not the other is reported rather than
   quietly dropped out of a denominator.
2. **Calling noise a finding.** The noise floor is asserted from both sides — a difference
   below it must be reported as indistinguishable, and one above it must not be explained
   away. This is the substance of #24: at fifteen cases a one-case difference is a coin
   flip, and a report that presents it as a result gets somebody to pick ``low`` on the
   strength of one.
3. **Fabricating a recommendation.** With zero real cases in the golden set the tool must
   refuse to name a winner, whatever the numbers happen to say.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.evals import compare, diff_results
from tests.evals.report import RESULT_SCHEMA_VERSION

# --------------------------------------------------------------------- result documents


def _ratio(numerator: int, denominator: int, reason: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if denominator == 0 else numerator / denominator,
    }
    if denominator == 0:
        payload["undefined_reason"] = reason or "nothing to measure"
    return payload


def _case(
    case_id: str,
    expected: str,
    predicted: str,
    *,
    status: str = "ok",
    latency_ms: int = 4000,
    cost: str = "0.0200",
    escalated: bool = False,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "provenance": "synthetic",
        "hard_case": False,
        "injection_case_id": None,
        "tags": [],
        "expected_tier": expected,
        "expected_band": {"lower": expected, "upper": expected},
        "predicted_tier": predicted,
        "status": status,
        "exact_match": expected == predicted,
        "adjacent_match": True,
        "within_band": expected == predicted,
        "expected_contactable": expected in {"hot", "warm"},
        "predicted_contactable": predicted in {"hot", "warm"},
        "false_disqualification": expected in {"hot", "warm"} and predicted not in {"hot", "warm"},
        "assessed": True,
        "escalated": escalated,
        "escalation_reason": None,
        "expect_escalation": False,
        "missing_expected_escalation": False,
        "dimension_range_violations": [],
        "extracted_mismatches": [],
        "cost_usd": cost,
        "latency_ms": latency_ms,
        "labelers": ["seed_author"],
        "label_notes": "",
        "model_reasoning": "",
        "record": {},
    }


#: Fifteen cases, the size of the shipped seed set, so the arithmetic in these tests is
#: the arithmetic the owner will actually be reading.
def _fifteen(exact_hits: int) -> list[dict[str, Any]]:
    """Fifteen hot-labeled cases, ``exact_hits`` of which the pipeline got right."""
    return [
        _case(
            f"case_{index:02d}",
            "hot",
            "hot" if index < exact_hits else "cold",
            status="ok" if index < exact_hits else "LOST",
        )
        for index in range(15)
    ]


def _payload(
    *,
    effort: str = "medium",
    cases: Sequence[Mapping[str, Any]] | None = None,
    metrics: Mapping[str, tuple[int, int]] | None = None,
    schema_version: int = RESULT_SCHEMA_VERSION,
    prompt_version: str = "rubric_v1",
    model_id: str = "claude-opus-5",
    git_sha: str = "abc1234def",
    git_dirty: bool = False,
    real_cases: int = 0,
    cost_per_lead: str = "0.020000",
    p95_ms: int = 5000,
    stability: Mapping[str, Any] | None = None,
    golden_set_path: str = "tests/evals/golden_leads.jsonl",
) -> dict[str, Any]:
    """A complete result document in ``tests.evals.report.as_json`` shape."""
    case_list = list(cases if cases is not None else _fifteen(13))
    total = len(case_list)
    counts = metrics or {}
    default = (sum(1 for case in case_list if case["exact_match"]), total)
    resolved = {
        name: counts.get(name, default)
        for name in (
            "recall_on_contactable",
            "precision_on_hot",
            "exact_tier_accuracy",
            "adjacent_tier_accuracy",
            "within_band_accuracy",
        )
    }
    return {
        "schema_version": schema_version,
        "caveat": "Synthetic set; these numbers measure self-consistency, not correctness.",
        "run": {
            "started_at": "2026-09-03T12:00:00+00:00",
            "finished_at": "2026-09-03T12:02:00+00:00",
            "duration_seconds": 120.0,
            "git_sha": git_sha,
            "git_dirty": git_dirty,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "effort": effort,
            "tenant_id": "default",
            "concurrency": 4,
            "repeats": 1 if stability is None else int(stability["repeats"]),
            "golden_set_path": golden_set_path,
        },
        "golden_set": {
            "path": golden_set_path,
            "counts": {"total": total, "real": real_cases, "synthetic": total - real_cases},
            "inter_labeler_agreement": None,
            "meets_acceptance_criteria": False,
            "acceptance_gaps": ["needs 50 real cases"],
            "summary": "15 synthetic cases, 0 real.",
        },
        "metrics": {
            "name": "all",
            "cases": total,
            "synthetic_cases": total - real_cases,
            "real_cases": real_cases,
            "assessed": total,
            "failures": 0,
            **{name: _ratio(*value) for name, value in resolved.items()},
            "false_disqualified_case_ids": [],
            "escalations": 0,
            "escalations_by_reason": {},
            "missing_expected_escalations": [],
            "total_cost_usd": "0.300000",
            "cost_usd_per_lead": cost_per_lead,
            "latency": {"p50_ms": 4000, "p95_ms": p95_ms, "max_ms": p95_ms},
            "caveat": "Every one of these cases is synthetic.",
        },
        "segments": {},
        "confusion_matrix": {},
        "stability": stability,
        "security_findings": [],
        "cases": case_list,
    }


def _stability(repeats: int, spreads: Mapping[str, float | None]) -> dict[str, Any]:
    return {
        "repeats": repeats,
        "tier_stability": _ratio(14, 15),
        "unstable_case_ids": ["case_14"],
        "metric_spreads": {
            name: {"minimum": None, "maximum": None, "spread": spread, "values": []}
            for name, spread in spreads.items()
        },
    }


def _write(tmp_path: Path, name: str, payload: Mapping[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- loading


def test_a_result_document_round_trips_into_a_snapshot() -> None:
    snapshot = compare.parse_result(_payload(effort="low"), label="low")
    assert snapshot.effort == "low"
    assert snapshot.cases_total == 15
    assert snapshot.real_cases == 0
    assert snapshot.metrics["exact_tier_accuracy"].numerator == 13
    assert snapshot.metrics["exact_tier_accuracy"].denominator == 15
    assert set(snapshot.cases) == {f"case_{index:02d}" for index in range(15)}


def test_a_newer_schema_version_is_refused_rather_than_guessed_at() -> None:
    """The keys may have moved. Reading them anyway would produce a confident wrong diff."""
    with pytest.raises(compare.ComparisonError) as caught:
        compare.parse_result(_payload(schema_version=RESULT_SCHEMA_VERSION + 1), label="new")
    message = str(caught.value)
    assert str(RESULT_SCHEMA_VERSION + 1) in message
    assert str(RESULT_SCHEMA_VERSION) in message


def test_an_older_schema_version_is_refused_too() -> None:
    with pytest.raises(compare.ComparisonError):
        compare.parse_result(_payload(schema_version=RESULT_SCHEMA_VERSION - 1), label="old")


def test_a_document_that_is_not_a_result_file_is_rejected_with_the_key_it_wanted() -> None:
    with pytest.raises(compare.ComparisonError) as caught:
        compare.parse_result({"hello": "world"}, label="junk")
    assert "schema_version" in str(caught.value)


# ---------------------------------------------------------------------- noise floor


def test_the_resolution_floor_is_one_case() -> None:
    """Arithmetic, not statistics: a proportion over n cases moves in steps of 1/n."""
    floor = compare.NoiseFloor(cases=15, observed_spread_pp=None, repeats=1)
    assert floor.resolution_pp == pytest.approx(100 / 15)
    assert floor.floor_pp == pytest.approx(compare.MIN_MEANINGFUL_CASES * 100 / 15)
    assert not floor.measured


def test_a_one_case_difference_at_fifteen_cases_is_within_noise() -> None:
    floor = compare.NoiseFloor(cases=15, observed_spread_pp=None, repeats=1)
    assert not floor.is_meaningful(100 / 15)
    assert floor.verdict_for(100 / 15) == compare.VERDICT_WITHIN_NOISE


def test_a_measured_spread_can_raise_the_floor_above_the_resolution_limit() -> None:
    """Repeats of an unchanged prompt that move 40pp make a 20pp difference meaningless."""
    floor = compare.NoiseFloor(cases=15, observed_spread_pp=40.0, repeats=3)
    assert floor.measured
    assert floor.floor_pp == pytest.approx(40.0)
    assert not floor.is_meaningful(20.0)
    assert floor.is_meaningful(45.0)


def test_a_large_difference_is_reported_as_meaningful() -> None:
    floor = compare.NoiseFloor(cases=100, observed_spread_pp=1.0, repeats=2)
    assert floor.is_meaningful(20.0)
    assert floor.verdict_for(20.0) == compare.VERDICT_MEANINGFUL


def test_the_floor_describes_itself_including_whether_it_was_measured() -> None:
    unmeasured = compare.NoiseFloor(cases=15, observed_spread_pp=None, repeats=1).describe()
    assert "--repeat" in unmeasured
    assert "not measured" in unmeasured
    measured = compare.NoiseFloor(cases=15, observed_spread_pp=6.0, repeats=2).describe()
    assert "not measured" not in measured


def test_cases_needed_for_a_target_resolution_is_the_reciprocal() -> None:
    assert compare.cases_needed_for(6.67) == 15
    assert compare.cases_needed_for(5.0) == 20
    assert compare.cases_needed_for(2.0) == 50


def test_p95_over_a_small_set_is_the_slowest_case_not_a_tail_estimate() -> None:
    """Nearest-rank p95 at n<=20 selects the maximum. Worth saying out loud in a report."""
    assert compare.p95_is_max(15)
    assert compare.p95_is_max(19)
    assert not compare.p95_is_max(20)


def test_an_empty_set_has_no_resolution_at_all() -> None:
    floor = compare.NoiseFloor(cases=0, observed_spread_pp=None, repeats=1)
    assert floor.resolution_pp is None
    assert not floor.is_meaningful(100.0)


def test_the_floor_is_taken_from_the_widest_spread_across_the_runs_compared() -> None:
    quiet = compare.parse_result(
        _payload(effort="low", stability=_stability(2, {"exact_tier_accuracy": 0.02})),
        label="low",
    )
    noisy = compare.parse_result(
        _payload(effort="high", stability=_stability(2, {"exact_tier_accuracy": 0.25})),
        label="high",
    )
    floor = compare.noise_floor([quiet, noisy], "exact_tier_accuracy")
    assert floor.observed_spread_pp == pytest.approx(25.0)
    assert floor.floor_pp == pytest.approx(25.0)


# ----------------------------------------------------------------------- comparison


def test_a_metric_delta_reports_both_percentage_points_and_whole_cases() -> None:
    baseline = compare.parse_result(
        _payload(effort="medium", metrics={"exact_tier_accuracy": (13, 15)}), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="low", metrics={"exact_tier_accuracy": (10, 15)}), label="low"
    )
    comparison = compare.Comparison.of(baseline, candidate)
    delta = comparison.metric("exact_tier_accuracy")
    assert delta.delta_cases == -3
    assert delta.delta_pp == pytest.approx(-20.0)


def test_a_one_case_difference_is_reported_as_indistinguishable_not_as_a_regression() -> None:
    """The whole issue in one assertion."""
    baseline = compare.parse_result(
        _payload(effort="medium", metrics={"exact_tier_accuracy": (13, 15)}), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="low", metrics={"exact_tier_accuracy": (12, 15)}), label="low"
    )
    comparison = compare.Comparison.of(baseline, candidate)
    delta = comparison.metric("exact_tier_accuracy")
    assert delta.delta_cases == -1
    assert not delta.meaningful
    assert delta.verdict == compare.VERDICT_WITHIN_NOISE
    assert not comparison.any_meaningful


def test_a_difference_above_the_floor_is_not_explained_away() -> None:
    baseline = compare.parse_result(
        _payload(effort="medium", metrics={"recall_on_contactable": (15, 15)}), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="low", metrics={"recall_on_contactable": (8, 15)}), label="low"
    )
    comparison = compare.Comparison.of(baseline, candidate)
    delta = comparison.metric("recall_on_contactable")
    assert delta.meaningful
    assert delta.verdict == compare.VERDICT_MEANINGFUL
    assert comparison.any_meaningful
    assert comparison.regressions == ("recall_on_contactable",)


def test_an_undefined_metric_on_either_side_is_not_comparable() -> None:
    """Precision on hot with nothing predicted hot is not zero, and not a delta either."""
    baseline = compare.parse_result(
        _payload(effort="medium", metrics={"precision_on_hot": (0, 0)}), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="low", metrics={"precision_on_hot": (4, 5)}), label="low"
    )
    delta = compare.Comparison.of(baseline, candidate).metric("precision_on_hot")
    assert delta.delta_pp is None
    assert delta.verdict == compare.VERDICT_NOT_COMPARABLE


def test_different_denominators_report_points_but_refuse_to_count_cases() -> None:
    """12/15 to 12/16 is the golden set growing, not the model regressing."""
    baseline = compare.parse_result(
        _payload(effort="medium", metrics={"exact_tier_accuracy": (12, 15)}), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="medium", metrics={"exact_tier_accuracy": (12, 16)}), label="candidate"
    )
    delta = compare.Comparison.of(baseline, candidate).metric("exact_tier_accuracy")
    assert delta.delta_cases is None
    assert delta.delta_pp is not None


def test_cost_and_latency_deltas_are_reported_beside_the_accuracy() -> None:
    baseline = compare.parse_result(
        _payload(effort="medium", cost_per_lead="0.020000", p95_ms=5000), label="medium"
    )
    candidate = compare.parse_result(
        _payload(effort="low", cost_per_lead="0.005000", p95_ms=2000), label="low"
    )
    comparison = compare.Comparison.of(baseline, candidate)
    assert comparison.cost_per_lead_delta is not None
    assert float(comparison.cost_per_lead_delta) == pytest.approx(-0.015)
    assert comparison.p95_latency_delta_ms == -3000


# ------------------------------------------------------------- case-set differences


def test_a_case_present_in_one_run_and_not_the_other_is_named_not_dropped() -> None:
    baseline = compare.parse_result(
        _payload(cases=[_case("kept", "hot", "hot"), _case("only_in_baseline", "warm", "warm")]),
        label="baseline",
    )
    candidate = compare.parse_result(
        _payload(cases=[_case("kept", "hot", "hot"), _case("only_in_candidate", "cold", "cold")]),
        label="candidate",
    )
    comparison = compare.Comparison.of(baseline, candidate)
    assert comparison.cases.only_in_baseline == ("only_in_baseline",)
    assert comparison.cases.only_in_candidate == ("only_in_candidate",)
    assert comparison.cases.shared == ("kept",)
    assert not comparison.cases.comparable
    assert any("case set" in warning for warning in comparison.warnings)


def test_a_tier_that_moved_between_two_runs_is_listed_with_both_verdicts() -> None:
    baseline = compare.parse_result(
        _payload(cases=[_case("moved", "hot", "hot"), _case("stayed", "cold", "cold")]),
        label="baseline",
    )
    candidate = compare.parse_result(
        _payload(
            cases=[_case("moved", "hot", "cold", status="LOST"), _case("stayed", "cold", "cold")]
        ),
        label="candidate",
    )
    comparison = compare.Comparison.of(baseline, candidate)
    changed = comparison.cases.changes
    assert [change.case_id for change in changed] == ["moved"]
    assert changed[0].baseline_tier == "hot"
    assert changed[0].candidate_tier == "cold"
    assert changed[0].regressed


def test_two_identical_runs_report_no_case_movement() -> None:
    payload = _payload()
    comparison = compare.Comparison.of(
        compare.parse_result(payload, label="a"), compare.parse_result(payload, label="b")
    )
    assert comparison.cases.changes == ()
    assert not comparison.any_meaningful


# -------------------------------------------------------------------- attribution


def test_a_prompt_version_change_is_attributed_rather_than_read_as_a_regression() -> None:
    """The diff tool's job is exactly this comparison, so it explains rather than refuses."""
    baseline = compare.parse_result(_payload(prompt_version="rubric_v1"), label="before")
    candidate = compare.parse_result(_payload(prompt_version="rubric_v2"), label="after")
    comparison = compare.Comparison.of(baseline, candidate)
    joined = " ".join(comparison.warnings)
    assert "prompt_version" in joined
    assert "rubric_v1" in joined and "rubric_v2" in joined


def test_a_different_golden_set_or_model_is_also_attributed() -> None:
    baseline = compare.parse_result(_payload(model_id="claude-opus-5"), label="before")
    candidate = compare.parse_result(
        _payload(model_id="claude-haiku-9", golden_set_path="other.jsonl"), label="after"
    )
    joined = " ".join(compare.Comparison.of(baseline, candidate).warnings)
    assert "model_id" in joined
    assert "golden_set" in joined


def test_a_dirty_working_tree_is_called_out_as_unreproducible() -> None:
    baseline = compare.parse_result(_payload(), label="before")
    candidate = compare.parse_result(_payload(git_dirty=True), label="after")
    assert any(
        "dirty" in warning for warning in compare.Comparison.of(baseline, candidate).warnings
    )


# ------------------------------------------------------------------ the diff CLI


def test_the_diff_cli_compares_two_files_and_prints_the_deltas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _write(tmp_path, "before.json", _payload(metrics={"exact_tier_accuracy": (13, 15)}))
    after = _write(tmp_path, "after.json", _payload(metrics={"exact_tier_accuracy": (12, 15)}))
    code = diff_results.main([str(before), str(after)])
    assert code == diff_results.EXIT_OK
    out = capsys.readouterr().out
    assert "exact_tier_accuracy" in out
    assert compare.VERDICT_WITHIN_NOISE.lower() in out.lower()


def test_the_diff_cli_needs_no_key_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    before = _write(tmp_path, "before.json", _payload())
    after = _write(tmp_path, "after.json", _payload())
    assert diff_results.main([str(before), str(after)]) == diff_results.EXIT_OK
    assert "confirm-spend" not in capsys.readouterr().out


def test_the_diff_cli_exits_nonzero_on_a_meaningful_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _write(tmp_path, "before.json", _payload(metrics={"recall_on_contactable": (15, 15)}))
    after = _write(tmp_path, "after.json", _payload(metrics={"recall_on_contactable": (7, 15)}))
    code = diff_results.main([str(before), str(after)])
    assert code == diff_results.EXIT_MEANINGFUL_REGRESSION
    assert "recall_on_contactable" in capsys.readouterr().out


def test_the_diff_cli_refuses_a_schema_mismatch_with_both_versions_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _write(tmp_path, "before.json", _payload())
    after = _write(tmp_path, "after.json", _payload(schema_version=RESULT_SCHEMA_VERSION + 1))
    code = diff_results.main([str(before), str(after)])
    assert code == diff_results.EXIT_INPUT_ERROR
    assert "schema" in capsys.readouterr().err.lower()


def test_the_diff_cli_reports_a_missing_case_rather_than_a_smaller_denominator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _write(
        tmp_path,
        "before.json",
        _payload(cases=[_case("kept", "hot", "hot"), _case("dropped", "warm", "warm")]),
    )
    after = _write(tmp_path, "after.json", _payload(cases=[_case("kept", "hot", "hot")]))
    assert diff_results.main([str(before), str(after)]) == diff_results.EXIT_OK
    out = capsys.readouterr().out
    assert "dropped" in out
    assert "only in" in out.lower()


def test_the_diff_cli_reports_an_unreadable_file_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.json"
    existing = _write(tmp_path, "before.json", _payload())
    code = diff_results.main([str(existing), str(missing)])
    assert code == diff_results.EXIT_INPUT_ERROR
    assert "nope.json" in capsys.readouterr().err


def test_the_diff_cli_can_emit_the_comparison_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _write(tmp_path, "before.json", _payload(metrics={"exact_tier_accuracy": (13, 15)}))
    after = _write(tmp_path, "after.json", _payload(metrics={"exact_tier_accuracy": (12, 15)}))
    assert diff_results.main([str(before), str(after), "--json"]) == diff_results.EXIT_OK
    payload: object = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert payload["baseline"]["effort"] == "medium"
    assert payload["noise_floor"]["cases"] == 15
    metrics = payload["metrics"]
    assert isinstance(metrics, list)
    assert {entry["name"] for entry in metrics} >= {"exact_tier_accuracy"}


def test_the_rendered_diff_always_carries_the_synthetic_caveat(tmp_path: Path) -> None:
    """A number lifted out of this report must not lose the sentence that qualifies it."""
    comparison = compare.Comparison.of(
        compare.parse_result(_payload(), label="a"), compare.parse_result(_payload(), label="b")
    )
    rendered = compare.render_comparison(comparison)
    assert "self-consistency" in rendered


# ------------------------------------------------------------- the effort comparison


def _levels(**by_effort: tuple[int, int]) -> list[compare.ResultSnapshot]:
    """One snapshot per effort level, differing only in exact-tier accuracy."""
    return [
        compare.parse_result(
            _payload(effort=effort, metrics={"exact_tier_accuracy": counts}), label=effort
        )
        for effort, counts in by_effort.items()
    ]


def test_a_sweep_orders_its_levels_by_spend_not_by_argument_order() -> None:
    sweep = compare.SweepComparison.of(
        _levels(high=(13, 15), low=(12, 15), medium=(13, 15)), baseline="medium"
    )
    assert [snapshot.effort for snapshot in sweep.snapshots] == ["low", "medium", "high"]
    assert sweep.baseline.effort == "medium"


def test_a_sweep_compares_every_level_against_the_baseline() -> None:
    sweep = compare.SweepComparison.of(
        _levels(low=(12, 15), medium=(13, 15), high=(14, 15)), baseline="medium"
    )
    assert [comparison.candidate.effort for comparison in sweep.comparisons] == ["low", "high"]


def test_a_sweep_whose_levels_differ_by_one_case_says_so_in_exactly_those_words() -> None:
    """If the honest answer is that the set cannot tell these apart, the tool says it."""
    sweep = compare.SweepComparison.of(
        _levels(low=(12, 15), medium=(13, 15), high=(13, 15)), baseline="medium"
    )
    assert not sweep.distinguishable
    assert sweep.verdict == compare.VERDICT_INDISTINGUISHABLE
    assert compare.VERDICT_INDISTINGUISHABLE in compare.render_sweep(sweep)


def test_a_sweep_with_a_real_gap_reports_the_level_that_is_meaningfully_worse() -> None:
    sweep = compare.SweepComparison.of(
        _levels(low=(6, 15), medium=(14, 15), high=(14, 15)), baseline="medium"
    )
    assert sweep.distinguishable
    assert sweep.verdict != compare.VERDICT_INDISTINGUISHABLE
    assert "low" in sweep.verdict


def test_a_sweep_never_names_a_winner_while_the_golden_set_has_no_real_cases() -> None:
    """Indistinguishable is not the same as equivalent, and synthetic is not evidence."""
    sweep = compare.SweepComparison.of(
        _levels(low=(13, 15), medium=(13, 15), high=(13, 15)), baseline="medium"
    )
    recommendation = sweep.recommendation()
    assert recommendation.level is None
    assert "real" in recommendation.rationale


def test_a_sweep_declines_to_pick_a_level_purely_because_it_is_cheaper() -> None:
    """The failure mode this whole issue exists to prevent."""
    sweep = compare.SweepComparison.of(_levels(low=(13, 15), medium=(13, 15)), baseline="medium")
    rendered = compare.render_sweep(sweep)
    assert "cheapest" not in rendered.lower() or compare.VERDICT_INDISTINGUISHABLE in rendered
    assert sweep.recommendation().level is None


def test_the_sweep_report_states_the_minimum_detectable_difference() -> None:
    sweep = compare.SweepComparison.of(_levels(low=(12, 15), medium=(13, 15)), baseline="medium")
    rendered = compare.render_sweep(sweep)
    assert "6.7" in rendered
    assert "13.3" in rendered
    assert "self-consistency" in rendered


def test_the_sweep_report_warns_that_p95_over_a_small_set_is_the_slowest_case() -> None:
    sweep = compare.SweepComparison.of(_levels(low=(12, 15), medium=(13, 15)), baseline="medium")
    assert "slowest" in compare.render_sweep(sweep).lower()


def test_the_sweep_json_carries_every_level_and_its_deltas() -> None:
    sweep = compare.SweepComparison.of(
        _levels(low=(12, 15), medium=(13, 15), high=(14, 15)), baseline="medium"
    )
    payload = sweep.as_json()
    assert [level["effort"] for level in payload["levels"]] == ["low", "medium", "high"]
    assert payload["verdict"] == compare.VERDICT_INDISTINGUISHABLE
    assert payload["recommendation"]["level"] is None
    assert payload["caveat"]


def test_a_sweep_needs_a_baseline_that_was_actually_run() -> None:
    with pytest.raises(compare.ComparisonError):
        compare.SweepComparison.of(_levels(low=(12, 15), high=(13, 15)), baseline="medium")


def test_a_sweep_of_one_level_cannot_compare_anything_and_says_so() -> None:
    sweep = compare.SweepComparison.of(_levels(medium=(13, 15)), baseline="medium")
    assert sweep.comparisons == ()
    assert compare.VERDICT_INDISTINGUISHABLE in compare.render_sweep(sweep)


def _real_levels(**by_effort: tuple[int, int]) -> list[compare.ResultSnapshot]:
    """Levels whose golden set is fully real, so the recommendation gate can open."""
    return [
        compare.parse_result(
            _payload(effort=effort, real_cases=15, metrics={"recall_on_contactable": counts}),
            label=effort,
        )
        for effort, counts in by_effort.items()
    ]


def test_a_real_set_with_a_real_gap_names_the_cheapest_level_that_holds() -> None:
    """The issue's actual outcome, reachable only once the evidence supports it."""
    sweep = compare.SweepComparison.of(
        _real_levels(low=(15, 15), medium=(15, 15), high=(5, 15)), baseline="medium"
    )
    recommendation = sweep.recommendation()
    assert recommendation.level == "low"
    assert "cheapest" in recommendation.rationale


def test_a_level_that_regresses_meaningfully_is_passed_over_for_the_baseline() -> None:
    sweep = compare.SweepComparison.of(
        _real_levels(low=(5, 15), medium=(15, 15), high=(15, 15)), baseline="medium"
    )
    assert sweep.recommendation().level == "medium"


def test_a_real_set_that_still_cannot_separate_the_levels_recommends_nothing() -> None:
    """Real cases are necessary, not sufficient: the set must also be able to see."""
    sweep = compare.SweepComparison.of(
        _real_levels(low=(14, 15), medium=(15, 15), high=(15, 15)), baseline="medium"
    )
    recommendation = sweep.recommendation()
    assert recommendation.level is None
    assert compare.VERDICT_INDISTINGUISHABLE in recommendation.rationale
    assert "untested" in recommendation.rationale


def test_a_case_that_got_worse_is_surfaced_even_when_the_rate_is_noise() -> None:
    """A lost lead is a fact about that lead, whatever the denominator can resolve."""
    baseline = compare.parse_result(
        _payload(cases=[_case("kept", "warm", "warm"), _case("other", "cold", "cold")]),
        label="before",
    )
    candidate = compare.parse_result(
        _payload(
            cases=[
                _case("kept", "warm", "disqualified", status="LOST"),
                _case("other", "cold", "cold"),
            ]
        ),
        label="after",
    )
    comparison = compare.Comparison.of(baseline, candidate)
    assert not comparison.any_meaningful
    assert "kept" in comparison.verdict
    assert "evidence about that case" in comparison.verdict
    assert "kept" in compare.render_comparison(comparison)


def test_a_clean_comparison_does_not_invent_a_case_to_worry_about() -> None:
    payload = _payload()
    comparison = compare.Comparison.of(
        compare.parse_result(payload, label="a"), compare.parse_result(payload, label="b")
    )
    assert "Read the cases anyway" not in comparison.verdict
