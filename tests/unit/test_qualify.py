"""The pipeline, exercised entirely against in-memory ports.

The test that matters most is at the bottom: over every combination of assessment outcome
and tenant routing table that can be constructed, a lead is either dispatched to a person
or explicitly suppressed *and recorded*. Never neither. Everything above it is that same
invariant checked one failure at a time — a broken enricher, a broken model, a broken
notifier, a duplicate delivery, a tenant whose warm tier has nowhere to go.

No network, no database, no clock.
"""

from __future__ import annotations

import ast
import itertools
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
)
from leadquali.app.enrichment import Enrichment
from leadquali.app.ports import (
    ClockPort,
    EnricherPort,
    LeadAssessorPort,
    LeadStorePort,
    NotifierPort,
    RoutingOutcome,
    TenantConfigPort,
)
from leadquali.app.qualify import (
    Disposition,
    QualificationPipeline,
    QualificationRequest,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.routing import LOW_CONFIDENCE_NOTE, SPAM_NOTE, SYSTEM_FAILURE_BANNER
from leadquali.domain.tenant_config import TenantConfig, TenantNotFoundError
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import (
    FakeAssessorError,
    FakeClock,
    FakeEnricherError,
    FakeNotifierError,
    FakeStoreError,
    InMemoryLeadStore,
    RecordingNotifier,
    ScriptedAssessor,
    StaticConfigSource,
    StaticEnricher,
)

TENANT = "acme"
OPERATOR_INBOX = "leadquali-escalations@vendoworks.test"

EMAIL_EVERYTHING: dict[str, Any] = {
    "hot": {"action": "email_sales", "destination": "hot@acme.test"},
    "warm": {"action": "email_sales", "destination": "sales@acme.test"},
    "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
    "disqualified": {"action": "email_sales", "destination": "audit@acme.test"},
}

#: The shipped default: the bottom tier is suppressed by policy.
SUPPRESS_THE_BOTTOM: dict[str, Any] = {**EMAIL_EVERYTHING, "disqualified": {"action": "suppress"}}

#: The shape #9 flagged: warm has nowhere to go, but the confidence gate and the
#: system-failure path both route ``WARM`` + ``EMAIL_SALES`` regardless.
WARM_SUPPRESSED: dict[str, Any] = {**SUPPRESS_THE_BOTTOM, "warm": {"action": "suppress"}}

#: A tenant that has switched everything off. Legal configuration; still may not eat a lead.
EVERYTHING_SUPPRESSED: dict[str, Any] = {tier: {"action": "suppress"} for tier in EMAIL_EVERYTHING}

ESCALATE_EVERYTHING: dict[str, Any] = {
    tier: {"action": "escalate_human", "destination": f"{tier}-desk@acme.test"}
    for tier in EMAIL_EVERYTHING
}

TENANT_SHAPES: dict[str, dict[str, Any]] = {
    "email_everything": EMAIL_EVERYTHING,
    "suppress_the_bottom": SUPPRESS_THE_BOTTOM,
    "warm_suppressed": WARM_SUPPRESSED,
    "everything_suppressed": EVERYTHING_SUPPRESSED,
    "escalate_everything": ESCALATE_EVERYTHING,
}


def make_config(rules: dict[str, Any] | None = None, **overrides: Any) -> TenantConfig:
    """A valid tenant config with the plan's defaults and the given routing table."""
    document = {
        "tenant_id": TENANT,
        "name": "Acme Corp",
        "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
        "routing_rules": rules if rules is not None else SUPPRESS_THE_BOTTOM,
        **overrides,
    }
    return TenantConfig.model_validate(document)


def make_assessment(
    *, score: int = 25, confidence: float = 0.9, spam: bool = False
) -> LeadAssessment:
    """An assessment whose dimensions are all ``score`` (0-15 is safe for every field)."""
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=score,
            intent=score,
            authority=min(score, 15),
            urgency=min(score, 15),
            budget_signal=min(score, 15),
        ),
        extracted=ExtractedFacts(
            company_name="Acme",
            industry="saas",
            company_size_estimate="120",
            role_seniority="vp",
            stated_use_case="lead routing",
            stated_timeline="this quarter",
        ),
        reasoning="Fits the profile and states a timeline.",
        confidence=confidence,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=spam,
    )


