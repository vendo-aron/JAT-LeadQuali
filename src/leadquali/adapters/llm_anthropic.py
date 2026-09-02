"""The Anthropic adapter: one lead in, one judgment or one typed failure out.

This is the only module in the repository that imports ``anthropic`` (``CLAUDE.md``), and
that isolation is the reason the rest of the system is testable without a key. Everything
above it sees :class:`~leadquali.app.ports.LeadAssessorPort` and an
:data:`~leadquali.app.assessment_result.AssessmentOutcome`.

Four decisions are worth stating, because each one is a place this could quietly go wrong.

**Failures are values, not exceptions.** Invariant 3 of ``CLAUDE.md`` — a lead is never
silently dropped — is only as strong as its weakest error path. Raising would push the
enumeration of failure modes onto every caller and make "we lost the lead" the default
behaviour of a missed ``except``. So every boundary failure is caught here and returned as
:class:`~leadquali.app.assessment_result.AssessmentFailed` carrying the matching
:class:`~leadquali.domain.models.EscalationReason`, including a final catch-all for
exceptions nobody has seen yet. The two failure modes that would actually hurt the business
are structurally impossible as a result: a failure can never be read as a low score
(:class:`~leadquali.app.assessment_result.AssessmentFailed` has no ``assessment``
attribute), and it can never vanish (there is no path that returns nothing).

**A refusal is checked before content is read, and it escalates.** ``stop_reason ==
"refusal"`` is an HTTP 200 with a safety classifier's verdict attached; an adversarial or
disturbing form submission can trigger it. The check happens before anything touches the
response body, and it produces
:attr:`~leadquali.domain.models.EscalationReason.MODEL_REFUSAL` — never a disqualification.
"The model would not answer" and "this lead is worthless" are different facts.

**Retries are the SDK's, not ours.** ``anthropic.Anthropic`` already retries connection
errors, 408, 409, 429 and 5xx with exponential backoff. Wrapping a second loop around it
multiplies the wall clock and the bill for no gain; see :func:`build_anthropic_client` for
exactly what that covers and what it does not.

**Prices live in one named constant.** :data:`CLAUDE_OPUS_5_PRICES` is the single place a
rate change lands, and it is documented with its source and with the standing instruction
to re-check it against the invoice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import anthropic
import pydantic
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    ParsedMessage,
    TextBlockParam,
    Usage,
)

from leadquali.app.assessment_result import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
    Effort,
)
from leadquali.domain.models import EscalationReason, LeadAssessment
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts import (
    PROMPT_VERSION,
    PromptAssetError,
    PromptVersionMismatchError,
    build_system_blocks,
)

logger = logging.getLogger(__name__)

#: The model the plan specifies (§5). Pinned, never a date-suffixed variant.
MODEL_ID: Final[str] = "claude-opus-5"

#: Output budget for one call. Thinking is adaptive and on by default on ``claude-opus-5``,
#: and thinking tokens count against ``max_tokens`` — so this is far larger than the ~600
#: tokens a :class:`~leadquali.domain.models.LeadAssessment` serialises to. Too small and
#: the response truncates mid-JSON and the whole call is wasted (plan §5: 8000).
MAX_TOKENS: Final[int] = 8000

#: The cache breakpoint marker, applied to the rubric block and to nothing else. Five
#: minutes is the default TTL and the right one here: leads arrive in bursts behind a form.
CACHE_CONTROL_EPHEMERAL: Final[CacheControlEphemeralParam] = {"type": "ephemeral"}

#: Client defaults. The SDK's own default timeout is 10 minutes, which is far too long for
#: a worker with a Lambda deadline; two retries is the SDK default and is left alone.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0
DEFAULT_MAX_RETRIES: Final[int] = 2

_TOKENS_PER_MTOK: Final[Decimal] = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class TokenPrices:
    """US dollars per million tokens, by how the token was billed."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal
    cache_write_usd_per_mtok: Decimal


