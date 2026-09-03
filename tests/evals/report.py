"""What an eval run produced, rendered twice: for a person, and for #24.

One assembled value (:class:`EvalRun`) and two renderings of it. They are in the same
module on purpose — the text a human reads and the JSON the next tool diffs must describe
the same run, and keeping them apart is how a report ends up quoting a number the file
does not contain.

**The JSON is a contract.** #24 sweeps effort levels by running this harness twice and
diffing the results, so key names and the shape of a metric (``numerator`` /
``denominator`` / ``value``, never a bare float) are stable, and every collection in it is
sorted by ``case_id``. :data:`RESULT_SCHEMA_VERSION` is bumped if that ever has to change.

**The caveat travels with every number.** Both renderings print
:attr:`~tests.evals.metrics.SegmentMetrics.caveat` next to each block of metrics, and the
document carries the golden set's own summary at the top. The seed set is entirely
synthetic (``docs/labeling-golden-set.md`` §0), so an accuracy figure from it measures
whether the model agrees with the person who invented the cases. A harness that printed
``86.7%`` without that sentence would be a machine for manufacturing false confidence, and
the sentence is therefore structural rather than editorial.

**No lead payload appears in either rendering.** Not the email address, not the message
body — a result file gets attached to a pull request (invariant 5). The label's notes and
the model's reasoning are included because reading them side by side is the whole point of
the per-case detail, and neither is ever asserted on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

from tests.evals.golden_set import GoldenSet, describe_golden_set
from tests.evals.metrics import (
    TIERS_BY_RANK,
    CaseResult,
    ConfusionMatrix,
    SecurityFinding,
    SegmentMetrics,
    StabilityReport,
    compute_segment,
    compute_stability,
    security_findings,
)

#: Version of the result document. #24 reads it before diffing two runs; a change to any
#: key below that is not purely additive bumps it.
RESULT_SCHEMA_VERSION: Final[int] = 1

#: The banner that opens every report and sits at the top of every result file. Long,
#: unmissable, and repeated per metric block by :func:`render_text_report` — because the
#: failure mode this defends against is one number being copied into a sales conversation.
HEADLINE_CAVEAT: Final[str] = (
    "These numbers are computed against the golden set as it currently stands. Any figure "
    "computed against synthetic cases measures self-consistency with whoever wrote them, "
    "not correctness: it says whether the model agrees with the seed author, not whether "
    "the rubric is right. Read docs/labeling-golden-set.md before quoting anything here, "
    "and never quote a number from this run without the sentence attached to it."
)

#: Segments broken out beside the headline. ``hard_cases`` is where a rubric earns its
#: keep, ``injection_cases`` is a security surface rather than a quality one, and the
#: real/synthetic split is the one that says what the rest is worth.
SEGMENT_NAMES: Final[tuple[str, ...]] = ("hard_cases", "injection_cases", "real", "synthetic")

_RULE: Final[str] = "=" * 92
_THIN: Final[str] = "-" * 92


@dataclass(frozen=True, slots=True)
class RunMetadata:
    """Provenance of one run: what was run, against what, and from which commit.

    Every field here is something two comparable runs must agree on. A pair of runs whose
    ``prompt_version`` or ``git_sha`` differ are not a regression signal, they are two
    different experiments, and #24 refuses to read them as one.
    """

    started_at: datetime
    finished_at: datetime
    git_sha: str
    git_dirty: bool
    """``True`` when the working tree had uncommitted changes: the run is then not
    reproducible from ``git_sha`` alone, and the report says so out loud."""

    model_id: str
    prompt_version: str
    effort: str
    tenant_id: str
    concurrency: int
    repeats: int
    golden_set_path: str

    @property
    def duration_seconds(self) -> float:
        """Wall clock for the whole run, including the concurrency the harness used."""
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def revision(self) -> str:
        """Short commit, marked when the tree was dirty."""
        return f"{self.git_sha[:7]}{'+dirty' if self.git_dirty else ''}"

    def as_json(self) -> dict[str, Any]:
        """The machine-readable form."""
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "effort": self.effort,
            "tenant_id": self.tenant_id,
            "concurrency": self.concurrency,
            "repeats": self.repeats,
            "golden_set_path": self.golden_set_path,
        }


@dataclass(frozen=True, slots=True)
class EvalRun:
    """One run, assembled: the results, everything derived from them, and its provenance."""

    metadata: RunMetadata
    golden: GoldenSet
    results: tuple[CaseResult, ...]
    """The primary repeat, sorted by ``case_id`` so two runs diff line for line."""

    repeats: tuple[tuple[CaseResult, ...], ...]
    headline: SegmentMetrics
    segments: Mapping[str, SegmentMetrics]
    confusion: ConfusionMatrix
    stability: StabilityReport | None
    findings: tuple[SecurityFinding, ...]

    @classmethod
    def of(
        cls,
        metadata: RunMetadata,
        golden: GoldenSet,
        repeats: Sequence[Sequence[CaseResult]],
    ) -> Self:
        """Assemble a run from its raw per-repeat results.

        The first repeat is the primary one: its metrics are the headline, and the others
        exist to quantify how much of the difference between two runs is just sampling.

        Raises:
            ValueError: ``repeats`` is empty. A run with no repeats is a bug, not an
                empty result — the caller has lost the results it collected.
        """
        if not repeats:
            raise ValueError("an eval run needs at least one repeat of results")
        ordered = tuple(tuple(sorted(run, key=lambda item: item.case_id)) for run in repeats)
        primary = ordered[0]
        return cls(
            metadata=metadata,
            golden=golden,
            results=primary,
            repeats=ordered,
            headline=compute_segment("all", primary),
            segments={
                "hard_cases": compute_segment(
                    "hard_cases", [case for case in primary if case.hard_case]
                ),
                "injection_cases": compute_segment(
                    "injection_cases", [case for case in primary if case.is_injection]
                ),
                "real": compute_segment(
                    "real", [case for case in primary if not case.is_synthetic]
                ),
                "synthetic": compute_segment(
                    "synthetic", [case for case in primary if case.is_synthetic]
                ),
            },
            confusion=ConfusionMatrix.of(primary),
            stability=compute_stability(ordered),
            findings=security_findings(primary),
        )


def as_json(run: EvalRun) -> dict[str, Any]:
    """The result document. This shape is what #24 diffs between two runs."""
    golden = run.golden
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "caveat": HEADLINE_CAVEAT,
        "run": run.metadata.as_json(),
        "golden_set": {
            "path": str(golden.path),
            "counts": dict(golden.counts()),
            "inter_labeler_agreement": golden.inter_labeler_agreement,
            "meets_acceptance_criteria": golden.meets_acceptance_criteria,
            "acceptance_gaps": list(golden.acceptance_gaps),
            "summary": describe_golden_set(golden),
        },
        "metrics": run.headline.as_json(),
        "segments": {name: run.segments[name].as_json() for name in SEGMENT_NAMES},
        "confusion_matrix": run.confusion.as_json(),
        "stability": None if run.stability is None else run.stability.as_json(),
        "security_findings": [finding.as_json() for finding in run.findings],
        "cases": [_case_json(case) for case in run.results],
    }