def metering() -> CallMetering:
    return CallMetering(
        model_id="claude-opus-5",
        prompt_version="rubric_v1",
        effort="medium",
        input_tokens=500,
        output_tokens=800,
        cache_read_tokens=1500,
        cache_creation_tokens=0,
        cost_usd=Decimal("0.0225"),
        latency_ms=3200,
    )


def succeeded(**kwargs: Any) -> AssessmentSucceeded:
    return AssessmentSucceeded(assessment=make_assessment(**kwargs), metering=metering())


def failed(reason: EscalationReason = EscalationReason.API_ERROR) -> AssessmentFailed:
    return AssessmentFailed(reason=reason, detail="503 after 3 retries", latency_ms=9000)


SUBMISSION = LeadSubmission(
    full_name="Dana Reed",
    email="dana@acme.test",
    company="Acme",
    message="We need lead routing before the end of the quarter.",
)


def make_request(submission_id: str = "sub-1") -> QualificationRequest:
    return QualificationRequest(
        tenant_id=TENANT, submission_id=submission_id, submission=SUBMISSION
    )


def build_pipeline(
    *,
    config: TenantConfig | None = None,
    outcome: AssessmentOutcome | None = None,
    assessor: ScriptedAssessor | None = None,
    store: InMemoryLeadStore | None = None,
    notifier: RecordingNotifier | None = None,
    enricher: StaticEnricher | None = None,
    clock: FakeClock | None = None,
    escalation_destination: str = OPERATOR_INBOX,
) -> tuple[QualificationPipeline, InMemoryLeadStore, RecordingNotifier, ScriptedAssessor]:
    """A pipeline wired to fakes, plus the fakes, so a test can assert on both."""
    the_store = store if store is not None else InMemoryLeadStore()
    the_notifier = notifier if notifier is not None else RecordingNotifier()
    the_assessor = assessor if assessor is not None else ScriptedAssessor(outcome or succeeded())
    pipeline = QualificationPipeline(
        config_source=StaticConfigSource({TENANT: config or make_config()}),
        assessor=the_assessor,
        store=the_store,
        notifier=the_notifier,
        enricher=enricher if enricher is not None else StaticEnricher(),
        clock=clock if clock is not None else FakeClock(),
        escalation_destination=escalation_destination,
    )
    return pipeline, the_store, the_notifier, the_assessor


# --------------------------------------------------------------------------- happy path


def test_a_qualified_lead_is_persisted_then_dispatched_then_recorded() -> None:
    pipeline, store, notifier, _ = build_pipeline(outcome=succeeded(score=25, confidence=0.9))

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.decision is not None
    assert result.decision.tier is Tier.HOT
    assert result.destination == "hot@acme.test"
    assert result.provider_message_id == "provider-msg-1"
    assert result.used_fallback_destination is False

    assert len(store.assessments) == 1
    assert store.assessments[0].lead_id == result.lead_id
    assert len(notifier.dispatches) == 1
    assert notifier.dispatches[0].destination == "hot@acme.test"
    assert notifier.dispatches[0].assessment is not None

    events = store.terminal_events(result.lead_id)
    assert [event.outcome for event in events] == [RoutingOutcome.DISPATCHED]
    assert events[0].destination == "hot@acme.test"
    assert events[0].provider_message_id == "provider-msg-1"


def test_the_assessment_is_persisted_before_the_dispatch() -> None:
    """Ordering is the whole reason a dispatch failure can safely re-raise."""
    order: list[str] = []

    class OrderingStore(InMemoryLeadStore):
        def record_assessment(self, *args: Any, **kwargs: Any) -> None:
            order.append("assessment")
            super().record_assessment(*args, **kwargs)

    class OrderingNotifier(RecordingNotifier):
        def dispatch(self, *args: Any, **kwargs: Any) -> str | None:
            order.append("dispatch")
            return super().dispatch(*args, **kwargs)

    pipeline, _, _, _ = build_pipeline(store=OrderingStore(), notifier=OrderingNotifier())
    pipeline.qualify(make_request())

    assert order == ["assessment", "dispatch"]


