"""``python -m tests.evals.run_eval --confirm-spend`` — the eval harness (plan §7).

Runs every case in the golden set through the real pipeline — #12's renderer, #11's
adapter, #9's ``decide`` — and reports the four numbers a prompt change is judged by, a
confusion matrix, a per-case breakdown, and a JSON result file #24 diffs between runs.

**This command spends money, so it refuses to start by accident.** Two independent things
must be true: ``--confirm-spend`` on the command line, and an ``ANTHROPIC_API_KEY`` in the
environment. Either one alone exits :data:`EXIT_REFUSED` without making a single call. The
module is also marked ``live_api``, so ``pytest`` never collects it into the default suite
(``tests/conftest.py`` skips that marker unless a run selects it), and the CI workflow that
invokes it is ``workflow_dispatch`` only. Three separate guards for one mistake, because
the mistake is a bill and a rate-limit incident rather than a red build.

**Rate-limit posture.** Calls run on a bounded thread pool — :data:`DEFAULT_CONCURRENCY` by
default, ``--concurrency`` to change it. Bounded rather than unbounded because a 100-case
sweep fired at once is a 429 storm that costs more wall clock than it saves; concurrent
rather than serial because a 100-case sweep at ~8s a lead is a quarter of an hour of
staring. There is deliberately **no retry loop in this module**: the SDK already retries
408/409/429/5xx with exponential backoff (see
:func:`~leadquali.adapters.llm_anthropic.build_anthropic_client`), and a second loop on top
would multiply the bill and re-try what the SDK decided was hopeless. If a run hits
sustained 429s the answer is a lower ``--concurrency``, not more retries.

**A failure is a data point, not a crash.** A refusal, a timeout, a parse error or an
adapter that raises when its port says it must not — each one becomes a
``system_failure`` decision, which is exactly what production does with it (invariant 3).
The case is reported as an escalation, counted in the denominators, and the run continues.
One unqualifiable lead must not cost you the other fourteen results you have already paid
for.

**Deterministic output.** Cases are dispatched in ``case_id`` order and every collection in
the report and the JSON is sorted by ``case_id``, so two runs differ only where the model
did.

**Nothing here asserts on model prose.** ``reasoning`` is printed beside the label's notes
because reading them together is how a human decides whether the label or the rubric is
wrong; it is never compared, matched or scored.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from shutil import which
from typing import Any, Final

import pytest

from leadquali.adapters.llm_anthropic import (
    CLAUDE_OPUS_5_PRICES,
    MODEL_ID,
    AnthropicLeadAssessor,
    TokenPrices,
    build_anthropic_client,
    token_cost_usd,
)
from leadquali.adapters.tenant_config_json import (
    DEFAULT_TENANT_ID,
    JsonFileTenantConfigLoader,
    default_tenants_dir,
)
from leadquali.app.assessment_result import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    AssessmentOutcome,
    AssessmentSucceeded,
    Effort,
)
from leadquali.app.ports import LeadAssessorPort
from leadquali.cli import decision_for
from leadquali.config import get_settings
from leadquali.domain.models import EscalationReason, RoutingDecision
from leadquali.domain.routing import system_failure
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError
from leadquali.observability import configure_logging
from leadquali.prompts.lead import render_lead_detailed
from leadquali.prompts.rubric import PROMPT_VERSION, build_system_blocks
from tests.evals.golden_set import GOLDEN_LEADS_PATH, GoldenCase, GoldenSet, load_golden_set
from tests.evals.metrics import CaseResult
from tests.evals.report import EvalRun, RunMetadata, dumps, render_text_report

#: Marks the whole module billable. Nothing here is collected today - the file is not
#: named ``test_*.py`` - so this is the guard for the day somebody adds a live test beside
#: the harness or renames the file: ``tests/conftest.py`` skips ``live_api`` unless a run
#: selects markers explicitly, and the default suite therefore still cannot spend money.
pytestmark = pytest.mark.live_api

#: The run finished and produced a report.
EXIT_OK: Final[int] = 0

#: A usage or input problem: an unreadable golden set, an unknown tenant, a bad effort.
EXIT_INPUT_ERROR: Final[int] = 2

#: The run was never authorised — no ``--confirm-spend``, or no API key. Distinct from
#: :data:`EXIT_INPUT_ERROR` so a wrapper can tell "you did not mean this" from "this is
#: broken", and so a CI job that forgot the secret fails with a legible reason.
EXIT_REFUSED: Final[int] = 3

#: The run completed and an injection case behaved badly. Non-zero on purpose: a run that
#: reports an attack payload tiered hot must not be green in the Actions tab. Note that no
#: *accuracy* threshold fails the run — a pass mark computed against a synthetic set would
#: be a gate on a number that measures self-consistency.
EXIT_SECURITY_FINDING: Final[int] = 4

#: In-flight model calls. Four is a deliberate compromise: fast enough that the seed set
#: finishes in well under a minute, small enough to stay under a fresh account's request
#: rate limit without the SDK ever having to back off.
DEFAULT_CONCURRENCY: Final[int] = 4

#: Where result files are written when ``--out`` is not given. Git-ignored: a result file
#: is evidence attached to a pull request, not a source file.
DEFAULT_RESULTS_DIR: Final[Path] = Path(__file__).resolve().parent / "results"

#: Output tokens assumed per call by ``--estimate``. A guess, and labeled as one wherever
#: it is printed: real output length depends on the effort level, since thinking tokens are
#: billed as output. Override with ``--assumed-output-tokens`` once a real run has told you
#: the true figure for the effort level you care about.
DEFAULT_ASSUMED_OUTPUT_TOKENS: Final[int] = 1200

#: Characters per token used by ``--estimate``. English prose runs about four; this is a
#: sizing heuristic for a pre-flight number, never a billing figure.
CHARS_PER_TOKEN: Final[float] = 4.0

AssessorFactory = Callable[[Effort], LeadAssessorPort]


# --------------------------------------------------------------------------- estimating


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A pre-flight guess at what a run will cost, with its assumptions attached.

    Exists so nobody has to discover the price of a sweep by paying it. The input side is
    close — it is measured off the text that will actually be sent — and the output side is
    an assumption, which is why the assumptions travel with the number.
    """

    cases: int
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: Decimal
    assumptions: tuple[str, ...]

    def render(self) -> str:
        """The text printed by ``--estimate``."""
        lines = [
            f"Estimated cost of one run over {self.cases} cases: ${self.cost_usd:.4f} "
            f"(about ${self.cost_usd / max(self.cases, 1):.4f} per lead).",
            f"  input {self.input_tokens} tok, cache write {self.cache_write_tokens} tok, "
            f"cache read {self.cache_read_tokens} tok, output {self.output_tokens} tok",
            "This is an estimate, not a quote. It assumes:",
        ]
        lines.extend(f"  - {assumption}" for assumption in self.assumptions)
        return "\n".join(lines)


