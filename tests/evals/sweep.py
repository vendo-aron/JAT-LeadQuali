"""``python -m tests.evals.sweep --confirm-spend`` — the effort sweep (plan §5, §8).

#24 asks a measurement question: ``effort: "medium"`` was a starting point, so what do
``low``, ``medium`` and ``high`` actually cost, and does accuracy hold when you go
cheaper? This module answers it by running :func:`tests.evals.run_eval.main` once per
level and comparing the result files it wrote.

**There is no second evaluation loop here, on purpose.** #23 left an ``assessor_factory``
seam precisely so a sweep could vary the assessor without re-implementing the harness, and
this module is what that seam was for: it passes the same factory to the same ``main``
three times with a different ``--effort`` and a different ``--out``. Everything after that
is arithmetic over saved JSON, which means the sweep can only ever report something a
later ``diff_results`` of the same files would also report, and that a level's numbers are
reproducible from an artifact rather than from this process's memory.

**A sweep is three runs, so it costs three times as much.** ``--estimate`` prints the
whole bill per level and in total, needs no key, and calls nothing. Without
``--confirm-spend`` *and* an ``ANTHROPIC_API_KEY`` the command refuses before it builds a
single assessor, and the number in the refusal is the sweep's price, not one run's.

**The comparison is honest about what it cannot say.** Fifteen synthetic cases resolve
differences no finer than one case — 6.7 points — so a level that scores one case better
has not been shown to be better at all. See :mod:`tests.evals.compare` for the noise
floor, and ``docs/rubric-tuning.md`` for what to do with the output. The one thing this
tool will not do is name a cheapest-that-holds-accuracy winner off a synthetic set: that
is the decision #24 exists to inform, and it needs real labeled leads under it first.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from leadquali.adapters.tenant_config_json import (
    DEFAULT_TENANT_ID,
    JsonFileTenantConfigLoader,
    default_tenants_dir,
)
from leadquali.app.assessment_result import DEFAULT_EFFORT, EFFORT_LEVELS, Effort
from leadquali.config import get_settings
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError
from tests.evals.compare import (
    ComparisonError,
    SweepComparison,
    load_result,
    render_sweep,
)
from tests.evals.golden_set import GOLDEN_LEADS_PATH, GoldenSet, load_golden_set
from tests.evals.run_eval import (
    DEFAULT_ASSUMED_OUTPUT_TOKENS,
    DEFAULT_CONCURRENCY,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_SECURITY_FINDING,
    AssessorFactory,
    CostEstimate,
    estimate_cost,
)
from tests.evals.run_eval import main as run_eval_main

#: Billable, exactly like #23's harness. Nothing here is collected today, and this is the
#: guard for the day somebody renames the file: ``tests/conftest.py`` keeps ``live_api``
#: out of the default suite.
pytestmark = pytest.mark.live_api

#: The three levels #24 names. ``xhigh`` and ``max`` exist and can be swept with repeated
#: ``--effort`` flags, but they are not in the default sweep: the question on the table is
#: whether the *cheapest* level holds, and paying for two levels above the incumbent to
#: answer it is money spent in the wrong direction.
DEFAULT_SWEEP_EFFORTS: Final[tuple[Effort, ...]] = ("low", "medium", "high")

#: The level everything is compared against: the incumbent default, so the deltas answer
#: "may I move off medium?" rather than "how do these three rank among themselves?".
DEFAULT_BASELINE: Final[Effort] = DEFAULT_EFFORT

#: Where the sweep writes, when ``--out`` is not given. Git-ignored like #23's results.
DEFAULT_SWEEP_DIR: Final[Path] = Path(__file__).resolve().parent / "results" / "sweeps"


# --------------------------------------------------------------------------- estimating


@dataclass(frozen=True, slots=True)
class SweepEstimate:
    """What the whole sweep will cost, per level and in total, before spending anything."""

    per_level: Mapping[str, CostEstimate]
    repeat: int

    @property
    def total_cost_usd(self) -> Decimal:
        """Every level, multiplied by ``--repeat``."""
        return sum((level.cost_usd for level in self.per_level.values()), Decimal(0)) * self.repeat

    @property
    def levels(self) -> int:
        """How many effort levels this sweep would run."""
        return len(self.per_level)

    def render(self) -> str:
        """The text printed by ``--estimate``."""
        first = next(iter(self.per_level.values()), None)
        cases = 0 if first is None else first.cases
        lines = [
            f"Estimated cost of a sweep over {self.levels} effort level(s) x {cases} cases "
            f"x {self.repeat} repeat(s):",
        ]
        for effort, estimate in self.per_level.items():
            lines.append(
                f"  {effort:<8} ${estimate.cost_usd * self.repeat:.4f} "
                f"(${estimate.cost_usd:.4f} per repeat, "
                f"~${estimate.cost_usd / max(estimate.cases, 1):.4f} per lead)"
            )
        lines.append(f"  {'TOTAL':<8} ${self.total_cost_usd:.4f}")
        shown_for = next(iter(self.per_level), "-")
        lines.append(
            f"This is an estimate, not a quote. It assumes (shown for {shown_for}; the "
            f"input side is identical at every level, since the prompts are):"
        )
        assumptions = list(first.assumptions) if first is not None else []
        assumptions.append(
            "the same assumed output-token count at every effort level, which overstates "
            "low and understates high - thinking tokens are billed as output and are "
            "exactly what effort buys. Re-estimate with --assumed-output-tokens once one "
            "real run has told you the true figure for a level."
        )
        lines.extend(f"  - {assumption}" for assumption in assumptions)
        return "\n".join(lines)


def load_inputs(
    golden_set: Path | None, tenant: str, tenants_dir: Path | None
) -> tuple[GoldenSet, TenantConfig]:
    """Load the golden set and the tenant config a sweep will run against.

    Raises:
        OSError, ValueError, TenantConfigError: the same failures ``run_eval`` reports, and
            reported here first so a sweep fails before its first billable call rather
            than a third of the way through.
    """
    golden = load_golden_set(golden_set)
    config = JsonFileTenantConfigLoader(tenants_dir or default_tenants_dir()).get(tenant)
    return golden, config


def estimate_sweep(
    golden: GoldenSet,
    config: TenantConfig,
    *,
    efforts: Sequence[Effort],
    concurrency: int = DEFAULT_CONCURRENCY,
    repeat: int = 1,
    output_tokens_per_case: int = DEFAULT_ASSUMED_OUTPUT_TOKENS,
) -> SweepEstimate:
    """Price the sweep by pricing each level with #23's own estimator."""
    return SweepEstimate(
        per_level={
            effort: estimate_cost(
                golden,
                config,
                effort=effort,
                concurrency=concurrency,
                output_tokens_per_case=output_tokens_per_case,
            )
            for effort in efforts
        },
        repeat=repeat,
    )


