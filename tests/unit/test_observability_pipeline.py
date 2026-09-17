"""What one lead's journey actually emits, asserted end to end on parsed JSON.

Two things are being proved here, and only the second one is about metrics.

**Invariant 5 holds through the whole pipeline.** A lead carrying a distinctive address and
a distinctive free-text message goes through the real ingest service, the real queue
message, and the real pipeline with real logging captured, and neither string appears in
any emitted record. Then the same lead goes through the *exception* path, because that is
where the leak actually happens: a notifier that raises with the submission in its message
produces a traceback, and a traceback is logged. There is no way to make a rule like
"never log a lead's message" true by reading the code — the only way it stays true is a
test that would fail the day somebody makes it false, on both paths.

The message assertion is the load-bearing one. An address is a *pattern* and the formatter
redacts it as a last resort, so a sloppy call site would still pass that half; the tests
below therefore also assert that the redaction marker appears nowhere, which says nothing
even tried. A lead's prose is not a pattern and nothing can save it, which is exactly why
it is the string this file watches.

**The metrics #29 alarms on are emitted, on every path.** A success, a refusal that was
still billed, a hard failure with no metering at all, both suppressions, a duplicate and a
fallback destination — each one asserted on the EMF document that rides on its log line.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
    CallMetering,
)
from leadquali.app.enrichment import Enrichment
from leadquali.app.ingest import IngestRequest, IngestService, QueuedLead
from leadquali.app.qualify import (
    Disposition,
    QualificationPipeline,
    QualificationRequest,
)
from leadquali.config import Environment, Settings
from leadquali.domain.models import (
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.tenant_config import TenantConfig
from leadquali.observability import (
    EMAIL_REDACTION,
    EVENT_ASSESSMENT,
    EVENT_DISPATCH_FAILED,
    EVENT_LEAD_ACCEPTED,
    EVENT_LEAD_DUPLICATE,
    EVENT_LEAD_ROUTED,
    EVENT_LEAD_SUPPRESSED,
    LOG_FORMAT_JSON,
    METRIC_NAMESPACE,
    SuppressionCause,
    configure_logging,
    contact_email_hash,
)
from leadquali.observability.metrics import (
    ASSESSMENT_FAILURES,
    ASSESSMENTS,
    CACHE_READ_TOKENS,
    COST_USD,
    DIM_ESCALATION_REASON,
    DIM_SUPPRESSION_CAUSE,
    DIM_TENANT,
    DIM_TIER,
    DISPATCH_FAILURES,
    DISPATCHES,
    DUPLICATES,
    ENRICHMENT_UNAVAILABLE,
    ESCALATIONS,
    FALLBACK_DESTINATIONS,
    INGESTED_LEADS,
    INPUT_TOKENS,
    MODEL_LATENCY_MS,
    OUTPUT_TOKENS,
    PIPELINE_LATENCY_MS,
    SUPPRESSIONS,
)
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import (
    FakeClock,
    FakeNotifierError,
    InMemoryLeadStore,
    RecordingNotifier,
    ScriptedAssessor,
    StaticConfigSource,
    StaticEnricher,
)
from tests.logcapture import LogCapture, capture_json_logs

TENANT = "acme"
OPERATOR_INBOX = "leadquali-escalations@vendoworks.test"

#: Distinctive enough that a substring search cannot produce a false negative, and shaped
#: like a real address so the formatter's pattern would match it if it ever got out.
LEAD_EMAIL = "ada.lovelace+jat21@analytical-engines-quali.co.uk"

#: Not a pattern, on purpose. Nothing in the formatter can recognise this, so the only
#: reason it stays out of the logs is that no call site ever passes it to one.
LEAD_MESSAGE = (
    "We run 40 difference engines in Marylebone and our punch-card vendor just doubled "
    "their price; I need routing sorted before the Michaelmas board meeting."
)

LEAD_NAME = "Augusta Ada King-Noel"
LEAD_PHONE = "+44 20 7946 0958"

SUBMISSION = LeadSubmission(
    full_name=LEAD_NAME,
    email=LEAD_EMAIL,
    company="Analytical Engines Ltd",
    role="Countess of Lovelace",
    phone=LEAD_PHONE,
    website="https://analytical-engines-quali.co.uk",
    message=LEAD_MESSAGE,
    extra={"how_did_you_hear": "Charles mentioned you at the Royal Society"},
)

#: Every string in the submission that must never be logged. The company, the role and the
#: website are on the list too: a leak is a leak, and the test that only guards the two
#: obvious fields is the test that misses the third.
SECRETS: tuple[str, ...] = (
    LEAD_EMAIL,
    LEAD_MESSAGE,
    LEAD_NAME,
    LEAD_PHONE,
    "Analytical Engines Ltd",
    "Countess of Lovelace",
    "Charles mentioned you at the Royal Society",
)

EMAIL_EVERYWHERE: dict[str, Any] = {
    "hot": {"action": "email_sales", "destination": "hot@acme.test"},
    "warm": {"action": "email_sales", "destination": "sales@acme.test"},
    "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
    "disqualified": {"action": "suppress"},
}

METERING = CallMetering(
    model_id="claude-opus-5",
    prompt_version="rubric_v1",
    effort="medium",
    input_tokens=512,
    output_tokens=843,
    cache_read_tokens=1487,
    cache_creation_tokens=0,
    cost_usd=Decimal("0.02347"),
    latency_ms=4213,
)


def make_config(rules: dict[str, Any] | None = None, **overrides: Any) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "tenant_id": TENANT,
            "name": "Acme Corp",
            "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
            "routing_rules": rules if rules is not None else EMAIL_EVERYWHERE,
            **overrides,
        }
    )


def make_assessment(
    *, score: int = 25, confidence: float = 0.9, spam: bool = False
) -> LeadAssessment:
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=min(score, 30),
            intent=min(score, 25),
            authority=min(score, 15),
            urgency=min(score, 15),
            budget_signal=min(score, 15),
        ),
        extracted=ExtractedFacts(
            company_name="Analytical Engines",
            industry="manufacturing",
            company_size_estimate="120",
            role_seniority="owner",
            stated_use_case="lead routing",
            stated_timeline="this quarter",
        ),
        reasoning="Fits the profile and states a timeline.",
        confidence=confidence,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=spam,
    )


def succeeded(**kwargs: Any) -> AssessmentSucceeded:
    return AssessmentSucceeded(assessment=make_assessment(**kwargs), metering=METERING)


def build_pipeline(
    *,
    outcome: AssessmentOutcome | None = None,
    notifier: RecordingNotifier | None = None,
    store: InMemoryLeadStore | None = None,
    enricher: StaticEnricher | None = None,
    rules: dict[str, Any] | None = None,
) -> tuple[QualificationPipeline, InMemoryLeadStore, RecordingNotifier]:
    """A pipeline over in-memory ports. No network, no database, no clock."""
    resolved_store = store if store is not None else InMemoryLeadStore()
    resolved_notifier = notifier if notifier is not None else RecordingNotifier()
    pipeline = QualificationPipeline(
        config_source=StaticConfigSource({TENANT: make_config(rules)}),
        assessor=ScriptedAssessor(outcome if outcome is not None else succeeded()),
        store=resolved_store,
        notifier=resolved_notifier,
        enricher=enricher if enricher is not None else StaticEnricher(),
        clock=FakeClock(),
        escalation_destination=OPERATOR_INBOX,
    )
    return pipeline, resolved_store, resolved_notifier


def request_for(submission_id: str = "sub-1", trace_id: str | None = None) -> QualificationRequest:
    return QualificationRequest(
        tenant_id=TENANT,
        submission_id=submission_id,
        submission=SUBMISSION,
        received_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        trace_id=trace_id,
    )


def directives(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The EMF directives on one record. Fails the test if the line carries no metrics."""
    aws = record.get("_aws")
    assert isinstance(aws, dict), f"{record.get('event')!r} carries no EMF document"
    found = aws["CloudWatchMetrics"]
    assert all(directive["Namespace"] == METRIC_NAMESPACE for directive in found)
    return list(found)


