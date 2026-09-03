"""The eval harness, exercised offline: the refusals, the wiring, and the output contract.

Every test here drives ``run_eval.main`` with a scripted assessor and a fixed clock. None
of them needs an API key and none makes a network call — which is the point: the harness
is the one piece of this repository that spends money when it runs, so the machinery
around the spend has to be provable without spending.

Three groups, in order of how expensive the bug would be:

1. **The refusals.** A harness that starts because a CI job was mis-wired bills a real
   card and, at any real golden-set size, trips a rate limit. Both guards are asserted, and
   asserted to fire *before* an assessor is ever constructed.
2. **Failures are data.** A refusal, a timeout, a parse error, or an adapter that raises
   when its port says it must not: each one must land as an escalation on the tier
   ``system_failure`` produces, and must not cost the run the fourteen results already
   paid for.
3. **The output contract.** #24 diffs two result files, so ordering is deterministic, the
   JSON shape is pinned, and the synthetic-set caveat is asserted to appear beside every
   metric block rather than once at the top where it can be cropped out of a screenshot.

Nothing here asserts on model prose, because nothing here has any: the assessments are
fixtures, and what the model would actually say is the golden set's business, not this
file's.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from leadquali.adapters.tenant_config_json import (
    JsonFileTenantConfigLoader,
    default_tenants_dir,
)
from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
    Effort,
)
from leadquali.config import get_settings
from leadquali.domain.models import (
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.routing import system_failure
from leadquali.domain.tenant_config import TenantConfig
from tests.evals import golden_set as golden_set_module
from tests.evals import run_eval
from tests.evals.report import RESULT_SCHEMA_VERSION, SEGMENT_NAMES

# --------------------------------------------------------------------------- the corpus

_NOTE = (
    "Fixture set for the harness tests. Every case is synthetic, so any number computed "
    "against it measures self-consistency, not correctness."
)

_HEADER = {
    "$golden_set": {
        "schema_version": 1,
        "note": _NOTE,
        "min_total_cases": 1,
        "min_real_cases": 0,
        "acceptance_target_total_cases": 50,
        "acceptance_target_real_cases": 50,
    }
}


def _label(tier: str) -> list[dict[str, str]]:
    return [
        {
            "labeler": "fixture_author",
            "tier": tier,
            "labeled_at": "2026-09-03",
            "notes": "Written for the harness tests; the tier is whatever the test needs.",
        }
    ]


#: Three cases, one of each shape the harness treats differently: an ordinary lead, an
#: attack from #12's corpus (referenced, never copied), and a lead with a label band.
_CASES: list[dict[str, Any]] = [
    {
        "case_id": "a_hot_buyer",
        "provenance": "synthetic",
        "expected_tier": "hot",
        "form": {
            "full_name": "Sam Buyer",
            "email": "sam@example.com",
            "company": "Example Co",
            "message": "We have budget and a deadline and we want to buy this quarter.",
        },
        "labels": _label("hot"),
    },
    {
        "case_id": "b_injection",
        "provenance": "synthetic",
        "expected_tier": "disqualified",
        "injection_case_id": "direct_override",
        "hard_case": True,
        "labels": _label("disqualified"),
    },
    {
        "case_id": "c_warm_lead",
        "provenance": "synthetic",
        "expected_tier": "warm",
        "expected_max_tier": "hot",
        "form": {
            "full_name": "Robin Maybe",
            "email": "robin@example.org",
            "message": "Interested but no timeline yet, exploring options for next year.",
        },
        "labels": _label("warm"),
    },
]

#: A substring of each case's rendered lead, so a scripted assessor can answer per case
#: without depending on the renderer's per-call nonce.
MARKERS = {"a_hot_buyer": "Sam Buyer", "b_injection": "Alex Vance", "c_warm_lead": "Robin Maybe"}

API_KEY = "sk-ant-not-a-real-key"


@pytest.fixture
def golden_file(tmp_path: Path) -> Path:
    """The fixture golden set on disk, so ``main`` exercises the real loader."""
    path = tmp_path / "fixture_leads.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in [_HEADER, *_CASES]) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No key by default, and never a cached one from another test."""
    get_settings.cache_clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
    get_settings.cache_clear()


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key in the environment. Never used to call anything: the assessor is injected."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    get_settings.cache_clear()