def dumps(run: EvalRun) -> str:
    """The result document as text: two-space indent, trailing newline.

    Keys are emitted in the order the builders above insert them, which is fixed and
    meaningful — recall before precision, tiers best-first — rather than sorted, which
    would put ``cold`` above ``hot`` in the confusion matrix for the same reason ``Tier``
    needed a ``rank``. Insertion order is deterministic, so two runs still diff line for
    line, which is the only reason this file exists.
    """
    return json.dumps(as_json(run), indent=2) + "\n"


def _case_json(case: CaseResult) -> dict[str, Any]:
    """One case. ``record`` is #13's ``--json`` record, carried verbatim."""
    return {
        "case_id": case.case_id,
        "provenance": case.provenance.value,
        "hard_case": case.hard_case,
        "injection_case_id": case.injection_case_id,
        "tags": list(case.tags),
        "expected_tier": case.expected_tier.value,
        "expected_band": {"lower": case.lower_bound.value, "upper": case.upper_bound.value},
        "predicted_tier": case.predicted_tier.value,
        "status": case_status(case),
        "exact_match": case.exact_match,
        "adjacent_match": case.adjacent_match,
        "within_band": case.within_band,
        "expected_contactable": case.expected_contactable,
        "predicted_contactable": case.predicted_contactable,
        "false_disqualification": case.false_disqualification,
        "assessed": case.assessed,
        "escalated": case.escalated,
        "escalation_reason": None
        if case.escalation_reason is None
        else case.escalation_reason.value,
        "expect_escalation": case.expect_escalation,
        "missing_expected_escalation": case.missing_expected_escalation,
        "dimension_range_violations": list(case.dimension_range_violations),
        "extracted_mismatches": list(case.extracted_mismatches),
        "cost_usd": str(case.cost_usd),
        "latency_ms": case.latency_ms,
        "labelers": list(case.labelers),
        "label_notes": case.label_notes,
        "model_reasoning": case.model_reasoning,
        "record": dict(case.record),
    }


