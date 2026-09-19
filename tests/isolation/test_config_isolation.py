"""Config isolation: the same lead, two tenants, two different answers — and no crossover.

Invariant 1 says the rubric is tenant configuration rather than code, which is what makes
onboarding a customer a config write. The isolation question that follows is: can one
tenant's rubric, thresholds or routing table reach another tenant's lead? The answer has to
hold at two levels, and this module asserts both, because passing at one of them says
nothing about the other:

* **Policy.** One identical :class:`~leadquali.domain.models.LeadAssessment` decided under
  ``tenants/default.json`` and under ``tenants/acme-demo.json`` produces a different tier,
  a different action *and* a different destination. All three, because two configs that
  differed only in tier would leave "and then it is routed the same way" untested.
* **Pipeline.** The same lead through the real
  :class:`~leadquali.app.qualify.QualificationPipeline` twice, once per tenant, with a
  scripted assessor so the model's answer is held constant. The assertion is on where the
  notifier was told to send it and on what the store recorded — because the interesting
  failure is not ``TenantConfig`` returning the wrong document, it is a pipeline that
  fetches the right one and then routes with a cached, shared or defaulted one.

The two real shipped tenant files are used rather than fixtures invented here. They differ
in every field that matters — weights, all three thresholds, ``min_confidence`` and every
routing rule — and a change to either that collapsed those differences would make this
module fail, which is the right outcome: the demo config exists to be visibly different.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

import pytest

from leadquali.adapters.tenant_config_json import JsonFileTenantConfigLoader, default_tenants_dir
from leadquali.app.assessment_result import AssessmentSucceeded, CallMetering
from leadquali.app.qualify import (
    Disposition,
    QualificationPipeline,
    QualificationRequest,
    QualificationResult,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.routing import decide
from leadquali.domain.scoring import weighted_total
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import (
    FakeClock,
    InMemoryLeadStore,
    RecordingNotifier,
    ScriptedAssessor,
    StaticConfigSource,
    StaticEnricher,
)

#: The two tenants shipped in ``tenants/``. Named here rather than reusing the isolation
#: package's synthetic A and B because the *point* of this module is that two real,
#: differently-configured customers get different answers.
TENANT_DEFAULT: Final[str] = "default"
TENANT_ACME: Final[str] = "acme-demo"

OPERATOR_INBOX: Final[str] = "escalations@leadquali.invalid"

#: A lead that lands on different sides of both tenants' boundaries. Under ``default``'s
#: flat weights it scores 43.0 — cold, emailed to sales. Under ``acme-demo``'s weighting of
#: authority and budget it scores 52.35 — warm, and ``acme-demo`` escalates warm leads to a
#: human triage inbox. Twelve points of margin on every threshold involved, so this is a
#: statement about the two policies and not about rounding.
DIVERGENT_SCORES: Final[Mapping[str, int]] = {
    "icp_fit": 27,
    "intent": 4,
    "authority": 9,
    "urgency": 0,
    "budget_signal": 3,
}

#: Above ``default``'s ``min_confidence`` of 0.6 and below ``acme-demo``'s 0.75. The same
#: assessment is therefore trustworthy to one tenant and not to the other, which is a
#: second, independent axis of the same guarantee.
STRADDLING_CONFIDENCE: Final[float] = 0.70

SUBMISSION: Final[LeadSubmission] = LeadSubmission(
    full_name="Hedy Lamarr",
    email="hedy@frequency-hopping.invalid",
    company="Frequency Hopping GmbH",
    role="Head of Operations",
    message="We run four production lines and lose a shift a month to unplanned downtime.",
    extra={},
)

METERING: Final[CallMetering] = CallMetering(
    model_id="claude-opus-5",
    prompt_version="rubric_v1",
    effort="medium",
    input_tokens=512,
    output_tokens=200,
    cache_read_tokens=1024,
    cache_creation_tokens=0,
    cost_usd=Decimal("0.012000"),
    latency_ms=3100,
)

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)


def assessment(*, confidence: float = 0.9) -> LeadAssessment:
    """One assessment, identical for both tenants. Judgment only — no tier, no score."""
    return LeadAssessment(
        dimension_scores=DimensionScores(**DIVERGENT_SCORES),
        extracted=ExtractedFacts(
            company_name="Frequency Hopping GmbH",
            industry="discrete manufacturing",
            company_size_estimate="200-500",
            role_seniority="head of function",
            stated_use_case="unplanned downtime reporting",
            stated_timeline=None,
        ),
        reasoning="A plausible operations lead with a concrete problem and no stated budget.",
        confidence=confidence,
        missing_information=["budget"],
        suggested_first_question="Which line loses the most hours?",
        spam_or_test_submission=False,
    )


@pytest.fixture(scope="module")
def configs() -> Mapping[str, TenantConfig]:
    """Both shipped tenant configurations, loaded the way the Phase 1 entrypoint does."""
    loader = JsonFileTenantConfigLoader(default_tenants_dir())
    return {TENANT_DEFAULT: loader.get(TENANT_DEFAULT), TENANT_ACME: loader.get(TENANT_ACME)}


# ------------------------------------------------------------------------ the policies


def test_the_two_shipped_configs_disagree_about_everything_that_routes_a_lead(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The premise the rest of the module rests on, asserted rather than assumed.

    If ``tenants/acme-demo.json`` were ever edited into a copy of ``tenants/default.json``,
    every test below would still pass while proving nothing at all.
    """
    left, right = configs[TENANT_DEFAULT], configs[TENANT_ACME]
    assert left.tenant_id != right.tenant_id
    assert left.icp_description != right.icp_description
    assert left.min_confidence != right.min_confidence
    assert left.weight_for("authority") != right.weight_for("authority")
    for band in ("hot", "warm", "cold"):
        assert getattr(left.thresholds, band) != getattr(right.thresholds, band), band
    assert left.routing_rules != right.routing_rules