def dimensioned(record: dict[str, Any], *dimensions: str) -> dict[str, Any]:
    """The one directive published under exactly ``dimensions``."""
    wanted = list(dimensions)
    matches = [directive for directive in directives(record) if directive["Dimensions"] == [wanted]]
    assert len(matches) == 1, f"expected one directive on {wanted}, got {len(matches)}"
    return matches[0]


def metric_names(directive: dict[str, Any]) -> set[str]:
    return {metric["Name"] for metric in directive["Metrics"]}


def assert_no_pii(logs: LogCapture) -> None:
    """No field, message, traceback or metric on any record carries the lead's data.

    Checked against the raw output rather than the parsed records, so an address hiding in
    a key, in a nested object or inside an escaped traceback string is still caught.
    """
    blob = logs.text
    assert blob, "nothing was logged, so this test proves nothing"
    for secret in SECRETS:
        assert secret not in blob, f"{secret!r} reached the logs"
    # Nothing even *tried* to log an address: the formatter's net never had to fire. This is
    # the assertion that keeps the net from becoming a licence to be careless upstream.
    assert EMAIL_REDACTION not in blob
    # And nothing address-shaped survives under any other spelling.
    for record in logs.records():
        leaked = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", json.dumps(record))
        assert leaked == [], f"{record.get('event')!r} carries {leaked}"