#: Per-case verdicts, worst first. ``LOST`` is separate from ``MISS`` because it is the
#: only one that costs money: a lead a human would have called and the pipeline binned.
STATUS_LOST: Final[str] = "LOST"
STATUS_FAILED: Final[str] = "FAILED"
STATUS_MISS: Final[str] = "MISS"
STATUS_BAND: Final[str] = "band"
STATUS_OK: Final[str] = "ok"


def case_status(case: CaseResult) -> str:
    """One word for how this case went, worst-first: LOST, FAILED, MISS, band, ok."""
    if case.false_disqualification:
        return STATUS_LOST
    if not case.assessed:
        return STATUS_FAILED
    if case.exact_match:
        return STATUS_OK
    if case.within_band:
        return STATUS_BAND
    return STATUS_MISS


def needs_detail(case: CaseResult) -> bool:
    """Whether this case is worth printing the notes and the reasoning for.

    Everything that did not land exactly on the label, plus anything that escalated, plus
    anything whose declared expectations were violated. A case the model got right teaches
    nobody anything, and printing fifteen of them buries the four that matter.
    """
    return (
        case_status(case) != STATUS_OK
        or case.escalated
        or bool(case.dimension_range_violations)
        or bool(case.extracted_mismatches)
        or case.missing_expected_escalation
    )


def render_text_report(run: EvalRun, *, detail_for_every_case: bool = False) -> str:
    """The report a human reads, and what the CI job posts to its run summary."""
    lines: list[str] = []
    lines.extend(_header(run))
    lines.extend(_headline_section(run))
    lines.extend(_accuracy_section(run))
    lines.extend(_cost_section(run))
    lines.extend(_confusion_section(run))
    lines.extend(_segments_section(run))
    lines.extend(_findings_section(run))
    lines.extend(_stability_section(run))
    lines.extend(_case_table(run, detail_for_every_case=detail_for_every_case))
    return "\n".join(lines) + "\n"


def _header(run: EvalRun) -> list[str]:
    meta = run.metadata
    return [
        _RULE,
        f"LeadQuali eval - {meta.started_at.isoformat(timespec='seconds')}",
        _RULE,
        f"model {meta.model_id}  prompt {meta.prompt_version}  effort {meta.effort}  "
        f"tenant {meta.tenant_id}",
        f"commit {meta.revision}  concurrency {meta.concurrency}  repeats {meta.repeats}  "
        f"duration {meta.duration_seconds:.1f}s",
        f"golden set {meta.golden_set_path}",
        "",
        _wrap(describe_golden_set(run.golden)),
        "",
        "READ THIS BEFORE QUOTING ANY NUMBER BELOW",
        _wrap(HEADLINE_CAVEAT),
        "",
    ]


def _headline_section(run: EvalRun) -> list[str]:
    """Recall first, and on its own, because it is the number that costs money."""
    metrics = run.headline
    lines = [
        _THIN,
        "THE NUMBER THAT COSTS MONEY",
        _THIN,
        f"  Recall on contactable (hot+warm)   {metrics.recall_on_contactable.text}",
        "  = of the leads a human said should have been contacted, the share the",
        "    pipeline actually surfaced. Every miss below is a deal nobody called.",
    ]
    if metrics.false_disqualified_case_ids:
        lines.append(
            f"  FALSE DISQUALIFICATIONS ({len(metrics.false_disqualified_case_ids)}): "
            + ", ".join(metrics.false_disqualified_case_ids)
        )
    else:
        lines.append("  False disqualifications: none in this run.")
    lines.extend(["  " + _wrap(metrics.caveat, indent=4).strip(), ""])
    return lines


