"""The Anthropic adapter, exercised entirely offline against a fake client.

The happy path of this adapter is one API call, and it is the least interesting thing
here. What earns the tests is everything that can go wrong, because invariant 3 of
``CLAUDE.md`` — *a lead is never silently dropped* — is only true if every failure mode
comes back as a typed outcome a human can be routed on. So the assertions below are mostly
about failure:

* a refusal is read from ``stop_reason`` **before** the content is validated, in all three
  shapes a refusal arrives in, and it escalates rather than disqualifying;
* every SDK exception class lands on exactly one :class:`EscalationReason`;
* every failure that reached an HTTP 200 carries its metering, because a truncation and a
  schema violation are billed in full and are the two most expensive ways to fail;
* nothing — not a truncation, not a schema violation, not an exception the SDK has never
  raised before — can come back as a success carrying a low score.

The request side is asserted just as tightly, because a silently wrong request is the
expensive kind: block 0 must carry ``cache_control`` and block 1 must not (a breakpoint on
the tenant block caches nothing while still paying the write premium), and the model,
``max_tokens``, effort and the output schema must be exactly what the plan says.

**How the gate order is proved.** The fake used to hand back an already-parsed object and
a ``.content`` property that raised, so that reading content at all failed the test. That
stopped being able to prove anything once the adapter took over validation — the whole
point of the change is that it now reads content itself. The proof moved into the data:
the refused response carries prose (:data:`REFUSAL_PROSE`) that cannot validate, so
``MODEL_REFUSAL`` is reachable only by checking ``stop_reason`` first. ``FakeMessages``
also raises if anything calls ``messages.parse``, the helper whose internal parse step
caused the original defect.

No network, no key: the fake client returns objects shaped like the SDK's. Whether the SDK
agrees with that shape is ``tests/contract/``'s job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from leadquali.adapters.llm_anthropic import (
    CACHE_CONTROL_EPHEMERAL,
    CLAUDE_OPUS_5_PRICES,
    MAX_TOKENS,
    MODEL_ID,
    OUTPUT_FORMAT,
    AnthropicLeadAssessor,
    TokenPrices,
    token_cost_usd,
)
from leadquali.app.assessment_result import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    AssessmentFailed,
    AssessmentSucceeded,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts import PROMPT_VERSION, build_system_blocks

# --------------------------------------------------------------------------- fixtures


def make_config(**overrides: Any) -> TenantConfig:
    """A minimal valid tenant, so a test can say only what it cares about."""
    document: dict[str, Any] = {
        "tenant_id": "acme",
        "name": "Acme Robotics",
        "icp_description": "Mid-market manufacturers automating quality inspection.",
        "routing_rules": {
            Tier.HOT.value: {"action": Action.EMAIL_SALES.value, "destination": "hot@acme.test"},
            Tier.WARM.value: {"action": Action.EMAIL_SALES.value, "destination": "sales@acme.test"},
            Tier.COLD.value: {
                "action": Action.EMAIL_SALES.value,
                "destination": "nurture@acme.test",
            },
            Tier.DISQUALIFIED.value: {"action": Action.SUPPRESS.value},
        },
    }
    document.update(overrides)
    return TenantConfig.from_dict(document)


def make_assessment(confidence: float = 0.8) -> LeadAssessment:
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=24, intent=20, authority=10, urgency=9, budget_signal=11
        ),
        extracted=ExtractedFacts(
            company_name="Northwind Tooling",
            industry="Manufacturing",
            company_size_estimate="200-500",
            role_seniority="vp",
            stated_use_case="Automate weld inspection",
            stated_timeline="This quarter",
        ),
        reasoning="Mid-market manufacturer, VP of ops, named a quarter-end deadline.",
        confidence=confidence,
        missing_information=["budget range"],
        suggested_first_question="Which line would you pilot on?",
        spam_or_test_submission=False,
    )


class FakeUsage(BaseModel):
    """Shaped like ``anthropic.types.Usage`` for the fields the adapter reads."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass(frozen=True)
class FakeTextBlock:
    """Shaped like ``anthropic.types.TextBlock``."""

    text: str
    type: str = "text"