# ------------------------------------------------------------------ trace propagation


def ingest_service(store: InMemoryLeadStore, queue: InProcessLeadQueue) -> IngestService:
    return IngestService(store=store, queue=queue, clock=FakeClock())


def test_one_trace_id_spans_ingest_the_queue_and_the_worker() -> None:
    """The acceptance criterion: one lead's whole journey, by trace id alone."""
    store = InMemoryLeadStore()
    queue = InProcessLeadQueue()

    with capture_json_logs() as logs:
        receipt = ingest_service(store, queue).accept(
            IngestRequest(tenant_id=TENANT, submission_id="sub-1", submission=SUBMISSION)
        )
        # The wire hop, for real: serialise the message and rebuild it the way #26's worker
        # will, rather than passing the object through and assuming the field survives.
        rebuilt = QueuedLead.from_message(json.loads(json.dumps(queue.drain()[0].to_message())))
        pipeline, _, _ = build_pipeline(store=store)
        result = pipeline.qualify(
            QualificationRequest(
                tenant_id=rebuilt.tenant_id,
                submission_id=rebuilt.submission_id,
                submission=rebuilt.submission,
                received_at=rebuilt.received_at,
                trace_id=rebuilt.trace_id,
            )
        )

    assert rebuilt.trace_id == receipt.trace_id
    assert result.trace_id == receipt.trace_id
    assert result.disposition is Disposition.DISPATCHED

    emitted = logs.records()
    # *Every* record, not only the structured ones. The queue's own line is a plain
    # printf-style ``LOGGER.info`` written by code that was never handed a trace id, and it
    # carries one anyway — that is the whole reason the id lives on a ContextVar instead of
    # in six port signatures.
    assert len(emitted) >= 4
    assert {record["trace_id"] for record in emitted} == {receipt.trace_id}
    assert any("event" not in record for record in emitted)
    # And the structured journey is complete under that one id: edge, assessment, dispatch.
    assert [record["event"] for record in emitted if "event" in record] == [
        EVENT_LEAD_ACCEPTED,
        EVENT_ASSESSMENT,
        EVENT_LEAD_ROUTED,
    ]


def test_the_lead_id_appears_from_the_edge_onwards() -> None:
    store = InMemoryLeadStore()
    queue = InProcessLeadQueue()
    with capture_json_logs() as logs:
        receipt = ingest_service(store, queue).accept(
            IngestRequest(tenant_id=TENANT, submission_id="sub-1", submission=SUBMISSION)
        )
    assert logs.one(EVENT_LEAD_ACCEPTED)["lead_id"] == receipt.lead_id
    assert logs.one(EVENT_LEAD_ACCEPTED)["submission_id"] == "sub-1"


def test_a_queue_message_round_trips_its_trace_id() -> None:
    lead = QueuedLead(
        tenant_id=TENANT,
        lead_id="lead-0001",
        submission_id="sub-1",
        submission=SUBMISSION,
        source="web_form",
        received_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
        trace_id="0123456789abcdef0123456789abcdef",
    )
    assert lead.to_message()["trace_id"] == lead.trace_id
    assert QueuedLead.from_message(lead.to_message()).trace_id == lead.trace_id


def test_a_message_written_before_trace_ids_existed_still_loads() -> None:
    """The in-flight case. A version bump would have DLQ'd these; an optional field does not.

    The message keeps ``version: 1`` because the field is additive both ways — an old
    worker ignores a key it does not know, and a new worker mints one for a message that
    has none. What must never happen is a real lead refused to protect a log field.
    """
    message = QueuedLead(
        tenant_id=TENANT,
        lead_id="lead-0001",
        submission_id="sub-1",
        submission=SUBMISSION,
        source="web_form",
        received_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
    ).to_message()
    del message["trace_id"]

    rebuilt = QueuedLead.from_message(message)

    assert len(rebuilt.trace_id) == 32
    assert rebuilt.submission == SUBMISSION


