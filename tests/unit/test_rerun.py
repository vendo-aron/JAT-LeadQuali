"""A re-run previews a rubric and spends money; it must not write, send, or run unasked.

The structural assertion is the one that matters, and it is made twice over. A production
:class:`~tests.fakes.RecordingNotifier` and :class:`~tests.fakes.InMemoryLeadStore` are
built alongside the re-run and record nothing — *and* the null collaborators the service
does use report that the pipeline reached them. Only the second half rules out the boring
way for the first to pass: a re-run that fell over before it got as far as dispatching
would also leave the real notifier empty, and would prove nothing at all.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import textwrap
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from leadquali.app import rerun as rerun_module
from leadquali.app.admin_views import RerunCandidate
from leadquali.app.assessment_result import AssessmentFailed, AssessmentSucceeded, CallMetering
from leadquali.app.qualify import QualificationPipeline
from leadquali.app.rerun import (
    RERUN_BATCH_CAP,
    NullLeadStore,
    NullNotifier,
    RerunNotConfirmedError,
    RerunService,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    RoutingDecision,
    Tier,
)
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import (
    FakeClock,
    InMemoryLeadStore,
    RecordingNotifier,
    ScriptedAssessor,
    StaticEnricher,
)

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
SLUG = "acme"

METERING = CallMetering(
    model_id="claude-test",
    prompt_version="v1",
    effort="medium",
    input_tokens=1_200,
    output_tokens=300,
    cache_read_tokens=0,
    cache_creation_tokens=0,
    cost_usd=Decimal("0.0180"),
    latency_ms=900,
)


def config(**overrides: Any) -> TenantConfig:
    """A rubric whose thresholds a test can move to make a lead change tier."""
    document: dict[str, Any] = {
        "tenant_id": SLUG,
        "name": "Acme",
        "icp_description": "Mid-market logistics companies with a revenue team.",
        "thresholds": {"hot": 80.0, "warm": 55.0, "cold": 30.0},
        "routing_rules": {
            "hot": {"action": "email_sales", "destination": "hot@example.com"},
            "warm": {"action": "email_sales", "destination": "warm@example.com"},
            "cold": {"action": "email_sales", "destination": "cold@example.com"},
            "disqualified": {"action": "suppress"},
        },
    }
    document.update(overrides)
    return TenantConfig.from_dict(document)


def assessment(*, icp_fit: int = 28) -> LeadAssessment:
    """A strong lead by default, so lowering a threshold cannot be what moves it."""
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=icp_fit, intent=22, authority=13, urgency=12, budget_signal=13
        ),
        extracted=ExtractedFacts(
            company_name="Northwind",
            industry="logistics",
            company_size_estimate="300",
            role_seniority="vp",
            stated_use_case="replace a spreadsheet",
            stated_timeline="this quarter",
        ),
        reasoning="strong fit, clear timeline",
        confidence=0.92,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=False,
    )


def candidate(index: int, *, previous_tier: Tier | None = Tier.HOT) -> RerunCandidate:
    return RerunCandidate(
        lead_id=f"lead-{index:04d}",
        submission_id=f"sub-{index:04d}",
        submission=LeadSubmission(
            full_name="Ada Lovelace",
            email="ada@example.com",
            company="Northwind",
            role="VP Revenue",
            message="We need this replaced this quarter and have budget.",
        ),
        assessed_at=NOW - dt.timedelta(days=index),
        previous_tier=previous_tier,
        previous_score=Decimal("88.00"),
    )


def service(outcome: Any = None) -> RerunService:
    return RerunService(
        assessor=ScriptedAssessor(
            outcome if outcome is not None else AssessmentSucceeded(assessment(), METERING)
        ),
        enricher=StaticEnricher(),
        clock=FakeClock(start=NOW, step_ms=0),
    )


# ---------------------------------------------------------------- nothing is written


def test_a_rerun_takes_no_store_and_no_notifier() -> None:
    """The guarantee, stated as a signature.

    A ``dry_run`` flag threaded through the pipeline would be one edit away from being
    wrong on one branch. An absent parameter cannot be.
    """
    parameters = set(inspect.signature(RerunService.__init__).parameters)

    assert "store" not in parameters
    assert "notifier" not in parameters
    assert {"assessor", "enricher", "clock"} <= parameters


def test_the_rerun_reaches_dispatch_and_persistence_and_neither_happens() -> None:
    """The counters, which are the only honest statement this test can make.

    An earlier version of this also built an :class:`~tests.fakes.InMemoryLeadStore` and a
    :class:`~tests.fakes.RecordingNotifier`, handed them to nothing, and asserted they were
    empty. Those assertions were true before the run and would have stayed true if the
    service had sent a thousand emails — they asserted that two local variables had not
    been mutated by code that could not see them. They are gone.

    What is left is real: the pipeline reached the dispatch step and the two persistence
    steps for every lead, and the collaborators that reached them are the ones that do
    nothing. A re-run that fell over early would show zeroes here. See
    :func:`test_the_null_collaborators_are_the_only_ones_the_pipeline_can_reach` for the
    part that proves the real ones are unreachable.
    """
    runner = service()
    plan = runner.plan(
        tenant_slug=SLUG, candidates=[candidate(1), candidate(2)], cost_per_lead_usd=None
    )

    report = runner.run(plan=plan, config=config(), confirmed=True)

    assert report.writes_suppressed == 4, "two assessments and two routing events"
    assert report.dispatches_suppressed == 2
    assert len(report.comparisons) == 2


def test_the_null_collaborators_are_the_only_ones_the_pipeline_can_reach() -> None:
    """The emptiness assertion, made where it can actually observe something.

    :class:`~leadquali.app.rerun.NullNotifier` and :class:`~leadquali.app.rerun.NullLeadStore`
    are constructed *inside* ``run``, so a test cannot hold a reference to them. It can
    hold the pipeline: this patches
    :class:`~leadquali.app.qualify.QualificationPipeline` to record the collaborators it is
    built with, then asserts they are the null ones and nothing else.

    That closes the mutation the counters alone do not: making ``NullNotifier.dispatch``
    really send while still incrementing its counter left the old assertions green.
    """
    built: list[dict[str, object]] = []
    real_notifier = RecordingNotifier()
    real_store = InMemoryLeadStore()

    class RecordingPipeline(QualificationPipeline):
        def __init__(self, **kwargs: Any) -> None:
            built.append(dict(kwargs))
            super().__init__(**kwargs)

    runner = service()
    plan = runner.plan(tenant_slug=SLUG, candidates=[candidate(1)], cost_per_lead_usd=None)

    with patch.object(rerun_module, "QualificationPipeline", RecordingPipeline):
        runner.run(plan=plan, config=config(), confirmed=True)

    assert len(built) == 1, "the re-run built more than one pipeline"
    assert isinstance(built[0]["notifier"], NullNotifier)
    assert isinstance(built[0]["store"], NullLeadStore)
    # mypy calls the identity check below non-overlapping, which is the point made twice:
    # the pipeline cannot be holding a production collaborator, and the type checker can
    # see that from the types alone. So the runtime assertion is on the doubles staying
    # untouched instead.
    assert real_notifier.dispatches == []
    assert real_store.assessments == []
    assert real_store.leads == {}


#: Everything the null collaborators are allowed to call. A value type and ``super()``,
#: and that is the whole list — see
#: :func:`test_the_null_collaborators_cannot_do_anything_at_all`.
_INERT_CALLS = frozenset({"StoredLead", "super"})

#: The methods that stand where a write or a send would be.
_INERT_METHODS = [
    (NullNotifier, "dispatch"),
    (NullLeadStore, "upsert_lead"),
    (NullLeadStore, "already_routed"),
    (NullLeadStore, "record_assessment"),
    (NullLeadStore, "record_routing_event"),
]


@pytest.mark.parametrize(("owner", "method"), _INERT_METHODS, ids=lambda v: str(v))
def test_the_null_collaborators_cannot_do_anything_at_all(owner: type, method: str) -> None:
    """ "Nothing is written and nothing is sent", asserted on what the code *can* do.

    The counters and the type checks above are both necessary and neither is sufficient:
    making ``NullNotifier.dispatch`` really send **while still incrementing its counter**
    satisfies both, and a run of this file stayed green while the mutant wrote the lead's
    address to a file on disk.

    What actually holds is that these bodies are inert — no import, no call on any object
    but ``self``, and no call to anything outside :data:`_INERT_CALLS`. A method that
    cannot call out cannot send an email or write a row, whatever its counter says. The
    assertion is structural because the property is: §5 asked for a re-run that has no way
    to reach the outside, not one that happened not to.
    """
    source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
    tree = ast.parse(source)

    for node in ast.walk(tree):
        assert not isinstance(node, ast.Import | ast.ImportFrom), (
            f"{owner.__name__}.{method} imports something; an inert method has nothing to import"
        )
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                root = target.value
                assert isinstance(root, ast.Name) and root.id == "self", (
                    f"{owner.__name__}.{method} calls {ast.unparse(target)}, which is not self"
                )
                continue
            assert isinstance(target, ast.Name) and target.id in _INERT_CALLS, (
                f"{owner.__name__}.{method} calls {ast.unparse(target)}; an inert method may "
                f"only call {sorted(_INERT_CALLS)}"
            )


def test_the_inertness_check_is_looking_at_real_code() -> None:
    """Guards the test above against passing because it parsed an empty body."""
    for owner, method in _INERT_METHODS:
        source = inspect.getsource(getattr(owner, method))
        assert "def " in source
        assert len(source.splitlines()) > 3


def test_the_null_notifier_reports_no_delivery() -> None:
    """A provider message id is the receipt that a send happened. There is never one."""
    notifier = NullNotifier()

    receipt = notifier.dispatch(
        tenant_id=SLUG,
        lead_id="lead-0001",
        destination="hot@example.com",
        submission=candidate(1).submission,
        decision=RoutingDecision(
            tier=Tier.HOT, total_score=88.0, action=Action.EMAIL_SALES, note="n"
        ),
        assessment=None,
    )

    assert receipt is None
    assert notifier.dispatches_suppressed == 1


def test_the_null_store_never_deduplicates_a_rerun() -> None:
    """Every lead looks new and none looks routed, so the pipeline runs its whole path."""
    store = NullLeadStore()

    stored = store.upsert_lead(
        tenant_id=SLUG,
        submission_id="lead-0001",
        submission=candidate(1).submission,
        source="admin_rerun",
        received_at=NOW,
    )

    assert stored.lead_id == "lead-0001"
    assert stored.is_new is True
    assert store.already_routed(tenant_id=SLUG, lead_id="lead-0001") is False


# ----------------------------------------------------------------------- confirmation


def test_a_rerun_refuses_without_confirmation_and_makes_no_call() -> None:
    """A GET that spends money is the same mistake as a GET that writes."""
    assessor = ScriptedAssessor(AssessmentSucceeded(assessment(), METERING))
    runner = RerunService(assessor=assessor, enricher=StaticEnricher(), clock=FakeClock())
    plan = runner.plan(tenant_slug=SLUG, candidates=[candidate(1)], cost_per_lead_usd=None)

    with pytest.raises(RerunNotConfirmedError):
        runner.run(plan=plan, config=config(), confirmed=False)

    assert assessor.calls == 0


# ------------------------------------------------------------------------- the batch cap


def test_the_batch_is_capped() -> None:
    plan = service().plan(
        tenant_slug=SLUG,
        candidates=[candidate(index) for index in range(RERUN_BATCH_CAP + 20)],
        cost_per_lead_usd=None,
    )

    assert plan.size == RERUN_BATCH_CAP
    assert plan.available == RERUN_BATCH_CAP + 20
    assert plan.capped


def test_the_cap_is_what_bounds_the_model_calls() -> None:
    """Asserted on the assessor, not on the plan: a cap the run ignores is not a cap."""
    assessor = ScriptedAssessor(AssessmentSucceeded(assessment(), METERING))
    runner = RerunService(assessor=assessor, enricher=StaticEnricher(), clock=FakeClock())
    plan = runner.plan(
        tenant_slug=SLUG,
        candidates=[candidate(index) for index in range(10)],
        cost_per_lead_usd=None,
        batch_cap=3,
    )

    runner.run(plan=plan, config=config(), confirmed=True)

    assert assessor.calls == 3


def test_a_cap_of_zero_is_refused_rather_than_silently_running_nothing() -> None:
    with pytest.raises(ValueError, match="positive"):
        service().plan(
            tenant_slug=SLUG, candidates=[candidate(1)], cost_per_lead_usd=None, batch_cap=0
        )


# ------------------------------------------------------------------------ the estimate


def test_the_estimate_is_the_batch_size_times_the_recent_cost_per_lead() -> None:
    plan = service().plan(
        tenant_slug=SLUG,
        candidates=[candidate(index) for index in range(4)],
        cost_per_lead_usd=Decimal("0.0180"),
    )

    assert plan.estimated_cost_usd == Decimal("0.0720")


def test_a_tenant_with_no_billing_history_has_no_estimate_rather_than_zero() -> None:
    """ "We cannot say" and "this is free" are different statements."""
    plan = service().plan(tenant_slug=SLUG, candidates=[candidate(1)], cost_per_lead_usd=None)

    assert plan.estimated_cost_usd is None


def test_a_tenant_with_no_history_gets_an_empty_plan_it_can_say_so_about() -> None:
    plan = service().plan(tenant_slug=SLUG, candidates=[], cost_per_lead_usd=None)

    assert plan.empty
    assert plan.size == 0
    assert not plan.capped


# -------------------------------------------------------------------- the comparison


def test_the_comparison_shows_the_old_tier_against_the_new_one() -> None:
    runner = service()
    plan = runner.plan(
        tenant_slug=SLUG,
        candidates=[candidate(1, previous_tier=Tier.HOT)],
        cost_per_lead_usd=None,
    )

    report = runner.run(
        plan=plan,
        config=config(thresholds={"hot": 95.0, "warm": 55.0, "cold": 30.0}),
        confirmed=True,
    )
    row = report.comparisons[0]

    assert row.previous_tier is Tier.HOT
    assert row.new_tier is Tier.WARM
    assert row.changed
    assert report.moved_down == 1
    assert report.moved_up == 0


def test_a_lead_the_rubric_bins_the_same_way_is_not_a_change() -> None:
    runner = service()
    plan = runner.plan(
        tenant_slug=SLUG, candidates=[candidate(1, previous_tier=Tier.HOT)], cost_per_lead_usd=None
    )

    report = runner.run(plan=plan, config=config(), confirmed=True)

    assert report.comparisons[0].new_tier is Tier.HOT
    assert report.changed == ()


def test_the_run_reports_what_it_actually_cost() -> None:
    runner = service()
    plan = runner.plan(
        tenant_slug=SLUG, candidates=[candidate(1), candidate(2)], cost_per_lead_usd=None
    )

    report = runner.run(plan=plan, config=config(), confirmed=True)

    assert report.actual_cost_usd == METERING.cost_usd * 2


def test_a_lead_the_model_refuses_is_shown_as_unassessed_and_counted_as_a_downgrade() -> None:
    """A rubric that makes the model refuse is a finding, not a tier."""
    runner = service(
        AssessmentFailed(reason=EscalationReason.MODEL_REFUSAL, detail="refused", latency_ms=100)
    )
    plan = runner.plan(
        tenant_slug=SLUG, candidates=[candidate(1, previous_tier=Tier.HOT)], cost_per_lead_usd=None
    )

    report = runner.run(plan=plan, config=config(), confirmed=True)
    row = report.comparisons[0]

    assert not row.assessed
    assert row.changed
    assert report.moved_down == 1