# ------------------------------------------------------------------------ running levels


@dataclass(frozen=True, slots=True)
class LevelRun:
    """One effort level's run: where its result landed and what the harness printed."""

    effort: Effort
    exit_code: int
    result_path: Path
    report: str


def run_level(
    effort: Effort,
    *,
    arguments: Sequence[str],
    out_dir: Path,
    assessor_factory: AssessorFactory | None,
    now: Callable[[], datetime] | None,
) -> LevelRun:
    """Run #23's harness once at ``effort`` and return where it wrote its result.

    Each level gets its own ``--out`` directory. Result files are named for the instant the
    run started, and three levels started in the same second would otherwise collide on one
    filename — losing two thirds of a sweep that has already been paid for.

    Raises:
        ComparisonError: the harness reported success but left no result file behind.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = run_eval_main(
            [*arguments, "--effort", effort, "--out", str(out_dir)],
            assessor_factory=assessor_factory,
            now=now,
        )
    written = sorted(out_dir.glob("eval-*.json"))
    if not written and code in {EXIT_OK, EXIT_SECURITY_FINDING}:
        raise ComparisonError(
            f"the {effort} run reported success but wrote no result file into {out_dir}"
        )
    return LevelRun(
        effort=effort,
        exit_code=code,
        # Latest by name is latest by start time: the filename leads with the timestamp.
        result_path=written[-1] if written else out_dir,
        report=captured.getvalue(),
    )


# ------------------------------------------------------------------------------- the CLI


def build_parser() -> argparse.ArgumentParser:
    """The command line. ``--confirm-spend`` is required for any run that calls the API."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.sweep",
        description=(
            "Run the golden set at each effort level and compare accuracy, cost and p95 "
            "latency side by side. Makes one billable model call per case per level."
        ),
        epilog=(
            "The seed golden set is entirely synthetic, and fifteen cases cannot resolve "
            "a difference finer than one case. Read docs/rubric-tuning.md before acting "
            "on anything this prints."
        ),
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help=(
            "Required. Acknowledges one billable model call per golden case per effort "
            "level. Without it nothing is called. Use --estimate to see the price first."
        ),
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Print the estimated cost of the whole sweep and exit. Calls nothing.",
    )
    parser.add_argument(
        "--effort",
        action="append",
        dest="efforts",
        default=None,
        help=(
            f"An effort level to sweep; repeat the flag for more. Default: "
            f"{', '.join(DEFAULT_SWEEP_EFFORTS)}. Known levels: {', '.join(EFFORT_LEVELS)}."
        ),
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE,
        help=f"The level every other level is compared against (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT_ID,
        help=f"Tenant whose rubric to apply (default: {DEFAULT_TENANT_ID}).",
    )
    parser.add_argument(
        "--tenants-dir", type=Path, default=None, help="Directory of tenant config files."
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
        help=f"In-flight model calls per level (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Repeats per level. Two or more measures the run-to-run spread, which is what "
            "turns the noise floor from a stated convention into a measured number. "
            "Multiplies the cost by the same factor."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Directory for the per-level results and the comparison (default: "
        f"{DEFAULT_SWEEP_DIR}).",
    )
    parser.add_argument(
        "--per-level-report",
        action="store_true",
        help="Also print each level's full eval report, not only the comparison.",
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
    """Run the sweep. Returns the process exit code.

    ``assessor_factory`` and ``now`` are handed straight to #23's ``main`` for each level,
    which is what makes the whole sweep testable offline with assessors scripted per
    effort level.
    """
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        efforts = _checked_efforts(args.efforts)
        baseline = _checked_baseline(args.baseline, efforts)
        golden, config = load_inputs(args.golden_set, args.tenant, args.tenants_dir)
        _check_bounds(args.concurrency, args.repeat)
    except (OSError, ValueError, TenantConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    estimate = estimate_sweep(
        golden,
        config,
        efforts=efforts,
        concurrency=args.concurrency,
        repeat=args.repeat,
        output_tokens_per_case=args.assumed_output_tokens,
    )
    if args.estimate:
        print(estimate.render())
        return EXIT_OK

    refusal = _refuse_unless_authorised(args.confirm_spend, estimate)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_REFUSED

    out_dir = args.out or DEFAULT_SWEEP_DIR
    shared = _harness_arguments(args)
    runs: list[LevelRun] = []
    try:
        for effort in efforts:
            run = run_level(
                effort,
                arguments=shared,
                out_dir=out_dir / effort,
                assessor_factory=assessor_factory,
                now=now,
            )
            if run.exit_code not in {EXIT_OK, EXIT_SECURITY_FINDING}:
                print(run.report, file=sys.stderr)
                print(f"error: the {effort} run failed; abandoning the sweep", file=sys.stderr)
                return run.exit_code
            runs.append(run)
    except ComparisonError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    if args.per_level_report:
        for run in runs:
            print(run.report, end="")

    try:
        snapshots = [load_result(run.result_path, label=run.effort) for run in runs]
        sweep = SweepComparison.of(snapshots, baseline=baseline)
    except (OSError, ComparisonError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    print(render_sweep(sweep), end="")
    destination = write_sweep(sweep, runs, out_dir, (now or _utc_now)())
    print(f"Sweep comparison written to {destination}")
    return (
        EXIT_SECURITY_FINDING
        if any(run.exit_code == EXIT_SECURITY_FINDING for run in runs)
        else EXIT_OK
    )


def write_sweep(
    sweep: SweepComparison, runs: Sequence[LevelRun], directory: Path, at: datetime
) -> Path:
    """Write the comparison document, with a pointer to each level's own result file."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        **sweep.as_json(),
        "result_files": {run.effort: str(run.result_path) for run in runs},
    }
    destination = directory / f"sweep-{at.strftime('%Y%m%dT%H%M%SZ')}.json"
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return destination


def _harness_arguments(args: argparse.Namespace) -> list[str]:
    """The flags every level's run shares. ``--effort`` and ``--out`` are added per level."""
    arguments = [
        "--confirm-spend",
        "--tenant",
        args.tenant,
        "--concurrency",
        str(args.concurrency),
        "--repeat",
        str(args.repeat),
        "--assumed-output-tokens",
        str(args.assumed_output_tokens),
    ]
    if args.tenants_dir is not None:
        arguments += ["--tenants-dir", str(args.tenants_dir)]
    if args.golden_set is not None:
        arguments += ["--golden-set", str(args.golden_set)]
    return arguments


def _refuse_unless_authorised(confirmed: bool, estimate: SweepEstimate) -> str | None:
    """The message to print, or ``None`` when the sweep may proceed.

    The headline number is the sweep's, not one run's: somebody who has seen "$0.30" for a
    single run and types the sweep command is about to spend three times that, and the
    figure they are asked to confirm has to be the one they will pay.
    """
    missing: list[str] = []
    if not confirmed:
        missing.append(
            "--confirm-spend was not given: this makes one billable model call per golden "
            "case per effort level"
        )
    if not _api_key_present():
        missing.append(
            "ANTHROPIC_API_KEY is not set: the sweep calls the real API, and there is no "
            "offline mode that would produce a meaningful number"
        )
    if not missing:
        return None
    return (
        f"refusing to run a {estimate.levels} effort level sweep "
        f"(about ${estimate.total_cost_usd:.2f} in total):\n"
        + "\n".join(f"  - {reason}" for reason in missing)
        + "\n\nRun with --estimate to see the full breakdown without calling anything."
    )


def _api_key_present() -> bool:
    try:
        get_settings().require_anthropic_api_key()
    except RuntimeError:
        return False
    return True


def _checked_efforts(raw: Sequence[str] | None) -> tuple[Effort, ...]:
    """Validate the requested levels and order them by spend.

    Raises:
        ValueError: an unknown level, or the same level twice — a sweep that ran ``low``
            twice would report a difference between a level and itself as if it were a
            comparison.
    """
    requested: list[str] = [str(item) for item in (raw if raw else DEFAULT_SWEEP_EFFORTS)]
    unknown = [effort for effort in requested if effort not in EFFORT_LEVELS]
    if unknown:
        raise ValueError(
            f"unknown effort level(s) {', '.join(repr(item) for item in unknown)}; "
            f"expected one of {', '.join(EFFORT_LEVELS)}"
        )
    duplicates = sorted({item for item in requested if requested.count(item) > 1})
    if duplicates:
        raise ValueError(
            f"effort level(s) {', '.join(repr(item) for item in duplicates)} requested more "
            f"than once; each level is run exactly once"
        )
    levels: list[Effort] = [effort for effort in EFFORT_LEVELS if effort in requested]
    return tuple(levels)


def _checked_baseline(baseline: str, efforts: Sequence[Effort]) -> Effort:
    """The baseline must be a level the sweep actually runs."""
    if baseline not in efforts:
        raise ValueError(
            f"baseline effort {baseline!r} is not among the levels being swept "
            f"({', '.join(efforts)}); add it with --effort {baseline} or pick another baseline"
        )
    return baseline  # type: ignore[return-value]  # membership in efforts proves the Literal


def _check_bounds(concurrency: int, repeat: int) -> None:
    if concurrency < 1:
        raise ValueError(f"--concurrency must be at least 1, got {concurrency}")
    if repeat < 1:
        raise ValueError(f"--repeat must be at least 1, got {repeat}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m tests.evals.sweep`
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BASELINE",
    "DEFAULT_SWEEP_DIR",
    "DEFAULT_SWEEP_EFFORTS",
    "EXIT_INPUT_ERROR",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_SECURITY_FINDING",
    "LevelRun",
    "SweepEstimate",
    "build_parser",
    "estimate_sweep",
    "load_inputs",
    "main",
    "run_level",
    "write_sweep",
]
