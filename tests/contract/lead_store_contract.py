"""The behaviour every ``LeadStorePort`` implementation has to share.

This module holds no tests of its own — it is a mixin, imported by
``tests/unit/test_lead_store_inmemory.py`` (against ``tests.fakes.InMemoryLeadStore``) and
by ``tests/integration/test_store_postgres.py`` (against real Postgres). One suite, two
implementations, because the fake is what every other issue's tests run against: any
behaviour the double and the adapter do not share is a bug that can only be discovered in
production, where the fake is not the thing running.

The contract is deliberately about *observable* behaviour — what ``upsert_lead`` returns
on a redelivery, what ``already_routed`` says after each kind of routing event, that a
tenant cannot see another tenant's history. Columns, SQL and rows are the Postgres
adapter's own business and are asserted in its own module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentSucceeded,
    CallMetering,
    Effort,
)
from leadquali.app.ports import LeadStorePort, RoutingOutcome
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    RoutingDecision,
    Tier,
)
from leadquali.prompts.lead import LeadSubmission

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def make_submission(
    *,
    email: str | None = "Ada@Example.com",
    message: str = "We get about 200 web-form leads a month and nobody triages them.",
) -> LeadSubmission:
    """A plausible inbound submission. ``email`` is mixed-case on purpose: the hash is
    computed over the normalised address, and a test that only ever passes lowercase text
    cannot tell whether normalisation happens."""
    return LeadSubmission(
        full_name="Ada Lovelace",
        email=email,
        company="Analytical Engines",
        role="VP Sales",
        message=message,
    )


def make_assessment(*, confidence: float = 0.82, spam: bool = False) -> LeadAssessment:
    """A schema-valid model output."""
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=24, intent=20, authority=12, urgency=9, budget_signal=10
        ),
        extracted=ExtractedFacts(
            company_name="Analytical Engines",
            industry="B2B software",
            company_size_estimate="50-100",
            role_seniority="vp",
            stated_use_case="triage inbound leads",
            stated_timeline="this quarter",
        ),
        reasoning="Inbound volume and a named owner are both stated.",
        confidence=confidence,
        missing_information=["budget"],
        suggested_first_question="How is the queue triaged today?",
        spam_or_test_submission=spam,
    )


def make_metering(
    *,
    model_id: str = "claude-opus-5",
    prompt_version: str = "rubric_v1",
    effort: Effort = "medium",
    latency_ms: int = 2_100,
) -> CallMetering:
    """Provenance and cost for one call, as the Anthropic adapter reports it."""
    return CallMetering(
        model_id=model_id,
        prompt_version=prompt_version,
        effort=effort,
        input_tokens=1_200,
        output_tokens=380,
        cache_read_tokens=4_800,
        cache_creation_tokens=0,
        cost_usd=Decimal("0.012345"),
        latency_ms=latency_ms,
    )


def make_succeeded(*, confidence: float = 0.82) -> AssessmentSucceeded:
    """A billed call that produced a usable assessment."""
    return AssessmentSucceeded(
        assessment=make_assessment(confidence=confidence), metering=make_metering()
    )


def make_failed(
    *,
    reason: EscalationReason = EscalationReason.API_ERROR,
    metering: CallMetering | None = None,
    latency_ms: int = 900,
) -> AssessmentFailed:
    """A call that produced no assessment. ``metering`` is set for a *billed* failure —
    a refusal or a ``max_tokens`` truncation — and ``None`` when the call never completed.
    """
    return AssessmentFailed(
        reason=reason,
        detail=f"{reason.value} from the provider",
        latency_ms=latency_ms,
        metering=metering,
    )


def make_decision(
    *,
    tier: Tier = Tier.WARM,
    action: Action = Action.EMAIL_SALES,
    total_score: float = 62.5,
    escalation_reason: EscalationReason | None = None,
    note: str = "Routed to sales.",
) -> RoutingDecision:
    """What the deterministic layer concluded. Produced by code, never by the model."""
    return RoutingDecision(
        tier=tier,
        action=action,
        total_score=total_score,
        note=note,
        escalation_reason=escalation_reason,
    )


class LeadStoreContract:
    """Mixin of the behaviour a ``LeadStorePort`` must have, whatever backs it.

    Subclasses override the two fixtures. ``tenants`` yields two *distinct* tenant ids that
    both exist as far as the implementation is concerned, because half of this contract is
    about what one tenant cannot see of the other.
    """

    @pytest.fixture
    def store(self) -> LeadStorePort:
        raise NotImplementedError("a LeadStoreContract subclass provides the store")

    @pytest.fixture
    def tenants(self) -> tuple[str, str]:
        raise NotImplementedError("a LeadStoreContract subclass provides two tenant ids")

    # ------------------------------------------------------------------ upsert_lead

    def test_a_first_delivery_creates_a_new_lead(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        tenant, _ = tenants
        lead = store.upsert_lead(
            tenant_id=tenant,
            submission_id="sub-1",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        assert lead.is_new is True
        assert lead.lead_id

    def test_replaying_a_submission_returns_the_first_lead(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """SQS is at-least-once: the second delivery must not raise, must not create a
        second lead, and must come back carrying the first row's id."""
        tenant, _ = tenants
        first = store.upsert_lead(
            tenant_id=tenant,
            submission_id="sub-replay",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        second = store.upsert_lead(
            tenant_id=tenant,
            submission_id="sub-replay",
            submission=make_submission(message="resent by the browser"),
            source="web_form",
            received_at=NOW,
        )
        assert second.lead_id == first.lead_id
        assert second.is_new is False

    def test_two_tenants_may_use_the_same_submission_id(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """Submission ids are unique *per tenant*. Two customers numbering their forms
        from 1 is not a collision, and treating it as one would hand tenant B's lead to
        tenant A."""
        tenant_a, tenant_b = tenants
        first = store.upsert_lead(
            tenant_id=tenant_a,
            submission_id="shared-id",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        second = store.upsert_lead(
            tenant_id=tenant_b,
            submission_id="shared-id",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        assert second.is_new is True
        assert second.lead_id != first.lead_id

    # --------------------------------------------------------------- already_routed

    def test_a_stored_lead_has_not_been_routed_yet(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-fresh")
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is False

    def test_a_dispatched_lead_counts_as_routed(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-dispatched")
        store.record_routing_event(
            tenant_id=tenant,
            lead_id=lead,
            action=Action.EMAIL_SALES,
            destination="sales@example.invalid",
            outcome=RoutingOutcome.DISPATCHED,
            provider_message_id="ses-1",
            occurred_at=NOW,
            detail="Routed to sales.",
        )
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is True

    def test_a_suppressed_lead_counts_as_routed(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """A suppression is a final answer, so a redelivery must do nothing — and the
        suppression itself is still on the record."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-suppressed")
        store.record_routing_event(
            tenant_id=tenant,
            lead_id=lead,
            action=Action.SUPPRESS,
            destination=None,
            outcome=RoutingOutcome.SUPPRESSED,
            provider_message_id=None,
            occurred_at=NOW,
            detail="Spam or test submission.",
        )
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is True

    def test_a_failed_dispatch_does_not_count_as_routed(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """The one that matters most: if a failed send made this true, a single SES outage
        would convert a retryable failure into a permanently lost lead."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-failed-send")
        store.record_routing_event(
            tenant_id=tenant,
            lead_id=lead,
            action=Action.EMAIL_SALES,
            destination="sales@example.invalid",
            outcome=RoutingOutcome.FAILED,
            provider_message_id=None,
            occurred_at=NOW,
            detail="dispatch failed: ConnectionError",
        )
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is False

    def test_a_retry_after_a_failed_dispatch_marks_the_lead_routed(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-retry")
        for outcome, message_id in (
            (RoutingOutcome.FAILED, None),
            (RoutingOutcome.DISPATCHED, "ses-2"),
        ):
            store.record_routing_event(
                tenant_id=tenant,
                lead_id=lead,
                action=Action.EMAIL_SALES,
                destination="sales@example.invalid",
                outcome=outcome,
                provider_message_id=message_id,
                occurred_at=NOW,
                detail="attempt",
            )
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is True

    def test_already_routed_is_scoped_to_the_tenant(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """Invariant 4, asked as the question that would hurt: tenant B must not be able to
        learn anything about tenant A's lead, even holding its id."""
        tenant_a, tenant_b = tenants
        lead = self._lead(store, tenant_a, "sub-private")
        store.record_routing_event(
            tenant_id=tenant_a,
            lead_id=lead,
            action=Action.EMAIL_SALES,
            destination="sales@example.invalid",
            outcome=RoutingOutcome.DISPATCHED,
            provider_message_id="ses-3",
            occurred_at=NOW,
            detail="Routed to sales.",
        )
        assert store.already_routed(tenant_id=tenant_b, lead_id=lead) is False

    def test_an_assessment_does_not_make_a_lead_routed(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """Assessed is not delivered. A worker that died between the two must retry."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-assessed-only")
        store.record_assessment(
            tenant_id=tenant,
            lead_id=lead,
            outcome=make_succeeded(),
            decision=make_decision(),
            recorded_at=NOW,
        )
        assert store.already_routed(tenant_id=tenant, lead_id=lead) is False

    # ------------------------------------------------------------ record_assessment

    def test_a_successful_assessment_is_recorded(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-ok")
        store.record_assessment(
            tenant_id=tenant,
            lead_id=lead,
            outcome=make_succeeded(),
            decision=make_decision(tier=Tier.HOT, total_score=88.25),
            recorded_at=NOW,
        )

    def test_a_failed_assessment_is_recorded_too(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """Invariant 3: an unassessable lead is still a lead, and "never dropped" is only
        auditable if the attempt leaves a row."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-api-error")
        store.record_assessment(
            tenant_id=tenant,
            lead_id=lead,
            outcome=make_failed(reason=EscalationReason.API_ERROR),
            decision=make_decision(
                escalation_reason=EscalationReason.API_ERROR,
                total_score=0.0,
                note="The system could not assess this lead.",
            ),
            recorded_at=NOW,
        )

    def test_a_billed_failure_is_recorded(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """A refusal is an HTTP 200 and it is charged for. A billed failure that left no
        metering is money the business cannot see."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-refusal")
        store.record_assessment(
            tenant_id=tenant,
            lead_id=lead,
            outcome=make_failed(reason=EscalationReason.MODEL_REFUSAL, metering=make_metering()),
            decision=make_decision(escalation_reason=EscalationReason.MODEL_REFUSAL),
            recorded_at=NOW,
        )

    def test_a_low_confidence_escalation_is_a_successful_assessment(
        self, store: LeadStorePort, tenants: tuple[str, str]
    ) -> None:
        """The model answered; code did not trust the answer. Both facts are recorded."""
        tenant, _ = tenants
        lead = self._lead(store, tenant, "sub-low-confidence")
        store.record_assessment(
            tenant_id=tenant,
            lead_id=lead,
            outcome=make_succeeded(confidence=0.2),
            decision=make_decision(escalation_reason=EscalationReason.LOW_CONFIDENCE),
            recorded_at=NOW,
        )

    # ------------------------------------------------------------------------ helper

    @staticmethod
    def _lead(store: LeadStorePort, tenant_id: str, submission_id: str) -> str:
        return store.upsert_lead(
            tenant_id=tenant_id,
            submission_id=submission_id,
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        ).lead_id
