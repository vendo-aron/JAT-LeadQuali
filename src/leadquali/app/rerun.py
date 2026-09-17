"""Re-running real leads against a candidate rubric, without dispatching anything.

This is the view that makes editing a rubric safe. A diff tells an operator that
``thresholds.hot`` moved from 80 to 85; it does not tell them that eleven of last month's
hot leads become warm, which is the thing they actually wanted to know. So: pick a set of
historical leads, run them through the **real** pipeline against the candidate config, and
show old tier against new tier side by side.

Nothing is written and nothing is sent
--------------------------------------

The pipeline is #14's, unmodified — the same enrichment, the same prompt, the same
scoring and the same routing — wired to a :class:`NullLeadStore` and a
:class:`NullNotifier`. That is a *substitution*, not a flag: there is no ``dry_run``
parameter threaded through :class:`~leadquali.app.qualify.QualificationPipeline` that a
later edit could get wrong on one branch, because the collaborators that could write or
send are not present. :class:`RerunService` takes no store and no notifier, which is the
form the guarantee takes, and ``tests/unit/test_rerun.py`` asserts both halves: the real
notifier and the real store record nothing, *and* the null ones were actually reached — a
re-run that never got as far as dispatching would prove nothing about the ones that do.

Re-running costs money
----------------------

Every lead in a batch is a model call we pay for. Three things follow, and all three are
here rather than in the handler, because a cap enforced by a template is a cap.

* The cost is **estimated before the run**, from #33's own metering: what this tenant's
  assessments have actually cost per billable lead recently, times the batch size. It is
  an estimate and says so — a longer rubric or a slower model moves it — but an operator
  about to spend money deserves a number rather than a shrug.
* The batch is **capped** at :data:`RERUN_BATCH_CAP`. Twenty-five leads is enough to see a
  band move and small enough that a mis-click costs pennies.
* The run **refuses without an explicit confirmation**. A GET that spends money is the
  same mistake as a GET that writes, and for the same reason: things fetch URLs.

Where the money is recorded
---------------------------

A rubric experiment is our cost and not the customer's, so it must not reach an invoice.
With a null store it cannot: #33 computes ``leads_billable`` from ``assessments`` rows, and
a re-run writes none, so the spend is invisible to billing by construction. It is also
therefore invisible to #33 entirely — ``usage_daily`` has no column that could hold "spent
on this tenant, not billable to them". The spend is emitted as a log event instead
(:data:`~leadquali.observability.events.EVENT_ADMIN_RERUN_COMPLETED`), where a CloudWatch metric
filter can total it, and ``docs/admin.md`` says so.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from leadquali.app.admin_views import RerunCandidate
from leadquali.app.assessment_result import AssessmentOutcome
from leadquali.app.ports import (
    ClockPort,
    EnricherPort,
    LeadAssessorPort,
    RoutingOutcome,
    StoredLead,
)
from leadquali.app.qualify import QualificationPipeline, QualificationRequest
from leadquali.domain.models import Action, LeadAssessment, RoutingDecision, Tier
from leadquali.domain.tenant_config import TenantConfig
from leadquali.observability import log_admin_rerun_completed
from leadquali.prompts.lead import LeadSubmission

__all__ = [
    "RERUN_BATCH_CAP",
    "NullLeadStore",
    "NullNotifier",
    "RerunComparison",
    "RerunNotConfirmedError",
    "RerunPlan",
    "RerunReport",
    "RerunService",
]

LOGGER: Final = logging.getLogger(__name__)

#: How many leads one re-run may touch. Twenty-five: enough to see a tier band move,
#: small enough that a mis-click costs pennies rather than a day's inference budget. A
#: named constant because it is a spending limit, and a spending limit that lives in a
#: template is not one.
RERUN_BATCH_CAP: Final[int] = 25

#: The destination the re-run pipeline is built with. It is never used — the notifier is
#: null — but :class:`~leadquali.app.qualify.QualificationPipeline` refuses to be
#: constructed without one, correctly, and a blank string here would be a deployment bug
#: waiting to be copied somewhere it matters.
_UNUSED_ESCALATION_DESTINATION: Final[str] = "rerun@localhost.invalid"


class RerunNotConfirmedError(RuntimeError):
    """The batch was not confirmed, so no model call was made and no money was spent."""


class NullLeadStore:
    """A :class:`~leadquali.app.ports.LeadStorePort` that persists nothing.

    Every lead looks new and none has been routed, so the pipeline runs its whole path
    rather than short-circuiting on the idempotency check. The counters exist so a test
    can prove the pipeline *reached* the writes it is not doing: a re-run that silently
    stopped before recording an assessment would otherwise look identical to one that was
    correctly prevented from writing.
    """

    def __init__(self) -> None:
        self.assessments_suppressed = 0
        self.routing_events_suppressed = 0

    def upsert_lead(
        self,
        *,
        tenant_id: str,
        submission_id: str,
        submission: LeadSubmission,
        source: str,
        received_at: dt.datetime,
    ) -> StoredLead:
        """Hand back the lead's own id without writing a row."""
        del tenant_id, submission, source, received_at
        return StoredLead(lead_id=submission_id, is_new=True)

    def already_routed(self, *, tenant_id: str, lead_id: str) -> bool:
        """Always ``False``: a re-run is not a delivery and must not be deduplicated."""
        del tenant_id, lead_id
        return False

    def record_assessment(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        outcome: AssessmentOutcome,
        decision: RoutingDecision,
        recorded_at: dt.datetime,
    ) -> None:
        """Count the write and discard it."""
        del tenant_id, lead_id, outcome, decision, recorded_at
        self.assessments_suppressed += 1

    def record_routing_event(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        action: Action,
        destination: str | None,
        outcome: RoutingOutcome,
        provider_message_id: str | None,
        occurred_at: dt.datetime,
        detail: str,
    ) -> None:
        """Count the write and discard it."""
        del tenant_id, lead_id, action, destination, outcome, provider_message_id
        del occurred_at, detail
        self.routing_events_suppressed += 1


