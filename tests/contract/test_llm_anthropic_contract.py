"""The adapter against the real SDK, driven by a recorded response payload.

``tests/unit/test_llm_anthropic.py`` proves the adapter's logic with a fake client. What
it cannot prove is that our idea of the wire is the SDK's idea of the wire — that
``messages.create`` really serialises ``cache_control`` where we put it, that
:data:`~leadquali.adapters.llm_anthropic.OUTPUT_FORMAT` really lands as a ``json_schema``
on ``output_config``, and that a payload shaped like the API's really becomes a validated
:class:`~leadquali.domain.models.LeadAssessment`.

So this file swaps only the transport. The client is a genuine ``anthropic.Anthropic``;
its HTTP layer is an ``httpx2.MockTransport`` that replays a response payload and captures
the request that produced it. Everything between — request serialisation, response
modelling, error translation — is the SDK's own code. No key, no network, no cost.

Three things are asserted that only a real serialisation can show:

* the JSON schema that actually goes over the wire contains no ``tier`` and no
  ``total_score`` (invariant 2, checked at the boundary rather than in the abstract),
* the ``cache_control`` breakpoint lands on the rubric block and on nothing else, and
* a refusal carrying a prose text block comes back as ``MODEL_REFUSAL``. That one is the
  reason the adapter does not use ``client.messages.parse``: ``parse`` validates every
  text block *inside the SDK* before it returns, so on that payload it raises
  ``pydantic.ValidationError`` from the call itself and the refusal is misfiled as a
  parse error. A fake client cannot catch that — it bypasses the SDK's parse step
  entirely — which is precisely why the case lives here, against the real thing.

**On "recorded".** These payloads are hand-written to the documented response shape, not
captured from a live account: this repository has never had an API key. Each fixture says
so in its own ``_comment`` field. What is real is everything downstream of the socket.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from leadquali.adapters.llm_anthropic import MAX_TOKENS, MODEL_ID, AnthropicLeadAssessor
from leadquali.app.assessment_result import AssessmentFailed, AssessmentSucceeded
from leadquali.app.ports import LeadAssessorPort
from leadquali.domain.models import Action, EscalationReason, Tier
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts import PROMPT_VERSION, build_system_blocks

FIXTURES = Path(__file__).parent / "fixtures"

RENDERED_LEAD = (
    "<lead_submission>\n"
    "name: Dana Okafor\n"
    "company: Northwind Tooling\n"
    "message: We need to automate weld seam inspection before quarter end.\n"
    "</lead_submission>"
)


def recorded(name: str) -> dict[str, Any]:
    """One ``POST /v1/messages`` response body, replayed from ``fixtures/``."""
    with (FIXTURES / f"{name}.json").open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def tenant() -> TenantConfig:
    return TenantConfig.from_dict(
        {
            "tenant_id": "northwind",
            "name": "Northwind Vision Systems",
            "icp_description": "Industrial manufacturers with 100-1000 staff automating QA.",
            "routing_rules": {
                Tier.HOT.value: {
                    "action": Action.EMAIL_SALES.value,
                    "destination": "hot@northwind.test",
                },
                Tier.WARM.value: {
                    "action": Action.EMAIL_SALES.value,
                    "destination": "sales@northwind.test",
                },
                Tier.COLD.value: {
                    "action": Action.EMAIL_SALES.value,
                    "destination": "nurture@northwind.test",
                },
                Tier.DISQUALIFIED.value: {"action": Action.SUPPRESS.value},
            },
        }
    )


class Recorder:
    """A mock transport that replays one recorded body and keeps the request."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return httpx2.Response(self.status, json=self.body)

    @property
    def sent(self) -> dict[str, Any]:
        assert self.requests, "no request was made"
        return self.requests[0]


def build(recorder: Recorder, **kwargs: Any) -> AnthropicLeadAssessor:
    client = anthropic.Anthropic(
        api_key="not-a-real-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return AnthropicLeadAssessor(client=client, **kwargs)


@pytest.fixture
def success_recorder() -> Recorder:
    return Recorder(recorded("messages_parse_success"))


# ------------------------------------------------------------------ the outgoing request


def test_request_serialises_the_cache_breakpoint_onto_the_rubric_only(
    success_recorder: Recorder,
) -> None:
    config = tenant()
    build(success_recorder).assess(config=config, rendered_lead=RENDERED_LEAD)

    system = success_recorder.sent["system"]
    rubric_block, tenant_block = build_system_blocks(config)
    assert [block["text"] for block in system] == [rubric_block.text, tenant_block.text]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]


