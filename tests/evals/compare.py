"""Comparing saved eval results: two of them, or one per effort level.

Everything here reads result files that :mod:`tests.evals.run_eval` already wrote. Nothing
in this module calls a model, needs a key, or costs anything — which is the point. Once a
run is paid for, "did last Tuesday's prompt change make things worse?" and "does ``low``
hold up against ``medium``?" are questions about two JSON files, and re-running the eval to
answer them is paying twice for evidence you already have.

**The statistics are the substance, not a caveat.** The seed golden set is fifteen
synthetic cases (``docs/labeling-golden-set.md`` §0). A proportion measured over fifteen
cases moves in steps of ``1/15`` — 6.7 percentage points — so a "3-point improvement"
cannot exist, and a one-case difference between two effort levels is a coin flip with a
number printed next to it. This module therefore refuses to report a difference without
also reporting what difference the set could have detected:

* :attr:`NoiseFloor.resolution_pp` is arithmetic: ``100 / cases``, the finest difference
  the set can represent at all.
* :attr:`NoiseFloor.floor_pp` is the *stated* minimum detectable difference:
  :data:`MIN_MEANINGFUL_CASES` cases, raised to the observed run-to-run spread whenever
  ``--repeat`` measured one. It is a declared convention, and it is labeled as one
  everywhere it is printed. **There is no significance test in this module**, because there
  is no sampling model for "fifteen leads one person invented" that would justify one.
* Anything at or below that floor is reported as :data:`VERDICT_WITHIN_NOISE`, and a sweep
  in which every difference lands there reports :data:`VERDICT_INDISTINGUISHABLE` —
  verbatim, because "this set cannot tell these apart" is the finding.

**Indistinguishable is not equivalent.** Two levels the set cannot separate have not been
shown to be interchangeable; nothing has been shown. :meth:`SweepComparison.recommendation`
therefore declines to name a winner in that case, and declines again — whatever the numbers
say — while the golden set holds no real cases at all, because tuning against a
self-labeled synthetic set optimises for agreement with its author.

**Two runs are compared, never merged.** A pair of results whose ``prompt_version``,
``model_id``, ``git_sha`` or golden set differ are two different experiments; the tool
still compares them, because comparing across a prompt change is the entire use case, but
every such difference is listed as attribution so that no one reads a single cause into a
number that has several.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Self

from leadquali.app.assessment_result import EFFORT_LEVELS
from tests.evals.metrics import TAIL_QUANTILE
from tests.evals.report import HEADLINE_CAVEAT, RESULT_SCHEMA_VERSION

#: Metrics compared between runs, in reporting order: the one that costs money first.
#: Every one of them is a :class:`~tests.evals.metrics.Ratio` in the result file, so a
#: delta can always be expressed in whole cases as well as in points.
COMPARED_METRICS: Final[tuple[str, ...]] = (
    "recall_on_contactable",
    "precision_on_hot",
    "exact_tier_accuracy",
    "adjacent_tier_accuracy",
    "within_band_accuracy",
)

#: How many whole cases must move before a difference is reported as a difference.
#:
#: **A stated convention, not a test statistic.** Two is the smallest number that is not a
#: single case, and a single case is both the resolution limit of the set and precisely
#: what an unchanged prompt moves between two repeats — ``compute_stability`` exists
#: because the same lead lands either side of a threshold on consecutive runs. Requiring
#: two is deliberately conservative: at fifteen cases it means nothing below 13.3 points
#: is reported as a finding, which is the correct amount of scepticism for a set this
#: small. It is not a confidence level and must never be quoted as one.
MIN_MEANINGFUL_CASES: Final[int] = 2

#: The difference is larger than the set's floor: it may be real. "May": the floor says
#: what the set *cannot* detect, never that what it can detect is causal.
VERDICT_MEANINGFUL: Final[str] = "meaningful"

#: The difference is at or below the floor. Not "no difference" — no detectable one.
VERDICT_WITHIN_NOISE: Final[str] = "within noise"

#: One side of the comparison has no number at all (an undefined ratio, an empty segment).
VERDICT_NOT_COMPARABLE: Final[str] = "not comparable"

#: Printed verbatim when no difference in a sweep clears the floor. This sentence is the
#: honest output of #24 against the seed set, and it is a constant so that the report, the
#: JSON and the tests all say exactly the same thing.
VERDICT_INDISTINGUISHABLE: Final[str] = "This golden set cannot distinguish these effort levels."

#: Printed verbatim whenever the tool declines to name a winner.
NO_RECOMMENDATION: Final[str] = "No effort level is recommended from this run."

_RULE: Final[str] = "=" * 92
_THIN: Final[str] = "-" * 92


class ComparisonError(ValueError):
    """A result file cannot be read, or two of them cannot honestly be compared."""


# ------------------------------------------------------------------------ result files


@dataclass(frozen=True, slots=True)
class RatioSnapshot:
    """One metric as it was recorded: the two counts, and why it may not exist.

    A mirror of :class:`~tests.evals.metrics.Ratio` reconstructed from JSON rather than the
    class itself, because a result file may have been written by an older checkout and a
    comparison tool that imported the live class would silently acquire whatever that class
    means today.
    """

    numerator: int
    denominator: int
    undefined_reason: str = ""

    @property
    def defined(self) -> bool:
        """Whether there was anything to divide."""
        return self.denominator > 0

    @property
    def value(self) -> float | None:
        """The fraction, or ``None`` when the denominator is zero."""
        return None if not self.defined else self.numerator / self.denominator

    @property
    def percent(self) -> float | None:
        """The fraction in percentage points."""
        value = self.value
        return None if value is None else value * 100.0

    @property
    def text(self) -> str:
        """One cell: ``86.7% (13/15)``, or the sentence saying why there is no number."""
        if not self.defined:
            return f"undefined ({self.undefined_reason or 'nothing to measure'})"
        return f"{self.numerator / self.denominator:.1%} ({self.numerator}/{self.denominator})"

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form, matching what the result file carried."""
        payload: dict[str, Any] = {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": self.value,
        }
        if not self.defined:
            payload["undefined_reason"] = self.undefined_reason or "nothing to measure"
        return payload


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """What one run did with one case. Enough to say what moved, and nothing more."""

    case_id: str
    expected_tier: str
    predicted_tier: str
    status: str
    exact_match: bool
    false_disqualification: bool
    escalated: bool
    latency_ms: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    """One saved eval result, reduced to what a comparison needs.

    ``label`` is how this run is named in reports: the effort level in a sweep, the file
    name in an ad-hoc diff.
    """

    label: str
    source: str
    schema_version: int
    started_at: str
    effort: str
    model_id: str
    prompt_version: str
    tenant_id: str
    git_sha: str
    git_dirty: bool
    repeats: int
    golden_set_path: str
    cases_total: int
    real_cases: int
    synthetic_cases: int
    failures: int
    escalations: int
    inter_labeler_agreement: float | None
    metrics: Mapping[str, RatioSnapshot]
    metric_spreads: Mapping[str, float | None]
    total_cost_usd: Decimal
    cost_usd_per_lead: Decimal | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_max_ms: int | None
    security_finding_case_ids: tuple[str, ...]
    caveat: str
    cases: Mapping[str, CaseSnapshot]

    @property
    def effort_rank(self) -> int:
        """Position in :data:`~leadquali.app.assessment_result.EFFORT_LEVELS`.

        Levels are ordered by spend rather than alphabetically, for the same reason
        ``Tier`` needed a ``rank``: ``"high" < "low" < "medium"`` as strings, and a sweep
        table in that order is unreadable.
        """
        try:
            return EFFORT_LEVELS.index(self.effort)
        except ValueError:
            return len(EFFORT_LEVELS)

    def as_json(self) -> dict[str, Any]:
        """The per-level block of a comparison document."""
        return {
            "label": self.label,
            "source": self.source,
            "effort": self.effort,
            "started_at": self.started_at,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "tenant_id": self.tenant_id,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "repeats": self.repeats,
            "golden_set_path": self.golden_set_path,
            "cases": self.cases_total,
            "real_cases": self.real_cases,
            "synthetic_cases": self.synthetic_cases,
            "failures": self.failures,
            "escalations": self.escalations,
            "inter_labeler_agreement": self.inter_labeler_agreement,
            "metrics": {name: ratio.as_json() for name, ratio in self.metrics.items()},
            "total_cost_usd": str(self.total_cost_usd),
            "cost_usd_per_lead": None
            if self.cost_usd_per_lead is None
            else str(self.cost_usd_per_lead),
            "latency": {
                "p50_ms": self.latency_p50_ms,
                "p95_ms": self.latency_p95_ms,
                "max_ms": self.latency_max_ms,
            },
            "security_findings": list(self.security_finding_case_ids),
            "caveat": self.caveat,
        }