def estimate_cost(
    golden: GoldenSet,
    config: TenantConfig,
    *,
    effort: Effort,
    concurrency: int = DEFAULT_CONCURRENCY,
    output_tokens_per_case: int = DEFAULT_ASSUMED_OUTPUT_TOKENS,
    prices: TokenPrices = CLAUDE_OPUS_5_PRICES,
) -> CostEstimate:
    """What one run over ``golden`` will cost, before spending anything.

    The input side is measured from the prompts that will really be sent: #10's two system
    blocks and #12's rendered lead for each case. The output side cannot be measured
    without making the call, so it is an explicit assumption. Caching is modelled as one
    cache write per concurrent worker — the first ``concurrency`` calls start before any of
    them has populated the prefix — and a cache read for everything after that.
    """
    rubric, tenant = build_system_blocks(config)
    rubric_tokens = _tokens(rubric.text)
    tenant_tokens = _tokens(tenant.text)
    lead_tokens = sum(
        _tokens(render_lead_detailed(case.to_submission()).text) for case in golden.cases
    )
    cases = len(golden.cases)
    writes = min(concurrency, cases)
    reads = max(cases - writes, 0)
    output_tokens = cases * output_tokens_per_case
    return CostEstimate(
        cases=cases,
        input_tokens=cases * tenant_tokens + lead_tokens,
        cache_write_tokens=writes * rubric_tokens,
        cache_read_tokens=reads * rubric_tokens,
        output_tokens=output_tokens,
        cost_usd=token_cost_usd(
            input_tokens=cases * tenant_tokens + lead_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=reads * rubric_tokens,
            cache_creation_tokens=writes * rubric_tokens,
            prices=prices,
        ),
        assumptions=(
            f"{output_tokens_per_case} output tokens per call, thinking included - a guess, "
            f"and the largest source of error at effort={effort}",
            f"{CHARS_PER_TOKEN:.0f} characters per token for the prompts, measured off the "
            "text that will actually be sent",
            f"{writes} cache write(s) and {reads} cache read(s), i.e. one write per "
            f"concurrent worker at --concurrency {concurrency}",
            "the published rate card in adapters/llm_anthropic.py, which is a documented "
            "snapshot and not a feed; the invoice wins",
        ),
    )