def test_request_carries_the_model_budget_and_effort(success_recorder: Recorder) -> None:
    build(success_recorder, effort="high").assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    sent = success_recorder.sent
    assert sent["model"] == MODEL_ID
    assert sent["max_tokens"] == MAX_TOKENS
    assert sent["output_config"]["effort"] == "high"
    assert sent["messages"] == [{"role": "user", "content": RENDERED_LEAD}]


def test_output_format_serialises_to_the_assessment_json_schema(
    success_recorder: Recorder,
) -> None:
    """``output_format=LeadAssessment`` must reach the wire as a strict JSON schema."""
    build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    fmt = success_recorder.sent["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "dimension_scores",
        "extracted",
        "reasoning",
        "confidence",
        "missing_information",
        "suggested_first_question",
        "spam_or_test_submission",
    }


def test_the_wire_schema_offers_the_model_no_way_to_route(
    success_recorder: Recorder,
) -> None:
    """Invariant 2, asserted on the bytes that actually leave the process."""
    build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    serialised = json.dumps(success_recorder.sent["output_config"]["format"])
    for forbidden in ("tier", "total_score", "action", "escalat"):
        assert forbidden not in serialised.lower(), forbidden


def test_the_lead_never_enters_the_cached_prefix(success_recorder: Recorder) -> None:
    """Attacker-controlled text belongs in the user turn, downstream of every breakpoint."""
    build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    sent = success_recorder.sent
    assert "Northwind Tooling" not in json.dumps(sent["system"])
    assert "Northwind Tooling" in json.dumps(sent["messages"])


# -------------------------------------------------------------------- the parsed response


def test_recorded_success_parses_into_a_validated_assessment(
    success_recorder: Recorder,
) -> None:
    outcome = build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assessment = outcome.assessment
    # Never assert on prose: structure, ranges and extracted fields only.
    assert assessment.dimension_scores.icp_fit == 26
    assert 0.0 <= assessment.confidence <= 1.0
    assert assessment.extracted.company_name == "Northwind Tooling"
    assert assessment.spam_or_test_submission is False
    assert assessment.missing_information


def test_recorded_success_meters_the_call_from_the_payloads_usage(
    success_recorder: Recorder,
) -> None:
    """612 input, 431 output, 1,984 cache reads, 0 writes.

      612 x $5.00/MTok  = $0.003060
      431 x $25.00/MTok = $0.010775
    1,984 x $0.50/MTok  = $0.000992
                          ---------
                          $0.014827
    """
    outcome = build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    metering = outcome.metering
    assert metering.input_tokens == 612
    assert metering.output_tokens == 431
    assert metering.cache_read_tokens == 1_984
    assert metering.cache_creation_tokens == 0
    assert metering.cost_usd == Decimal("0.014827")
    assert metering.model_id == MODEL_ID
    assert metering.prompt_version == PROMPT_VERSION
    assert metering.effort == "medium"
    assert metering.latency_ms >= 0