def _accuracy_section(run: EvalRun) -> list[str]:
    metrics = run.headline
    return [
        _THIN,
        "PRECISION AND CALIBRATION",
        _THIN,
        f"  Precision on hot (exact label)     {metrics.precision_on_hot.text}",
        f"  Precision on hot (within band)     {metrics.precision_on_hot_within_band.text}",
        f"  Tier accuracy, exact match         {metrics.exact_tier_accuracy.text}",
        f"  Tier accuracy, adjacent tier       {metrics.adjacent_tier_accuracy.text}",
        f"  Inside the label's accepted band   {metrics.within_band_accuracy.text}",
        "  " + _wrap(metrics.caveat, indent=4).strip(),
        "",
    ]


def _cost_section(run: EvalRun) -> list[str]:
    metrics = run.headline
    per_lead = "n/a" if metrics.cost_usd_per_lead is None else f"${metrics.cost_usd_per_lead:.6f}"
    latency = metrics.latency
    lines = [
        _THIN,
        "COST AND LATENCY",
        _THIN,
        f"  Total spend                        ${metrics.total_cost_usd:.4f} "
        f"over {metrics.cases} leads",
        f"  Cost per lead                      {per_lead}",
        f"  Latency p50 / p95 / max            {_ms(latency.p50_ms)} / {_ms(latency.p95_ms)} "
        f"/ {_ms(latency.max_ms)}",
        f"  Assessed / failed                  {metrics.assessed} / {metrics.failures}",
    ]
    if metrics.failures:
        lines.append(
            "  Failures route through system_failure to WARM and are reported as escalations,"
        )
        lines.append(
            "  exactly as production would: they lower accuracy without being a prompt regression."
        )
    if metrics.escalations_by_reason:
        lines.append(
            "  Escalations                        "
            + ", ".join(
                f"{reason} {count}" for reason, count in metrics.escalations_by_reason.items()
            )
        )
    if metrics.missing_expected_escalations:
        lines.append(
            "  Labeled expect_escalation but did not escalate: "
            + ", ".join(metrics.missing_expected_escalations)
        )
    lines.append("")
    return lines


def _confusion_section(run: EvalRun) -> list[str]:
    header = "".join(f"{tier.value[:4]:>6}" for tier in TIERS_BY_RANK)
    lines = [
        _THIN,
        "CONFUSION MATRIX  (rows = human label, columns = pipeline)",
        _THIN,
        f"  {'label':<14}{header}",
    ]
    for expected in TIERS_BY_RANK:
        row = run.confusion.counts[expected]
        cells = "".join(f"{row[predicted]:>6}" for predicted in TIERS_BY_RANK)
        lines.append(f"  {expected.value:<14}{cells}")
    lines.append("")
    return lines


def _segments_section(run: EvalRun) -> list[str]:
    lines = [_THIN, "SEGMENTS", _THIN]
    for name in SEGMENT_NAMES:
        metrics = run.segments[name]
        lines.append(f"  {name} ({metrics.cases} cases)")
        if metrics.cases == 0:
            lines.append(f"    {metrics.caveat}")
            lines.append("")
            continue
        lines.extend(
            [
                f"    recall on contactable   {metrics.recall_on_contactable.text}",
                f"    precision on hot        {metrics.precision_on_hot.text}",
                f"    exact tier accuracy     {metrics.exact_tier_accuracy.text}",
                f"    adjacent tier accuracy  {metrics.adjacent_tier_accuracy.text}",
                f"    failures / escalations  {metrics.failures} / {metrics.escalations}",
                "    " + _wrap(metrics.caveat, indent=4).strip(),
                "",
            ]
        )
    return lines


def _findings_section(run: EvalRun) -> list[str]:
    lines = [_THIN, "PROMPT-INJECTION FINDINGS", _THIN]
    if not run.findings:
        lines.extend(
            [
                "  None. No injection case was scored above the ceiling its corpus entry",
                "  declares, and none was scored hot.",
                "",
            ]
        )
        return lines
    lines.append(
        f"  {len(run.findings)} finding(s). These are security results, not metrics: an attack that"
    )
    lines.append("  raises a lead's tier is a defect regardless of what the accuracy says.")
    for finding in run.findings:
        lines.append(
            f"  - {finding.case_id} [{finding.injection_case_id}] "
            f"predicted {finding.predicted_tier.value}, "
            f"ceiling {finding.upper_bound.value}: {', '.join(finding.kinds)}"
        )
        lines.append(f"      {finding.summary}")
    lines.append("")
    return lines