@dataclass(frozen=True)
class FakeThinkingBlock:
    """Shaped like ``anthropic.types.ThinkingBlock``.

    Present in every real ``claude-opus-5`` response — thinking is adaptive and on by
    default — and it carries no ``.text``. A block-selection bug that reaches for
    ``content[0].text`` crashes here rather than in production.
    """

    thinking: str = ""
    signature: str = "ErUBCkYIBRgCIkZmYWtl"
    type: str = "thinking"


def blocks(payload: str | None, *, thinking: bool = True) -> list[object]:
    """The content list of a realistic response: a thinking block, then the JSON text.

    ``payload=None`` is the pathological response that carries no text block at all.
    """
    content: list[object] = [FakeThinkingBlock()] if thinking else []
    if payload is not None:
        content.append(FakeTextBlock(text=payload))
    return content


def assessment_json(assessment: LeadAssessment | None = None) -> str:
    """A well-formed response body: what the model returns when everything works."""
    return (assessment or make_assessment()).model_dump_json()


@dataclass
class FakeMessage:
    """A response object shaped like ``anthropic.types.Message``.

    The adapter now reads and validates ``.content`` itself, so — unlike the previous
    fake, whose ``.content`` raised — this one hands back real blocks. Proving the gate
    order therefore moves to :data:`REFUSAL_PROSE`: a refusal whose text block would fail
    validation, so the only way to reach ``MODEL_REFUSAL`` is to check ``stop_reason``
    first. That is the same shape the contract fixture replays through the real SDK.
    """

    stop_reason: str | None = "end_turn"
    stop_details: object | None = None
    content: list[object] = field(default_factory=lambda: blocks(assessment_json()))
    usage: FakeUsage = field(default_factory=FakeUsage)


#: A refusal's prose: valid content, and not a ``LeadAssessment``. Anthropic documents the
#: classifier firing mid-stream, after output has already been emitted.
REFUSAL_PROSE: Final[str] = "I can't help with this request."


@dataclass
class FakeMessages:
    """Stands in for ``client.messages``, recording the kwargs it was called with."""

    result: object
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def parse(self, **kwargs: Any) -> Any:
        """The helper the adapter must *not* call.

        ``messages.parse`` validates every text block inside the SDK before returning, so
        a refusal carrying prose raises ``ValidationError`` from the call and never
        reaches the ``stop_reason`` gate. The adapter went to some trouble to stop using
        it; this makes a regression loud instead of silent.
        """
        raise AssertionError(
            "the adapter called messages.parse; it must call messages.create and gate on "
            "stop_reason before validating content"
        )


@dataclass
class FakeClient:
    """Stands in for ``anthropic.Anthropic``."""

    messages: FakeMessages


def assessor(result: object, **kwargs: Any) -> tuple[AnthropicLeadAssessor, FakeMessages]:
    messages = FakeMessages(result=result)
    client: Any = FakeClient(messages=messages)
    return AnthropicLeadAssessor(client=client, **kwargs), messages


def httpx_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def httpx_response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx_request(), json={"error": {"message": "no"}})


LEAD = "<lead_submission>Hi, we need weld inspection.</lead_submission>"


# ------------------------------------------------------------------- the request shape


def test_block_zero_is_cacheable_and_block_one_is_not() -> None:
    """The breakpoint goes on the rubric, and only on the rubric.

    Block 1 is the tenant's ICP text: it differs per customer, so a breakpoint there buys
    nothing and costs the 1.25x write premium on every first request of every tenant.
    """
    config = make_config()
    lead_assessor, messages = assessor(FakeMessage())

    lead_assessor.assess(config=config, rendered_lead=LEAD)

    system = messages.calls[0]["system"]
    rubric_block, tenant_block = build_system_blocks(config)
    assert system == [
        {"type": "text", "text": rubric_block.text, "cache_control": CACHE_CONTROL_EPHEMERAL},
        {"type": "text", "text": tenant_block.text},
    ]
    assert "cache_control" not in system[1]
    assert CACHE_CONTROL_EPHEMERAL == {"type": "ephemeral"}


