"""The core domain contract: what the model judges, and what code decides.

Two families of types live here, and the boundary between them is the most important line
in the system:

* :class:`LeadAssessment` (with :class:`DimensionScores` and :class:`ExtractedFacts`) is
  the **model's** output. It carries judgment — scores, extracted facts, confidence — and
  nothing else. It is handed to the LLM verbatim as a structured-output schema, so every
  field is JSON-schema-expressible and carries a description that steers the model.
* :class:`RoutingDecision` (with :class:`Tier`, :class:`Action` and
  :class:`EscalationReason`) is **code's** output, produced by the deterministic layer from
  an assessment plus tenant configuration.

Invariant 2 of ``CLAUDE.md`` is absolute: ``tier``, ``total_score`` and any routing
instruction must never appear in :class:`LeadAssessment`, at any depth. A reviewer should
be able to confirm that by reading this module alone; ``tests/unit/
test_assessment_schema_purity.py`` proves it against the generated JSON schema.

Every model is frozen. An assessment is a record of what the model said at one instant and
a decision is a record of what policy concluded from it — both are values, not workspaces.
Mutating one after the fact would make the audit trail a lie, and freezing also makes them
hashable and safe to pass across layers. Every model is ``extra="forbid"``: if the model
volunteers a field we did not ask for (a tier, most of all), validation fails loudly
instead of silently dropping it.

Pure Pydantic v2 and the standard library: no I/O, no SDK imports, no configuration
values. Thresholds, weights and routing rules are tenant configuration (invariant 1) and
live nowhere near this file.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Highest value ``DimensionScores`` can sum to, and the scale weighted totals are
#: normalised onto. Thresholds that carve this range into tiers are tenant configuration.
MAX_TOTAL_SCORE: float = 100.0


class Tier(StrEnum):
    """How much of a salesperson's attention a lead has earned.

    Ordering is **not** the enum's string ordering — ``Tier.COLD < Tier.HOT`` is ``True``
    only because ``"cold"`` sorts before ``"hot"``. Compare :attr:`rank` instead.
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DISQUALIFIED = "disqualified"

    @property
    def rank(self) -> int:
        """Order by value to the business, ascending: disqualified 0 … hot 3.

        Use this for every comparison, sort and "at least as good as" check. The
        lexicographic ordering inherited from :class:`str` would rank ``cold`` above
        ``hot`` and no one would notice until sales did.
        """
        return _TIER_RANKS[self]


_TIER_RANKS: dict[Tier, int] = {
    Tier.DISQUALIFIED: 0,
    Tier.COLD: 1,
    Tier.WARM: 2,
    Tier.HOT: 3,
}


class Action(StrEnum):
    """What the system does with a lead once it has been placed in a tier."""

    EMAIL_SALES = "email_sales"
    """Notify the sales destination configured for the tenant."""

    ESCALATE_HUMAN = "escalate_human"
    """Put it in front of a person because the system is not sure. Never a dead end."""

    SUPPRESS = "suppress"
    """Record it and stop. Reachable only from an explicit spam/test determination."""


class EscalationReason(StrEnum):
    """Why a decision fell back to a human.

    Recorded so observability and the eval harness can group on it: a rise in
    ``LOW_CONFIDENCE`` is a prompt or rubric problem, a rise in ``API_ERROR`` is an
    operational one, and the two need different people woken up.
    """

    LOW_CONFIDENCE = "low_confidence"
    MODEL_REFUSAL = "model_refusal"
    PARSE_ERROR = "parse_error"
    API_ERROR = "api_error"
    TIMEOUT = "timeout"


