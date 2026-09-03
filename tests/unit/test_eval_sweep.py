"""The effort sweep, driven end to end offline with assessors scripted per effort level.

The sweep is #23's harness run three times and the three result files compared. That is
the design, and these tests hold it to it: the assessor is built once per level through
the ``assessor_factory`` seam, each level writes its own result file, and the comparison
is computed from those files rather than from anything held in memory — so the sweep can
only ever report what a diff of the saved artifacts would also report.

The expensive mistakes, in order:

1. **Spending by accident, three times over.** A sweep is three runs. Both of #23's guards
   are asserted again at sweep scale, and the estimate is asserted to be the multiple.
2. **Manufacturing a winner.** Three cases (or fifteen) cannot separate three effort
   levels, and the sweep must say so rather than ranking noise.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from leadquali.app.assessment_result import (
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
    Effort,
)
from leadquali.app.ports import LeadAssessorPort
from leadquali.config import get_settings
from leadquali.domain.models import Tier
from tests.evals import compare, sweep

# The fixture golden set and the scripted-assessor machinery are #23's, and are imported
# rather than copied: a sweep that ran against a different fixture set from the harness
# tests would be testing a pipeline nobody has.
from tests.unit.test_run_eval import (
    _CASES,
    _HEADER,
    LABEL_TIERS,
    MARKERS,
    MappedAssessor,
    _assessment,
)

API_KEY = "sk-ant-not-a-real-key"

#: What each level costs and how long it takes in the fixtures. Spread wide on purpose:
#: cost and latency are the only columns a fifteen-case sweep can separate, and the tests
#: assert that they survive into the comparison while accuracy does not.
COST_BY_EFFORT: Mapping[Effort, str] = {"low": "0.0050", "medium": "0.0200", "high": "0.0600"}
LATENCY_BY_EFFORT: Mapping[Effort, int] = {"low": 2000, "medium": 4500, "high": 9000}


def _outcome(tier: Tier, *, effort: Effort) -> AssessmentOutcome:
    return AssessmentSucceeded(
        assessment=_assessment(tier),
        metering=CallMetering(
            model_id="claude-opus-5",
            prompt_version="rubric_v1",
            effort=effort,
            input_tokens=2000,
            output_tokens=900,
            cache_read_tokens=1500,
            cache_creation_tokens=0,
            cost_usd=Decimal(COST_BY_EFFORT[effort]),
            latency_ms=LATENCY_BY_EFFORT[effort],
        ),
    )


class ScriptedFactory:
    """Builds one assessor per effort level, and records which levels were asked for."""

    def __init__(self, script: Mapping[str, Mapping[str, Tier]] | None = None) -> None:
        self._script = script or {}
        self.efforts: list[str] = []

    def __call__(self, effort: Effort) -> LeadAssessorPort:
        self.efforts.append(effort)
        per_case = {**LABEL_TIERS, **self._script.get(effort, {})}
        return MappedAssessor(
            {
                MARKERS[case_id]: [_outcome(tier, effort=effort)]
                for case_id, tier in per_case.items()
            }
        )


def _exploding_factory(effort: Effort) -> LeadAssessorPort:
    raise AssertionError("no assessor may be built for a sweep that was never authorised")


@pytest.fixture
def golden_file(tmp_path: Path) -> Path:
    path = tmp_path / "fixture_leads.jsonl"
    path.write_text(
        "\n".join(json.dumps(record) for record in [_HEADER, *_CASES]) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
    get_settings.cache_clear()


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    get_settings.cache_clear()


def _run(
    golden_file: Path,
    tmp_path: Path,
    *,
    factory: Any,
    extra: tuple[str, ...] = (),
) -> int:
    return sweep.main(
        [
            "--confirm-spend",
            "--golden-set",
            str(golden_file),
            "--out",
            str(tmp_path / "sweep"),
            *extra,
        ],
        assessor_factory=factory,
    )


# --------------------------------------------------------------------- the refusals


def test_the_sweep_refuses_without_the_confirmation_flag(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sweep.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "sweep")],
        assessor_factory=_exploding_factory,
    )
    assert code == sweep.EXIT_REFUSED
    assert "--confirm-spend" in capsys.readouterr().err


def test_the_sweep_refuses_without_an_api_key(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sweep.main(
        ["--confirm-spend", "--golden-set", str(golden_file), "--out", str(tmp_path / "sweep")],
        assessor_factory=_exploding_factory,
    )
    assert code == sweep.EXIT_REFUSED
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_refusal_quotes_the_whole_sweep_price_not_one_run(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sweep is three runs, and the number the person sees must be the one they pay."""
    code = sweep.main(
        ["--golden-set", str(golden_file), "--out", str(tmp_path / "sweep")],
        assessor_factory=_exploding_factory,
    )
    assert code == sweep.EXIT_REFUSED
    stderr = capsys.readouterr().err
    assert "3 effort level" in stderr