def _tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


# ------------------------------------------------------------------------- running cases


def assess_case(
    case: GoldenCase, *, assessor: LeadAssessorPort, config: TenantConfig
) -> CaseResult:
    """Run one golden case through the pipeline and score it against its label.

    Never raises for anything the model or the network can do. The port says an assessor
    returns an ``AssessmentFailed`` rather than raising, but a buggy adapter can still
    throw, and one exception must not cost the run the results it has already paid for —
    so an escaped exception is converted into the same ``API_ERROR`` failure the adapter
    should have returned, and routed through ``system_failure`` like any other.
    """
    rendered = render_lead_detailed(case.to_submission())
    try:
        outcome: AssessmentOutcome = assessor.assess(config=config, rendered_lead=rendered.text)
    except Exception as error:  # a raise here is a bug, and must still not lose the run
        return _failed_result(case, EscalationReason.API_ERROR, type(error).__name__)
    # #13's own policy call, imported rather than reimplemented: the tier an eval reports
    # must be the tier production produces, including the `warm` a `system_failure` gives.
    decision = decision_for(outcome, config)
    return build_case_result(case, outcome, decision)


def build_case_result(
    case: GoldenCase, outcome: AssessmentOutcome, decision: RoutingDecision
) -> CaseResult:
    """Pair what the pipeline decided with what the human said, for one case."""
    metering = outcome.metering
    assessment = outcome.assessment if isinstance(outcome, AssessmentSucceeded) else None
    return CaseResult(
        case_id=case.case_id,
        provenance=case.provenance,
        expected_tier=case.expected_tier,
        lower_bound=case.lower_bound,
        upper_bound=case.upper_bound,
        predicted_tier=decision.tier,
        assessed=assessment is not None,
        escalation_reason=decision.escalation_reason,
        total_score=decision.total_score,
        confidence=None if assessment is None else assessment.confidence,
        hard_case=case.hard_case,
        injection_case_id=case.injection_case_id,
        cost_usd=Decimal(0) if metering is None else metering.cost_usd,
        latency_ms=_latency_of(outcome),
        note=decision.note,
        label_notes=case.labels[0].notes if case.labels else "",
        labelers=case.labelers,
        model_reasoning="" if assessment is None else assessment.reasoning,
        failure_detail="" if isinstance(outcome, AssessmentSucceeded) else outcome.detail,
        dimension_range_violations=_dimension_violations(case, outcome),
        extracted_mismatches=_extracted_mismatches(case, outcome),
        expect_escalation=case.expect_escalation,
        tags=case.tags,
        record=cli_shaped_record(outcome, decision),
    )


def _failed_result(case: GoldenCase, reason: EscalationReason, detail: str) -> CaseResult:
    """A case the harness itself could not complete, routed exactly as production would."""
    decision = system_failure(reason, detail)
    return CaseResult(
        case_id=case.case_id,
        provenance=case.provenance,
        expected_tier=case.expected_tier,
        lower_bound=case.lower_bound,
        upper_bound=case.upper_bound,
        predicted_tier=decision.tier,
        assessed=False,
        escalation_reason=decision.escalation_reason,
        total_score=decision.total_score,
        confidence=None,
        hard_case=case.hard_case,
        injection_case_id=case.injection_case_id,
        cost_usd=Decimal(0),
        latency_ms=0,
        note=decision.note,
        label_notes=case.labels[0].notes if case.labels else "",
        labelers=case.labelers,
        failure_detail=detail,
        expect_escalation=case.expect_escalation,
        tags=case.tags,
        record=cli_shaped_record(None, decision, failure=(reason, detail)),
    )