def test_request_parameters_are_exactly_as_intended() -> None:
    config = make_config()
    lead_assessor, messages = assessor(FakeMessage())

    lead_assessor.assess(config=config, rendered_lead=LEAD)

    call = messages.calls[0]
    assert call["model"] == MODEL_ID == "claude-opus-5"
    assert call["max_tokens"] == MAX_TOKENS == 8000
    # The schema goes on the request explicitly, because the adapter no longer lets the
    # SDK derive it — and derive a parse of the response along with it.
    assert call["output_config"] == {"effort": DEFAULT_EFFORT, "format": OUTPUT_FORMAT}
    assert call["messages"] == [{"role": "user", "content": LEAD}]
    # The lead is a user turn, never a system block: it is attacker-controlled text and it
    # must not sit inside the cached prefix.
    assert LEAD not in "".join(block["text"] for block in call["system"])


def test_default_effort_is_medium_and_effort_is_a_parameter() -> None:
    """#24 sweeps effort, so it cannot be a constant buried in the call."""
    assert DEFAULT_EFFORT == "medium"
    config = make_config()
    for effort in EFFORT_LEVELS:
        lead_assessor, messages = assessor(FakeMessage(), effort=effort)
        lead_assessor.assess(config=config, rendered_lead=LEAD)
        assert messages.calls[0]["output_config"]["effort"] == effort


def test_unknown_effort_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="effort"):
        AnthropicLeadAssessor(client=object(), effort="turbo")  # type: ignore[arg-type]


# ------------------------------------------------------------------------ the happy path


def test_success_carries_the_assessment_and_full_provenance() -> None:
    config = make_config()
    expected = make_assessment()
    usage = FakeUsage(
        input_tokens=1_500,
        output_tokens=900,
        cache_read_input_tokens=4_000,
        cache_creation_input_tokens=0,
    )
    lead_assessor, _ = assessor(FakeMessage(content=blocks(assessment_json(expected)), usage=usage))

    outcome = lead_assessor.assess(config=config, rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.ok is True
    assert outcome.assessment == expected
    metering = outcome.metering
    assert metering.model_id == MODEL_ID
    assert metering.prompt_version == PROMPT_VERSION
    assert metering.effort == DEFAULT_EFFORT
    assert metering.input_tokens == 1_500
    assert metering.output_tokens == 900
    assert metering.cache_read_tokens == 4_000
    assert metering.cache_creation_tokens == 0
    assert metering.latency_ms >= 0


def test_absent_cache_counters_read_as_zero_not_none() -> None:
    """The SDK types both cache counters ``Optional``; cost arithmetic cannot see ``None``."""
    usage = FakeUsage(
        input_tokens=10,
        output_tokens=10,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    lead_assessor, _ = assessor(FakeMessage(usage=usage))

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.metering.cache_read_tokens == 0
    assert outcome.metering.cache_creation_tokens == 0


def test_latency_is_recorded_on_success_and_on_failure() -> None:
    slow = FakeMessage()
    lead_assessor, messages = assessor(slow)

    original = messages.create

    def slow_create(**kwargs: Any) -> Any:
        # Busy-wait past a millisecond so the assertion is about the clock, not luck.
        import time

        deadline = time.monotonic() + 0.003
        while time.monotonic() < deadline:
            pass
        return original(**kwargs)

    messages.create = slow_create  # type: ignore[method-assign]
    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)
    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.metering.latency_ms >= 1

    failed_assessor, _ = assessor(anthropic.APITimeoutError(request=httpx_request()))
    failure = failed_assessor.assess(config=make_config(), rendered_lead=LEAD)
    assert isinstance(failure, AssessmentFailed)
    assert failure.latency_ms >= 0


# ----------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "content",
    [
        pytest.param([], id="pre-output"),
        pytest.param(blocks(None), id="thinking-only"),
        pytest.param(blocks(REFUSAL_PROSE), id="mid-stream-prose"),
    ],
)
def test_every_documented_refusal_shape_escalates(content: list[object]) -> None:
    """Invariant 3, across all three shapes a refusal actually arrives in.

    Anthropic documents the classifier firing *before any output* (empty ``content``) or
    *mid-stream* after partial output. The third row is the one that used to be misfiled:
    prose in a text block is not a ``LeadAssessment``, so any implementation that
    validates content before consulting ``stop_reason`` reports it as a parse error and
    the ``MODEL_REFUSAL`` signal is lost. All three must reach the same verdict.
    """
    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="refusal",
            stop_details=None,
            content=content,
            usage=FakeUsage(input_tokens=1_200, output_tokens=3),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.ok is False
    assert outcome.reason is EscalationReason.MODEL_REFUSAL
    assert not hasattr(outcome, "assessment")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param([], id="pre-output"),
        pytest.param(blocks(REFUSAL_PROSE), id="mid-stream-prose"),
    ],
)
def test_refusal_still_meters_the_call(content: list[object]) -> None:
    """A refusal is an HTTP 200 and a mid-stream one is billed, so it must be metered."""
    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="refusal",
            content=content,
            usage=FakeUsage(input_tokens=1_200, output_tokens=3),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.metering is not None
    assert outcome.metering.input_tokens == 1_200
    assert outcome.metering.cost_usd > Decimal(0)