class NullNotifier:
    """A :class:`~leadquali.app.ports.NotifierPort` that delivers nothing.

    Returns ``None`` rather than raising: raising would make the pipeline record a failed
    dispatch and re-raise, turning every re-run into an exception, and would exercise the
    failure path instead of the one being previewed.
    """

    def __init__(self) -> None:
        self.dispatches_suppressed = 0

    def dispatch(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        destination: str,
        submission: LeadSubmission,
        decision: RoutingDecision,
        assessment: LeadAssessment | None,
    ) -> str | None:
        """Count the send and make none."""
        del tenant_id, lead_id, destination, submission, decision, assessment
        self.dispatches_suppressed += 1
        return None


class _CandidateConfigSource:
    """A :class:`~leadquali.app.ports.TenantConfigPort` serving one candidate rubric.

    The whole point of the re-run is to use a config that is *not* what is stored, so the
    pipeline is given one that answers with the candidate whatever tenant it is asked
    about — and the service only ever asks about the one tenant being previewed.
    """

    def __init__(self, config: TenantConfig) -> None:
        self._config = config

    def get(self, tenant_id: str) -> TenantConfig:
        """The candidate config."""
        del tenant_id
        return self._config


@dataclass(frozen=True, slots=True)
class RerunComparison:
    """One lead's before and after."""

    lead_id: str
    previous_tier: Tier | None
    new_tier: Tier | None
    """What the candidate rubric binned this lead into.

    A lead the model could not assess still has a tier — invariant 3 routes it ``WARM`` and
    escalates rather than scoring it low — so the tier alone does not distinguish "the
    candidate rubric thinks this is warm" from "the model refused under it". :attr:`assessed`
    is what separates them, and the page must render that distinction: a rubric that makes
    the model refuse is a finding, not a downgrade to warm.
    """

    previous_score: Decimal | None
    new_score: Decimal | None
    new_action: Action
    cost_usd: Decimal
    assessed: bool

    @property
    def changed(self) -> bool:
        """Whether the candidate rubric would have binned this lead differently."""
        return self.previous_tier is not self.new_tier