def _latency_of(outcome: AssessmentOutcome) -> int:
    if isinstance(outcome, AssessmentSucceeded):
        return outcome.metering.latency_ms
    return outcome.latency_ms


def cli_shaped_record(
    outcome: AssessmentOutcome | None,
    decision: RoutingDecision,
    *,
    failure: tuple[EscalationReason, str] | None = None,
) -> dict[str, Any]:
    """One case in the vocabulary #13's ``--json`` already defined.

    Same four keys, same field names, same ``cost_usd``-as-a-string convention: the CLI's
    record is the project's answer to "what happened to one lead", and the eval extends it
    rather than competing with it. ``tests/unit/test_run_eval.py`` pins the shapes against
    the CLI's own output so the two cannot drift apart unnoticed.
    """
    record: dict[str, Any] = {
        "assessment": None,
        "decision": decision.model_dump(mode="json"),
        "metering": None,
        "failure": None,
    }
    if isinstance(outcome, AssessmentSucceeded):
        record["assessment"] = outcome.assessment.model_dump(mode="json")
    if outcome is not None and outcome.metering is not None:
        metering = outcome.metering
        record["metering"] = {
            "model_id": metering.model_id,
            "prompt_version": metering.prompt_version,
            "effort": metering.effort,
            "input_tokens": metering.input_tokens,
            "output_tokens": metering.output_tokens,
            "cache_read_tokens": metering.cache_read_tokens,
            "cache_creation_tokens": metering.cache_creation_tokens,
            "cost_usd": str(metering.cost_usd),
            "latency_ms": metering.latency_ms,
        }
    if outcome is not None and not isinstance(outcome, AssessmentSucceeded):
        record["failure"] = {
            "reason": outcome.reason.value,
            "detail": outcome.detail,
            "latency_ms": outcome.latency_ms,
        }
    elif failure is not None:
        reason, detail = failure
        record["failure"] = {"reason": reason.value, "detail": detail, "latency_ms": 0}
    return record


def _dimension_violations(case: GoldenCase, outcome: AssessmentOutcome) -> tuple[str, ...]:
    """Dimension scores outside the range the label declared. Numbers, never prose."""
    if not isinstance(outcome, AssessmentSucceeded) or not case.expected_dimension_ranges:
        return ()
    scores = outcome.assessment.dimension_scores
    violations: list[str] = []
    for name, (low, high) in sorted(case.expected_dimension_ranges.items()):
        actual = getattr(scores, name)
        if not low <= actual <= high:
            violations.append(f"{name}={actual}, expected {low}..{high}")
    return tuple(violations)


def _extracted_mismatches(case: GoldenCase, outcome: AssessmentOutcome) -> tuple[str, ...]:
    """Extracted fields that differ from the label. Compared case-insensitively, trimmed."""
    if not isinstance(outcome, AssessmentSucceeded) or not case.expected_extracted:
        return ()
    extracted = outcome.assessment.extracted
    mismatches: list[str] = []
    for name, expected in sorted(case.expected_extracted.items()):
        actual = getattr(extracted, name)
        if _normalise(actual) != _normalise(expected):
            mismatches.append(f"{name}={actual!r}, expected {expected!r}")
    return tuple(mismatches)


def _normalise(value: str | None) -> str | None:
    return None if value is None else " ".join(value.split()).casefold()