def test_a_lead_with_no_trace_id_at_all_still_gets_one() -> None:
    """A CLI replay or a direct call. Untraced is the one outcome that helps nobody."""
    pipeline, _, _ = build_pipeline()
    with capture_json_logs() as logs:
        result = pipeline.qualify(request_for(trace_id=None))
    assert len(result.trace_id) == 32
    assert logs.one(EVENT_ASSESSMENT)["trace_id"] == result.trace_id


# -------------------------------------------------------------------------- invariant 5


def test_no_record_carries_the_address_or_the_lead_s_own_words() -> None:
    store = InMemoryLeadStore()
    queue = InProcessLeadQueue()

    with capture_json_logs() as logs:
        ingest_service(store, queue).accept(
            IngestRequest(tenant_id=TENANT, submission_id="sub-1", submission=SUBMISSION)
        )
        pipeline, _, _ = build_pipeline(store=store)
        pipeline.qualify(request_for())

    assert_no_pii(logs)
    # The hash is there instead, and it is the one the store writes.
    assert logs.one(EVENT_LEAD_ACCEPTED)["contact_email_hash"] == contact_email_hash(LEAD_EMAIL)


def test_the_exception_path_carries_no_more_than_the_happy_one() -> None:
    """A traceback that formats a ``LeadSubmission`` would leak the entire lead.

    The notifier raises with the submission interpolated into its message — the realistic
    shape of the bug, since a provider error is normally reported with the thing that was
    being sent. The pipeline logs that at ``ERROR`` with the traceback attached and then
    re-raises, so this is the one path where the whole payload is one ``repr`` away from
    CloudWatch.
    """
    notifier = RecordingNotifier()

    def explode(**kwargs: Any) -> str | None:
        raise FakeNotifierError(f"SES rejected this message: {kwargs['submission']!r}")

    notifier.dispatch = explode  # type: ignore[method-assign]  # a deliberately leaky double
    pipeline, _, _ = build_pipeline(notifier=notifier)

    with capture_json_logs() as logs, pytest.raises(FakeNotifierError):
        pipeline.qualify(request_for())

    assert_no_pii(logs)
    failed = logs.one(EVENT_DISPATCH_FAILED)
    assert failed["level"] == "ERROR"
    assert failed["error_type"] == "FakeNotifierError"
    assert failed["exception"]["type"] == "FakeNotifierError"
    # The traceback is genuinely there — the assertion above is not passing because the
    # exception was swallowed.
    assert "FakeNotifierError" in failed["exception"]["stack"]
    assert "raise FakeNotifierError" in failed["exception"]["stack"]


def test_a_submission_repr_carries_no_field_values() -> None:
    """The mechanism behind the test above, asserted on its own.

    ``repr`` is where a submission escapes without anybody deciding to emit it. Redaction
    cannot help — the message is prose, not a pattern — so the payload has to be absent
    from the string in the first place.
    """
    rendered = repr(SUBMISSION)
    for secret in SECRETS:
        assert secret not in rendered
    assert "LeadSubmission" in rendered


def test_an_unassessable_lead_leaks_nothing_either() -> None:
    """The system-failure path: a detail string, a banner, and no submission."""
    pipeline, _, _ = build_pipeline(
        outcome=AssessmentFailed(
            reason=EscalationReason.API_ERROR,
            detail="APIStatusError 503",
            latency_ms=900,
        )
    )
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    assert_no_pii(logs)


def test_a_broken_enricher_leaks_nothing() -> None:
    """The enricher sees the submission, so its exception is another leak candidate."""
    pipeline, _, _ = build_pipeline(
        enricher=StaticEnricher(raises=RuntimeError(f"DNS lookup failed for {LEAD_EMAIL}"))
    )
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    assert_no_pii(logs)


# ------------------------------------------------------------- per-assessment metrics


