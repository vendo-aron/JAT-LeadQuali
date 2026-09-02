"""What happens to the lead: the decision half of "the model assesses, code routes".

Everything here is a pure function from an assessment (or the absence of one) plus a tenant
config to a :class:`~leadquali.domain.models.RoutingDecision`. No LLM, no I/O, no clock —
which is the point: routing can be changed, reviewed and re-tested without touching a
prompt or re-running an eval, and every decision the system has ever made can be reproduced
from the two values that produced it.

Precedence in :func:`decide` is deliberate, and the order is the whole design:

1. **Spam first.** A spam or test submission is not a lead at all, so scoring it and gating
   it on confidence would be meaningless work on meaningless input. Putting it first also
   guarantees the one combination :class:`RoutingDecision` forbids can never be built: a
   spam lead the model was also unsure about is suppressed *without* an escalation reason,
   because suppression and escalation are mutually exclusive outcomes.
2. **The confidence gate second, before tiering.** A score computed from an assessment the
   model does not stand behind is not evidence, and tiering on it would launder uncertainty
   into a confident-looking tier. Uncertainty escalates and never disqualifies (invariant
   3): the lead goes to sales as ``WARM`` with a note saying why, which is the cheap side of
   an asymmetric bet — a needless look costs minutes, a missed deal costs the deal.
3. **The tenant's rubric last.** Only a lead that is real and confidently assessed is worth
   comparing against thresholds, and then the tenant's own table decides where it goes.

Invariant 3 — "a lead is never silently dropped" — is a statement about *uncertainty*, not
about low scores, and it is enforced structurally by that precedence. :data:`Action.SUPPRESS`
is reachable from exactly two places, and nowhere else:

* step 1, an explicit ``spam_or_test_submission`` from the model; and
* step 3, a confidently-assessed lead whose computed tier the tenant has configured to
  suppress — the shipped default does this for ``disqualified`` (< 30), which is the whole
  reason that tier exists.

What is unreachable is suppression by *doubt*. No escalation path can ever suppress or
disqualify: not the confidence gate, at any score down to zero, and not
:func:`system_failure` for any reason. Those paths do not consult the tenant's routing
table at all — a tenant configures what happens to leads it has judged, and a lead the
system failed to judge has not been judged. ``tests/unit/test_routing.py`` asserts both
halves as properties over the whole input space, because a lead dropped by a bug produces
no alert, no bounce and no complaint; it is simply gone.

Neither suppression is silent: both decisions are persisted with the assessment that
produced them, and both carry a note saying which of the two happened. "The model called it
spam" and "it scored 11/100 against this tenant's rubric" are completely different answers
to "why did we never contact this lead?", and #21's metrics have to be able to tell them
apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from leadquali.domain.models import (
    Action,
    EscalationReason,
    LeadAssessment,
    RoutingDecision,
    Tier,
)
from leadquali.domain.scoring import weighted_total
from leadquali.domain.tenant_config import TenantConfig

#: Note on a decision reached because the model flagged the submission itself.
SPAM_NOTE: Final[str] = "suppressed: the model identified this as a spam or test submission"

#: Note on a decision reached through the confidence gate. Plan section 5's wording, kept
#: verbatim so the phrase a salesperson learns to recognise never drifts.
LOW_CONFIDENCE_NOTE: Final[str] = "low model confidence — human review"

#: Opening of the note on a lead suppressed by the tenant's own routing table, as opposed
#: to one the model called spam. The two are distinguishable by prefix on purpose: they are
#: different answers to "why did we never contact this lead?", and #21 groups on them.
LOW_SCORE_SUPPRESSION_NOTE: Final[str] = (
    "suppressed: scored below this tenant's qualification threshold"
)

#: Opening words of every note produced by :func:`system_failure`. Dependents match on this
#: prefix rather than on the full sentence.
SYSTEM_FAILURE_BANNER: Final[str] = "system could not assess"

#: Where a lead goes when there is no assessment at all. ``WARM`` and ``EMAIL_SALES``
#: because the lead must reach a person who can qualify it by hand, and because the tier a
#: failed assessment deserves is unknown — never ``DISQUALIFIED``, which would assert a
#: judgement the system explicitly failed to make.
SYSTEM_FAILURE_TIER: Final[Tier] = Tier.WARM
SYSTEM_FAILURE_ACTION: Final[Action] = Action.EMAIL_SALES

#: Why a human is looking at this lead, in words that belong in an email rather than a log
#: line. Distinct per reason so the recipient knows whether to retry or to escalate.
_FAILURE_EXPLANATIONS: Final[Mapping[EscalationReason, str]] = {
    EscalationReason.LOW_CONFIDENCE: "the model was not confident enough to score this lead",
    EscalationReason.MODEL_REFUSAL: "the model declined to assess this submission",
    EscalationReason.PARSE_ERROR: "the model's response did not match the expected schema",
    EscalationReason.API_ERROR: "the assessment service returned an error",
    EscalationReason.TIMEOUT: "the assessment did not finish in time",
}


def decide(assessment: LeadAssessment, cfg: TenantConfig) -> RoutingDecision:
    """Turn one assessment into one routing decision, deterministically.

    Precedence — spam, then the confidence gate, then the tenant's rubric — and the reasons
    for it are documented at module level; they are policy, not implementation detail.

    Args:
        assessment: What the model said about this lead. Judgement only.
        cfg: The tenant's weights, thresholds, confidence gate and routing table.

    Returns:
        A decision carrying the tier, the action, the score it was reached with and a note
        for whoever receives the lead. :data:`Action.SUPPRESS` appears only via an explicit
        ``spam_or_test_submission``, or via a confident assessment whose computed tier the
        tenant configured to suppress. It is unreachable from the confidence gate at any
        score, and the note says which of the two suppressions happened.
    """
    if assessment.spam_or_test_submission:
        # No score is recorded: nothing was qualified, so a number here would imply a
        # judgement about a submission we have decided is not a lead.
        return RoutingDecision(
            tier=Tier.DISQUALIFIED,
            action=Action.SUPPRESS,
            total_score=0.0,
            note=SPAM_NOTE,
        )

    total = weighted_total(assessment.dimension_scores, cfg)

    if assessment.confidence < cfg.min_confidence:
        # The gate is strictly "below": a tenant that sets 0.6 means 0.6 is good enough.
        # The score is still recorded — it was computed, and the human reviewing the lead
        # deserves to see what the model's own numbers implied, flagged as unreliable.
        return RoutingDecision(
            tier=Tier.WARM,
            action=Action.EMAIL_SALES,
            total_score=total,
            note=LOW_CONFIDENCE_NOTE,
            escalation_reason=EscalationReason.LOW_CONFIDENCE,
        )

    tier = cfg.tier_for(total)
    action = cfg.action_for(tier)
    if action is Action.SUPPRESS:
        # The tenant configured this tier to go nowhere, and the assessment behind it was
        # confident, so this is a judgement rather than a doubt: honour it (invariant 1 —
        # the routing table is the tenant's, not ours). It is recorded, not dropped, and
        # the note distinguishes it from a spam suppression.
        return RoutingDecision(
            tier=tier,
            action=action,
            total_score=total,
            note=f"{LOW_SCORE_SUPPRESSION_NOTE} ({total:.2f}/100)",
        )

    return RoutingDecision(
        tier=tier,
        action=action,
        total_score=total,
        note=f"scored {total:.2f}/100 — {tier.value}",
    )


def system_failure(reason: EscalationReason, detail: str = "") -> RoutingDecision:
    """The decision for a lead that could never be assessed at all.

    Used by the pipeline (#14) and the LLM adapter (#11) when the model refused, the
    response would not parse, the API failed or the call timed out. The lead reaches sales
    unqualified, banner first, so a person qualifies it by hand: a failure of ours must
    never look like a judgement about the lead, and must never be a dead end.

    Args:
        reason: Which failure occurred; recorded for grouping in observability.
        detail: Optional operator-facing specifics (an error class, a retry count).
            Whitespace is collapsed, since this ends up in an email body. Must not contain
            PII: notes are stored and displayed (invariant 5).

    Returns:
        A ``WARM`` / ``EMAIL_SALES`` decision carrying ``reason`` and a score of ``0.0``,
        because nothing was scored. Never ``DISQUALIFIED`` and never suppressed.
    """
    note = f"{SYSTEM_FAILURE_BANNER}: {_FAILURE_EXPLANATIONS[reason]}"
    collapsed = " ".join(detail.split())
    if collapsed:
        note = f"{note} ({collapsed})"
    return RoutingDecision(
        tier=SYSTEM_FAILURE_TIER,
        action=SYSTEM_FAILURE_ACTION,
        total_score=0.0,
        note=note,
        escalation_reason=reason,
    )


__all__ = [
    "LOW_CONFIDENCE_NOTE",
    "LOW_SCORE_SUPPRESSION_NOTE",
    "SPAM_NOTE",
    "SYSTEM_FAILURE_ACTION",
    "SYSTEM_FAILURE_BANNER",
    "SYSTEM_FAILURE_TIER",
    "decide",
    "system_failure",
]