def _stability_section(run: EvalRun) -> list[str]:
    lines = [_THIN, "NONDETERMINISM", _THIN]
    stability = run.stability
    if stability is None:
        lines.extend(
            [
                "  Not measured: this was a single run. The model samples, so a case near a",
                "  threshold can land either side of it between two identical runs. Use",
                "  --repeat 2 (or more) to quantify it rather than assuming it away.",
                "",
            ]
        )
        return lines
    lines.append(f"  Repeats                     {stability.repeats}")
    lines.append(f"  Cases with a stable tier    {stability.tier_stability.text}")
    if stability.unstable_case_ids:
        lines.append("  Moved between repeats:      " + ", ".join(stability.unstable_case_ids))
    for name, spread in stability.metric_spreads.items():
        rendered = ", ".join("n/a" if value is None else f"{value:.4f}" for value in spread.values)
        gap = "n/a" if spread.spread is None else f"{spread.spread:.4f}"
        lines.append(f"  {name:<27} spread {gap}  [{rendered}]")
    lines.append("")
    return lines


def _case_table(run: EvalRun, *, detail_for_every_case: bool) -> list[str]:
    lines = [
        _THIN,
        "PER-CASE RESULTS  (sorted by case id; LOST = a lead a human would have called)",
        _THIN,
        f"  {'status':<7}{'case_id':<40}{'label':>13} {'pipeline':>13}{'score':>8}"
        f"{'ms':>8}{'usd':>10}",
    ]
    for case in run.results:
        lines.append(
            f"  {case_status(case):<7}{case.case_id[:39]:<40}"
            f"{case.expected_tier.value:>13} {case.predicted_tier.value:>13}"
            f"{case.total_score:>8.1f}{case.latency_ms:>8}{float(case.cost_usd):>10.5f}"
        )
    lines.append("")

    detailed = [case for case in run.results if detail_for_every_case or needs_detail(case)]
    if not detailed:
        lines.extend(["  Every case landed exactly on its label.", ""])
        return lines
    lines.extend(
        [
            _THIN,
            "CASE DETAIL  (the label's reasoning beside the model's; neither is asserted on)",
            _THIN,
        ]
    )
    for case in detailed:
        lines.extend(_case_detail(case))
    return lines


def _case_detail(case: CaseResult) -> list[str]:
    band = (
        ""
        if case.lower_bound is case.upper_bound
        else f"  accepted band {case.lower_bound.value}..{case.upper_bound.value}"
    )
    lines = [
        f"  [{case_status(case)}] {case.case_id}"
        f"{'  (hard case)' if case.hard_case else ''}"
        f"{'  (injection: ' + str(case.injection_case_id) + ')' if case.is_injection else ''}",
        f"      label {case.expected_tier.value} -> pipeline {case.predicted_tier.value}"
        f"  score {case.total_score:.1f}{band}",
    ]
    if case.confidence is not None:
        lines.append(f"      model confidence {case.confidence:.2f}")
    if case.escalation_reason is not None:
        lines.append(f"      escalated: {case.escalation_reason.value}")
    if not case.assessed:
        lines.append(f"      no assessment: {case.failure_detail}")
    if case.note:
        lines.append(f"      decision note: {case.note}")
    for violation in case.dimension_range_violations:
        lines.append(f"      dimension out of labeled range: {violation}")
    for mismatch in case.extracted_mismatches:
        lines.append(f"      extracted field mismatch: {mismatch}")
    if case.missing_expected_escalation:
        lines.append("      the label says a human must see this lead, and it did not escalate")
    labelers = ", ".join(case.labelers) or "unattributed"
    lines.append(f"      label notes ({labelers}):")
    lines.append(_wrap(case.label_notes or "(none)", indent=10))
    lines.append("      model reasoning:")
    lines.append(_wrap(case.model_reasoning or "(none - no assessment)", indent=10))
    lines.append("")
    return lines


def _ms(value: int | None) -> str:
    return "n/a" if value is None else f"{value}ms"


def _wrap(text: str, *, indent: int = 0, width: int = 92) -> str:
    """Wrap prose to the report width. Local rather than ``textwrap`` for the indent."""
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
    "HEADLINE_CAVEAT",
    "RESULT_SCHEMA_VERSION",
    "SEGMENT_NAMES",
    "STATUS_BAND",
    "STATUS_FAILED",
    "STATUS_LOST",
    "STATUS_MISS",
    "STATUS_OK",
    "EvalRun",
    "RunMetadata",
    "as_json",
    "case_status",
    "dumps",
    "needs_detail",
    "render_text_report",
]