def run_cases(
    cases: Sequence[GoldenCase],
    *,
    assessor: LeadAssessorPort,
    config: TenantConfig,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[CaseResult, ...]:
    """Assess every case on a bounded pool and return the results in ``case_id`` order.

    ``ThreadPoolExecutor.map`` preserves input order regardless of completion order, and
    the input is sorted, so the output is deterministic even though the calls are not.
    """
    ordered = sorted(cases, key=lambda case: case.case_id)
    if not ordered:
        return ()
    workers = max(1, min(concurrency, len(ordered)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="eval") as pool:
        return tuple(
            pool.map(lambda case: assess_case(case, assessor=assessor, config=config), ordered)
        )


# ------------------------------------------------------------------------------- the CLI


def build_parser() -> argparse.ArgumentParser:
    """The command line. ``--confirm-spend`` is required for any run that calls the API."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.run_eval",
        description=(
            "Score the golden set through the real pipeline and report the four eval "
            "metrics. Makes one billable model call per case."
        ),
        epilog=(
            "The seed golden set is entirely synthetic: numbers computed against it "
            "measure self-consistency with its author, not correctness. Read "
            "docs/labeling-golden-set.md before quoting any of them."
        ),
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help=(
            "Required. Acknowledges that this makes one billable model call per golden "
            "case. Without it nothing is called. Use --estimate to see the price first."
        ),
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Print the estimated cost of a run and exit. Needs no key and calls nothing.",
    )
    parser.add_argument(
        "--effort",
        choices=sorted(EFFORT_LEVELS),
        default=DEFAULT_EFFORT,
        help=f"Model effort level (default: {DEFAULT_EFFORT}).",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT_ID,
        help=f"Tenant whose rubric to apply (default: {DEFAULT_TENANT_ID}).",
    )
    parser.add_argument(
        "--tenants-dir",
        type=Path,
        default=None,
        help="Directory of tenant config files (default: the bundled tenants/).",
    )
    parser.add_argument(
        "--golden-set",
        type=Path,
        default=None,
        help=f"Golden set to run (default: {GOLDEN_LEADS_PATH}).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            f"In-flight model calls (default: {DEFAULT_CONCURRENCY}). Lower this on a 429; "
            "the SDK already retries with backoff, so do not add retries."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run the whole set this many times and report how much the numbers moved "
            "with nothing changed. Multiplies the cost by the same factor."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Directory for the JSON result file (default: {DEFAULT_RESULTS_DIR}).",
    )
    parser.add_argument(
        "--all-case-detail",
        action="store_true",
        help="Print notes and reasoning for every case, not only the ones that moved.",
    )
    parser.add_argument(
        "--assumed-output-tokens",
        type=int,
        default=DEFAULT_ASSUMED_OUTPUT_TOKENS,
        help=f"Output tokens per call assumed by --estimate (default: "
        f"{DEFAULT_ASSUMED_OUTPUT_TOKENS}).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    assessor_factory: AssessorFactory | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Run the eval. Returns the process exit code.

    ``assessor_factory`` and ``now`` are injected so the harness itself is testable
    offline: every test in ``tests/unit/test_run_eval.py`` drives this function with a
    scripted assessor and a fixed clock, and none of them needs a key or a network.
    """
    configure_logging(stream=sys.stderr)
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        golden = load_golden_set(args.golden_set)
        config = _load_config(args.tenant, args.tenants_dir)
        effort = _checked_effort(args.effort)
        _check_bounds(args.concurrency, args.repeat)
    except (OSError, ValueError, TenantConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    estimate = estimate_cost(
        golden,
        config,
        effort=effort,
        concurrency=args.concurrency,
        output_tokens_per_case=args.assumed_output_tokens,
    )
    if args.estimate:
        print(estimate.render())
        if args.repeat > 1:
            print(f"Multiply by {args.repeat} for --repeat {args.repeat}.")
        return EXIT_OK

    refusal = _refuse_unless_authorised(args.confirm_spend, estimate, args.repeat)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_REFUSED

    factory = assessor_factory or _default_assessor_factory
    try:
        assessor = factory(effort)
    except RuntimeError as error:  # a missing ANTHROPIC_API_KEY, most likely
        print(f"error: {error}", file=sys.stderr)
        return EXIT_REFUSED

    clock = now or _utc_now
    started_at = clock()
    repeats = [
        run_cases(golden.cases, assessor=assessor, config=config, concurrency=args.concurrency)
        for _ in range(args.repeat)
    ]
    finished_at = clock()

    run = EvalRun.of(
        RunMetadata(
            started_at=started_at,
            finished_at=finished_at,
            git_sha=git_sha(),
            git_dirty=git_dirty(),
            model_id=MODEL_ID,
            prompt_version=PROMPT_VERSION,
            effort=effort,
            tenant_id=config.tenant_id,
            concurrency=args.concurrency,
            repeats=args.repeat,
            golden_set_path=str(golden.path),
        ),
        golden,
        repeats,
    )
    print(render_text_report(run, detail_for_every_case=args.all_case_detail))
    destination = write_result(run, args.out or DEFAULT_RESULTS_DIR)
    print(f"JSON result written to {destination}")
    return EXIT_SECURITY_FINDING if run.findings else EXIT_OK


def _refuse_unless_authorised(confirmed: bool, estimate: CostEstimate, repeat: int) -> str | None:
    """The message to print, or ``None`` when the run may proceed.

    Both conditions are checked before any assessor is built, so a refusal costs nothing
    and reports *everything* that is missing rather than one thing at a time.
    """
    missing: list[str] = []
    if not confirmed:
        missing.append(
            "--confirm-spend was not given: this command makes one billable model call "
            f"per golden case (about ${estimate.cost_usd * repeat:.2f} for this run)"
        )
    if not _api_key_present():
        missing.append(
            "ANTHROPIC_API_KEY is not set: the eval calls the real API, and there is no "
            "offline mode that would produce a meaningful number"
        )
    if not missing:
        return None
    return (
        "refusing to run:\n"
        + "\n".join(f"  - {reason}" for reason in missing)
        + ("\n\nRun with --estimate to see the cost without calling anything.")
    )


def _api_key_present() -> bool:
    try:
        get_settings().require_anthropic_api_key()
    except RuntimeError:
        return False
    return True


def write_result(run: EvalRun, directory: Path) -> Path:
    """Write the timestamped JSON result file and return its path.

    Named for the instant it started and the commit it ran against, because the question
    six weeks from now is "which run was that, and against what?" and a filename is the
    cheapest possible answer.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = run.metadata.started_at.strftime("%Y%m%dT%H%M%SZ")
    destination = directory / f"eval-{stamp}-{run.metadata.revision}.json"
    destination.write_text(dumps(run), encoding="utf-8")
    return destination


def _load_config(tenant_id: str, tenants_dir: Path | None) -> TenantConfig:
    loader = JsonFileTenantConfigLoader(tenants_dir or default_tenants_dir())
    return loader.get(tenant_id)


def _checked_effort(effort: str) -> Effort:
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {sorted(EFFORT_LEVELS)}")
    return effort


def _check_bounds(concurrency: int, repeat: int) -> None:
    if concurrency < 1:
        raise ValueError(f"--concurrency must be at least 1, got {concurrency}")
    if repeat < 1:
        raise ValueError(f"--repeat must be at least 1, got {repeat}")


def _default_assessor_factory(effort: Effort) -> LeadAssessorPort:
    """The real assessor. Only reached once the run has been authorised."""
    return AnthropicLeadAssessor(
        build_anthropic_client(get_settings().require_anthropic_api_key()), effort=effort
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def git_sha() -> str:
    """The commit this run was made from, or ``"unknown"`` outside a checkout."""
    return _git("rev-parse", "HEAD") or "unknown"


def git_dirty() -> bool:
    """Whether the working tree had uncommitted changes. A dirty run is not reproducible."""
    return bool(_git("status", "--porcelain"))


def _git(*arguments: str) -> str:
    """Run one read-only git command, or return ``""`` if git is unavailable.

    Provenance is worth having and never worth failing a paid run over: a checkout without
    git, or a tarball with no ``.git``, reports ``unknown`` and carries on.
    """
    executable = which("git")
    if executable is None:
        return ""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, resolved binary, no shell
            [executable, *arguments],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m tests.evals.run_eval`
    raise SystemExit(main())


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_ASSUMED_OUTPUT_TOKENS",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RESULTS_DIR",
    "EXIT_INPUT_ERROR",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_SECURITY_FINDING",
    "CostEstimate",
    "assess_case",
    "build_case_result",
    "build_parser",
    "cli_shaped_record",
    "estimate_cost",
    "git_dirty",
    "git_sha",
    "main",
    "run_cases",
    "write_result",
]
