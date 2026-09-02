"""The Phase 1 gate: one command, one readable qualification decision.

Every test here uses a fake assessor. The CLI's job is to load configuration, render the
lead, hand it to a port, apply the deterministic decision and present the result — none of
which needs an API key, and all of which is worth pinning. What the *model* says about a
lead is not tested here or anywhere in the unit suite; that is the golden set's job (#22).
"""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
)
from leadquali.cli import (
    JSON_SCHEMA_VERSION,
    REDACTED_EMAIL,
    build_parser,
    main,
    redact_addresses,
    render_report,
)
from leadquali.domain.models import (
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.tenant_config import TenantConfig

LEADS = Path(__file__).resolve().parents[1] / "fixtures" / "leads"


def _assessment(
    *, confidence: float = 0.9, spam: bool = False, high: bool = True
) -> LeadAssessment:
    scores = (
        DimensionScores(icp_fit=29, intent=24, authority=14, urgency=14, budget_signal=14)
        if high
        else DimensionScores(icp_fit=2, intent=1, authority=0, urgency=0, budget_signal=0)
    )
    return LeadAssessment(
        dimension_scores=scores,
        extracted=ExtractedFacts(
            company_name="Northwind Logistics",
            industry="Logistics",
            company_size_estimate="310",
            role_seniority="vp",
            stated_use_case="Qualify inbound demo requests",
            stated_timeline="six weeks",
        ),
        reasoning="States budget, timeline and ownership of the decision.",
        confidence=confidence,
        missing_information=["current CRM"],
        suggested_first_question="Which CRM do you route into today?",
        spam_or_test_submission=spam,
    )


def _metering(cache_read: int = 1500) -> CallMetering:
    return CallMetering(
        model_id="claude-opus-5",
        prompt_version="rubric_v1",
        effort="medium",
        input_tokens=2000,
        output_tokens=800,
        cache_read_tokens=cache_read,
        cache_creation_tokens=0,
        cost_usd=Decimal("0.0235"),
        latency_ms=4210,
    )


class FakeAssessor:
    """A `LeadAssessorPort` that returns a scripted outcome and records what it was sent."""

    def __init__(self, outcome: AssessmentOutcome) -> None:
        self._outcome = outcome
        self.rendered_lead: str | None = None
        self.config: TenantConfig | None = None

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        self.config = config
        self.rendered_lead = rendered_lead
        return self._outcome


def _run(
    args: list[str], outcome: AssessmentOutcome, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    assessor = FakeAssessor(outcome)
    code = main(args, assessor_factory=lambda _effort: assessor)
    return code, capsys.readouterr().out


# ------------------------------------------------------------------------ the happy path


def test_a_hot_lead_prints_a_decision_a_non_engineer_can_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    code, out = _run([str(LEADS / "hot_enterprise_buyer.json")], outcome, capsys)

    assert code == 0
    lowered = out.lower()
    for expected in ("tier", "hot", "score", "icp_fit", "reasoning", "confidence", "cost"):
        assert expected in lowered
    assert "Northwind Logistics" in out
    assert "current CRM" in out


def test_every_dimension_and_extracted_fact_is_shown(capsys: pytest.CaptureFixture[str]) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    _, out = _run([str(LEADS / "hot_enterprise_buyer.json")], outcome, capsys)
    for dimension in ("icp_fit", "intent", "authority", "urgency", "budget_signal"):
        assert dimension in out
    for fact in ("industry", "role_seniority", "stated_timeline"):
        assert fact in out


def test_metering_is_reported_including_the_cache_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cache read of zero across repeated runs is the symptom of a broken prefix."""
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    _, out = _run([str(LEADS / "hot_enterprise_buyer.json")], outcome, capsys)
    assert "1500" in out or "1,500" in out
    assert "4210" in out or "4,210" in out
    assert "0.0235" in out


# --------------------------------------------------------------------- the decision path


def test_the_confidence_gate_is_reported_when_it_fires(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(confidence=0.2), metering=_metering())
    code, out = _run([str(LEADS / "sparse_ambiguous.json")], outcome, capsys)

    assert code == 0
    assert "confidence" in out.lower()
    payload = _json_of([str(LEADS / "sparse_ambiguous.json"), "--json"], outcome, capsys)
    decision = payload["decision"]
    assert isinstance(decision, dict)
    assert decision["escalation_reason"] == EscalationReason.LOW_CONFIDENCE.value
    assert decision["tier"] != Tier.DISQUALIFIED.value


def _json_of(
    args: list[str], outcome: AssessmentOutcome, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    _, out = _run(args, outcome, capsys)
    parsed: dict[str, object] = json.loads(out)
    return parsed


def test_a_low_confidence_lead_is_never_disqualified(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The asymmetric-cost rule, visible at the only place a human currently sees it."""
    outcome = AssessmentSucceeded(
        assessment=_assessment(confidence=0.05, high=False), metering=_metering()
    )
    payload = _json_of([str(LEADS / "sparse_ambiguous.json"), "--json"], outcome, capsys)
    decision = payload["decision"]
    assert isinstance(decision, dict)
    assert decision["tier"] != Tier.DISQUALIFIED.value
    assert decision["action"] != "suppress"


# --------------------------------------------------------------------------- --json mode


def test_json_mode_emits_one_machine_readable_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#23's eval harness consumes exactly this."""
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    payload = _json_of([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)

    assert set(payload) >= {"lead_file", "tenant", "assessment", "decision", "metering"}
    assessment = payload["assessment"]
    assert isinstance(assessment, dict)
    assert assessment["dimension_scores"]["icp_fit"] == 29
    metering = payload["metering"]
    assert isinstance(metering, dict)
    assert metering["cost_usd"] == "0.0235"
    assert metering["prompt_version"] == "rubric_v1"


def test_json_mode_prints_nothing_but_json(capsys: pytest.CaptureFixture[str]) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    _, out = _run([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)
    json.loads(out)


# ------------------------------------------------------------------------- failure paths


@pytest.mark.parametrize(
    "reason",
    [
        EscalationReason.MODEL_REFUSAL,
        EscalationReason.API_ERROR,
        EscalationReason.TIMEOUT,
        EscalationReason.PARSE_ERROR,
    ],
)
def test_an_api_failure_escalates_and_never_disqualifies(
    reason: EscalationReason, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome = AssessmentFailed(reason=reason, detail="upstream said no", latency_ms=900)
    code, out = _run([str(LEADS / "hot_enterprise_buyer.json")], outcome, capsys)

    assert code == 0, "a failed assessment is a routed lead, not a CLI error"
    assert reason.value in out
    payload = _json_of([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)
    decision = payload["decision"]
    assert isinstance(decision, dict)
    assert decision["tier"] != Tier.DISQUALIFIED.value
    assert decision["action"] != "suppress"
    assert decision["escalation_reason"] == reason.value


def test_a_missing_lead_file_is_a_clean_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assessor = FakeAssessor(AssessmentSucceeded(assessment=_assessment(), metering=_metering()))
    code = main(["/nonexistent/lead.json"], assessor_factory=lambda _e: assessor)
    captured = capsys.readouterr()

    assert code == 2
    assert "nonexistent" in captured.err
    assert "Traceback" not in captured.err


def test_malformed_json_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    code, _ = _run([str(bad)], outcome, capsys)
    assert code == 2


def test_an_unknown_tenant_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    code, _ = _run([str(LEADS / "sparse_ambiguous.json"), "--tenant", "no-such"], outcome, capsys)
    assert code == 2


# ----------------------------------------------------------------------------- plumbing


def test_the_lead_reaches_the_assessor_already_delimited(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI must not hand raw form text to the adapter (#12)."""
    assessor = FakeAssessor(AssessmentSucceeded(assessment=_assessment(), metering=_metering()))
    main([str(LEADS / "injection_attempt.json")], assessor_factory=lambda _e: assessor)
    capsys.readouterr()

    rendered = assessor.rendered_lead
    assert rendered is not None
    assert "lead_submission_" in rendered
    assert "untrusted" in rendered.lower()
    assert "</lead_submission>" not in rendered, "the forged delimiter must be neutered"


def test_effort_is_passed_through_to_the_assessor_factory() -> None:
    seen: list[str] = []
    assessor = FakeAssessor(AssessmentSucceeded(assessment=_assessment(), metering=_metering()))

    def factory(effort: str) -> FakeAssessor:
        seen.append(effort)
        return assessor

    main([str(LEADS / "sparse_ambiguous.json"), "--effort", "low"], assessor_factory=factory)
    assert seen == ["low"]


def test_the_parser_rejects_an_unknown_effort() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["score", "x.json", "--effort", "turbo"])


def test_render_report_itself_does_not_redact_and_that_is_why_main_does() -> None:
    """Pins where the guarantee actually lives.

    `render_report` formats; it has no idea which strings are addresses the model echoed.
    An earlier version of this test asserted the opposite using a fixture whose text
    happened to contain no address — it was a property of the fixture, not the code, and
    it passed while the report leaked. The real guarantee is `main` redacting the report
    before printing, covered by the tests above.
    """
    config = TenantConfig.from_dict(
        json.loads((Path("tenants") / "default.json").read_text(encoding="utf-8"))
    )
    address = "priya.raghavan@northwind-logistics.com"
    outcome = AssessmentSucceeded(assessment=_assessment_quoting(address), metering=_metering())
    report = render_report(
        lead_file=LEADS / "hot_enterprise_buyer.json", config=config, outcome=outcome
    )

    assert address in report, "the formatter is not where redaction belongs"
    assert address not in redact_addresses(report, address)


# ------------------------------------------------------------------- the sample fixtures


def test_the_four_sample_leads_exist_and_cover_the_documented_shapes() -> None:
    names = sorted(path.name for path in LEADS.glob("*.json"))
    assert names == [
        "disqualified_student.json",
        "hot_enterprise_buyer.json",
        "injection_attempt.json",
        "sparse_ambiguous.json",
    ]
    for path in LEADS.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert payload.get("message"), f"{path.name} needs a message to be worth scoring"


# --------------------------------------------------------------------------- review fixes
#
# Each test below reproduces a defect an adversarial review found and demonstrated by
# running it. They are named for the failure, not the fix.


def _assessment_quoting(address: str) -> LeadAssessment:
    """An assessment that quotes the lead's address back — ordinary model behaviour."""
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=20, intent=15, authority=10, urgency=8, budget_signal=8
        ),
        extracted=ExtractedFacts(
            company_name="Northwind",
            industry=None,
            company_size_estimate=None,
            role_seniority=None,
            stated_use_case=f"Wants a reply sent to {address}",
            stated_timeline=None,
        ),
        reasoning=f"The contact writes from {address}, a corporate domain.",
        confidence=0.8,
        missing_information=[f"whether {address} is a shared inbox"],
        suggested_first_question=f"Is {address} the best address to reply to?",
        spam_or_test_submission=False,
    )


def test_the_report_redacts_an_address_the_model_wrote_into_its_own_prose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The leak is not in our text — the model quotes the address back into all four
    free-text fields, which is ordinary behaviour and no formatter can prevent."""
    address = "priya.raghavan@northwind-logistics.com"
    outcome = AssessmentSucceeded(assessment=_assessment_quoting(address), metering=_metering())
    _, out = _run([str(LEADS / "hot_enterprise_buyer.json")], outcome, capsys)

    assert address not in out
    assert out.count(REDACTED_EMAIL) >= 4
    assert "a corporate domain" in out, "only the address goes, not the reasoning"


def test_json_mode_keeps_the_address_because_it_is_the_machine_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    address = "priya.raghavan@northwind-logistics.com"
    outcome = AssessmentSucceeded(assessment=_assessment_quoting(address), metering=_metering())
    payload = _json_of([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)
    assert address in json.dumps(payload)


def test_redact_addresses_removes_the_submitted_address_even_if_unusually_shaped() -> None:
    assert "odd@localdomain" not in redact_addresses("mail odd@localdomain", "odd@localdomain")


def test_a_console_that_cannot_encode_the_model_s_prose_does_not_fail_the_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An em dash on a legacy code page used to exit 1 — a non-zero exit for a *lead*
    reason, which is exactly what this command promises never to do."""
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buffer, encoding="ascii", errors="strict"))

    assessment = _assessment().model_copy(update={"reasoning": "Budget — confirmed — Zürich."})
    assessor = FakeAssessor(AssessmentSucceeded(assessment=assessment, metering=_metering()))
    code = main([str(LEADS / "hot_enterprise_buyer.json")], assessor_factory=lambda _e: assessor)
    sys.stdout.flush()

    assert code == 0
    assert "Z" in buffer.getvalue().decode("utf-8", errors="replace")


def test_a_port_that_raises_still_produces_an_escalation_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`LeadAssessorPort` is a Protocol; the exit-0 promise cannot rest on someone
    else's docstring."""

    class ExplodingAssessor:
        def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
            raise RuntimeError("socket closed")

    code = main(
        [str(LEADS / "hot_enterprise_buyer.json")], assessor_factory=lambda _e: ExplodingAssessor()
    )
    out = capsys.readouterr().out

    assert code == 0
    assert EscalationReason.API_ERROR.value in out
    assert "socket closed" in out


def test_metering_is_reported_for_a_billed_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """A refusal is a billed HTTP 200. Dropping its metering under-counts spend on exactly
    the traffic where cost surprises live."""
    outcome = AssessmentFailed(
        reason=EscalationReason.MODEL_REFUSAL,
        detail="classifier fired",
        latency_ms=900,
        metering=_metering(),
    )
    payload = _json_of([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)
    metering = payload["metering"]
    assert isinstance(metering, dict)
    assert metering["cost_usd"] == "0.0235"


def test_a_truncated_lead_is_visible_in_both_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#12 records truncation precisely so it is never silent; the operator at the phase
    gate must be able to tell the model saw a cut-down lead."""
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"message": "A" * 40_000, "email": "a@b.test"}), encoding="utf-8")
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())

    _, out = _run([str(big)], outcome, capsys)
    assert "too large" in out.lower()

    payload = _json_of([str(big), "--json"], outcome, capsys)
    rendering = payload["rendering"]
    assert isinstance(rendering, dict)
    truncated = rendering["truncated_fields"]
    assert isinstance(truncated, dict)
    assert truncated["message"] > 0


def test_the_json_record_carries_a_schema_version(capsys: pytest.CaptureFixture[str]) -> None:
    outcome = AssessmentSucceeded(assessment=_assessment(), metering=_metering())
    payload = _json_of([str(LEADS / "hot_enterprise_buyer.json"), "--json"], outcome, capsys)
    assert payload["schema_version"] == JSON_SCHEMA_VERSION