# ---------------------------------------------------------------------------------------
# PRICES — the one place a rate change lands.
#
# Source: the ``claude-api`` skill, § Current Models (cached 2026-06-24): ``claude-opus-5``
# at $5.00 per MTok input and $25.00 per MTok output, first-party Anthropic API rates. The
# cache multipliers are from the same skill, ``shared/prompt-caching.md`` § Economics: a
# cache read costs 0.1x the base input price ($0.50) and a 5-minute-TTL cache write costs
# 1.25x ($6.25). A 1-hour TTL would be 2x — this adapter only ever writes 5-minute entries.
#
# THESE MUST BE RE-CHECKED AGAINST THE ANTHROPIC INVOICE before anyone bills a customer,
# reports a margin, or sets a price on them. They are a documented snapshot, not a feed:
# published rates change, partner platforms (Bedrock, Vertex) bill differently, and
# negotiated or committed-use rates differ again. The number this module computes is an
# estimate of the invoice, and the invoice wins.
# ---------------------------------------------------------------------------------------
CLAUDE_OPUS_5_PRICES: Final[TokenPrices] = TokenPrices(
    input_usd_per_mtok=Decimal("5.00"),
    output_usd_per_mtok=Decimal("25.00"),
    cache_read_usd_per_mtok=Decimal("0.50"),
    cache_write_usd_per_mtok=Decimal("6.25"),
)


def token_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    prices: TokenPrices = CLAUDE_OPUS_5_PRICES,
) -> Decimal:
    """Cost of one call in US dollars, from its four token counters.

    Exact decimal arithmetic throughout: these values are summed per tenant for usage
    billing, and binary floating point drifts in exactly the direction that produces an
    invoice nobody can reconcile. The four counters are disjoint — ``usage.input_tokens``
    from the API already excludes the cached ones — so this is a plain weighted sum, taken
    with a single division so no intermediate rounding creeps in.

    Args:
        input_tokens: uncached input tokens, billed at the full input rate.
        output_tokens: output tokens, thinking included.
        cache_read_tokens: prefix tokens served from cache.
        cache_creation_tokens: prefix tokens written to cache.
        prices: the rate card to apply; defaults to :data:`CLAUDE_OPUS_5_PRICES`.

    Returns:
        The cost, unrounded. Rounding is the biller's decision, not this function's.
    """
    micro_dollars = (
        Decimal(input_tokens) * prices.input_usd_per_mtok
        + Decimal(output_tokens) * prices.output_usd_per_mtok
        + Decimal(cache_read_tokens) * prices.cache_read_usd_per_mtok
        + Decimal(cache_creation_tokens) * prices.cache_write_usd_per_mtok
    )
    return micro_dollars / _TOKENS_PER_MTOK


def build_anthropic_client(
    api_key: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> anthropic.Anthropic:
    """A configured SDK client, with the retry policy stated where it can be read.

    **What the SDK's built-in retries cover** (source: the ``claude-api`` skill, § Client
    config): connection errors and HTTP 408, 409, 429 and >=500, retried with exponential
    backoff, ``max_retries`` times. That is the entire transient-failure surface of this
    call, which is why there is no retry loop in this module. A second loop layered on top
    would multiply both the wall clock and the bill, and would retry things the SDK already
    decided were not worth retrying.

    **What it does not cover, and what therefore reaches :meth:`AnthropicLeadAssessor.assess`
    as a first and final answer:** 400/401/403/404 (a bad request or a bad key does not get
    better by being asked twice); ``stop_reason == "refusal"``, which is a successful HTTP
    200 the SDK has no reason to touch; and a schema-validation failure of the returned
    JSON. Each of those maps to an :class:`~leadquali.domain.models.EscalationReason`.

    **Budget for the worst case.** Timeouts *are* retried, so total wall clock can reach
    ``timeout_seconds x (max_retries + 1)`` — ~6 minutes at the defaults. A worker running
    under a shorter deadline must lower one of the two rather than discover this in
    production.

    Args:
        api_key: from :meth:`leadquali.config.Settings.require_anthropic_api_key`. Never a
            literal, and never logged.
        timeout_seconds: per-attempt request timeout.
        max_retries: SDK retry attempts after the first.
    """
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=max_retries,
    )