# --------------------------------------------------------------------- the estimate


def test_the_estimate_prints_a_line_per_level_and_a_total(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sweep.main(
        ["--estimate", "--golden-set", str(golden_file), "--out", str(tmp_path / "sweep")],
        assessor_factory=_exploding_factory,
    )
    assert code == sweep.EXIT_OK
    out = capsys.readouterr().out
    for level in ("low", "medium", "high"):
        assert level in out
    assert "total" in out.lower()


def test_the_estimate_is_the_single_run_estimate_times_the_levels(
    golden_file: Path, tmp_path: Path
) -> None:
    estimate = sweep.estimate_sweep(
        *sweep.load_inputs(golden_file, "default", None),
        efforts=("low", "medium", "high"),
        concurrency=4,
        repeat=1,
        output_tokens_per_case=1200,
    )
    assert len(estimate.per_level) == 3
    assert estimate.total_cost_usd == sum(
        (level.cost_usd for level in estimate.per_level.values()), Decimal(0)
    )


def test_the_estimate_multiplies_by_repeat(golden_file: Path) -> None:
    inputs = sweep.load_inputs(golden_file, "default", None)
    once = sweep.estimate_sweep(
        *inputs, efforts=("low", "medium"), concurrency=4, repeat=1, output_tokens_per_case=1200
    )
    twice = sweep.estimate_sweep(
        *inputs, efforts=("low", "medium"), concurrency=4, repeat=2, output_tokens_per_case=1200
    )
    assert twice.total_cost_usd == once.total_cost_usd * 2


def test_the_estimate_spends_nothing_and_needs_no_key(
    golden_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sweep.main(
        ["--estimate", "--golden-set", str(golden_file), "--out", str(tmp_path / "sweep")],
        assessor_factory=_exploding_factory,
    )
    assert code == sweep.EXIT_OK
    assert "ANTHROPIC_API_KEY" not in capsys.readouterr().err


# ----------------------------------------------------------------- input validation


def test_an_unknown_effort_level_is_an_input_error(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(golden_file, tmp_path, factory=_exploding_factory, extra=("--effort", "turbo"))
    assert code == sweep.EXIT_INPUT_ERROR
    assert "turbo" in capsys.readouterr().err


def test_a_baseline_outside_the_swept_levels_is_an_input_error(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        golden_file,
        tmp_path,
        factory=_exploding_factory,
        extra=("--effort", "low", "--effort", "high", "--baseline", "medium"),
    )
    assert code == sweep.EXIT_INPUT_ERROR
    assert "medium" in capsys.readouterr().err


def test_a_repeated_effort_level_is_an_input_error(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        golden_file,
        tmp_path,
        factory=_exploding_factory,
        extra=("--effort", "low", "--effort", "low"),
    )
    assert code == sweep.EXIT_INPUT_ERROR
    assert "low" in capsys.readouterr().err


# ------------------------------------------------------------------------ the sweep


def test_the_sweep_builds_one_assessor_per_level_through_the_injected_factory(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    factory = ScriptedFactory()
    assert _run(golden_file, tmp_path, factory=factory) == sweep.EXIT_OK
    assert factory.efforts == ["low", "medium", "high"]


def test_each_level_writes_its_own_result_file(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """One artifact per level, so the sweep and a later diff read the same evidence."""
    assert _run(golden_file, tmp_path, factory=ScriptedFactory()) == sweep.EXIT_OK
    for level in ("low", "medium", "high"):
        files = list((tmp_path / "sweep" / level).glob("eval-*.json"))
        assert len(files) == 1, f"expected one result file for {level}, found {files}"


def test_the_sweep_writes_one_comparison_document(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    assert _run(golden_file, tmp_path, factory=ScriptedFactory()) == sweep.EXIT_OK
    files = list((tmp_path / "sweep").glob("sweep-*.json"))
    assert len(files) == 1
    payload: object = json.loads(files[0].read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert [level["effort"] for level in payload["levels"]] == ["low", "medium", "high"]
    assert payload["result_files"]["low"].endswith(".json")


def test_the_comparison_carries_cost_and_latency_side_by_side(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(golden_file, tmp_path, factory=ScriptedFactory()) == sweep.EXIT_OK
    out = capsys.readouterr().out
    assert "cost per lead" in out.lower()
    assert "p95" in out.lower()
    assert "0.0050" in out.replace("$", "")


def test_a_sweep_over_a_tiny_set_reports_that_it_cannot_distinguish_the_levels(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three cases and a one-case difference is a coin flip, and the report says so."""
    factory = ScriptedFactory({"low": {"a_hot_buyer": Tier.WARM}})
    assert _run(golden_file, tmp_path, factory=factory) == sweep.EXIT_OK
    assert compare.VERDICT_INDISTINGUISHABLE in capsys.readouterr().out


def test_the_sweep_never_recommends_a_level_off_a_synthetic_set(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(golden_file, tmp_path, factory=ScriptedFactory()) == sweep.EXIT_OK
    out = capsys.readouterr().out
    assert compare.NO_RECOMMENDATION in out


def test_repeat_is_passed_through_and_turns_the_noise_floor_into_a_measured_one(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(golden_file, tmp_path, factory=ScriptedFactory(), extra=("--repeat", "2")) == (
        sweep.EXIT_OK
    )
    # Collapse the report's line wrapping before matching: the sentence is the assertion,
    # not where the renderer happened to break it.
    normalised = " ".join(capsys.readouterr().out.split())
    assert "measured over 2 repeats" in normalised
    assert "spread not measured" not in normalised


def test_a_security_finding_at_any_level_makes_the_whole_sweep_non_green(
    golden_file: Path, tmp_path: Path, with_key: None
) -> None:
    """An injection case tiered hot is a defect at any effort level."""
    factory = ScriptedFactory({"low": {"b_injection": Tier.HOT}})
    assert _run(golden_file, tmp_path, factory=factory) == sweep.EXIT_SECURITY_FINDING


def test_a_single_level_sweep_still_produces_a_report(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = ScriptedFactory()
    code = _run(golden_file, tmp_path, factory=factory, extra=("--effort", "medium"))
    assert code == sweep.EXIT_OK
    assert factory.efforts == ["medium"]
    assert compare.VERDICT_INDISTINGUISHABLE in capsys.readouterr().out


def test_the_per_level_reports_are_suppressed_unless_asked_for(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sweep's output is the comparison; three full reports would bury it."""
    _run(golden_file, tmp_path, factory=ScriptedFactory())
    assert "CONFUSION MATRIX" not in capsys.readouterr().out
    _run(golden_file, tmp_path, factory=ScriptedFactory(), extra=("--per-level-report",))
    assert "CONFUSION MATRIX" in capsys.readouterr().out


def test_the_sweep_report_names_the_golden_set_it_ran_against(
    golden_file: Path, tmp_path: Path, with_key: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(golden_file, tmp_path, factory=ScriptedFactory())
    assert golden_file.name in capsys.readouterr().out