def test_refusal_detail_carries_the_stop_details_category() -> None:
    """``stop_details`` is populated only on a refusal; it says which classifier fired."""

    @dataclass(frozen=True)
    class Details:
        type: str = "refusal"
        category: str = "cyber"
        explanation: str = "declined"

    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="refusal",
            stop_details=Details(),
            content=blocks(REFUSAL_PROSE),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert "cyber" in outcome.detail


def test_refusal_detail_never_quotes_the_refused_content() -> None:
    """Invariant 5: the refusal prose and ``explanation`` both describe the submission."""

    @dataclass(frozen=True)
    class Details:
        type: str = "refusal"
        category: str = "cyber"
        explanation: str = "the submission asks about intrusion tooling"

    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="refusal",
            stop_details=Details(),
            content=blocks(REFUSAL_PROSE),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.detail == "model declined to answer (category=cyber)"
    assert "intrusion" not in outcome.detail
    assert REFUSAL_PROSE not in outcome.detail


def test_a_refusal_is_never_read_as_a_schema_violation() -> None:
    """The regression this whole change exists to prevent, stated as one assertion.

    The refused response carries prose that cannot validate. If the gate order is ever
    inverted — validate first, check ``stop_reason`` second — this comes back as
    ``PARSE_ERROR`` with no metering, and the fact that a safety classifier is firing on
    our traffic vanishes into the parse-error bucket.
    """
    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="refusal",
            content=blocks(REFUSAL_PROSE),
            usage=FakeUsage(input_tokens=731, output_tokens=96),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    # Stated in the order the bug would break: not the parse-error bucket, and metered.
    assert outcome.reason is not EscalationReason.PARSE_ERROR
    assert outcome.reason is EscalationReason.MODEL_REFUSAL
    assert outcome.metering is not None
    assert outcome.metering.output_tokens == 96


# ------------------------------------------------------------------- exception mapping


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            anthropic.APITimeoutError(request=httpx_request()),
            EscalationReason.TIMEOUT,
            id="timeout",
        ),
        pytest.param(
            anthropic.APIConnectionError(request=httpx_request()),
            EscalationReason.API_ERROR,
            id="connection",
        ),
        pytest.param(
            anthropic.RateLimitError("slow down", response=httpx_response(429), body=None),
            EscalationReason.API_ERROR,
            id="rate-limit",
        ),
        pytest.param(
            anthropic.InternalServerError("boom", response=httpx_response(500), body=None),
            EscalationReason.API_ERROR,
            id="server-error",
        ),
        pytest.param(
            anthropic.BadRequestError("bad", response=httpx_response(400), body=None),
            EscalationReason.API_ERROR,
            id="bad-request",
        ),
        pytest.param(
            anthropic.AuthenticationError("nope", response=httpx_response(401), body=None),
            EscalationReason.API_ERROR,
            id="auth",
        ),
        pytest.param(
            RuntimeError("something nobody predicted"), EscalationReason.API_ERROR, id="unexpected"
        ),
    ],
)
def test_every_exception_maps_to_an_escalation_reason(
    error: BaseException, expected: EscalationReason
) -> None:
    lead_assessor, _ = assessor(error)

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is expected
    assert outcome.detail
    assert outcome.metering is None


def test_timeout_is_distinguished_from_a_plain_connection_error() -> None:
    """``APITimeoutError`` subclasses ``APIConnectionError``, so handler order matters.

    Catch them the other way round and every timeout is filed as ``api_error``, and the
    one metric that tells you the model is thinking too long stops moving.
    """
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)
    timeout_assessor, _ = assessor(anthropic.APITimeoutError(request=httpx_request()))
    outcome = timeout_assessor.assess(config=make_config(), rendered_lead=LEAD)
    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.TIMEOUT


