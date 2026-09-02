"""``python -m leadquali.cli score lead.json`` — the Phase 1 deliverable.

The point of this command is that a person can judge qualification quality before any
infrastructure exists. It loads a tenant's configuration, renders the lead as untrusted
data, calls the model through a port, applies the deterministic decision, and prints the
result in a form a non-engineer can read — or as JSON for the eval harness (#23).

Two behaviours are load-bearing rather than cosmetic:

* **A failed assessment is a routed lead, not a CLI error.** A refusal, timeout, parse
  failure or API error exits ``0`` and prints an escalation, because that is exactly what
  the production pipeline will do with it. Exiting non-zero would teach the reader that a
  model failure means "no lead", which is the mistake invariant 3 exists to prevent.
* **The human report never prints the lead's email address.** The report is the thing that
  gets pasted into a ticket or a chat window; invariant 5 applies to it as much as to logs.
  ``--json`` is the machine path and carries the full record.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

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
from leadquali.config import get_settings
from leadquali.domain.models import RoutingDecision
from leadquali.domain.routing import decide, system_failure
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError
from leadquali.prompts.lead import LeadSubmission, render_lead_detailed

#: Exit code for a usage or input problem. Reserved so a caller can tell "you gave me a
#: bad file" apart from "the model could not assess this lead", which exits 0.
EXIT_INPUT_ERROR: Final[int] = 2

AssessorFactory = Callable[[str], LeadAssessorPort]


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m leadquali.cli",
        description="Qualify a single web-form lead and print the decision.",
    )
    subcommands = parser.add_subparsers(dest="command")
    score = subcommands.add_parser("score", help="Score one lead from a JSON file.")
    score.add_argument("lead_file", type=Path, help="Path to a JSON file holding one lead.")
    score.add_argument(
        "--tenant",
        default=DEFAULT_TENANT_ID,
        help=f"Tenant whose rubric to apply (default: {DEFAULT_TENANT_ID}).",
    )
    score.add_argument(
        "--effort",
        choices=sorted(EFFORT_LEVELS),
        default=DEFAULT_EFFORT,
        help=f"Model effort level (default: {DEFAULT_EFFORT}). Swept against the golden set.",
    )
    score.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON object instead of a human report.",
    )
    score.add_argument(
        "--tenants-dir",
        type=Path,
        default=None,
        help="Directory of tenant config files (default: the bundled tenants/).",
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, assessor_factory: AssessorFactory | None = None
) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    # `score` is the only subcommand, so allow it to be omitted for everyday use.
    if raw and raw[0] != "score" and not raw[0].startswith("-"):
        raw = ["score", *raw]
    args = parser.parse_args(raw)
    if args.command != "score":
        parser.print_help()
        return EXIT_INPUT_ERROR

    try:
        submission = _load_submission(args.lead_file)
        config = _load_config(args.tenant, args.tenants_dir)
    except (OSError, ValueError, TenantConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    factory = assessor_factory or _default_assessor_factory
    try:
        assessor = factory(args.effort)
    except RuntimeError as error:  # a missing ANTHROPIC_API_KEY, most likely
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    rendered = render_lead_detailed(submission)
    outcome = assessor.assess(config=config, rendered_lead=rendered.text)
    decision = decision_for(outcome, config)

    if args.as_json:
        print(json.dumps(_json_record(args.lead_file, config, outcome, decision), indent=2))
    else:
        print(render_report(lead_file=args.lead_file, config=config, outcome=outcome))
    return 0


def decision_for(outcome: AssessmentOutcome, config: TenantConfig) -> RoutingDecision:
    """Apply the deterministic policy to whatever came back from the model."""
    if outcome.ok:
        return decide(outcome.assessment, config)
    return system_failure(outcome.reason, outcome.detail)


def _load_submission(path: Path) -> LeadSubmission:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OSError(f"no such lead file: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must hold a JSON object, got {type(payload).__name__}")
    return LeadSubmission.from_mapping(payload)


def _load_config(tenant_id: str, tenants_dir: Path | None) -> TenantConfig:
    loader = JsonFileTenantConfigLoader(tenants_dir or default_tenants_dir())
    return loader.get(tenant_id)


def _default_assessor_factory(effort: str) -> LeadAssessorPort:
    """Build the real Anthropic assessor. Imported lazily so tests never need a key."""
    from leadquali.adapters.llm_anthropic import AnthropicLeadAssessor, build_anthropic_client

    api_key = get_settings().require_anthropic_api_key()
    checked: Effort = _checked_effort(effort)
    return AnthropicLeadAssessor(build_anthropic_client(api_key), effort=checked)


def _checked_effort(effort: str) -> Effort:
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unknown effort {effort!r}; expected one of {sorted(EFFORT_LEVELS)}")
    return effort


def _json_record(
    lead_file: Path,
    config: TenantConfig,
    outcome: AssessmentOutcome,
    decision: RoutingDecision,
) -> dict[str, Any]:
    """The machine-readable record. #23's eval harness consumes exactly this shape."""
    record: dict[str, Any] = {
        "lead_file": str(lead_file),
        "tenant": config.tenant_id,
        "assessment": None,
        "decision": decision.model_dump(mode="json"),
        "metering": None,
        "failure": None,
    }
    if isinstance(outcome, AssessmentSucceeded):
        record["assessment"] = outcome.assessment.model_dump(mode="json")
        record["metering"] = _metering_json(outcome)
    else:
        record["failure"] = {
            "reason": outcome.reason.value,
            "detail": outcome.detail,
            "latency_ms": outcome.latency_ms,
        }
    return record