def test_a_cache_read_actually_showed_up_in_the_recorded_usage(
    success_recorder: Recorder,
) -> None:
    """The one number that proves caching is working at all; zero here is the alarm."""
    outcome = build(success_recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.metering.cache_read_tokens > 0


def test_recorded_refusal_escalates_to_a_human() -> None:
    """The pre-output refusal: empty ``content``, and documented as unbilled.

    Every counter in the fixture is zero because Anthropic bills a classifier decline
    that fires before any output at nothing at all — no input tokens, no output tokens,
    no rate-limit consumption. The metering row is still written: a zero-cost call is a
    fact worth recording, and "no row" and "a row that cost nothing" answer different
    questions when someone asks how often the classifier is firing.
    """
    recorder = Recorder(recorded("messages_parse_refusal"))

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.MODEL_REFUSAL
    assert "cyber" in outcome.detail
    assert outcome.metering is not None
    assert outcome.metering.input_tokens == 0
    assert outcome.metering.cost_usd == Decimal(0)


def test_recorded_refusal_after_partial_output_is_still_a_refusal() -> None:
    """The refusal shape that a content-parsing SDK helper hides.

    Anthropic documents two refusal shapes: the classifier fires *before any output*
    (empty ``content``, unbilled) or *mid-stream* after partial output (a prose text
    block, billed at normal rates). ``client.messages.parse()`` reads and validates
    every text block inside the SDK before it returns, so on the second shape it raises
    ``pydantic.ValidationError`` from the call itself — the refusal is filed as a parse
    error and the billed call goes unmetered. That is why the adapter asks for a plain
    ``Message`` and gates on ``stop_reason`` itself.
    """
    recorder = Recorder(recorded("messages_refusal_after_output"))

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.MODEL_REFUSAL
    assert "cyber" in outcome.detail
    # Billed, so metered: 731 x $5.00 + 96 x $25.00 + 1,984 x $0.50 per MTok.
    assert outcome.metering is not None
    assert outcome.metering.input_tokens == 731
    assert outcome.metering.output_tokens == 96
    assert outcome.metering.cost_usd == Decimal("0.007047")


def test_the_refusal_prose_never_reaches_the_operator_detail() -> None:
    """A refusal explains itself in prose that quotes the lead. Invariant 5 says no."""
    recorder = Recorder(recorded("messages_refusal_after_output"))

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert "enumerate" not in outcome.detail
    assert outcome.detail == "model declined to answer (category=cyber)"


def test_recorded_malformed_response_is_a_parse_error_and_is_metered() -> None:
    """A complete, fully billed response that is not a valid assessment.

    This is the case the ``pydantic.ValidationError`` handler is named for, and after the
    move off ``messages.parse`` it is the only case that reaches it.
    """
    recorder = Recorder(recorded("messages_malformed"))

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.PARSE_ERROR
    # Field paths, never the offending value — model output derived from lead text.
    assert "dimension_scores.icp_fit" in outcome.detail
    assert "999" not in outcome.detail
    # 612 x $5.00 + 388 x $25.00 + 1,984 x $0.50 per MTok.
    assert outcome.metering is not None
    assert outcome.metering.output_tokens == 388
    assert outcome.metering.cost_usd == Decimal("0.013752")


def test_recorded_truncation_is_a_parse_error_and_is_metered() -> None:
    """The most expensive failure available: the whole output budget, spent on a fragment."""
    recorder = Recorder(recorded("messages_truncated"))

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.PARSE_ERROR
    assert "max_tokens" in outcome.detail
    assert outcome.metering is not None
    assert outcome.metering.output_tokens == MAX_TOKENS
    # 8,000 output tokens at $25/MTok is $0.20 before a single input token is counted.
    assert outcome.metering.cost_usd > Decimal("0.20")


def test_a_transport_timeout_becomes_a_typed_timeout_not_an_exception() -> None:
    """A timeout has no response body, so it is recorded as the transport failure it is.

    ``httpx2.ReadTimeout`` is what the connection layer raises; the assertion that matters
    is that the SDK turns it into ``anthropic.APITimeoutError`` and the adapter maps that
    to ``TIMEOUT`` rather than to the generic ``API_ERROR`` — the ordering of two handlers
    where ``APITimeoutError`` subclasses ``APIConnectionError``.
    """

    def time_out(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    client = anthropic.Anthropic(
        api_key="not-a-real-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(time_out)),
    )

    outcome = AnthropicLeadAssessor(client=client).assess(
        config=tenant(), rendered_lead=RENDERED_LEAD
    )

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.TIMEOUT
    # Nothing was billed, because nothing completed.
    assert outcome.metering is None


def test_a_server_error_becomes_an_api_error_not_an_exception() -> None:
    recorder = Recorder({"type": "error", "error": {"type": "api_error", "message": "boom"}}, 500)

    outcome = build(recorder).assess(config=tenant(), rendered_lead=RENDERED_LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.API_ERROR


def test_the_adapter_satisfies_the_port(success_recorder: Recorder) -> None:
    port: LeadAssessorPort = build(success_recorder)
    assert isinstance(port, LeadAssessorPort)


# -------------------------------------------------------------------------- the live call


@pytest.mark.live_api
def test_live_call_returns_an_assessment() -> None:  # pragma: no cover - never in CI
    """The same path against the real API. Billable; excluded from the default suite.

    Run deliberately with ``pytest -m live_api`` and ``ANTHROPIC_API_KEY`` set. It asserts
    only structure and provenance — never prose, never a specific score.
    """
    from leadquali.adapters.llm_anthropic import build_anthropic_client
    from leadquali.config import get_settings

    client = build_anthropic_client(get_settings().require_anthropic_api_key())
    outcome = AnthropicLeadAssessor(client=client).assess(
        config=tenant(), rendered_lead=RENDERED_LEAD
    )

    assert isinstance(outcome, AssessmentSucceeded)
    assert 0.0 <= outcome.assessment.confidence <= 1.0
    assert outcome.metering.output_tokens > 0
    assert outcome.metering.cost_usd > Decimal(0)