def test_the_lead_reaches_the_model_as_untrusted_data() -> None:
    """The pipeline hands #12's rendering to the assessor, not the raw submission."""
    pipeline, _, _, assessor = build_pipeline()
    pipeline.qualify(make_request())

    assert "untrusted data supplied by a stranger" in assessor.prompts[0]
    assert "lead routing before the end of the quarter" in assessor.prompts[0]


# ------------------------------------------------------------------------- idempotency


def test_the_same_lead_twice_is_dispatched_once() -> None:
    pipeline, store, notifier, assessor = build_pipeline()

    first = pipeline.qualify(make_request())
    second = pipeline.qualify(make_request())

    assert first.disposition is Disposition.DISPATCHED
    assert second.disposition is Disposition.DUPLICATE
    assert second.lead_id == first.lead_id
    assert len(notifier.dispatches) == 1
    assert len(store.assessments) == 1
    assert assessor.calls == 1, "a redelivery must not pay for a second model call"


def test_a_redelivery_after_a_crash_before_dispatch_is_processed() -> None:
    """The guard is "was it routed", not "have we seen it".

    A worker that died between the insert and the send leaves a lead row with no routing
    event. Treating *that* as a duplicate would lose the lead permanently — which is the
    exact failure idempotency is supposed to prevent, arriving from the other side.
    """
    store = InMemoryLeadStore()
    store.upsert_lead(
        tenant_id=TENANT,
        submission_id="sub-1",
        submission=SUBMISSION,
        source="web_form",
        received_at=datetime(2026, 9, 2, 11, 0, tzinfo=UTC),
    )
    pipeline, _, notifier, _ = build_pipeline(store=store)

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert len(notifier.dispatches) == 1


def test_a_suppressed_lead_is_not_re_processed_either() -> None:
    """A suppression is a final answer; a redelivery must not reopen it."""
    pipeline, store, notifier, _ = build_pipeline(outcome=succeeded(spam=True))

    first = pipeline.qualify(make_request())
    second = pipeline.qualify(make_request())

    assert first.disposition is Disposition.SUPPRESSED
    assert second.disposition is Disposition.DUPLICATE
    assert notifier.dispatches == []
    assert len(store.assessments) == 1


# ---------------------------------------------------------------------- assessment fails


@pytest.mark.parametrize(
    "reason",
    [
        EscalationReason.API_ERROR,
        EscalationReason.MODEL_REFUSAL,
        EscalationReason.PARSE_ERROR,
        EscalationReason.TIMEOUT,
    ],
)
def test_an_unassessable_lead_still_reaches_a_human(reason: EscalationReason) -> None:
    pipeline, store, notifier, _ = build_pipeline(outcome=failed(reason))

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.decision is not None
    assert result.decision.escalation_reason is reason
    assert result.decision.note.startswith(SYSTEM_FAILURE_BANNER)
    assert result.decision.action is Action.EMAIL_SALES
    assert result.decision.tier is not Tier.DISQUALIFIED

    assert len(store.assessments) == 1, "the failed attempt is still an assessments row"
    assert store.assessments[0].outcome.ok is False
    assert len(notifier.dispatches) == 1
    assert notifier.dispatches[0].assessment is None
    assert store.terminal_events(result.lead_id)[0].outcome is RoutingOutcome.DISPATCHED


def test_an_assessor_that_raises_is_treated_as_an_api_error() -> None:
    """The port says implementations do not raise. The pipeline does not bet a lead on it."""
    assessor = ScriptedAssessor(succeeded(), raises=FakeAssessorError("boom"))
    pipeline, store, notifier, _ = build_pipeline(assessor=assessor)

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.decision is not None
    assert result.decision.escalation_reason is EscalationReason.API_ERROR
    assert "FakeAssessorError" in result.decision.note
    assert len(notifier.dispatches) == 1
    assert len(store.assessments) == 1


def test_the_failure_detail_carries_no_lead_content() -> None:
    """Invariant 5: notes are stored and emailed, so they may not carry PII."""
    assessor = ScriptedAssessor(succeeded(), raises=FakeAssessorError("dana@acme.test refused"))
    pipeline, _, _, _ = build_pipeline(assessor=assessor)

    result = pipeline.qualify(make_request())

    assert result.decision is not None
    assert "dana@acme.test" not in result.decision.note