def _metering_json(outcome: AssessmentSucceeded) -> dict[str, Any]:
    metering = outcome.metering
    return {
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


def render_report(
    *,
    lead_file: Path,
    config: TenantConfig,
    outcome: AssessmentOutcome,
    decision_note_only: bool = False,
) -> str:
    """The human-readable report.

    Deliberately omits the lead's email address (invariant 5) — this text is what gets
    pasted into a ticket. ``--json`` is the path for anything that needs the full record.
    """
    decision = decision_for(outcome, config)
    lines = [
        f"Lead:    {lead_file.name}",
        f"Tenant:  {config.tenant_id} ({config.name})",
        "",
        f"TIER:    {decision.tier.value.upper()}   score {decision.total_score:.2f}/100",
        f"ACTION:  {decision.action.value}"
        + (
            f" -> {config.destination_for(decision.tier)}"
            if config.destination_for(decision.tier)
            else ""
        ),
    ]
    if decision.note:
        lines.append(f"NOTE:    {decision.note}")
    if decision.escalation_reason is not None:
        lines.append(f"ESCALATED: {decision.escalation_reason.value}")
    if decision_note_only:
        return "\n".join(lines)

    if isinstance(outcome, AssessmentSucceeded):
        lines.extend(_success_sections(outcome))
    else:
        lines.extend(
            [
                "",
                "The model could not assess this lead, so it has been escalated to a human.",
                f"  reason:     {outcome.reason.value}",
                f"  detail:     {outcome.detail}",
                f"  latency_ms: {outcome.latency_ms}",
            ]
        )
    return "\n".join(lines)


def _success_sections(outcome: AssessmentSucceeded) -> list[str]:
    assessment = outcome.assessment
    scores = assessment.dimension_scores
    metering = outcome.metering
    lines = ["", "Dimension scores (raw, before tenant weighting):"]
    lines.extend(f"  {name:<14} {getattr(scores, name)}" for name in scores.__class__.model_fields)
    lines.extend(["", "Extracted facts:"])
    lines.extend(
        f"  {name:<22} {getattr(assessment.extracted, name) or '-'}"
        for name in assessment.extracted.__class__.model_fields
    )
    lines.extend(
        [
            "",
            f"Confidence: {assessment.confidence:.2f}",
            "",
            "Reasoning:",
            f"  {assessment.reasoning}",
            "",
            "Missing information:",
        ]
    )
    lines.extend(f"  - {item}" for item in assessment.missing_information or ["(none)"])
    if assessment.suggested_first_question:
        lines.extend(["", f"Suggested first question: {assessment.suggested_first_question}"])
    lines.extend(
        [
            "",
            f"Model:  {metering.model_id}  prompt {metering.prompt_version}  "
            f"effort {metering.effort}",
            f"Tokens: in {metering.input_tokens}  out {metering.output_tokens}  "
            f"cache_read {metering.cache_read_tokens}  cache_write "
            f"{metering.cache_creation_tokens}",
            f"Cost:   ${metering.cost_usd}   latency {metering.latency_ms} ms",
        ]
    )
    if metering.cache_read_tokens == 0:
        lines.append(
            "  note: cache_read is 0. On a repeat run that means the cacheable prefix moved."
        )
    return lines


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m leadquali.cli`
    raise SystemExit(main())