@dataclass(frozen=True, slots=True)
class RerunPlan:
    """What a re-run would do, and what it would cost, before anything is spent."""

    tenant_slug: str
    candidates: tuple[RerunCandidate, ...]
    """Already capped at :data:`RERUN_BATCH_CAP`."""

    available: int
    """How many historical leads there were before the cap, so the page can say "the most
    recent 25 of 412" rather than implying that is all there is."""

    cost_per_lead_usd: Decimal | None
    """This tenant's recent inference cost per billable lead, from #33's rollups, or
    ``None`` when they have never been billed for one."""

    @property
    def size(self) -> int:
        """How many leads this run would assess."""
        return len(self.candidates)

    @property
    def empty(self) -> bool:
        """Whether there is nothing to re-run. The page says so rather than showing a
        table with no rows, which reads as a bug."""
        return not self.candidates

    @property
    def capped(self) -> bool:
        """Whether the cap held anything back."""
        return self.available > self.size

    @property
    def estimated_cost_usd(self) -> Decimal | None:
        """What this batch is expected to cost us, or ``None`` when there is no basis.

        ``None`` rather than zero: "we have never billed this tenant, so we cannot say" and
        "this is free" are different statements, and only one of them is true.
        """
        if self.cost_per_lead_usd is None:
            return None
        return self.cost_per_lead_usd * Decimal(self.size)


@dataclass(frozen=True, slots=True)
class RerunReport:
    """What a re-run actually did. Nothing in here was written or sent."""

    tenant_slug: str
    comparisons: tuple[RerunComparison, ...]
    actual_cost_usd: Decimal
    writes_suppressed: int
    """Store writes the pipeline made that the null store discarded. Non-zero on any run
    that reached an assessment, which is what makes "nothing was written" a claim about a
    path that ran rather than about one that was skipped."""

    dispatches_suppressed: int
    """Sends the pipeline made that the null notifier discarded."""

    @property
    def changed(self) -> tuple[RerunComparison, ...]:
        """The leads the candidate rubric would bin differently. The whole answer."""
        return tuple(row for row in self.comparisons if row.changed)

    @property
    def moved_up(self) -> int:
        """How many leads the candidate rubric promotes."""
        return sum(1 for row in self.changed if _rank(row.new_tier) > _rank(row.previous_tier))

    @property
    def moved_down(self) -> int:
        """How many it demotes — the number that decides whether a change ships."""
        return sum(1 for row in self.changed if _rank(row.new_tier) < _rank(row.previous_tier))