def test_one_assessment_decides_differently_under_each_tenants_policy(
    configs: Mapping[str, TenantConfig],
) -> None:
    """Tier, action and destination — all three differ for the same model output.

    This is config isolation stated positively: the tenant's own document, and nothing
    else, decides what happens to its lead.
    """
    judged = assessment()
    left = decide(judged, configs[TENANT_DEFAULT])
    right = decide(judged, configs[TENANT_ACME])

    assert left.tier is Tier.COLD and right.tier is Tier.WARM
    assert left.action is Action.EMAIL_SALES and right.action is Action.ESCALATE_HUMAN

    left_destination = configs[TENANT_DEFAULT].destination_for(left.tier)
    right_destination = configs[TENANT_ACME].destination_for(right.tier)
    assert left_destination == "sales@example.invalid"
    assert right_destination == "inside-sales-triage@acme-demo.invalid"
    assert left_destination != right_destination


def test_the_scores_are_not_sitting_on_a_threshold(configs: Mapping[str, TenantConfig]) -> None:
    """The divergence is a property of the policies, not of a rounding boundary.

    A fixture that scored 55.01 against a threshold of 55.0 would make this module fail the
    day somebody changed a weight by a hundredth, and pass for the wrong reason until then.
    """
    scores = DimensionScores(**DIVERGENT_SCORES)
    left = weighted_total(scores, configs[TENANT_DEFAULT])
    right = weighted_total(scores, configs[TENANT_ACME])

    assert left == pytest.approx(43.0)
    assert right == pytest.approx(52.35)

    # Each score sits at least ten points inside its band, on both sides.
    for total, tenant, band in ((left, TENANT_DEFAULT, "cold"), (right, TENANT_ACME, "warm")):
        thresholds = configs[tenant].thresholds
        above = getattr(thresholds, band)
        below = thresholds.warm if band == "cold" else thresholds.hot
        assert total - above >= 10.0, f"{tenant} is only {total - above:.2f} above {band}"
        assert below - total >= 10.0, f"{tenant} is only {below - total:.2f} below the next band"