def load_result(path: Path, *, label: str | None = None) -> ResultSnapshot:
    """Read one result file written by ``run_eval``.

    Args:
        path: the JSON file.
        label: how to name this run in reports. Defaults to the file's stem.

    Raises:
        ComparisonError: the file is not a result document this tool can read.
        OSError: the file cannot be opened. Deliberately not wrapped — the path is in the
            message and the caller wants to print it verbatim.
    """
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ComparisonError(f"{path} is not valid JSON: {error}") from error
    return parse_result(payload, label=label or path.stem, source=str(path))


def parse_result(payload: object, *, label: str, source: str = "<memory>") -> ResultSnapshot:
    """Turn a decoded result document into a :class:`ResultSnapshot`.

    The schema version is checked before anything else is read. A file written by a
    different version of :mod:`tests.evals.report` may have moved or redefined any key
    below, and a comparison built by guessing at it would be confidently wrong — which is
    worse than no comparison, because someone would act on it.

    Raises:
        ComparisonError: the document is not an object, carries no ``schema_version``,
            carries a version this tool does not read, or is missing a required key.
    """
    if not isinstance(payload, Mapping):
        raise ComparisonError(f"{source}: expected a JSON object, got {type(payload).__name__}")
    version = payload.get("schema_version")
    if version is None:
        raise ComparisonError(
            f"{source}: no schema_version key. This is not an eval result file — "
            f"run_eval writes one per run into its --out directory."
        )
    if version != RESULT_SCHEMA_VERSION:
        raise ComparisonError(
            f"{source}: result schema version {version}, but this tool reads version "
            f"{RESULT_SCHEMA_VERSION}. Keys may have moved between the two, so refusing to "
            f"compare rather than guessing. Re-run the eval on this checkout, or use the "
            f"checkout that wrote the file."
        )
    run = _mapping(payload, "run", source)
    metrics = _mapping(payload, "metrics", source)
    golden = _mapping(payload, "golden_set", source)
    latency = _mapping(metrics, "latency", source)
    return ResultSnapshot(
        label=label,
        source=source,
        schema_version=int(version),
        started_at=_text(run, "started_at", source),
        effort=_text(run, "effort", source),
        model_id=_text(run, "model_id", source),
        prompt_version=_text(run, "prompt_version", source),
        tenant_id=_text(run, "tenant_id", source),
        git_sha=_text(run, "git_sha", source),
        git_dirty=bool(run.get("git_dirty", False)),
        repeats=int(run.get("repeats", 1)),
        golden_set_path=_text(run, "golden_set_path", source),
        cases_total=_number(metrics, "cases", source),
        real_cases=_number(metrics, "real_cases", source),
        synthetic_cases=_number(metrics, "synthetic_cases", source),
        failures=_number(metrics, "failures", source),
        escalations=_number(metrics, "escalations", source),
        inter_labeler_agreement=_optional_float(golden.get("inter_labeler_agreement")),
        metrics={
            name: _ratio(metrics, name, source) for name in COMPARED_METRICS if name in metrics
        },
        metric_spreads=_spreads(payload.get("stability")),
        total_cost_usd=_decimal(metrics.get("total_cost_usd")) or Decimal(0),
        cost_usd_per_lead=_decimal(metrics.get("cost_usd_per_lead")),
        latency_p50_ms=_optional_int(latency.get("p50_ms")),
        latency_p95_ms=_optional_int(latency.get("p95_ms")),
        latency_max_ms=_optional_int(latency.get("max_ms")),
        security_finding_case_ids=_finding_ids(payload.get("security_findings")),
        caveat=str(payload.get("caveat", "")),
        cases=_cases(payload.get("cases"), source),
    )