def test_a_successful_assessment_emits_the_metering_it_was_given() -> None:
    pipeline, _, _ = build_pipeline(outcome=succeeded(score=28))

    with capture_json_logs() as logs:
        pipeline.qualify(request_for())

    record = logs.one(EVENT_ASSESSMENT)
    # Fields: the provenance and the numbers, exactly as the adapter computed them.
    assert record["model_id"] == METERING.model_id
    assert record["prompt_version"] == METERING.prompt_version
    assert record["effort"] == METERING.effort
    assert record["input_tokens"] == METERING.input_tokens
    assert record["output_tokens"] == METERING.output_tokens
    assert record["cache_read_tokens"] == METERING.cache_read_tokens
    assert record["cache_creation_tokens"] == METERING.cache_creation_tokens
    assert record["model_latency_ms"] == METERING.latency_ms
    assert record["cost_usd"] == pytest.approx(float(METERING.cost_usd))
    assert record["assessed"] is True
    assert record["tier"] == Tier.HOT.value
    assert record["confidence"] == pytest.approx(0.9)
    assert record["enrichment_available"] is True

    # Metrics: the same numbers, as EMF, under the dimensions #29 alarms on.
    per_tenant = dimensioned(record, DIM_TENANT)
    assert metric_names(per_tenant) >= {
        ASSESSMENTS,
        ASSESSMENT_FAILURES,
        ENRICHMENT_UNAVAILABLE,
        INPUT_TOKENS,
        OUTPUT_TOKENS,
        CACHE_READ_TOKENS,
        COST_USD,
        MODEL_LATENCY_MS,
    }
    assert record[DIM_TENANT] == TENANT
    assert record[ASSESSMENTS] == 1
    assert record[ASSESSMENT_FAILURES] == 0
    assert record[COST_USD] == pytest.approx(float(METERING.cost_usd))
    assert record[MODEL_LATENCY_MS] == METERING.latency_ms
    assert {"Name": COST_USD, "Unit": "None"} in per_tenant["Metrics"]


def test_cost_is_a_json_number_so_it_can_be_summed() -> None:
    """A ``Decimal`` serialises as a string, and CloudWatch drops a directive that has one."""
    pipeline, _, _ = build_pipeline()
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    assert f'"{COST_USD}": {float(METERING.cost_usd)}' in logs.text


def test_the_tier_of_every_decision_is_emitted() -> None:
    """Plan section 8's drift signal is ``Assessments`` by ``Tier`` — so every decision counts.

    Including the ones nobody was emailed about. A tier distribution computed only over
    dispatched leads would move whenever a tenant changed its routing table, which is
    exactly the false positive that gets a drift alarm switched off.
    """
    cases: dict[Tier, AssessmentOutcome] = {
        Tier.HOT: succeeded(score=28),
        Tier.COLD: succeeded(score=8),
        Tier.WARM: AssessmentFailed(
            reason=EscalationReason.TIMEOUT, detail="read timeout", latency_ms=30_000
        ),
        Tier.DISQUALIFIED: succeeded(spam=True),
    }
    for tier, outcome in cases.items():
        pipeline, _, _ = build_pipeline(outcome=outcome)
        with capture_json_logs() as logs:
            pipeline.qualify(request_for())
        record = logs.one(EVENT_ASSESSMENT)
        assert record["tier"] == tier.value, tier
        assert record[DIM_TIER] == tier.value
        assert dimensioned(record, DIM_TENANT, DIM_TIER)["Metrics"] == [
            {"Name": ASSESSMENTS, "Unit": "Count"}
        ]
        assert record[ASSESSMENTS] == 1


def test_a_failed_assessment_is_counted_and_attributed() -> None:
    pipeline, _, _ = build_pipeline(
        outcome=AssessmentFailed(
            reason=EscalationReason.PARSE_ERROR, detail="schema violation", latency_ms=1500
        )
    )

    with capture_json_logs() as logs:
        pipeline.qualify(request_for())

    record = logs.one(EVENT_ASSESSMENT)
    assert record["assessed"] is False
    assert record["escalation_reason"] == EscalationReason.PARSE_ERROR.value
    assert "confidence" not in record  # there is no assessment to be confident about
    assert "cost_usd" not in record  # the call never completed, so nothing was billed
    assert record[ASSESSMENT_FAILURES] == 1
    assert record[DIM_ESCALATION_REASON] == EscalationReason.PARSE_ERROR.value
    assert metric_names(dimensioned(record, DIM_TENANT, DIM_ESCALATION_REASON)) == {ESCALATIONS}
    assert record[ESCALATIONS] == 1


