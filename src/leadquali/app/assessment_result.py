"""What an assessment attempt produced: the judgment, or the reason there isn't one.

This is the return type of :class:`~leadquali.app.ports.LeadAssessorPort`, and it is a
closed union of exactly two frozen values — :class:`AssessmentSucceeded` and
:class:`AssessmentFailed`. That shape is deliberate and it is load-bearing.

**Why a result type rather than an exception.** Invariant 3 of ``CLAUDE.md`` says a lead is
never silently dropped: a refusal, a timeout, a 503 and a schema violation are all *normal,
expected* outcomes of qualifying inbound web-form leads, and each one has to reach a human
carrying the reason it happened. An exception makes that the caller's problem — and a
caller that forgets one ``except`` turns a lost lead into a stack trace in CloudWatch. A
union makes the failure branch unavoidable instead: a failure has no ``assessment``
attribute, so there is no way to accidentally read a score off one. The pipeline (#14) maps
:attr:`AssessmentFailed.reason` through ``domain.routing.system_failure`` and gets a
``RoutingDecision`` that escalates.

**Why the adapter does not build the RoutingDecision itself.** Routing is policy, and
policy is #9's. The adapter's job stops at "here is what happened at the API boundary".

**Why metering rides along.** The ``assessments`` table (plan §4) records ``model_id``,
``prompt_version``, ``effort``, the four token counters, ``cost_usd`` and ``latency_ms``
next to the judgment. Returning them together means #13 stores one value and per-tenant
usage billing is a ``SUM``, not a later migration. Failures carry metering too whenever the
call was billed — a refusal is an HTTP 200 and Anthropic charges for it, so a refusal that
was not metered is money the business cannot see. The same argument applies with more force
to a truncation and a schema violation, which burn a whole output budget rather than a few
tokens: every path that got a response records what it cost.

No I/O and no SDK types: ``adapters`` fills this in, ``app`` and ``domain`` read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from leadquali.domain.models import EscalationReason, LeadAssessment

#: Thinking depth and overall token spend for one call. Source: the ``claude-api`` skill,
#: § Thinking & Effort — ``claude-opus-5`` accepts all five levels.
type Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: The accepted levels, in ascending order of spend. Kept as data so #24's sweep can
#: iterate them instead of restating them.
EFFORT_LEVELS: Final[tuple[Effort, ...]] = ("low", "medium", "high", "xhigh", "max")

#: The documented starting point (plan §5). Not a tuning result: #24 sweeps ``low`` /
#: ``medium`` / ``high`` against the golden set and picks the cheapest level that holds
#: accuracy, which is a measurement, and this constant moves when that measurement lands.
DEFAULT_EFFORT: Final[Effort] = "medium"


@dataclass(frozen=True, slots=True)
class CallMetering:
    """Everything the ``assessments`` row records about *how* an answer was obtained.

    Provenance (``model_id``, ``prompt_version``, ``effort``) is what makes "did last
    Tuesday's prompt change make things worse?" and "is ``medium`` still good enough?"
    answerable questions rather than opinions. The token counters are kept separate rather
    than pre-summed because they are billed at four different rates, and because
    ``cache_read_tokens`` sitting at zero across a day is the only visible symptom of a
    broken cache prefix.
    """

    model_id: str
    """The exact model string sent, e.g. ``claude-opus-5``."""

    prompt_version: str
    """The rubric revision that produced this call, e.g. ``rubric_v1``."""

    effort: Effort
    """The ``output_config.effort`` level the call was made at."""

    input_tokens: int
    """Uncached input tokens, billed at the full input rate."""

    output_tokens: int
    """Output tokens, including thinking tokens, billed at the output rate."""

    cache_read_tokens: int
    """Prefix tokens served from cache, billed at 0.1x the input rate."""

    cache_creation_tokens: int
    """Prefix tokens written to cache, billed at 1.25x the input rate (5-minute TTL)."""

    cost_usd: Decimal
    """Computed cost of this one call. ``Decimal`` because it is summed for billing."""

    latency_ms: int
    """Wall-clock time for the whole attempt, retries included."""

    @property
    def total_input_tokens(self) -> int:
        """Every input token the request consumed, however it was billed."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass(frozen=True, slots=True)
class AssessmentSucceeded:
    """The model returned a schema-valid assessment. Judgment only — no tier, no score."""

    assessment: LeadAssessment
    metering: CallMetering
    ok: Literal[True] = True
    """Discriminator. ``isinstance`` narrows too; this is for readability at call sites."""


@dataclass(frozen=True, slots=True)
class AssessmentFailed:
    """No assessment exists, and this says why — in the vocabulary routing understands.

    ``reason`` is one of the four boundary failures the adapter can observe:
    :attr:`~leadquali.domain.models.EscalationReason.MODEL_REFUSAL`,
    :attr:`~leadquali.domain.models.EscalationReason.PARSE_ERROR`,
    :attr:`~leadquali.domain.models.EscalationReason.API_ERROR` and
    :attr:`~leadquali.domain.models.EscalationReason.TIMEOUT`. It is never
    ``LOW_CONFIDENCE`` — that is a judgement about a *successful* assessment and belongs to
    #9, not to the API boundary.

    Every one of them escalates to a human. None of them is ever a disqualification: "the
    model would not answer" and "this lead is worthless" are different facts, and conflating
    them loses real deals.
    """

    reason: EscalationReason
    detail: str
    """One short, PII-free line for the operator: the exception class, status or category."""

    latency_ms: int
    metering: CallMetering | None = None
    """Present whenever a response came back; ``None`` when the call never completed.

    Every HTTP 200 is metered, whatever it turned out to mean — a refusal, a ``max_tokens``
    truncation and a schema violation are all billed, and the last two are billed at the
    full output budget. ``None`` therefore means "no response, no bill": a timeout, a
    connection error, a 4xx/5xx, or a misconfiguration caught before the call was made.
    """

    ok: Literal[False] = False


#: What the port returns. Exhaustive: there is no third state and no ``None``.
type AssessmentOutcome = AssessmentSucceeded | AssessmentFailed


__all__ = [
    "DEFAULT_EFFORT",
    "EFFORT_LEVELS",
    "AssessmentFailed",
    "AssessmentOutcome",
    "AssessmentSucceeded",
    "CallMetering",
    "Effort",
]