@pytest.mark.parametrize(
    ("payload", "detail_fragment"),
    [
        pytest.param('{"dimension_scores": {"icp_fit": ', "<root>", id="truncated-json"),
        pytest.param("Sure! Here is the assessment.", "<root>", id="prose-not-json"),
        pytest.param("{}", "confidence", id="empty-object"),
        pytest.param(
            make_assessment().model_dump_json().replace('"icp_fit":24', '"icp_fit":999'),
            "dimension_scores.icp_fit",
            id="out-of-range-score",
        ),
    ],
)
def test_schema_violation_is_a_parse_error(payload: str, detail_fragment: str) -> None:
    """Validation now happens here, so the failure is raised and handled here.

    ``detail`` names field paths and never the offending value: a Pydantic message quotes
    its input, and the input on this path is model output derived from lead text.
    """
    lead_assessor, _ = assessor(FakeMessage(content=blocks(payload)))

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.PARSE_ERROR
    assert detail_fragment in outcome.detail
    assert payload not in outcome.detail


def test_a_schema_violation_is_metered_because_a_whole_response_was_billed() -> None:
    """A response that fails validation cost exactly as much as one that passes."""
    lead_assessor, _ = assessor(
        FakeMessage(
            content=blocks("not an assessment"),
            usage=FakeUsage(input_tokens=700, output_tokens=812),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.metering is not None
    assert outcome.metering.output_tokens == 812
    assert outcome.metering.cost_usd > Decimal(0)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param([], id="no-blocks"),
        pytest.param(blocks(None), id="thinking-only"),
    ],
)
def test_a_response_with_no_text_block_is_a_parse_error_not_a_crash(
    content: list[object],
) -> None:
    """Unreachable given ``output_config.format`` — and still not worth routing a lead on."""
    lead_assessor, _ = assessor(FakeMessage(stop_reason="end_turn", content=content))

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.PARSE_ERROR
    assert outcome.metering is not None


def test_a_thinking_block_never_gets_mistaken_for_the_answer() -> None:
    """Thinking is on by default, so block 0 is not the JSON — ``content[0]`` is a bug."""
    lead_assessor, _ = assessor(FakeMessage(content=blocks(assessment_json())))

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.assessment == make_assessment()


def test_truncated_response_is_a_parse_error() -> None:
    """``max_tokens`` means thinking ate the budget; whatever came back is not an answer.

    Note the content is *schema-valid* here: the gate is ``stop_reason``, not parseability.
    A truncated response that happens to validate is still a fragment, not a judgment.
    """
    lead_assessor, _ = assessor(
        FakeMessage(stop_reason="max_tokens", content=blocks(assessment_json()))
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.PARSE_ERROR
    assert "max_tokens" in outcome.detail


def test_a_truncation_is_metered_because_it_burned_the_whole_output_budget() -> None:
    """The most expensive failure the adapter has: 8,000 output tokens for nothing."""
    lead_assessor, _ = assessor(
        FakeMessage(
            stop_reason="max_tokens",
            content=blocks('{"dimension_scores": {"icp_fit": 2'),
            usage=FakeUsage(input_tokens=700, output_tokens=MAX_TOKENS),
        )
    )

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.metering is not None
    assert outcome.metering.output_tokens == MAX_TOKENS
    # 8,000 output tokens at $25/MTok is $0.20 of budget burned on a fragment.
    assert outcome.metering.cost_usd >= Decimal("0.20")


def test_prompt_version_mismatch_escalates_rather_than_raising() -> None:
    """Even a misconfigured tenant must not lose the lead — invariant 3 has no exceptions."""
    config = make_config(prompt_version="rubric_v99")
    lead_assessor, messages = assessor(FakeMessage())

    outcome = lead_assessor.assess(config=config, rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentFailed)
    assert outcome.reason is EscalationReason.API_ERROR
    assert "rubric_v99" in outcome.detail
    assert messages.calls == []


def test_no_failure_is_ever_reported_as_a_success() -> None:
    """The one bug this whole file exists to prevent: a failure read as a bad lead."""
    failures: list[object] = [
        FakeMessage(stop_reason="refusal", content=[]),
        FakeMessage(stop_reason="refusal", content=blocks(REFUSAL_PROSE)),
        # A refusal whose text block happens to be schema-valid: still never a score.
        FakeMessage(stop_reason="refusal", content=blocks(assessment_json())),
        FakeMessage(stop_reason="end_turn", content=blocks(None)),
        FakeMessage(stop_reason="end_turn", content=blocks("not json at all")),
        FakeMessage(stop_reason="max_tokens", content=blocks(assessment_json())),
        anthropic.APITimeoutError(request=httpx_request()),
        anthropic.InternalServerError("boom", response=httpx_response(500), body=None),
        RuntimeError("unpredicted"),
    ]
    for result in failures:
        lead_assessor, _ = assessor(result)
        outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)
        assert isinstance(outcome, AssessmentFailed), result
        assert outcome.reason is not EscalationReason.LOW_CONFIDENCE