@pytest.fixture
def config() -> TenantConfig:
    return JsonFileTenantConfigLoader(default_tenants_dir()).get("default")


# ------------------------------------------------------------------------- the fixtures

_FACTS = ExtractedFacts(
    company_name=None,
    industry=None,
    company_size_estimate=None,
    role_seniority=None,
    stated_use_case=None,
    stated_timeline=None,
)

# Weighted against the shipped default rubric these score 100 / 59 / 32 / 0, which is one
# of each tier. Computed from the thresholds in `tenants/default.json`, not guessed.
_SCORES: Mapping[Tier, DimensionScores] = {
    Tier.HOT: DimensionScores(icp_fit=30, intent=25, authority=15, urgency=15, budget_signal=15),
    Tier.WARM: DimensionScores(icp_fit=20, intent=15, authority=8, urgency=8, budget_signal=8),
    Tier.COLD: DimensionScores(icp_fit=12, intent=8, authority=4, urgency=4, budget_signal=4),
    Tier.DISQUALIFIED: DimensionScores(
        icp_fit=0, intent=0, authority=0, urgency=0, budget_signal=0
    ),
}


def _assessment(tier: Tier, *, confidence: float = 0.9) -> LeadAssessment:
    return LeadAssessment(
        dimension_scores=_SCORES[tier],
        extracted=_FACTS,
        reasoning=f"Fixture reasoning for a lead the fixture wants tiered {tier.value}.",
        confidence=confidence,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=False,
    )


def _metering(*, cost: str = "0.0200", latency_ms: int = 4000) -> CallMetering:
    return CallMetering(
        model_id="claude-opus-5",
        prompt_version="rubric_v1",
        effort="medium",
        input_tokens=2000,
        output_tokens=900,
        cache_read_tokens=1500,
        cache_creation_tokens=0,
        cost_usd=Decimal(cost),
        latency_ms=latency_ms,
    )


def scored(tier: Tier, *, confidence: float = 0.9, latency_ms: int = 4000) -> AssessmentOutcome:
    """A successful assessment that routes to ``tier`` under the default rubric."""
    return AssessmentSucceeded(
        assessment=_assessment(tier, confidence=confidence),
        metering=_metering(latency_ms=latency_ms),
    )


def refused(reason: EscalationReason = EscalationReason.MODEL_REFUSAL) -> AssessmentOutcome:
    """A billed non-answer: the shape the adapter returns instead of raising."""
    return AssessmentFailed(reason=reason, detail="stop_reason=refusal", latency_ms=2100)


class MappedAssessor:
    """A ``LeadAssessorPort`` that answers per case, keyed on a marker in the rendered lead.

    The renderer wraps every lead in a fresh nonce, so the rendered text is never equal
    twice; a marker substring is the stable way to recognise a case. Values are sequences,
    consumed one per call to that case, which is how a ``--repeat`` run is given a
    different answer the second time round.
    """

    def __init__(
        self,
        outcomes: Mapping[str, Sequence[AssessmentOutcome]],
        *,
        default: AssessmentOutcome | None = None,
    ) -> None:
        self._outcomes = {marker: list(values) for marker, values in outcomes.items()}
        self._default = default
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        with self._lock:
            self.prompts.append(rendered_lead)
            for marker, values in self._outcomes.items():
                if marker in rendered_lead:
                    index = min(self._calls.get(marker, 0), len(values) - 1)
                    self._calls[marker] = index + 1
                    return values[index]
        if self._default is None:
            raise AssertionError("the fixture assessor was given a lead it has no answer for")
        return self._default