def test_one_tenants_confidence_floor_does_not_apply_to_another(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The second axis: the same confidence is good enough for one tenant and not the other.

    ``acme-demo`` buys slowly and committee-driven, so it sets a higher bar and gets an
    escalation where ``default`` gets a routed lead. Nothing about that judgement may leak
    either way.
    """
    judged = assessment(confidence=STRADDLING_CONFIDENCE)
    left = decide(judged, configs[TENANT_DEFAULT])
    right = decide(judged, configs[TENANT_ACME])

    assert left.escalation_reason is None
    assert left.tier is Tier.COLD
    assert right.escalation_reason is EscalationReason.LOW_CONFIDENCE
    # The confidence gate routes to warm for everyone — that part is policy, not tenant
    # configuration (#9) — but *which inbox* warm means is still the tenant's own.
    assert right.tier is Tier.WARM
    assert configs[TENANT_DEFAULT].destination_for(left.tier) != configs[
        TENANT_ACME
    ].destination_for(right.tier)


# ------------------------------------------------------------------------ the pipeline


class TwoTenantRun:
    """One pipeline over both tenants' real configs, run once per tenant.

    Deliberately *one* pipeline rather than one per tenant: a pipeline constructed fresh
    for each tenant could not exhibit the failure this is looking for, which is state —
    a cached config, a memoised destination — carried from one lead to the next.
    """

    def __init__(self, configs: Mapping[str, TenantConfig], *, confidence: float = 0.9) -> None:
        self.store = InMemoryLeadStore()
        self.notifier = RecordingNotifier()
        self.assessor = ScriptedAssessor(
            AssessmentSucceeded(assessment=assessment(confidence=confidence), metering=METERING)
        )
        self.pipeline = QualificationPipeline(
            config_source=StaticConfigSource(configs),
            assessor=self.assessor,
            store=self.store,
            notifier=self.notifier,
            enricher=StaticEnricher(),
            clock=FakeClock(start=NOW, step_ms=1),
            escalation_destination=OPERATOR_INBOX,
        )

    def qualify(self, tenant_id: str) -> QualificationResult:
        """Run the identical lead for one tenant."""
        return self.pipeline.qualify(
            QualificationRequest(
                tenant_id=tenant_id,
                submission_id=f"config-isolation-{tenant_id}",
                submission=SUBMISSION,
                received_at=NOW,
            )
        )


def test_the_same_lead_is_routed_to_each_tenants_own_destination(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The pipeline-level assertion, on what the notifier was actually told to do.

    ``TenantConfig`` being right is not the same thing as the pipeline using it. This runs
    the real orchestration — config lookup, enrichment, render, assess, decide, persist,
    dispatch — and reads the answer off the notifier.
    """
    run = TwoTenantRun(configs)
    run.qualify(TENANT_DEFAULT)
    run.qualify(TENANT_ACME)

    dispatched = {item.tenant_id: item for item in run.notifier.dispatches}
    assert set(dispatched) == {TENANT_DEFAULT, TENANT_ACME}
    assert dispatched[TENANT_DEFAULT].destination == "sales@example.invalid"
    assert dispatched[TENANT_ACME].destination == "inside-sales-triage@acme-demo.invalid"
    assert dispatched[TENANT_DEFAULT].decision.tier is Tier.COLD
    assert dispatched[TENANT_ACME].decision.tier is Tier.WARM
    assert dispatched[TENANT_DEFAULT].decision.action is Action.EMAIL_SALES
    assert dispatched[TENANT_ACME].decision.action is Action.ESCALATE_HUMAN


def test_neither_tenants_lead_ever_reaches_the_other_tenants_inbox(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The negative half: no destination from one config appears on the other's dispatch.

    Built from the configs rather than from hardcoded addresses, so adding a routing rule
    to either file extends this test without anybody editing it.
    """
    run = TwoTenantRun(configs)
    run.qualify(TENANT_DEFAULT)
    run.qualify(TENANT_ACME)

    destinations = {
        tenant: {
            configs[tenant].destination_for(tier)
            for tier in Tier
            if configs[tenant].destination_for(tier)
        }
        for tenant in (TENANT_DEFAULT, TENANT_ACME)
    }
    other = {TENANT_DEFAULT: TENANT_ACME, TENANT_ACME: TENANT_DEFAULT}
    for item in run.notifier.dispatches:
        forbidden = destinations[other[item.tenant_id]] - destinations[item.tenant_id]
        assert item.destination not in forbidden, item
        assert item.destination != OPERATOR_INBOX, (
            "a tenant with a usable routing rule should never reach the operator fallback"
        )


def test_the_assessor_is_handed_the_calling_tenants_own_profile(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The prompt is configuration too, and it is the most expensive thing to get wrong.

    A lead assessed against another customer's ICP text produces a plausible, wrong score —
    no exception, no error, and nothing downstream that could notice. So the config each
    call was made under is read off the assessor.
    """
    run = TwoTenantRun(configs)
    run.qualify(TENANT_DEFAULT)
    run.qualify(TENANT_ACME)

    assert [config.tenant_id for config in run.assessor.configs] == [TENANT_DEFAULT, TENANT_ACME]
    assert run.assessor.configs[0].icp_description == configs[TENANT_DEFAULT].icp_description
    assert run.assessor.configs[1].icp_description == configs[TENANT_ACME].icp_description
    assert configs[TENANT_ACME].icp_description not in run.assessor.prompts[0], (
        "the default tenant's prompt carries the demo tenant's ICP text"
    )


def test_the_order_the_tenants_are_run_in_changes_nothing(
    configs: Mapping[str, TenantConfig],
) -> None:
    """Run them the other way round and get the same two answers.

    The cheapest test for shared state there is. A pipeline that cached the first tenant's
    config would route the second lead to the first tenant's inbox in one order and look
    fine in the other.
    """
    forwards = TwoTenantRun(configs)
    forwards.qualify(TENANT_DEFAULT)
    forwards.qualify(TENANT_ACME)

    backwards = TwoTenantRun(configs)
    backwards.qualify(TENANT_ACME)
    backwards.qualify(TENANT_DEFAULT)

    def routed(run: TwoTenantRun) -> dict[str, str]:
        return {item.tenant_id: item.destination for item in run.notifier.dispatches}

    assert routed(forwards) == routed(backwards)


def test_each_tenants_assessment_is_recorded_against_its_own_lead(
    configs: Mapping[str, TenantConfig],
) -> None:
    """What the store was told, since that is what a later report or invoice reads back."""
    run = TwoTenantRun(configs)
    left = run.qualify(TENANT_DEFAULT)
    right = run.qualify(TENANT_ACME)

    assert left.disposition is Disposition.DISPATCHED
    assert right.disposition is Disposition.DISPATCHED

    by_tenant = {row.tenant_id: row for row in run.store.assessments}
    assert set(by_tenant) == {TENANT_DEFAULT, TENANT_ACME}
    assert by_tenant[TENANT_DEFAULT].decision.tier is Tier.COLD
    assert by_tenant[TENANT_ACME].decision.tier is Tier.WARM
    assert by_tenant[TENANT_DEFAULT].lead_id != by_tenant[TENANT_ACME].lead_id

    for tenant, row in by_tenant.items():
        assert run.store.leads[(tenant, f"config-isolation-{tenant}")] == row.lead_id


def test_a_low_confidence_floor_escalates_for_one_tenant_only_through_the_pipeline(
    configs: Mapping[str, TenantConfig],
) -> None:
    """The confidence axis, again at pipeline level, because the destination changes with it.

    At 0.70 the demo tenant escalates and the default tenant does not — so the *same* lead,
    the *same* model output and the *same* pipeline put one in a sales inbox and the other
    in front of a human triage queue.
    """
    run = TwoTenantRun(configs, confidence=STRADDLING_CONFIDENCE)
    run.qualify(TENANT_DEFAULT)
    run.qualify(TENANT_ACME)

    dispatched = {item.tenant_id: item for item in run.notifier.dispatches}
    assert dispatched[TENANT_DEFAULT].decision.escalation_reason is None
    assert dispatched[TENANT_ACME].decision.escalation_reason is EscalationReason.LOW_CONFIDENCE
    assert dispatched[TENANT_DEFAULT].destination != dispatched[TENANT_ACME].destination