def test_a_refusal_is_a_failure_that_still_cost_money() -> None:
    """A refusal is an HTTP 200 and Anthropic bills for it. An unmetered refusal is a hole
    in the cost figure, so the metering rides on the failure and is emitted from it."""
    pipeline, _, _ = build_pipeline(
        outcome=AssessmentFailed(
            reason=EscalationReason.MODEL_REFUSAL,
            detail="refusal stop reason",
            latency_ms=2100,
            metering=METERING,
        )
    )

    with capture_json_logs() as logs:
        pipeline.qualify(request_for())

    record = logs.one(EVENT_ASSESSMENT)
    assert record[ASSESSMENT_FAILURES] == 1
    assert record[COST_USD] == pytest.approx(float(METERING.cost_usd))
    assert record["model_id"] == METERING.model_id


def test_a_low_confidence_escalation_is_attributed_to_the_gate() -> None:
    pipeline, _, _ = build_pipeline(outcome=succeeded(score=28, confidence=0.2))
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    record = logs.one(EVENT_ASSESSMENT)
    assert record["assessed"] is True
    assert record["escalation_reason"] == EscalationReason.LOW_CONFIDENCE.value
    assert record["tier"] == Tier.WARM.value
    assert record[ASSESSMENT_FAILURES] == 0  # our systems worked; the model was unsure


def test_enrichment_being_unavailable_is_counted() -> None:
    """#58's failure mode: an enricher that quietly stops working degrades every assessment
    and nothing in the product looks different. The counter is the only symptom."""
    pipeline, _, _ = build_pipeline(
        enricher=StaticEnricher(Enrichment.unavailable("mx lookup timed out"))
    )
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    record = logs.one(EVENT_ASSESSMENT)
    assert record["enrichment_available"] is False
    assert record[ENRICHMENT_UNAVAILABLE] == 1


def test_enrichment_being_available_is_counted_as_zero_not_omitted() -> None:
    """A metric that is absent on the happy path cannot be averaged or alarmed on."""
    pipeline, _, _ = build_pipeline()
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    assert logs.one(EVENT_ASSESSMENT)[ENRICHMENT_UNAVAILABLE] == 0


# --------------------------------------------------------------------- outcome metrics


def test_a_dispatch_emits_its_tier_destination_hash_and_latency() -> None:
    pipeline, _, notifier = build_pipeline(outcome=succeeded(score=28))

    with capture_json_logs() as logs:
        result = pipeline.qualify(request_for())

    record = logs.one(EVENT_LEAD_ROUTED)
    assert record["tier"] == Tier.HOT.value
    assert record["action"] == "email_sales"
    assert record["provider_message_id"] == notifier.message_id
    assert record["destination_hash"] == contact_email_hash("hot@acme.test")
    assert "hot@acme.test" not in logs.text
    assert record["latency_ms"] == result.latency_ms
    assert record[DISPATCHES] == 1
    assert record[FALLBACK_DESTINATIONS] == 0
    assert record[PIPELINE_LATENCY_MS] == result.latency_ms
    assert metric_names(dimensioned(record, DIM_TENANT, DIM_TIER)) == {DISPATCHES}


def test_a_fallback_destination_is_counted() -> None:
    """A tenant whose routing table has nowhere to put an escalation. The lead still lands;
    the counter is how anyone finds out the configuration disagrees with reality."""
    pipeline, _, _ = build_pipeline(
        outcome=AssessmentFailed(reason=EscalationReason.API_ERROR, detail="503", latency_ms=800),
        rules={tier: {"action": "suppress"} for tier in EMAIL_EVERYWHERE},
    )

    with capture_json_logs() as logs:
        pipeline.qualify(request_for())

    record = logs.one(EVENT_LEAD_ROUTED)
    assert record["used_fallback_destination"] is True
    assert record[FALLBACK_DESTINATIONS] == 1
    assert record["destination_hash"] == contact_email_hash(OPERATOR_INBOX)


def test_the_two_suppressions_are_told_apart() -> None:
    """#52 gave them distinguishable notes for exactly this. "A bot found our form" and
    "our rubric is rejecting everybody" need different people to look."""
    spam_pipeline, _, _ = build_pipeline(outcome=succeeded(spam=True))
    with capture_json_logs() as logs:
        spam_pipeline.qualify(request_for())
    spam = logs.one(EVENT_LEAD_SUPPRESSED)
    assert spam["suppression_cause"] == SuppressionCause.SPAM.value
    assert spam[DIM_SUPPRESSION_CAUSE] == SuppressionCause.SPAM.value
    assert spam[SUPPRESSIONS] == 1
    assert metric_names(dimensioned(spam, DIM_TENANT, DIM_SUPPRESSION_CAUSE)) == {SUPPRESSIONS}

    low_pipeline, _, _ = build_pipeline(outcome=succeeded(score=1))
    with capture_json_logs() as logs:
        low_pipeline.qualify(request_for())
    low = logs.one(EVENT_LEAD_SUPPRESSED)
    assert low["suppression_cause"] == SuppressionCause.BELOW_THRESHOLD.value
    assert low["tier"] == Tier.DISQUALIFIED.value