class AnthropicLeadAssessor:
    """Implements :class:`~leadquali.app.ports.LeadAssessorPort` against the Claude API.

    Stateless and cheap to construct, so a Lambda container builds one per cold start and
    reuses it; the SDK client it wraps owns the connection pool and is the expensive part.

    ``effort`` is a constructor parameter rather than a constant because #24 sweeps ``low``
    / ``medium`` / ``high`` against the golden set to find the cheapest level that holds
    accuracy. Sweeping by construction rather than by call argument keeps it off the port's
    signature: effort is a property of how this assessor is configured, not of the lead.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        effort: Effort = DEFAULT_EFFORT,
        model_id: str = MODEL_ID,
        max_tokens: int = MAX_TOKENS,
        prices: TokenPrices = CLAUDE_OPUS_5_PRICES,
    ) -> None:
        """Configure an assessor.

        Args:
            client: an SDK client, usually from :func:`build_anthropic_client`. Injected so
                the whole adapter is exercisable offline against a stub or a mock transport.
            effort: ``output_config.effort`` for every call this assessor makes.
            model_id: the model string; overridable only so an eval can pin a comparison.
            max_tokens: output budget, thinking included.
            prices: rate card used for :attr:`CallMetering.cost_usd`.

        Raises:
            ValueError: ``effort`` is not one of
                :data:`~leadquali.app.assessment_result.EFFORT_LEVELS`.
        """
        if effort not in EFFORT_LEVELS:
            raise ValueError(
                f"unknown effort {effort!r}; claude-opus-5 accepts {', '.join(EFFORT_LEVELS)}"
            )
        self._client = client
        self._effort: Effort = effort
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._prices = prices

    # ------------------------------------------------------------------- the request

    def _system_blocks(self, config: TenantConfig) -> list[TextBlockParam]:
        """Render #10's two prompt blocks into SDK system blocks, breakpoint on block 0.

        Block 0 is the rubric: byte-identical for every tenant and every request, so it is
        the only thing worth caching. Block 1 is the tenant's ICP text, which varies per
        customer — a breakpoint there would buy nothing and still pay the 1.25x write
        premium on the first request of every tenant.
        """
        rubric, tenant = build_system_blocks(config)
        return [
            {"type": "text", "text": rubric.text, "cache_control": CACHE_CONTROL_EPHEMERAL},
            {"type": "text", "text": tenant.text},
        ]

    def _meter(self, usage: Usage, latency_ms: int) -> CallMetering:
        """Turn the response's usage block into a stored metering row.

        Both cache counters are ``Optional`` on the SDK type and absent on a response that
        used no caching, so they are coerced to zero here rather than at four later call
        sites — a ``None`` reaching the cost arithmetic would be a crash on the cheapest
        possible request.
        """
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read = usage.cache_read_input_tokens or 0
        cache_creation = usage.cache_creation_input_tokens or 0
        return CallMetering(
            model_id=self._model_id,
            prompt_version=PROMPT_VERSION,
            effort=self._effort,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cost_usd=token_cost_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                prices=self._prices,
            ),
            latency_ms=latency_ms,
        )

    # -------------------------------------------------------------------- the call

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        """Assess one lead. Never raises for a failure of the model or the API.

        Args:
            config: the tenant whose ICP block and prompt version this call is made under.
            rendered_lead: #12's rendered user turn, already wrapped in untrusted-data
                delimiters. Sent verbatim as a user message and never as a system block —
                it is attacker-controlled text from a public form, and putting it in the
                system prompt would both invite injection and destroy the cache prefix.

        Returns:
            :class:`~leadquali.app.assessment_result.AssessmentSucceeded` or
            :class:`~leadquali.app.assessment_result.AssessmentFailed`.
        """
        started_ns = time.monotonic_ns()

        def elapsed_ms() -> int:
            return (time.monotonic_ns() - started_ns) // 1_000_000

        def failed(reason: EscalationReason, detail: str) -> AssessmentFailed:
            # Tenant id only: never the lead, never the contact (invariant 5).
            logger.warning(
                "lead assessment failed tenant_id=%s reason=%s detail=%s",
                config.tenant_id,
                reason.value,
                detail,
            )
            return AssessmentFailed(reason=reason, detail=detail, latency_ms=elapsed_ms())

        messages: list[MessageParam] = [{"role": "user", "content": rendered_lead}]
        try:
            response: ParsedMessage[LeadAssessment] = self._client.messages.parse(
                model=self._model_id,
                max_tokens=self._max_tokens,
                system=self._system_blocks(config),
                messages=messages,
                output_format=LeadAssessment,
                output_config={"effort": self._effort},
            )
        # Order matters: APITimeoutError subclasses APIConnectionError and RateLimitError
        # subclasses APIStatusError, so the specific classes have to come first or every
        # timeout would be filed as a generic API error and the one signal that says "the
        # model is thinking for too long at this effort level" would never move.
        except anthropic.APITimeoutError:
            return failed(
                EscalationReason.TIMEOUT,
                f"request timed out after {DEFAULT_MAX_RETRIES + 1} attempt(s)",
            )
        except anthropic.APIConnectionError as exc:
            return failed(EscalationReason.API_ERROR, f"connection error: {type(exc).__name__}")
        except anthropic.RateLimitError:
            # Already retried with backoff by the SDK; arriving here means it kept failing.
            return failed(EscalationReason.API_ERROR, "rate limited (429) after SDK retries")
        except anthropic.APIStatusError as exc:
            # The status only. The response body can echo the submission back, and a lead's
            # free text is PII (invariant 5).
            return failed(EscalationReason.API_ERROR, f"http {exc.status_code} from the API")
        except pydantic.ValidationError as exc:
            # The model returned something that is not a valid LeadAssessment. Field paths
            # only — a validation message quotes the offending input, which is lead text.
            locations = sorted({".".join(str(part) for part in e["loc"]) for e in exc.errors()})
            return failed(
                EscalationReason.PARSE_ERROR,
                f"response failed schema validation at: {', '.join(locations) or '<root>'}",
            )
        except (PromptVersionMismatchError, PromptAssetError) as exc:
            # Ours, and therefore safe to quote: it names the tenant and the missing
            # revision. An operator error, but still an escalation — invariant 3 does not
            # make an exception for our own misconfiguration.
            return failed(EscalationReason.API_ERROR, f"{type(exc).__name__}: {exc}")
        except anthropic.AnthropicError as exc:
            return failed(EscalationReason.API_ERROR, f"sdk error: {type(exc).__name__}")
        except Exception as exc:
            # The backstop that makes invariant 3 true rather than aspirational. Anything
            # unforeseen here would otherwise propagate out of the worker and the lead
            # would be lost, which is strictly worse than a needless human review. The
            # traceback goes to the log; the message does not go into `detail`, because an
            # arbitrary exception's text may contain the payload.
            logger.exception("unexpected error assessing lead tenant_id=%s", config.tenant_id)
            return failed(
                EscalationReason.API_ERROR,
                f"unexpected {type(exc).__name__} from the Anthropic adapter",
            )

        # ---- Refusal first. Nothing below this point may run for a refused response, and
        # nothing above it has touched `response.content`.
        if response.stop_reason == "refusal":
            details = response.stop_details
            category = details.category if details is not None else None
            metering = self._meter(response.usage, elapsed_ms())
            logger.warning(
                "model refused tenant_id=%s category=%s",
                config.tenant_id,
                category,
            )
            return AssessmentFailed(
                reason=EscalationReason.MODEL_REFUSAL,
                detail=f"model declined to answer (category={category or 'unspecified'})",
                latency_ms=metering.latency_ms,
                # A refusal is a billed HTTP 200. Metering it is the difference between
                # seeing that cost and not seeing it.
                metering=metering,
            )

        if response.stop_reason == "max_tokens":
            # Thinking ate the budget before the JSON was finished. Whatever came back is
            # a fragment, not a judgment — treat it as unparseable and raise max_tokens.
            return failed(
                EscalationReason.PARSE_ERROR,
                f"response truncated at max_tokens={self._max_tokens}",
            )

        assessment = response.parsed_output
        if assessment is None:
            # `parse` returns None when no text block carried schema-valid JSON. It should
            # be unreachable given output_config.format, but "should be" is not a guarantee
            # we are willing to route a lead on.
            return failed(
                EscalationReason.PARSE_ERROR,
                f"no structured output in the response (stop_reason={response.stop_reason})",
            )

        return AssessmentSucceeded(
            assessment=assessment,
            metering=self._meter(response.usage, elapsed_ms()),
        )


__all__ = [
    "CACHE_CONTROL_EPHEMERAL",
    "CLAUDE_OPUS_5_PRICES",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TOKENS",
    "MODEL_ID",
    "AnthropicLeadAssessor",
    "TokenPrices",
    "build_anthropic_client",
    "token_cost_usd",
]