# ------------------------------------------------------------------------- enrichment


def test_enrichment_facts_reach_the_prompt_ahead_of_the_untrusted_block() -> None:
    enricher = StaticEnricher(Enrichment(facts={"email_domain_type": "corporate"}))
    pipeline, _, _, assessor = build_pipeline(enricher=enricher)

    result = pipeline.qualify(make_request())
    prompt = assessor.prompts[0]

    assert "email_domain_type: corporate" in prompt
    assert prompt.index("email_domain_type") < prompt.index("untrusted data")
    assert result.enrichment_available is True


def test_an_enrichment_outage_degrades_and_the_lead_is_still_qualified() -> None:
    """Enrichment is an optimisation, not a gate."""
    enricher = StaticEnricher(raises=FakeEnricherError("dns timeout"))
    pipeline, store, notifier, assessor = build_pipeline(enricher=enricher)

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.enrichment_available is False
    assert "unavailable" in assessor.prompts[0]
    assert "FakeEnricherError" in assessor.prompts[0]
    assert len(notifier.dispatches) == 1
    assert len(store.assessments) == 1


def test_an_enrichment_outage_records_no_lead_content_in_the_prompt_note() -> None:
    enricher = StaticEnricher(raises=FakeEnricherError("dana@acme.test unreachable"))
    pipeline, _, _, assessor = build_pipeline(enricher=enricher)

    pipeline.qualify(make_request())

    assert "dana@acme.test" not in assessor.prompts[0].split("lead_submission")[0]


# --------------------------------------------------------------------------- dispatch


def test_a_dispatch_failure_raises_so_the_queue_redelivers() -> None:
    notifier = RecordingNotifier(fail_times=1)
    pipeline, store, _, _ = build_pipeline(notifier=notifier)

    with pytest.raises(FakeNotifierError):
        pipeline.qualify(make_request())

    lead_id = store.leads[(TENANT, "sub-1")]
    assert len(store.assessments) == 1, "the assessment survives the failed send"
    assert store.terminal_events(lead_id) == [], "nothing terminal, so redelivery retries"
    assert [event.outcome for event in store.routing_events] == [RoutingOutcome.FAILED]
    assert store.routing_events[0].destination == "hot@acme.test"


def test_the_redelivery_after_a_dispatch_failure_dispatches_exactly_once() -> None:
    notifier = RecordingNotifier(fail_times=1)
    store = InMemoryLeadStore()
    pipeline, _, _, _ = build_pipeline(store=store, notifier=notifier)

    with pytest.raises(FakeNotifierError):
        pipeline.qualify(make_request())
    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert len(notifier.dispatches) == 1
    assert [event.outcome for event in store.terminal_events(result.lead_id)] == [
        RoutingOutcome.DISPATCHED
    ]


def test_a_failure_recording_the_dispatch_failure_does_not_mask_the_dispatch_failure() -> None:
    """Best-effort bookkeeping must never replace the error the worker retries on."""
    notifier = RecordingNotifier(fail_times=1)
    store = InMemoryLeadStore(fail_on={"record_routing_event"})
    pipeline, _, _, _ = build_pipeline(store=store, notifier=notifier)

    with pytest.raises(FakeNotifierError):
        pipeline.qualify(make_request())


# ----------------------------------------------------------------------- suppression


def test_a_spam_submission_is_suppressed_and_recorded() -> None:
    pipeline, store, notifier, _ = build_pipeline(outcome=succeeded(spam=True))

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.SUPPRESSED
    assert result.destination is None
    assert result.provider_message_id is None
    assert notifier.dispatches == []
    events = store.terminal_events(result.lead_id)
    assert [event.outcome for event in events] == [RoutingOutcome.SUPPRESSED]
    assert events[0].action is Action.SUPPRESS
    assert SPAM_NOTE in events[0].detail
    assert len(store.assessments) == 1


def test_a_tenant_suppressed_tier_is_suppressed_and_recorded() -> None:
    pipeline, store, notifier, _ = build_pipeline(outcome=succeeded(score=0, confidence=0.9))

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.SUPPRESSED
    assert result.decision is not None
    assert result.decision.tier is Tier.DISQUALIFIED
    assert notifier.dispatches == []
    assert store.terminal_events(result.lead_id)[0].outcome is RoutingOutcome.SUPPRESSED