def test_a_suppressed_lead_emits_no_dispatch() -> None:
    pipeline, _, _ = build_pipeline(outcome=succeeded(spam=True))
    with capture_json_logs() as logs:
        pipeline.qualify(request_for())
    assert logs.events(EVENT_LEAD_ROUTED) == []


def test_a_redelivery_emits_the_duplicate_metric_and_nothing_else() -> None:
    store = InMemoryLeadStore()
    pipeline, _, _ = build_pipeline(store=store)
    pipeline.qualify(request_for())

    with capture_json_logs() as logs:
        result = pipeline.qualify(request_for())

    assert result.disposition is Disposition.DUPLICATE
    record = logs.one(EVENT_LEAD_DUPLICATE)
    assert record[DUPLICATES] == 1
    assert record[PIPELINE_LATENCY_MS] == result.latency_ms
    # No second model call, so no second assessment metric to double count the spend with.
    assert logs.events(EVENT_ASSESSMENT) == []
    assert logs.events(EVENT_LEAD_ROUTED) == []


def test_a_dispatch_failure_is_counted_for_the_worker_error_rate() -> None:
    pipeline, _, _ = build_pipeline(notifier=RecordingNotifier(fail_times=1))

    with capture_json_logs() as logs, pytest.raises(FakeNotifierError):
        pipeline.qualify(request_for())

    record = logs.one(EVENT_DISPATCH_FAILED)
    assert record[DISPATCH_FAILURES] == 1
    assert metric_names(dimensioned(record, DIM_TENANT)) == {DISPATCH_FAILURES}
    assert logs.events(EVENT_LEAD_ROUTED) == []


def test_the_edge_counts_what_it_did_with_each_submission() -> None:
    store = InMemoryLeadStore()
    queue = InProcessLeadQueue()
    with capture_json_logs() as logs:
        ingest_service(store, queue).accept(
            IngestRequest(tenant_id=TENANT, submission_id="sub-1", submission=SUBMISSION)
        )
    record = logs.one(EVENT_LEAD_ACCEPTED)
    assert record["disposition"] == "queued"
    assert record[INGESTED_LEADS] == 1
    assert record["Disposition"] == "queued"


def test_the_edge_counts_a_pre_filtered_submission_by_filter() -> None:
    store = InMemoryLeadStore()
    queue = InProcessLeadQueue()
    with capture_json_logs() as logs:
        ingest_service(store, queue).accept(
            IngestRequest(
                tenant_id=TENANT,
                submission_id="sub-1",
                submission=SUBMISSION,
                honeypot="filled-by-a-bot",
            )
        )
    record = logs.one(EVENT_LEAD_ACCEPTED)
    assert record["disposition"] == "suppressed"
    assert record["spam_reason"] == "honeypot"
    assert record["SpamReason"] == "honeypot"
    assert record["IngestSuppressions"] == 1
    assert queue.pending() == ()


# ------------------------------------------------------------------------ idempotency


def test_configuring_logging_twice_does_not_double_a_lead_s_journey() -> None:
    """A Lambda container is reused; a handler added per invocation doubles every metric.

    Asserted on the journey rather than on handler bookkeeping: two ``lead.routed`` lines
    for one lead is two dispatches as far as CloudWatch is concerned, and that is what the
    duplication actually costs.
    """
    pipeline, _, _ = build_pipeline()
    with capture_json_logs() as logs:
        configure_logging(
            Settings(env=Environment.PROD, log_level="INFO"),
            stream=logs.buffer,
            log_format=LOG_FORMAT_JSON,
        )
        pipeline.qualify(request_for())

    assert len(logs.events(EVENT_LEAD_ROUTED)) == 1
    assert len(logs.events(EVENT_ASSESSMENT)) == 1
    assert len(logs.events(EVENT_LEAD_ACCEPTED)) == 0  # nothing stray got in either