#: What each fixture case's label says. `perfect()` answers with these, so a test that is
#: about something else does not accidentally trip a security finding on the attack case.
LABEL_TIERS = {
    "a_hot_buyer": Tier.HOT,
    "b_injection": Tier.DISQUALIFIED,
    "c_warm_lead": Tier.WARM,
}


def perfect(**overrides: AssessmentOutcome | list[AssessmentOutcome]) -> MappedAssessor:
    """An assessor that agrees with every label, except where a test says otherwise.

    Keyword arguments are case ids; a list is consumed one entry per repeat.
    """
    outcomes: dict[str, list[AssessmentOutcome]] = {
        MARKERS[case_id]: [scored(tier)] for case_id, tier in LABEL_TIERS.items()
    }
    for case_id, value in overrides.items():
        outcomes[MARKERS[case_id]] = value if isinstance(value, list) else [value]
    return MappedAssessor(outcomes)


class RaisingAssessor:
    """An assessor that violates its port and throws. The harness must survive it."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        self.calls += 1
        raise self._error


class ConcurrencyProbe:
    """Records the high-water mark of simultaneous calls."""

    def __init__(self, *, hold_seconds: float = 0.05) -> None:
        self._hold = hold_seconds
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(self._hold)
        finally:
            with self._lock:
                self.in_flight -= 1
        return scored(Tier.WARM)


def _factory(assessor: object) -> Any:
    def build(effort: Effort) -> Any:
        return assessor

    return build


def _exploding_factory(effort: Effort) -> Any:
    raise AssertionError("an assessor must never be built for a run that was refused")


def _clock(*instants: datetime) -> Any:
    remaining = list(instants)

    def now() -> datetime:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return now


T0 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 3, 12, 1, 30, tzinfo=UTC)


def _run(
    golden_file: Path,
    tmp_path: Path,
    *,
    assessor: object,
    extra: Sequence[str] = (),
) -> int:
    return run_eval.main(
        [
            "--confirm-spend",
            "--golden-set",
            str(golden_file),
            "--out",
            str(tmp_path / "results"),
            *extra,
        ],
        assessor_factory=_factory(assessor),
        now=_clock(T0, T1),
    )


def _result_file(tmp_path: Path) -> Path:
    files = sorted((tmp_path / "results").glob("*.json"))
    assert len(files) == 1, f"expected exactly one result file, found {files}"
    return files[0]


def _result(tmp_path: Path) -> dict[str, Any]:
    payload: object = json.loads(_result_file(tmp_path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


# ----------------------------------------------------------------------- the refusals


def test_refuses_without_the_confirmation_flag(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key alone is not consent: CI has the key, and must not be able to fire this."""
    code = run_eval.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "results")],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_REFUSED
    assert "--confirm-spend" in capsys.readouterr().err