# ------------------------------------------------------- the escalation with nowhere to go


def test_a_low_confidence_lead_whose_warm_tier_has_no_destination_still_lands() -> None:
    """#9's finding: the gate routes WARM + EMAIL_SALES whatever the warm rule says.

    ``destination_for(WARM)`` is then ``None`` while the action says email, and an
    escalation with nowhere to go is a dropped lead by another name.
    """
    pipeline, store, notifier, _ = build_pipeline(
        config=make_config(WARM_SUPPRESSED), outcome=succeeded(score=12, confidence=0.1)
    )

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.decision is not None
    assert result.decision.note == LOW_CONFIDENCE_NOTE
    assert result.used_fallback_destination is True
    assert result.destination == "hot@acme.test", "the tenant's own best inbox comes first"
    assert len(notifier.dispatches) == 1
    assert store.terminal_events(result.lead_id)[0].destination == "hot@acme.test"


def test_a_system_failure_for_a_tenant_with_no_warm_destination_still_lands() -> None:
    pipeline, _, notifier, _ = build_pipeline(config=make_config(WARM_SUPPRESSED), outcome=failed())

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.used_fallback_destination is True
    assert len(notifier.dispatches) == 1


def test_a_tenant_that_suppressed_every_tier_falls_back_to_the_operator() -> None:
    """The last resort cannot be configured away: it is not part of the tenant's policy."""
    pipeline, store, notifier, _ = build_pipeline(
        config=make_config(EVERYTHING_SUPPRESSED), outcome=succeeded(score=12, confidence=0.1)
    )

    result = pipeline.qualify(make_request())

    assert result.disposition is Disposition.DISPATCHED
    assert result.destination == OPERATOR_INBOX
    assert result.used_fallback_destination is True
    assert notifier.dispatches[0].destination == OPERATOR_INBOX
    assert store.terminal_events(result.lead_id)[0].outcome is RoutingOutcome.DISPATCHED


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_pipeline_without_an_escalation_destination_cannot_be_built(blank: str) -> None:
    """Fail at wiring time, not at 3am with a lead in hand."""
    with pytest.raises(ValueError, match="escalation destination"):
        build_pipeline(escalation_destination=blank)


# --------------------------------------------------------------- unrecoverable failures


def test_an_unknown_tenant_raises_and_touches_nothing() -> None:
    """No policy means no defensible routing: the queue holds the lead, the DLQ alarms."""
    store = InMemoryLeadStore()
    pipeline, _, notifier, _ = build_pipeline(store=store)

    with pytest.raises(TenantNotFoundError):
        pipeline.qualify(
            QualificationRequest(tenant_id="ghost", submission_id="sub-1", submission=SUBMISSION)
        )

    assert store.leads == {}
    assert notifier.dispatches == []


def test_a_store_failure_before_dispatch_raises_and_dispatches_nothing() -> None:
    store = InMemoryLeadStore(fail_on={"record_assessment"})
    pipeline, _, notifier, _ = build_pipeline(store=store)

    with pytest.raises(FakeStoreError):
        pipeline.qualify(make_request())

    assert notifier.dispatches == []
    assert store.routing_events == []


def test_a_store_failure_recording_the_dispatch_raises_after_the_send() -> None:
    """The lead reached a human; the redelivery may duplicate the email, and that is the
    lesser evil against losing it."""
    store = InMemoryLeadStore(fail_on={"record_routing_event"})
    pipeline, _, notifier, _ = build_pipeline(store=store)

    with pytest.raises(FakeStoreError):
        pipeline.qualify(make_request())

    assert len(notifier.dispatches) == 1


# ------------------------------------------------------------------------- port shapes


def test_the_fakes_satisfy_the_protocols() -> None:
    """Structural conformance, checked once so five downstream issues can rely on it."""
    assert isinstance(InMemoryLeadStore(), LeadStorePort)
    assert isinstance(RecordingNotifier(), NotifierPort)
    assert isinstance(StaticEnricher(), EnricherPort)
    assert isinstance(ScriptedAssessor(succeeded()), LeadAssessorPort)
    assert isinstance(StaticConfigSource({}), TenantConfigPort)