def _mapping(payload: Mapping[str, Any], key: str, source: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ComparisonError(f"{source}: expected an object at {key!r}")
    return value


def _text(payload: Mapping[str, Any], key: str, source: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ComparisonError(f"{source}: expected a string at {key!r}")
    return value


def _number(payload: Mapping[str, Any], key: str, source: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ComparisonError(f"{source}: expected an integer at {key!r}")
    return value


def _ratio(payload: Mapping[str, Any], key: str, source: str) -> RatioSnapshot:
    body = payload.get(key)
    if not isinstance(body, Mapping):
        raise ComparisonError(f"{source}: expected a ratio object at {key!r}")
    numerator = body.get("numerator")
    denominator = body.get("denominator")
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise ComparisonError(
            f"{source}: {key!r} is not a ratio - it must carry integer numerator and "
            f"denominator, never a bare float"
        )
    return RatioSnapshot(numerator, denominator, str(body.get("undefined_reason", "")))


def _spreads(stability: object) -> Mapping[str, float | None]:
    """Per-metric run-to-run spread, as a fraction, from a ``--repeat`` run."""
    if not isinstance(stability, Mapping):
        return {}
    raw = stability.get("metric_spreads")
    if not isinstance(raw, Mapping):
        return {}
    spreads: dict[str, float | None] = {}
    for name, body in raw.items():
        spreads[str(name)] = (
            _optional_float(body.get("spread")) if isinstance(body, Mapping) else None
        )
    return spreads


def _cases(raw: object, source: str) -> Mapping[str, CaseSnapshot]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ComparisonError(f"{source}: expected a list at 'cases'")
    cases: dict[str, CaseSnapshot] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ComparisonError(f"{source}: every entry in 'cases' must be an object")
        case_id = _text(entry, "case_id", source)
        cases[case_id] = CaseSnapshot(
            case_id=case_id,
            expected_tier=_text(entry, "expected_tier", source),
            predicted_tier=_text(entry, "predicted_tier", source),
            status=str(entry.get("status", "")),
            exact_match=bool(entry.get("exact_match", False)),
            false_disqualification=bool(entry.get("false_disqualification", False)),
            escalated=bool(entry.get("escalated", False)),
            latency_ms=int(entry.get("latency_ms", 0)),
            cost_usd=_decimal(entry.get("cost_usd")) or Decimal(0),
        )
    return cases


def _finding_ids(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(
        sorted(
            str(entry["case_id"])
            for entry in raw
            if isinstance(entry, Mapping) and "case_id" in entry
        )
    )


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str | int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------------- noise floor


@dataclass(frozen=True, slots=True)
class NoiseFloor:
    """The smallest difference this set could have detected, and how that was decided.

    Two ingredients, both stated rather than inferred:

    * ``cases`` gives the *resolution*: a proportion over ``n`` cases can only take values
      ``k/n``, so nothing finer than ``100/n`` points exists to be measured.
    * ``observed_spread_pp`` is what the same configuration actually did across repeats.
      When it is present it replaces the convention wherever it is larger, because a
      measurement of the noise beats an assumption about it.
    """

    cases: int
    observed_spread_pp: float | None
    repeats: int

    @property
    def resolution_pp(self) -> float | None:
        """Points per case: the finest difference the set can represent."""
        return None if self.cases <= 0 else 100.0 / self.cases

    @property
    def measured(self) -> bool:
        """Whether ``--repeat`` gave this floor an observed spread to stand on."""
        return self.observed_spread_pp is not None

    @property
    def floor_pp(self) -> float | None:
        """The minimum detectable difference, in percentage points."""
        resolution = self.resolution_pp
        if resolution is None:
            return None
        stated = MIN_MEANINGFUL_CASES * resolution
        if self.observed_spread_pp is None:
            return stated
        return max(stated, self.observed_spread_pp)

    def is_meaningful(self, delta_pp: float | None) -> bool:
        """Whether a difference of ``delta_pp`` points clears the floor."""
        floor = self.floor_pp
        if delta_pp is None or floor is None:
            return False
        return abs(delta_pp) > floor

    def verdict_for(self, delta_pp: float | None) -> str:
        """:data:`VERDICT_MEANINGFUL`, :data:`VERDICT_WITHIN_NOISE`, or not comparable."""
        if delta_pp is None or self.floor_pp is None:
            return VERDICT_NOT_COMPARABLE
        return VERDICT_MEANINGFUL if self.is_meaningful(delta_pp) else VERDICT_WITHIN_NOISE

    def describe(self) -> str:
        """One sentence: the floor, and whether anything measured it."""
        floor = self.floor_pp
        if floor is None:
            return "no cases, so this set can detect nothing at all"
        stated = (
            f"{floor:.1f}pp minimum detectable difference "
            f"({MIN_MEANINGFUL_CASES} of {self.cases} cases)"
        )
        if self.observed_spread_pp is None:
            return (
                f"{stated}; run-to-run spread not measured on a single run - "
                f"re-run with --repeat 2 or more to measure it"
            )
        return (
            f"{stated}; run-to-run spread {self.observed_spread_pp:.1f}pp "
            f"measured over {self.repeats} repeats"
        )

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {
            "cases": self.cases,
            "resolution_pp": self.resolution_pp,
            "minimum_meaningful_cases": MIN_MEANINGFUL_CASES,
            "minimum_detectable_difference_pp": self.floor_pp,
            "observed_spread_pp": self.observed_spread_pp,
            "spread_measured": self.measured,
            "repeats": self.repeats,
            "description": self.describe(),
        }


def noise_floor(snapshots: Sequence[ResultSnapshot], metric: str) -> NoiseFloor:
    """The floor for ``metric`` across every run being compared.

    The coarsest resolution and the widest observed spread win: a comparison is only as
    sensitive as its least sensitive side, and rounding that off would manufacture
    precision the runs do not have.
    """
    if not snapshots:
        return NoiseFloor(cases=0, observed_spread_pp=None, repeats=1)
    cases = min(snapshot.cases_total for snapshot in snapshots)
    spreads = [
        spread
        for snapshot in snapshots
        if (spread := snapshot.metric_spreads.get(metric)) is not None
    ]
    return NoiseFloor(
        cases=cases,
        observed_spread_pp=max(spreads) * 100.0 if spreads else None,
        repeats=max(snapshot.repeats for snapshot in snapshots),
    )


def cases_needed_for(difference_pp: float) -> int:
    """How many cases a set needs before ``difference_pp`` points is representable.

    ``ceil(100 / difference)``. Twenty cases to see five points, fifty to see two. This is
    the honest answer to "how big does the golden set have to get", and it is arithmetic
    rather than a power calculation — a power calculation would need an effect size and a
    variance nobody has measured yet.
    """
    if difference_pp <= 0:
        raise ValueError(f"a difference must be positive, got {difference_pp}")
    return math.ceil(100.0 / difference_pp)


def p95_is_max(cases: int) -> bool:
    """Whether nearest-rank p95 over ``cases`` samples just returns the slowest one.

    True for any set below twenty cases, which is every set this project has. Worth saying
    in the report: a "p95 latency" that is really the single worst observation is a much
    noisier number than the name suggests.
    """
    return cases > 0 and math.ceil(TAIL_QUANTILE * cases) >= cases


# -------------------------------------------------------------------- comparing two runs


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """One metric, before and after, with the floor it has to clear to count."""

    name: str
    baseline: RatioSnapshot
    candidate: RatioSnapshot
    noise: NoiseFloor

    @property
    def comparable(self) -> bool:
        """Whether both sides produced a number at all."""
        return self.baseline.defined and self.candidate.defined

    @property
    def delta_pp(self) -> float | None:
        """Candidate minus baseline, in percentage points."""
        before, after = self.baseline.percent, self.candidate.percent
        if before is None or after is None:
            return None
        return after - before

    @property
    def same_denominator(self) -> bool:
        """Whether both sides measured the same number of cases."""
        return self.baseline.denominator == self.candidate.denominator

    @property
    def delta_cases(self) -> int | None:
        """Whole cases gained or lost, or ``None`` when the denominators differ.

        ``12/15`` against ``12/16`` is the golden set growing, not the model moving, and a
        "-3.3 points" headline on that pair is a lie of arithmetic. Points are still
        reported; cases are withheld, because there is no case count to report.
        """
        if not self.same_denominator:
            return None
        return self.candidate.numerator - self.baseline.numerator

    @property
    def meaningful(self) -> bool:
        """Whether the difference clears the set's minimum detectable difference."""
        return self.noise.is_meaningful(self.delta_pp)

    @property
    def regressed(self) -> bool:
        """Meaningfully worse, not merely worse."""
        delta = self.delta_pp
        return self.meaningful and delta is not None and delta < 0

    @property
    def verdict(self) -> str:
        """:data:`VERDICT_MEANINGFUL`, :data:`VERDICT_WITHIN_NOISE` or not comparable."""
        if not self.comparable:
            return VERDICT_NOT_COMPARABLE
        return self.noise.verdict_for(self.delta_pp)

    @property
    def text(self) -> str:
        """One report line: the movement, in points and in cases, and the verdict."""
        if not self.comparable:
            undefined = (
                self.baseline if not self.baseline.defined else self.candidate
            ).undefined_reason
            return f"{self.name:<26} {VERDICT_NOT_COMPARABLE}: {undefined or 'no value'}"
        delta = self.delta_pp
        assert delta is not None  # comparable implies both sides have a value
        cases = (
            "denominators differ"
            if self.delta_cases is None
            else f"{self.delta_cases:+d} case{'' if abs(self.delta_cases) == 1 else 's'}"
        )
        return (
            f"{self.name:<26} {self.baseline.text:>16} -> {self.candidate.text:>16}  "
            f"{delta:+6.1f}pp ({cases})  {self.verdict}"
        )

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {
            "name": self.name,
            "baseline": self.baseline.as_json(),
            "candidate": self.candidate.as_json(),
            "delta_pp": self.delta_pp,
            "delta_cases": self.delta_cases,
            "comparable": self.comparable,
            "meaningful": self.meaningful,
            "regressed": self.regressed,
            "verdict": self.verdict,
            "noise_floor": self.noise.as_json(),
        }


@dataclass(frozen=True, slots=True)
class CaseChange:
    """One case the two runs tiered differently."""

    case_id: str
    expected_tier: str
    baseline_tier: str
    candidate_tier: str
    baseline_status: str
    candidate_status: str
    baseline_exact: bool
    candidate_exact: bool
    baseline_lost: bool
    candidate_lost: bool

    @property
    def regressed(self) -> bool:
        """The candidate lost a lead the baseline kept, or missed a label it hit."""
        return (self.candidate_lost and not self.baseline_lost) or (
            self.baseline_exact and not self.candidate_exact
        )

    @property
    def improved(self) -> bool:
        """The mirror image."""
        return (self.baseline_lost and not self.candidate_lost) or (
            self.candidate_exact and not self.baseline_exact
        )

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {
            "case_id": self.case_id,
            "expected_tier": self.expected_tier,
            "baseline_tier": self.baseline_tier,
            "candidate_tier": self.candidate_tier,
            "baseline_status": self.baseline_status,
            "candidate_status": self.candidate_status,
            "regressed": self.regressed,
            "improved": self.improved,
        }


@dataclass(frozen=True, slots=True)
class CaseSetDiff:
    """Which cases the two runs share, and what moved among the shared ones."""

    shared: tuple[str, ...]
    only_in_baseline: tuple[str, ...]
    only_in_candidate: tuple[str, ...]
    changes: tuple[CaseChange, ...]

    @property
    def comparable(self) -> bool:
        """Whether both runs covered exactly the same cases."""
        return not self.only_in_baseline and not self.only_in_candidate

    @property
    def regressions(self) -> tuple[CaseChange, ...]:
        """Shared cases the candidate handled worse."""
        return tuple(change for change in self.changes if change.regressed)

    @property
    def improvements(self) -> tuple[CaseChange, ...]:
        """Shared cases the candidate handled better."""
        return tuple(change for change in self.changes if change.improved)

    @classmethod
    def of(cls, baseline: ResultSnapshot, candidate: ResultSnapshot) -> Self:
        """Diff two runs' case sets, sorted by ``case_id`` throughout."""
        before, after = baseline.cases, candidate.cases
        shared = tuple(sorted(set(before) & set(after)))
        return cls(
            shared=shared,
            only_in_baseline=tuple(sorted(set(before) - set(after))),
            only_in_candidate=tuple(sorted(set(after) - set(before))),
            changes=tuple(
                CaseChange(
                    case_id=case_id,
                    expected_tier=before[case_id].expected_tier,
                    baseline_tier=before[case_id].predicted_tier,
                    candidate_tier=after[case_id].predicted_tier,
                    baseline_status=before[case_id].status,
                    candidate_status=after[case_id].status,
                    baseline_exact=before[case_id].exact_match,
                    candidate_exact=after[case_id].exact_match,
                    baseline_lost=before[case_id].false_disqualification,
                    candidate_lost=after[case_id].false_disqualification,
                )
                for case_id in shared
                if before[case_id].predicted_tier != after[case_id].predicted_tier
            ),
        )

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {
            "shared": list(self.shared),
            "only_in_baseline": list(self.only_in_baseline),
            "only_in_candidate": list(self.only_in_candidate),
            "comparable": self.comparable,
            "changes": [change.as_json() for change in self.changes],
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """Two runs, compared: metric deltas, case movement, and what else changed."""

    baseline: ResultSnapshot
    candidate: ResultSnapshot
    metrics: tuple[MetricDelta, ...]
    cases: CaseSetDiff
    warnings: tuple[str, ...]
    noise: NoiseFloor

    @classmethod
    def of(cls, baseline: ResultSnapshot, candidate: ResultSnapshot) -> Self:
        """Compare two snapshots. Never refuses: differences are attributed, not hidden."""
        pair = (baseline, candidate)
        metrics = tuple(
            MetricDelta(
                name=name,
                baseline=baseline.metrics[name],
                candidate=candidate.metrics[name],
                noise=_metric_noise(pair, name),
            )
            for name in COMPARED_METRICS
            if name in baseline.metrics and name in candidate.metrics
        )
        return cls(
            baseline=baseline,
            candidate=candidate,
            metrics=metrics,
            cases=CaseSetDiff.of(baseline, candidate),
            warnings=_attribution(baseline, candidate),
            noise=NoiseFloor(
                cases=min(baseline.cases_total, candidate.cases_total),
                observed_spread_pp=_widest_spread(pair),
                repeats=max(baseline.repeats, candidate.repeats),
            ),
        )

    def metric(self, name: str) -> MetricDelta:
        """One delta by name.

        Raises:
            KeyError: neither run recorded that metric.
        """
        for delta in self.metrics:
            if delta.name == name:
                return delta
        raise KeyError(f"{name!r} was not recorded by both runs")

    @property
    def any_meaningful(self) -> bool:
        """Whether any metric moved further than the set could have moved by itself."""
        return any(delta.meaningful for delta in self.metrics)

    @property
    def meaningful_metrics(self) -> tuple[str, ...]:
        """Names of the metrics that cleared the floor, in reporting order."""
        return tuple(delta.name for delta in self.metrics if delta.meaningful)

    @property
    def regressions(self) -> tuple[str, ...]:
        """Names of the metrics that are meaningfully *worse* in the candidate."""
        return tuple(delta.name for delta in self.metrics if delta.regressed)

    @property
    def cost_per_lead_delta(self) -> Decimal | None:
        """Candidate cost per lead minus the baseline's, or ``None`` if either is absent."""
        before, after = self.baseline.cost_usd_per_lead, self.candidate.cost_usd_per_lead
        return None if before is None or after is None else after - before

    @property
    def p95_latency_delta_ms(self) -> int | None:
        """Candidate p95 minus the baseline's, or ``None`` if either is absent."""
        before, after = self.baseline.latency_p95_ms, self.candidate.latency_p95_ms
        return None if before is None or after is None else after - before

    @property
    def verdict(self) -> str:
        """One line: what, if anything, this comparison established.

        Two different claims, kept apart on purpose. A *rate* that did not move further
        than the set can resolve is not evidence of anything. An individual case that got
        worse is still evidence about that case — a lead the pipeline used to keep and now
        bins is a fact, whatever the denominator says — so it is reported even when the
        rate it sits inside is noise. Collapsing the two into "no change" is how a real
        regression gets filed under sampling error.
        """
        if self.any_meaningful:
            headline = (
                f"Moved beyond the noise floor: {', '.join(self.meaningful_metrics)} "
                f"({self.noise.describe()})."
            )
        else:
            headline = (
                f"No metric moved further than this set can resolve "
                f"({self.noise.describe()}), so no rate here is a finding."
            )
        worse = self.cases.regressions
        if not worse:
            return headline
        return (
            f"{headline} Read the cases anyway: "
            f"{len(worse)} shared case(s) got worse "
            f"({', '.join(change.case_id for change in worse)}). A single case is evidence "
            f"about that case even when it is not evidence about the rate."
        )

    def as_json(self) -> dict[str, Any]:
        """The machine-readable comparison document."""
        return {
            "baseline": self.baseline.as_json(),
            "candidate": self.candidate.as_json(),
            "noise_floor": self.noise.as_json(),
            "metrics": [delta.as_json() for delta in self.metrics],
            "cost_usd_per_lead_delta": None
            if self.cost_per_lead_delta is None
            else str(self.cost_per_lead_delta),
            "p95_latency_delta_ms": self.p95_latency_delta_ms,
            "cases": self.cases.as_json(),
            "warnings": list(self.warnings),
            "any_meaningful": self.any_meaningful,
            "regressions": list(self.regressions),
            "verdict": self.verdict,
            "caveat": HEADLINE_CAVEAT,
        }


def _metric_noise(snapshots: Sequence[ResultSnapshot], metric: str) -> NoiseFloor:
    """The floor for one metric, sized by that metric's own denominators.

    Precision on hot is measured over the leads predicted hot, which can be three of
    fifteen; sizing its floor by the whole set would claim a sensitivity it does not have.
    """
    denominators = [
        snapshot.metrics[metric].denominator
        for snapshot in snapshots
        if metric in snapshot.metrics and snapshot.metrics[metric].defined
    ]
    spreads = [
        spread
        for snapshot in snapshots
        if (spread := snapshot.metric_spreads.get(metric)) is not None
    ]
    return NoiseFloor(
        cases=min(denominators) if denominators else 0,
        observed_spread_pp=max(spreads) * 100.0 if spreads else None,
        repeats=max((snapshot.repeats for snapshot in snapshots), default=1),
    )


def _widest_spread(snapshots: Sequence[ResultSnapshot]) -> float | None:
    spreads = [
        spread
        for snapshot in snapshots
        for name in COMPARED_METRICS
        if (spread := snapshot.metric_spreads.get(name)) is not None
    ]
    return max(spreads) * 100.0 if spreads else None


def _attribution(baseline: ResultSnapshot, candidate: ResultSnapshot) -> tuple[str, ...]:
    """Everything that differs between two runs besides the numbers.

    A delta caused by three simultaneous changes is not evidence about any one of them.
    The tool still shows the delta — refusing would make the diff useless exactly when it
    is needed — but it lists every cause it can see, so nobody attributes the movement to
    the one change they happen to remember making.
    """
    warnings: list[str] = []
    for field_name, before, after in (
        ("prompt_version", baseline.prompt_version, candidate.prompt_version),
        ("model_id", baseline.model_id, candidate.model_id),
        ("tenant_id", baseline.tenant_id, candidate.tenant_id),
        ("git_sha", baseline.git_sha, candidate.git_sha),
        ("effort", baseline.effort, candidate.effort),
    ):
        if before != after:
            warnings.append(
                f"{field_name} differs: {before} -> {after}. Any movement below is caused "
                f"by this and by everything else in this list at once."
            )
    if baseline.golden_set_path != candidate.golden_set_path:
        warnings.append(
            f"golden_set differs: {baseline.golden_set_path} -> {candidate.golden_set_path}. "
            f"These are two experiments, not two measurements."
        )
    for snapshot in (baseline, candidate):
        if snapshot.git_dirty:
            warnings.append(
                f"{snapshot.label} ran from a dirty working tree, so it is not reproducible "
                f"from git_sha {snapshot.git_sha[:7]} alone."
            )
    diff = CaseSetDiff.of(baseline, candidate)
    if not diff.comparable:
        warnings.append(
            f"the case set differs: {len(diff.only_in_baseline)} case(s) only in "
            f"{baseline.label}, {len(diff.only_in_candidate)} only in {candidate.label}. "
            f"Metric denominators are therefore not the same population."
        )
    if baseline.real_cases == 0 and candidate.real_cases == 0:
        warnings.append(
            f"the golden set carries no real leads at all ({baseline.cases_total} synthetic "
            f"cases): every number here measures agreement with the person who wrote them."
        )
    return tuple(warnings)


# ------------------------------------------------------------------- comparing a sweep


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Whether the run supports picking an effort level, and why or why not.

    ``level`` is ``None`` far more often than it is not, and that is the correct behaviour
    rather than a limitation: naming a winner is a claim about evidence, and most runs of
    this harness do not have any.
    """

    level: str | None
    rationale: str

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {"level": self.level, "rationale": self.rationale}


@dataclass(frozen=True, slots=True)
class SweepComparison:
    """One eval result per effort level, lined up against a baseline level."""

    snapshots: tuple[ResultSnapshot, ...]
    baseline: ResultSnapshot
    comparisons: tuple[Comparison, ...]
    noise: NoiseFloor

    @classmethod
    def of(cls, snapshots: Sequence[ResultSnapshot], *, baseline: str) -> Self:
        """Assemble a sweep, ordered by spend.

        Raises:
            ComparisonError: no snapshots, or the baseline level was not among them —
                comparing against a level that was never run would silently pick a
                different reference point than the caller asked for.
        """
        if not snapshots:
            raise ComparisonError("a sweep needs at least one result to compare")
        ordered = tuple(sorted(snapshots, key=lambda item: (item.effort_rank, item.effort)))
        reference = next((item for item in ordered if item.effort == baseline), None)
        if reference is None:
            ran = ", ".join(item.effort for item in ordered)
            raise ComparisonError(
                f"baseline effort {baseline!r} was not one of the levels run ({ran})"
            )
        return cls(
            snapshots=ordered,
            baseline=reference,
            comparisons=tuple(
                Comparison.of(reference, item) for item in ordered if item is not reference
            ),
            noise=NoiseFloor(
                cases=min(item.cases_total for item in ordered),
                observed_spread_pp=_widest_spread(ordered),
                repeats=max(item.repeats for item in ordered),
            ),
        )

    @property
    def distinguishable(self) -> bool:
        """Whether any level differs from the baseline by more than the noise floor."""
        return any(comparison.any_meaningful for comparison in self.comparisons)

    @property
    def verdict(self) -> str:
        """The sentence the whole sweep comes down to."""
        if not self.distinguishable:
            return VERDICT_INDISTINGUISHABLE
        parts = [
            f"{comparison.candidate.effort} differs from {self.baseline.effort} on "
            f"{', '.join(comparison.meaningful_metrics)}"
            for comparison in self.comparisons
            if comparison.any_meaningful
        ]
        return f"Some levels separate on this set: {'; '.join(parts)}."

    def recommendation(self) -> Recommendation:
        """The cheapest level the evidence supports, or nothing, with the reason.

        Three gates, in order, and the first one that closes ends it:

        1. **No real cases.** Every number came from leads the labeler invented, so the
           cheapest level "holding accuracy" means the cheapest level that agrees with one
           person's imagination. Picking on that basis is the failure this issue exists to
           prevent, and no arrangement of the numbers opens this gate.
        2. **Nothing separated.** Indistinguishable is not equivalent: the set failed to
           detect a difference, which is not the same as there being none. Treating it as
           licence to take the cheapest option is exactly the coin flip.
        3. Otherwise the cheapest level with no meaningful regression against the
           baseline, named together with the metrics that decided it.
        """
        if self.baseline.real_cases == 0:
            return Recommendation(
                level=None,
                rationale=(
                    f"{NO_RECOMMENDATION} The golden set holds no real leads - all "
                    f"{self.baseline.cases_total} cases are synthetic and self-labeled, so "
                    f"every number above measures agreement with their author. Choosing an "
                    f"effort level on this evidence optimises for that agreement, not for "
                    f"revenue. Promote real leads first (docs/labeling-golden-set.md), then "
                    f"re-run this sweep."
                ),
            )
        if not self.distinguishable:
            return Recommendation(
                level=None,
                rationale=(
                    f"{NO_RECOMMENDATION} {VERDICT_INDISTINGUISHABLE} "
                    f"{self.noise.describe()}. Failing to detect a difference is not the "
                    f"same as showing there is none, so the cheaper level has not been "
                    f"shown to hold accuracy - it has been shown to be untested. Grow the "
                    f"set to at least {cases_needed_for(5.0)} cases to resolve 5 points."
                ),
            )
        for snapshot in self.snapshots:
            if snapshot is self.baseline:
                return Recommendation(
                    level=snapshot.effort,
                    rationale=(
                        f"{snapshot.effort} is the cheapest level with no meaningful "
                        f"regression against the baseline."
                    ),
                )
            comparison = self._comparison_for(snapshot.effort)
            if comparison is not None and not comparison.regressions:
                return Recommendation(
                    level=snapshot.effort,
                    rationale=(
                        f"{snapshot.effort} is the cheapest level whose metrics show no "
                        f"regression against {self.baseline.effort} larger than this set "
                        f"can resolve ({self.noise.describe()}). Metrics that separated: "
                        f"{', '.join(comparison.meaningful_metrics) or 'none'}."
                    ),
                )
        return Recommendation(
            level=self.baseline.effort,
            rationale=(
                f"every cheaper level regressed meaningfully against "
                f"{self.baseline.effort}; the baseline stands."
            ),
        )

    def _comparison_for(self, effort: str) -> Comparison | None:
        return next((item for item in self.comparisons if item.candidate.effort == effort), None)

    def as_json(self) -> dict[str, Any]:
        """The machine-readable sweep document."""
        return {
            "baseline_effort": self.baseline.effort,
            "levels": [snapshot.as_json() for snapshot in self.snapshots],
            "noise_floor": self.noise.as_json(),
            "comparisons": [comparison.as_json() for comparison in self.comparisons],
            "distinguishable": self.distinguishable,
            "verdict": self.verdict,
            "recommendation": self.recommendation().as_json(),
            "caveat": HEADLINE_CAVEAT,
        }


# ------------------------------------------------------------------------- rendering


def render_comparison(comparison: Comparison) -> str:
    """The text a human reads when diffing two saved runs."""
    baseline, candidate = comparison.baseline, comparison.candidate
    lines = [
        _RULE,
        f"LeadQuali eval diff - {baseline.label} -> {candidate.label}",
        _RULE,
        f"baseline   {baseline.label}  effort {baseline.effort}  prompt "
        f"{baseline.prompt_version}  commit {baseline.git_sha[:7]}  {baseline.started_at}",
        f"candidate  {candidate.label}  effort {candidate.effort}  prompt "
        f"{candidate.prompt_version}  commit {candidate.git_sha[:7]}  {candidate.started_at}",
        "",
        "READ THIS BEFORE QUOTING ANY NUMBER BELOW",
        _wrap(HEADLINE_CAVEAT),
        "",
        *_resolution_section(comparison.noise),
        _THIN,
        "METRIC DELTAS",
        _THIN,
    ]
    lines.extend(f"  {delta.text}" for delta in comparison.metrics)
    lines.extend(["", *_cost_lines(comparison), "", *_case_section(comparison.cases)])
    if comparison.warnings:
        lines.extend([_THIN, "ATTRIBUTION - what else differs between these two runs", _THIN])
        lines.extend(f"  - {_wrap(warning, indent=4).strip()}" for warning in comparison.warnings)
        lines.append("")
    lines.extend([_THIN, "VERDICT", _THIN, _wrap(comparison.verdict, indent=2), ""])
    return "\n".join(lines) + "\n"


def _resolution_section(noise: NoiseFloor) -> list[str]:
    """What the set could have detected. Printed before any delta, never after."""
    resolution = noise.resolution_pp
    stated = None if resolution is None else MIN_MEANINGFUL_CASES * resolution
    lines = [
        _THIN,
        "WHAT THIS SET CAN RESOLVE",
        _THIN,
        f"  Cases                               {noise.cases}",
    ]
    if resolution is None or stated is None or noise.floor_pp is None:
        lines.extend(["  No cases, so no difference is detectable at all.", ""])
        return lines
    lines.extend(
        [
            f"  Smallest representable difference   {resolution:.1f}pp (one case)",
            f"  Stated minimum detectable difference {stated:.1f}pp ({MIN_MEANINGFUL_CASES} cases)",
            f"  Effective noise floor               {noise.floor_pp:.1f}pp",
            f"  {_wrap(noise.describe(), indent=2).strip()}",
            _wrap(
                "The floor is a stated convention plus, where it was measured, the observed "
                "run-to-run spread. It is not a significance test and must never be quoted "
                "as one: with cases invented and labeled by one person there is no sampling "
                "model that would justify a p-value.",
                indent=2,
            ),
            _wrap(
                f"To resolve 5.0pp you would need at least {cases_needed_for(5.0)} cases; "
                f"2.0pp needs {cases_needed_for(2.0)}.",
                indent=2,
            ),
        ]
    )
    if p95_is_max(noise.cases):
        lines.append(
            _wrap(
                f"p95 latency over {noise.cases} cases is the slowest single call, not a "
                f"tail estimate: nearest-rank p95 selects the maximum at this set size.",
                indent=2,
            )
        )
    lines.append("")
    return lines


def _cost_lines(comparison: Comparison) -> list[str]:
    cost = comparison.cost_per_lead_delta
    latency = comparison.p95_latency_delta_ms
    return [
        _THIN,
        "COST AND LATENCY",
        _THIN,
        f"  cost per lead              {_money(comparison.baseline.cost_usd_per_lead):>16} -> "
        f"{_money(comparison.candidate.cost_usd_per_lead):>16}  "
        f"{'n/a' if cost is None else f'{cost:+.6f} USD'}",
        f"  p95 latency                {_ms(comparison.baseline.latency_p95_ms):>16} -> "
        f"{_ms(comparison.candidate.latency_p95_ms):>16}  "
        f"{'n/a' if latency is None else f'{latency:+d}ms'}",
        f"  failures                   {comparison.baseline.failures:>16} -> "
        f"{comparison.candidate.failures:>16}",
    ]


def _case_section(diff: CaseSetDiff) -> list[str]:
    lines = [_THIN, "CASES", _THIN]
    if diff.only_in_baseline:
        lines.append("  only in the baseline: " + ", ".join(diff.only_in_baseline))
    if diff.only_in_candidate:
        lines.append("  only in the candidate: " + ", ".join(diff.only_in_candidate))
    if not diff.comparable:
        lines.append(
            "  the two runs did not cover the same cases, so the metric denominators above"
        )
        lines.append("  describe different populations. Compare the shared cases, not the rates.")
    if not diff.changes:
        lines.extend([f"  No case changed tier across the {len(diff.shared)} shared cases.", ""])
        return lines
    lines.append(f"  {len(diff.changes)} of {len(diff.shared)} shared cases changed tier:")
    for change in diff.changes:
        marker = "WORSE" if change.regressed else ("better" if change.improved else "moved")
        lines.append(
            f"    {marker:<7}{change.case_id:<40} label {change.expected_tier:<13} "
            f"{change.baseline_tier} -> {change.candidate_tier}"
        )
    lines.append("")
    return lines


def render_sweep(sweep: SweepComparison) -> str:
    """The one comparison a sweep produces: every level, side by side, with the deltas."""
    baseline = sweep.baseline
    labels = [snapshot.effort for snapshot in sweep.snapshots]
    agreement = (
        "unmeasured"
        if baseline.inter_labeler_agreement is None
        else f"{baseline.inter_labeler_agreement:.2f}"
    )
    lines = [
        _RULE,
        f"LeadQuali effort sweep - {', '.join(labels)}",
        _RULE,
        f"model {baseline.model_id}  prompt {baseline.prompt_version}  tenant "
        f"{baseline.tenant_id}  commit {baseline.git_sha[:7]}",
        f"golden set {baseline.golden_set_path}",
        f"{baseline.cases_total} cases per level "
        f"({baseline.real_cases} real, {baseline.synthetic_cases} synthetic); "
        f"inter-labeler agreement {agreement}",
        "",
        "READ THIS BEFORE QUOTING ANY NUMBER BELOW",
        _wrap(HEADLINE_CAVEAT),
        "",
        *_resolution_section(sweep.noise),
        *_level_table(sweep),
        *_delta_section(sweep, shared=_shared_warnings(sweep)),
        *_shared_warning_section(_shared_warnings(sweep)),
        *_verdict_section(sweep),
    ]
    return "\n".join(lines) + "\n"


def _shared_warnings(sweep: SweepComparison) -> tuple[str, ...]:
    """Attribution that applies to every level, hoisted out of the per-level blocks.

    "The working tree was dirty" and "the golden set has no real leads" are facts about the
    sweep, not about ``low``. Repeated under each level they are wallpaper, and wallpaper
    is what a reader learns to skip.
    """
    if not sweep.comparisons:
        return ()
    shared = set(sweep.comparisons[0].warnings)
    for comparison in sweep.comparisons[1:]:
        shared &= set(comparison.warnings)
    return tuple(warning for warning in sweep.comparisons[0].warnings if warning in shared)


def _shared_warning_section(warnings: Sequence[str]) -> list[str]:
    if not warnings:
        return []
    lines = [_THIN, "ATTRIBUTION - true of every level in this sweep", _THIN]
    lines.extend(f"  - {_wrap(warning, indent=4).strip()}" for warning in warnings)
    lines.append("")
    return lines


def _level_table(sweep: SweepComparison) -> list[str]:
    width = 18
    header = "".join(
        f"{snapshot.effort + ('*' if snapshot is sweep.baseline else ''):>{width}}"
        for snapshot in sweep.snapshots
    )
    lines = [_THIN, "EFFORT LEVELS SIDE BY SIDE", _THIN, f"  {'metric':<28}{header}"]
    for name in COMPARED_METRICS:
        if not all(name in snapshot.metrics for snapshot in sweep.snapshots):
            continue
        cells = "".join(f"{snapshot.metrics[name].text:>{width}}" for snapshot in sweep.snapshots)
        lines.append(f"  {name:<28}{cells}")
    rows: tuple[tuple[str, Callable[[ResultSnapshot], str]], ...] = (
        ("cost per lead", lambda item: _money(item.cost_usd_per_lead)),
        ("total spend", lambda item: _money(item.total_cost_usd)),
        ("p95 latency", lambda item: _ms(item.latency_p95_ms)),
        ("p50 latency", lambda item: _ms(item.latency_p50_ms)),
        ("failures", lambda item: str(item.failures)),
        ("escalations", lambda item: str(item.escalations)),
        ("injection findings", lambda item: str(len(item.security_finding_case_ids))),
    )
    for label, render in rows:
        cells = "".join(f"{render(snapshot):>{width}}" for snapshot in sweep.snapshots)
        lines.append(f"  {label:<28}{cells}")
    lines.extend([f"  * baseline ({sweep.baseline.effort})", ""])
    return lines


def _delta_section(sweep: SweepComparison, *, shared: Sequence[str] = ()) -> list[str]:
    lines = [_THIN, f"DIFFERENCE FROM {sweep.baseline.effort}", _THIN]
    if not sweep.comparisons:
        lines.extend(
            [
                "  Only one level was run, so there is nothing to compare it against.",
                "",
            ]
        )
        return lines
    for comparison in sweep.comparisons:
        lines.append(f"  {comparison.candidate.effort}")
        lines.extend(f"    {delta.text}" for delta in comparison.metrics)
        cost = comparison.cost_per_lead_delta
        latency = comparison.p95_latency_delta_ms
        lines.append(
            f"    {'cost per lead':<26} "
            f"{'n/a' if cost is None else f'{cost:+.6f} USD per lead'}"
            f"{'' if cost is None else f' ({_percent_change(comparison)})'}"
        )
        lines.append(f"    {'p95 latency':<26} {'n/a' if latency is None else f'{latency:+d}ms'}")
        moved = comparison.cases.changes
        lines.append(
            f"    {'cases that moved tier':<26} "
            + (", ".join(change.case_id for change in moved) if moved else "none")
        )
        for warning in comparison.warnings:
            if warning.startswith("effort differs"):
                continue  # effort is the variable under test, not a confounder
            if warning in shared:
                continue  # reported once for the whole sweep instead
            lines.append(f"    ! {_wrap(warning, indent=6).strip()}")
        lines.append("")
    return lines


def _verdict_section(sweep: SweepComparison) -> list[str]:
    recommendation = sweep.recommendation()
    lines = [_THIN, "VERDICT", _THIN, _wrap(sweep.verdict, indent=2), ""]
    if not sweep.distinguishable:
        lines.extend(
            [
                _wrap(
                    "Every difference above is at or below what this set moves by itself. "
                    "That is a statement about the set, not about the model: it means the "
                    "measurement was not sensitive enough to answer the question, so no "
                    "effort level has been shown better or worse than any other here.",
                    indent=2,
                ),
                "",
            ]
        )
    lines.extend(
        [
            "  RECOMMENDATION",
            _wrap(recommendation.rationale, indent=2),
            "",
        ]
    )
    if recommendation.level is not None:
        lines.extend([f"  Suggested effort level: {recommendation.level}", ""])
    return lines


def _percent_change(comparison: Comparison) -> str:
    before = comparison.baseline.cost_usd_per_lead
    after = comparison.candidate.cost_usd_per_lead
    if before is None or after is None or before == 0:
        return "n/a"
    return f"{(after - before) / before * 100:+.1f}%"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:.6f}"


def _ms(value: int | None) -> str:
    return "n/a" if value is None else f"{value}ms"


def _wrap(text: str, *, indent: int = 0, width: int = 92) -> str:
    """Wrap prose to the report width, matching ``tests.evals.report``."""
    prefix = " " * indent
    words = text.split()
    if not words:
        return prefix
    lines: list[str] = []
    current = prefix
    for word in words:
        candidate = f"{current}{word}" if current == prefix else f"{current} {word}"
        if len(candidate) > width and current != prefix:
            lines.append(current)
            current = f"{prefix}{word}"
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


__all__ = [
    "COMPARED_METRICS",
    "MIN_MEANINGFUL_CASES",
    "NO_RECOMMENDATION",
    "VERDICT_INDISTINGUISHABLE",
    "VERDICT_MEANINGFUL",
    "VERDICT_NOT_COMPARABLE",
    "VERDICT_WITHIN_NOISE",
    "CaseChange",
    "CaseSetDiff",
    "CaseSnapshot",
    "Comparison",
    "ComparisonError",
    "MetricDelta",
    "NoiseFloor",
    "RatioSnapshot",
    "Recommendation",
    "ResultSnapshot",
    "SweepComparison",
    "cases_needed_for",
    "load_result",
    "noise_floor",
    "p95_is_max",
    "parse_result",
    "render_comparison",
    "render_sweep",
]