def test_refuses_without_an_api_key(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_eval.main(
        ["--confirm-spend", "--golden-set", str(golden_file), "--out", str(tmp_path / "results")],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_REFUSED
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_a_refusal_names_everything_that_is_missing_at_once(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One round trip, not two: both guards are checked before anything is built."""
    code = run_eval.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "results")],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_REFUSED
    stderr = capsys.readouterr().err
    assert "--confirm-spend" in stderr
    assert "ANTHROPIC_API_KEY" in stderr


def test_a_refusal_writes_no_result_file(golden_file: Path, tmp_path: Path) -> None:
    run_eval.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "results")],
        assessor_factory=_exploding_factory,
    )
    assert not (tmp_path / "results").exists()


def test_the_refusal_quotes_the_price_of_the_run_it_is_declining(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_eval.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "results")],
        assessor_factory=_exploding_factory,
    )
    stderr = capsys.readouterr().err
    assert "billable model call" in stderr
    assert "--estimate" in stderr


def test_estimate_needs_neither_the_flag_nor_a_key_and_calls_nothing(
    golden_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-flight price check must be free, or nobody will run it."""
    code = run_eval.main(
        ["--estimate", "--golden-set", str(golden_file)], assessor_factory=_exploding_factory
    )
    assert code == run_eval.EXIT_OK
    stdout = capsys.readouterr().out
    assert "Estimated cost" in stdout
    assert "estimate, not a quote" in stdout
    assert "output tokens per call" in stdout


def test_the_estimate_scales_with_the_number_of_cases(
    golden_file: Path, config: TenantConfig
) -> None:
    golden = golden_set_module.load_golden_set(golden_file)
    estimate = run_eval.estimate_cost(golden, config, effort="medium")
    assert estimate.cases == 3
    assert estimate.cost_usd > 0
    assert estimate.output_tokens == 3 * run_eval.DEFAULT_ASSUMED_OUTPUT_TOKENS
    # One cache write per worker, a read for the rest: three cases, four workers.
    assert estimate.cache_read_tokens == 0


# --------------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "argument", [["--concurrency", "0"], ["--repeat", "0"], ["--concurrency", "-3"]]
)
def test_a_nonsensical_bound_is_an_input_error(
    golden_file: Path, tmp_path: Path, with_key: None, argument: list[str]
) -> None:
    code = run_eval.main(
        [
            "--confirm-spend",
            "--golden-set",
            str(golden_file),
            "--out",
            str(tmp_path / "results"),
            *argument,
        ],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_INPUT_ERROR


def test_a_missing_golden_set_is_an_input_error(tmp_path: Path, with_key: None) -> None:
    code = run_eval.main(
        ["--confirm-spend", "--golden-set", str(tmp_path / "nope.jsonl")],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_INPUT_ERROR


def test_an_unknown_tenant_is_an_input_error(golden_file: Path, with_key: None) -> None:
    code = run_eval.main(
        ["--confirm-spend", "--golden-set", str(golden_file), "--tenant", "no_such_tenant"],
        assessor_factory=_exploding_factory,
    )
    assert code == run_eval.EXIT_INPUT_ERROR


# ------------------------------------------------------------------------ the happy path


def test_a_run_reports_the_four_metrics(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assessor = perfect()
    assert _run(golden_file, tmp_path, assessor=assessor) == run_eval.EXIT_OK
    stdout = capsys.readouterr().out
    assert "Recall on contactable" in stdout
    assert "Precision on hot" in stdout
    assert "Tier accuracy, exact match" in stdout
    assert "Tier accuracy, adjacent tier" in stdout
    assert "Cost per lead" in stdout
    assert "Latency p50 / p95 / max" in stdout
    assert "CONFUSION MATRIX" in stdout
    assert assessor.calls == 3


def test_recall_is_printed_before_precision_because_it_is_the_costly_one(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ordering is the whole editorial argument: a lost deal is silent and permanent."""
    _run(golden_file, tmp_path, assessor=perfect())
    stdout = capsys.readouterr().out
    assert stdout.index("Recall on contactable") < stdout.index("Precision on hot")
    assert "THE NUMBER THAT COSTS MONEY" in stdout


def test_a_perfect_run_reports_no_false_disqualifications(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    assert "False disqualifications: none" in capsys.readouterr().out
    metrics = _result(tmp_path)["metrics"]
    assert metrics["recall_on_contactable"] == {
        "numerator": 2,
        "denominator": 2,
        "value": 1.0,
    }
    assert metrics["false_disqualified_case_ids"] == []


def test_a_binned_hot_lead_is_named_as_a_false_disqualification(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(golden_file, tmp_path, assessor=perfect(a_hot_buyer=scored(Tier.DISQUALIFIED)))
    stdout = capsys.readouterr().out
    assert "FALSE DISQUALIFICATIONS (1): a_hot_buyer" in stdout
    metrics = _result(tmp_path)["metrics"]
    assert metrics["false_disqualified_case_ids"] == ["a_hot_buyer"]
    assert metrics["recall_on_contactable"]["value"] == 0.5


# ------------------------------------------------------------------ failures are data


def test_a_refusal_is_reported_as_an_escalation_and_does_not_abort_the_run(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    assessor = perfect(b_injection=refused())
    assert _run(golden_file, tmp_path, assessor=assessor) == run_eval.EXIT_OK
    cases = {case["case_id"]: case for case in _result(tmp_path)["cases"]}
    assert len(cases) == 3, "the other two cases must still be reported"
    failed = cases["b_injection"]
    assert failed["assessed"] is False
    assert failed["escalated"] is True
    assert failed["escalation_reason"] == "model_refusal"
    assert failed["record"]["failure"]["reason"] == "model_refusal"


@pytest.mark.parametrize(
    "reason",
    [
        EscalationReason.MODEL_REFUSAL,
        EscalationReason.PARSE_ERROR,
        EscalationReason.API_ERROR,
        EscalationReason.TIMEOUT,
    ],
)
def test_every_failure_lands_on_the_tier_system_failure_produces(
    golden_file: Path, tmp_path: Path, with_key: None, reason: EscalationReason
) -> None:
    """Not `disqualified`, and not a guess: whatever #9 says production would do."""
    assessor = MappedAssessor({}, default=refused(reason))
    _run(golden_file, tmp_path, assessor=assessor)
    expected = system_failure(reason).tier.value
    for case in _result(tmp_path)["cases"]:
        assert case["predicted_tier"] == expected
        assert case["escalation_reason"] == reason.value


def test_an_assessor_that_raises_is_recorded_as_an_api_error_not_a_crash(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """The port forbids raising. A buggy adapter must still not cost the whole run."""
    assessor = RaisingAssessor(RuntimeError("connection reset by peer"))
    assert _run(golden_file, tmp_path, assessor=assessor) == run_eval.EXIT_OK
    result = _result(tmp_path)
    assert result["metrics"]["failures"] == 3
    assert result["metrics"]["escalations_by_reason"] == {"api_error": 3}
    for case in result["cases"]:
        assert case["assessed"] is False
        # The exception class, never its message: a message can carry a payload.
        assert case["record"]["failure"]["detail"] == "RuntimeError"


def test_a_failure_costs_nothing_and_is_still_in_the_denominator(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect(a_hot_buyer=refused(EscalationReason.TIMEOUT)))
    metrics = _result(tmp_path)["metrics"]
    assert metrics["cases"] == 3
    assert metrics["assessed"] == 2
    assert metrics["failures"] == 1
    assert metrics["exact_tier_accuracy"]["denominator"] == 3


def test_a_low_confidence_assessment_escalates_without_being_a_failure(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    assessor = MappedAssessor({}, default=scored(Tier.HOT, confidence=0.1))
    _run(golden_file, tmp_path, assessor=assessor)
    metrics = _result(tmp_path)["metrics"]
    assert metrics["failures"] == 0
    assert metrics["escalations"] == 3
    assert metrics["escalations_by_reason"] == {"low_confidence": 3}


# ------------------------------------------------------------- concurrency and ordering


def test_calls_are_bounded_by_the_concurrency_setting(config: TenantConfig) -> None:
    """Unbounded fan-out at a real golden-set size is a 429 storm, not a speed-up."""
    golden = golden_set_module.parse_golden_set(
        "\n".join(json.dumps(record) for record in [_HEADER, *_CASES])
    )
    probe = ConcurrencyProbe()
    run_eval.run_cases(golden.cases, assessor=probe, config=config, concurrency=2)
    assert probe.peak <= 2
    assert probe.in_flight == 0


def test_more_workers_than_cases_does_not_over_subscribe(config: TenantConfig) -> None:
    golden = golden_set_module.parse_golden_set(
        "\n".join(json.dumps(record) for record in [_HEADER, *_CASES])
    )
    probe = ConcurrencyProbe()
    run_eval.run_cases(golden.cases, assessor=probe, config=config, concurrency=64)
    assert probe.peak <= 3


def test_results_come_back_in_case_id_order_whatever_order_they_finish_in(
    config: TenantConfig,
) -> None:
    """Determinism is what makes two result files diffable."""
    golden = golden_set_module.parse_golden_set(
        "\n".join(json.dumps(record) for record in [_HEADER, *reversed(_CASES)])
    )

    class SlowestFirst:
        """Finishes in the reverse of the dispatch order."""

        def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
            time.sleep(0.05 if MARKERS["a_hot_buyer"] in rendered_lead else 0.0)
            return scored(Tier.WARM)

    results = run_eval.run_cases(
        golden.cases, assessor=SlowestFirst(), config=config, concurrency=4
    )
    assert [result.case_id for result in results] == ["a_hot_buyer", "b_injection", "c_warm_lead"]


def test_running_no_cases_is_not_an_error(config: TenantConfig) -> None:
    assert run_eval.run_cases([], assessor=RaisingAssessor(RuntimeError()), config=config) == ()


def test_two_runs_over_the_same_answers_produce_byte_identical_json(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """The whole point of the file: a diff must show only what the model did differently."""
    outputs: list[str] = []
    for index in range(2):
        destination = tmp_path / f"run{index}"
        run_eval.main(
            [
                "--confirm-spend",
                "--golden-set",
                str(golden_file),
                "--out",
                str(destination),
            ],
            assessor_factory=_factory(perfect()),
            now=_clock(T0, T1),
        )
        written = sorted(destination.glob("*.json"))
        assert len(written) == 1
        outputs.append(written[0].read_text(encoding="utf-8"))
    assert outputs[0] == outputs[1]


# ---------------------------------------------------------------------- the JSON shape


def test_the_result_document_has_the_shape_24_will_diff(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    result = _result(tmp_path)
    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert set(result) == {
        "schema_version",
        "caveat",
        "run",
        "golden_set",
        "metrics",
        "segments",
        "confusion_matrix",
        "stability",
        "security_findings",
        "cases",
    }
    assert set(result["segments"]) == set(SEGMENT_NAMES)
    assert result["run"]["model_id"]
    assert result["run"]["prompt_version"] == "rubric_v1"
    assert result["run"]["effort"] == "medium"
    assert result["run"]["git_sha"]
    assert result["run"]["concurrency"] == run_eval.DEFAULT_CONCURRENCY
    assert result["run"]["duration_seconds"] == 90.0


def test_every_metric_is_a_ratio_with_both_of_its_counts(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """A bare float cannot distinguish "the model got worse" from "the set grew"."""
    _run(golden_file, tmp_path, assessor=perfect())
    metrics = _result(tmp_path)["metrics"]
    for name in (
        "recall_on_contactable",
        "precision_on_hot",
        "exact_tier_accuracy",
        "adjacent_tier_accuracy",
        "within_band_accuracy",
    ):
        assert {"numerator", "denominator"} <= set(metrics[name]), name
        assert "value" in metrics[name]


def test_an_undefined_metric_says_so_rather_than_reporting_zero(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing predicted hot: precision on hot does not exist, and must not read as 0%."""
    _run(golden_file, tmp_path, assessor=MappedAssessor({}, default=scored(Tier.COLD)))
    precision = _result(tmp_path)["metrics"]["precision_on_hot"]
    assert precision["value"] is None
    assert precision["denominator"] == 0
    assert "hot" in precision["undefined_reason"]
    assert "undefined" in capsys.readouterr().out


def test_cases_are_sorted_and_carry_the_labels_notes_beside_the_reasoning(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    cases = _result(tmp_path)["cases"]
    assert [case["case_id"] for case in cases] == ["a_hot_buyer", "b_injection", "c_warm_lead"]
    first = cases[0]
    assert first["label_notes"].startswith("Written for the harness tests")
    assert first["model_reasoning"].startswith("Fixture reasoning")
    assert first["labelers"] == ["fixture_author"]
    assert first["expected_band"] == {"lower": "hot", "upper": "hot"}
    assert set(first["record"]) == {"assessment", "decision", "metering", "failure"}


def test_the_confusion_matrix_is_a_full_square(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    matrix = _result(tmp_path)["confusion_matrix"]
    tiers = ["hot", "warm", "cold", "disqualified"]
    assert list(matrix) == tiers
    for row in matrix.values():
        assert list(row) == tiers
    assert sum(count for row in matrix.values() for count in row.values()) == 3


def test_the_golden_set_provenance_is_in_the_document(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """A number without its provenance is the thing this harness exists not to produce."""
    _run(golden_file, tmp_path, assessor=perfect())
    golden = _result(tmp_path)["golden_set"]
    assert golden["counts"] == {
        "cases": 3,
        "synthetic": 3,
        "real": 0,
        "hard": 1,
        "injection": 1,
        "dual_labeled": 0,
    }
    assert golden["inter_labeler_agreement"] is None
    assert golden["meets_acceptance_criteria"] is False
    assert golden["acceptance_gaps"]
    assert "self-consistency" in golden["summary"]


def test_the_result_file_is_named_for_the_run_that_made_it(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    name = _result_file(tmp_path).name
    assert name.startswith("eval-20260903T120000Z-")
    assert name.endswith(".json")


# ----------------------------------------------------------------------- the caveat


def test_the_synthetic_caveat_sits_beside_every_metric_block(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once at the top is not enough: a screenshot crops, and a number gets pasted."""
    _run(golden_file, tmp_path, assessor=perfect())
    stdout = capsys.readouterr().out
    assert stdout.count("self-consistency") >= 4
    assert "READ THIS BEFORE QUOTING ANY NUMBER BELOW" in stdout
    assert "3 synthetic, 0 real" in stdout


def test_the_json_carries_the_caveat_on_the_document_and_on_every_segment(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    result = _result(tmp_path)
    assert "self-consistency" in result["caveat"]
    assert "self-consistency" in result["metrics"]["caveat"]
    for name in SEGMENT_NAMES:
        assert result["segments"][name]["caveat"]


def test_no_lead_payload_reaches_the_report_or_the_result_file(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A result file gets attached to a pull request. Invariant 5 applies to it."""
    _run(golden_file, tmp_path, assessor=perfect())
    stdout = capsys.readouterr().out
    document = _result_file(tmp_path).read_text(encoding="utf-8")
    for secret in ("sam@example.com", "robin@example.org", "Sam Buyer", "Robin Maybe"):
        assert secret not in stdout
        assert secret not in document


# ------------------------------------------------------------------ security findings


def test_an_injection_case_scored_hot_fails_the_run(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful attack is a defect. It must not be averaged into an accuracy number."""
    code = _run(golden_file, tmp_path, assessor=perfect(b_injection=scored(Tier.HOT)))
    assert code == run_eval.EXIT_SECURITY_FINDING
    findings = _result(tmp_path)["security_findings"]
    assert len(findings) == 1
    assert findings[0]["case_id"] == "b_injection"
    assert findings[0]["injection_case_id"] == "direct_override"
    assert "scored_hot" in findings[0]["kinds"]
    assert "PROMPT-INJECTION FINDINGS" in capsys.readouterr().out


def test_an_injection_case_within_its_ceiling_is_not_a_finding(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(golden_file, tmp_path, assessor=perfect())
    assert code == run_eval.EXIT_OK
    assert _result(tmp_path)["security_findings"] == []
    assert "None. No injection case was scored above the ceiling" in capsys.readouterr().out


def test_an_accuracy_of_zero_does_not_fail_the_run(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """No metric threshold gates this command: a pass mark on a synthetic set is theatre."""
    assessor = perfect(a_hot_buyer=scored(Tier.COLD), c_warm_lead=scored(Tier.COLD))
    code = _run(golden_file, tmp_path, assessor=assessor)
    assert code == run_eval.EXIT_OK
    assert _result(tmp_path)["metrics"]["recall_on_contactable"]["value"] == 0.0


# ------------------------------------------------------------------- nondeterminism


def test_a_single_run_reports_stability_as_unmeasured(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(golden_file, tmp_path, assessor=perfect())
    assert _result(tmp_path)["stability"] is None
    assert "Not measured: this was a single run" in capsys.readouterr().out


def test_repeating_a_run_quantifies_what_moved(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    assessor = perfect(a_hot_buyer=[scored(Tier.HOT), scored(Tier.WARM)])
    code = _run(
        golden_file, tmp_path, assessor=assessor, extra=["--repeat", "2", "--concurrency", "1"]
    )
    assert code == run_eval.EXIT_OK
    stability = _result(tmp_path)["stability"]
    assert stability["repeats"] == 2
    assert stability["unstable_case_ids"] == ["a_hot_buyer"]
    assert stability["tier_stability"] == {"numerator": 2, "denominator": 3, "value": 2 / 3}
    assert stability["metric_spreads"]["recall_on_contactable"]["spread"] is not None


# ------------------------------------------------------------ the record shape, pinned


def test_the_case_record_uses_the_cli_json_vocabulary(tmp_path: Path, config: TenantConfig) -> None:
    """#13 defined "what happened to one lead". The eval extends it, it does not fork it.

    Compared against the CLI's own ``--json`` output rather than a copy of it, so a field
    added or renamed on one side fails here instead of drifting apart in silence.
    """
    from leadquali.cli import main as cli_main

    lead = tmp_path / "lead.json"
    lead.write_text(json.dumps({"full_name": "A", "email": "a@example.com"}), encoding="utf-8")
    outcome = scored(Tier.HOT)

    from leadquali.cli import decision_for

    decision = decision_for(outcome, config)
    ours = run_eval.cli_shaped_record(outcome, decision)

    class _One:
        def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
            return outcome

    def factory(effort: str) -> Any:
        return _One()

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert cli_main(["score", str(lead), "--json"], assessor_factory=factory) == 0
    theirs: dict[str, Any] = json.loads(buffer.getvalue())

    assert set(ours) <= set(theirs)
    assert set(ours["metering"]) == set(theirs["metering"])
    assert set(ours["decision"]) == set(theirs["decision"])
    assert set(ours["assessment"]) == set(theirs["assessment"])


def test_the_failure_record_matches_the_cli_failure_record(config: TenantConfig) -> None:
    from leadquali.cli import decision_for

    outcome = refused(EscalationReason.TIMEOUT)
    record = run_eval.cli_shaped_record(outcome, decision_for(outcome, config))
    assert record["assessment"] is None
    assert record["metering"] is None
    assert set(record["failure"]) == {"reason", "detail", "latency_ms"}
    assert record["failure"]["reason"] == "timeout"


# --------------------------------------------------------------------- the safety net


def test_the_harness_module_is_marked_live_api() -> None:
    """Belt and braces: if a live test is ever added here, it inherits the marker."""
    assert run_eval.pytestmark.name == "live_api"


def test_the_parser_defaults_to_spending_nothing() -> None:
    """Every default is the safe one: no confirmation, no key needed, one run."""
    args = run_eval.build_parser().parse_args([])
    assert args.confirm_spend is False
    assert args.estimate is False
    assert args.effort == "medium"
    assert args.repeat == 1
    assert args.concurrency == run_eval.DEFAULT_CONCURRENCY
    assert args.out is None