class RerunService:
    """Preview a candidate rubric against real history. Writes nothing, sends nothing.

    There is deliberately **no store and no notifier parameter**. That absence is the
    guarantee: a re-run cannot write or dispatch because the collaborators that could are
    not reachable from here, rather than because a flag was checked correctly on every
    branch.

    Args:
        assessor: The model, behind :class:`~leadquali.app.ports.LeadAssessorPort`. The
            real one — a re-run that used a double would preview nothing.
        enricher: The same enrichment the pipeline does, so the comparison is like for
            like.
        clock: Injected, as everywhere.
        logger: Where the spend event goes.
    """

    def __init__(
        self,
        *,
        assessor: LeadAssessorPort,
        enricher: EnricherPort,
        clock: ClockPort,
        logger: logging.Logger | None = None,
    ) -> None:
        self._assessor = assessor
        self._enricher = enricher
        self._clock = clock
        self._logger = logger if logger is not None else LOGGER

    def plan(
        self,
        *,
        tenant_slug: str,
        candidates: Sequence[RerunCandidate],
        cost_per_lead_usd: Decimal | None,
        batch_cap: int = RERUN_BATCH_CAP,
    ) -> RerunPlan:
        """Decide what a re-run would touch and what it would cost. Spends nothing.

        Args:
            tenant_slug: The tenant whose history is being re-run.
            candidates: Historical leads, newest first, from
                :meth:`~leadquali.app.admin_views.AdminQueryPort.rerun_candidates`.
            cost_per_lead_usd: From #33's metering; ``None`` when unknown.
            batch_cap: The ceiling. Defaults to :data:`RERUN_BATCH_CAP` and exists as an
                argument only so a test can drive the cap without twenty-five model calls.

        Returns:
            The capped plan, with an estimate attached.
        """
        if batch_cap < 1:
            raise ValueError(f"a re-run batch cap must be positive, got {batch_cap}")
        return RerunPlan(
            tenant_slug=tenant_slug,
            candidates=tuple(candidates[:batch_cap]),
            available=len(candidates),
            cost_per_lead_usd=cost_per_lead_usd,
        )

    def run(self, *, plan: RerunPlan, config: TenantConfig, confirmed: bool) -> RerunReport:
        """Re-assess the planned leads against ``config``.

        Args:
            plan: What :meth:`plan` decided, already capped.
            config: The **candidate** rubric — validated by
                :class:`~leadquali.domain.tenant_config.TenantConfig` before it reaches
                here, and deliberately not the one on the tenant's row.
            confirmed: Whether the operator confirmed the spend. ``False`` refuses without
                making a single model call.

        Returns:
            The comparison, plus the counts proving nothing was written or sent.

        Raises:
            RerunNotConfirmedError: ``confirmed`` is ``False``.
        """
        if not confirmed:
            raise RerunNotConfirmedError(
                f"re-running {plan.size} leads costs money and was not confirmed; nothing "
                "was assessed"
            )
        store = NullLeadStore()
        notifier = NullNotifier()
        pipeline = QualificationPipeline(
            config_source=_CandidateConfigSource(config),
            assessor=self._assessor,
            store=store,
            notifier=notifier,
            enricher=self._enricher,
            clock=self._clock,
            escalation_destination=_UNUSED_ESCALATION_DESTINATION,
        )
        comparisons = tuple(
            self._compare(pipeline, plan.tenant_slug, candidate) for candidate in plan.candidates
        )
        spent = sum((row.cost_usd for row in comparisons), Decimal(0))
        report = RerunReport(
            tenant_slug=plan.tenant_slug,
            comparisons=comparisons,
            actual_cost_usd=spent,
            writes_suppressed=store.assessments_suppressed + store.routing_events_suppressed,
            dispatches_suppressed=notifier.dispatches_suppressed,
        )
        self._log_spend(report)
        return report

    # ----------------------------------------------------------------------- internals

    def _compare(
        self, pipeline: QualificationPipeline, tenant_slug: str, candidate: RerunCandidate
    ) -> RerunComparison:
        """Run one lead and put the two answers side by side."""
        result = pipeline.qualify(
            QualificationRequest(
                tenant_id=tenant_slug,
                # The lead's own id, so the null store hands it straight back and the
                # comparison row names the lead an operator can open in the browser.
                submission_id=candidate.lead_id,
                submission=candidate.submission,
                source="admin_rerun",
                received_at=candidate.assessed_at,
            )
        )
        decision = result.decision
        assessed = decision is not None and not decision.escalated
        return RerunComparison(
            lead_id=candidate.lead_id,
            previous_tier=candidate.previous_tier,
            new_tier=decision.tier if decision is not None else None,
            previous_score=candidate.previous_score,
            new_score=(
                Decimal(str(decision.total_score))
                if decision is not None and decision.total_score is not None
                else None
            ),
            new_action=decision.action if decision is not None else Action.ESCALATE_HUMAN,
            cost_usd=result.metering.cost_usd if result.metering is not None else Decimal(0),
            assessed=assessed,
        )

    def _log_spend(self, report: RerunReport) -> None:
        """Record what the experiment cost, without a lead's data anywhere in it.

        #33's ``usage_daily`` cannot express "spent on this tenant and not billable to
        them" — every column in it is either a count of rows or a sum over ``assessments``,
        and a re-run writes neither — so this event is where the number lives. The fields
        are counts and money, which is all invariant 5 permits a log line about a lead to
        carry.
        """
        log_admin_rerun_completed(
            self._logger,
            tenant_id=report.tenant_slug,
            leads=len(report.comparisons),
            tier_changes=len(report.changed),
            cost_usd=report.actual_cost_usd,
        )


def _rank(tier: Tier | None) -> int:
    """A tier's rank, with "could not assess" below every real tier.

    Below rather than beside: a lead the candidate rubric cannot get an answer for is a
    worse outcome than the lowest tier, and counting it as a downgrade puts it in the
    number an operator looks at before shipping a change.
    """
    return tier.rank if tier is not None else -1