def test_the_shipped_adapters_satisfy_the_protocols() -> None:
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.enrich_null import NullEnricher

    assert isinstance(SystemClock(), ClockPort)
    assert isinstance(NullEnricher(), EnricherPort)


def test_qualify_imports_nothing_from_adapters() -> None:
    """Acceptance criterion: wiring happens in ``api/`` and the worker entrypoint."""
    source = Path(__file__).resolve().parents[2] / "src" / "leadquali" / "app" / "qualify.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not [name for name in modules if "adapters" in name]


def test_the_clock_is_injectable_and_used_for_both_timestamps_and_latency() -> None:
    clock = FakeClock(step_ms=7)
    pipeline = QualificationPipeline(
        config_source=StaticConfigSource({TENANT: make_config()}),
        assessor=ScriptedAssessor(succeeded()),
        store=(store := InMemoryLeadStore()),
        notifier=RecordingNotifier(),
        enricher=StaticEnricher(),
        clock=clock,
        escalation_destination=OPERATOR_INBOX,
    )

    result = pipeline.qualify(make_request())

    assert result.latency_ms >= 0
    assert store.assessments[0].recorded_at.tzinfo is not None
    assert clock.ticks > 0


# ------------------------------------------------------------------------ the invariant

OUTCOME_CASES: dict[str, AssessmentOutcome] = {
    "hot": succeeded(score=25, confidence=0.95),
    "middling": succeeded(score=12, confidence=0.9),
    "bottom": succeeded(score=0, confidence=0.9),
    "low_confidence": succeeded(score=12, confidence=0.05),
    "spam": succeeded(spam=True),
    **{f"failed_{reason.value}": failed(reason) for reason in EscalationReason},
}


@pytest.mark.parametrize(
    ("outcome_name", "shape_name"),
    list(itertools.product(sorted(OUTCOME_CASES), sorted(TENANT_SHAPES))),
)
def test_every_lead_is_either_dispatched_or_suppressed_and_recorded(
    outcome_name: str, shape_name: str
) -> None:
    """Invariant 3 as an executable property over the whole input space.

    For every assessment outcome the system can produce and every routing table a tenant
    can legally configure, the lead ends up in front of a person or explicitly suppressed —
    and either way a routing event says which. "Neither" is the failure that generates no
    alert, no bounce and no complaint.
    """
    pipeline, store, notifier, _ = build_pipeline(
        config=make_config(TENANT_SHAPES[shape_name]), outcome=OUTCOME_CASES[outcome_name]
    )

    result = pipeline.qualify(make_request())

    assert len(store.assessments) == 1, "every attempt is on the record"
    events = store.terminal_events(result.lead_id)
    assert len(events) == 1, "exactly one final answer per lead"

    if result.disposition is Disposition.DISPATCHED:
        assert events[0].outcome is RoutingOutcome.DISPATCHED
        assert len(notifier.dispatches) == 1
        assert notifier.dispatches[0].destination.strip()
        assert result.destination == notifier.dispatches[0].destination
        assert result.decision is not None
        assert result.decision.action is not Action.SUPPRESS
    else:
        assert result.disposition is Disposition.SUPPRESSED
        assert events[0].outcome is RoutingOutcome.SUPPRESSED
        assert notifier.dispatches == []
        assert result.decision is not None
        assert result.decision.action is Action.SUPPRESS
        assert result.decision.escalated is False, "doubt never suppresses"
        assert events[0].detail.strip(), "a suppression must say why"


@pytest.mark.parametrize("shape_name", sorted(TENANT_SHAPES))
def test_no_escalation_is_ever_suppressed_whatever_the_tenant_configured(
    shape_name: str,
) -> None:
    """The other half: a lead the system was unsure about always reaches a person."""
    for outcome in (succeeded(score=12, confidence=0.05), failed()):
        pipeline, _, notifier, _ = build_pipeline(
            config=make_config(TENANT_SHAPES[shape_name]), outcome=outcome
        )
        result = pipeline.qualify(make_request())
        assert result.disposition is Disposition.DISPATCHED
        assert len(notifier.dispatches) == 1