# --------------------------------------------------------------------------- the money


def test_prices_are_the_published_claude_opus_5_rates() -> None:
    assert CLAUDE_OPUS_5_PRICES.input_usd_per_mtok == Decimal("5.00")
    assert CLAUDE_OPUS_5_PRICES.output_usd_per_mtok == Decimal("25.00")
    # Cache reads are 0.1x base input, 5-minute-TTL writes are 1.25x base input.
    assert CLAUDE_OPUS_5_PRICES.cache_read_usd_per_mtok == Decimal("0.50")
    assert CLAUDE_OPUS_5_PRICES.cache_write_usd_per_mtok == Decimal("6.25")


def test_cost_arithmetic_against_hand_computed_values() -> None:
    """Hand-computed, not recomputed with the same expression the code uses.

    1,000,000 input   x $5.00/MTok  = $5.00
      200,000 output  x $25.00/MTok = $5.00
      400,000 cache reads x $0.50/MTok = $0.20
       80,000 cache writes x $6.25/MTok = $0.50
                                        --------
                                          $10.70
    """
    cost = token_cost_usd(
        input_tokens=1_000_000,
        output_tokens=200_000,
        cache_read_tokens=400_000,
        cache_creation_tokens=80_000,
    )
    assert cost == Decimal("10.70")


def test_cost_of_a_realistic_single_call() -> None:
    """1,500 uncached input, 900 output, 4,000 cache reads, no write.

    1_500 / 1e6 * 5.00   = 0.0075
      900 / 1e6 * 25.00  = 0.0225
    4_000 / 1e6 * 0.50   = 0.0020
                          -------
                           0.0320
    """
    assert token_cost_usd(
        input_tokens=1_500,
        output_tokens=900,
        cache_read_tokens=4_000,
        cache_creation_tokens=0,
    ) == Decimal("0.0320")


def test_zero_usage_costs_nothing() -> None:
    assert token_cost_usd(
        input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0
    ) == Decimal(0)


def test_cost_is_decimal_not_float() -> None:
    """Money is summed per tenant for billing; binary floats would drift."""
    cost = token_cost_usd(
        input_tokens=1, output_tokens=1, cache_read_tokens=1, cache_creation_tokens=1
    )
    assert isinstance(cost, Decimal)


def test_metering_cost_matches_the_standalone_calculation() -> None:
    usage = FakeUsage(
        input_tokens=1_500,
        output_tokens=900,
        cache_read_input_tokens=4_000,
        cache_creation_input_tokens=0,
    )
    lead_assessor, _ = assessor(FakeMessage(usage=usage))

    outcome = lead_assessor.assess(config=make_config(), rendered_lead=LEAD)

    assert isinstance(outcome, AssessmentSucceeded)
    assert outcome.metering.cost_usd == Decimal("0.0320")


def test_prices_can_be_overridden_without_editing_the_adapter() -> None:
    """A price change is a constant swap, not a code change — that is the whole point."""
    doubled = TokenPrices(
        input_usd_per_mtok=Decimal("10.00"),
        output_usd_per_mtok=Decimal("50.00"),
        cache_read_usd_per_mtok=Decimal("1.00"),
        cache_write_usd_per_mtok=Decimal("12.50"),
    )
    assert token_cost_usd(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        prices=doubled,
    ) == Decimal("10.00")