class DimensionScores(BaseModel):
    """The five judgment axes, each bounded by its own weight in the rubric.

    The bounds are the model's answer space, not the tenant's policy: a tenant reweights
    these dimensions in configuration, it does not change what a raw ``icp_fit`` of 30
    means.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    icp_fit: int = Field(
        ge=0,
        le=30,
        description="How closely the lead matches the ideal customer profile, 0-30.",
    )
    intent: int = Field(
        ge=0,
        le=25,
        description="Evidence the lead is trying to solve this problem now, 0-25.",
    )
    authority: int = Field(
        ge=0,
        le=15,
        description="Evidence the contact can buy or sponsor a purchase, 0-15.",
    )
    urgency: int = Field(
        ge=0,
        le=15,
        description="Evidence of a deadline, trigger event or compelling event, 0-15.",
    )
    budget_signal: int = Field(
        ge=0,
        le=15,
        description="Evidence the lead can fund a purchase of this size, 0-15.",
    )


class ExtractedFacts(BaseModel):
    """Facts read off the submission, each ``None`` when the lead did not supply it.

    Every field is required-but-nullable rather than defaulted: a web-form lead is usually
    sparse, and an explicit ``null`` from the model ("I looked, it is not there") is a
    different and more useful signal than a key the model forgot to emit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    company_name: str | None = Field(description="Company name as stated, or null.")
    industry: str | None = Field(description="Industry or sector, or null.")
    company_size_estimate: str | None = Field(
        description="Headcount or revenue band as stated or inferred, or null."
    )
    role_seniority: str | None = Field(
        description="Seniority of the contact, e.g. 'ic', 'manager', 'vp', or null."
    )
    stated_use_case: str | None = Field(
        description="What the lead says they want to do, in their own words, or null."
    )
    stated_timeline: str | None = Field(
        description="Any timeframe the lead states for deciding or buying, or null."
    )


class LeadAssessment(BaseModel):
    """The model's complete output for one lead — judgment only, never policy.

    Handed to the LLM as its structured-output schema, so the field descriptions are part
    of the prompt. Nothing here says what happens next.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension_scores: DimensionScores = Field(
        description="Score for each rubric dimension, within its stated range."
    )
    extracted: ExtractedFacts = Field(
        description="Facts read off the submission; null for anything not stated."
    )
    reasoning: str = Field(
        min_length=1,
        description="2-4 sentences citing specific evidence from the lead for the scores.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How confident you are in this assessment, 0.0-1.0. Be honest: a low value is "
            "handled safely, an overconfident one is not."
        ),
    )
    missing_information: list[str] = Field(
        description="Facts that would most change this assessment if known. May be empty."
    )
    suggested_first_question: str | None = Field(
        description="One question a salesperson should open with, or null if none helps."
    )
    spam_or_test_submission: bool = Field(
        description=(
            "True only for an obvious spam, bot or internal test submission. A vague or "
            "unpromising lead is not spam."
        )
    )


class RoutingDecision(BaseModel):
    """What the deterministic layer concluded. Produced by code, never by the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Tier
    action: Action
    total_score: float = Field(
        default=0.0,
        ge=0.0,
        le=MAX_TOTAL_SCORE,
        description=(
            "Weighted total on the 0-100 scale. Zero when no score was computed, e.g. a "
            "suppressed spam submission or a failure before assessment."
        ),
    )
    note: str = Field(
        default="",
        description="Human-readable explanation, shown to whoever receives the lead.",
    )
    escalation_reason: EscalationReason | None = Field(
        default=None,
        description="Set when this decision is a fallback to a human, for grouping.",
    )

    @property
    def escalated(self) -> bool:
        """Whether this decision was reached by falling back to a human."""
        return self.escalation_reason is not None

    @model_validator(mode="after")
    def _escalation_is_never_suppression(self) -> RoutingDecision:
        """Invariant 3: a lead is never silently dropped.

        Suppression is only ever an explicit spam/test determination. If something went
        wrong or we were unsure, that is an escalation, and the two can never be the same
        decision.
        """
        if self.escalation_reason is not None and self.action is Action.SUPPRESS:
            raise ValueError(
                "an escalated decision cannot suppress the lead: "
                f"escalation_reason={self.escalation_reason.value}"
            )
        return self
