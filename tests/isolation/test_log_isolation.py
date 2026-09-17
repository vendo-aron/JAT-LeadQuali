"""Log isolation: one tenant's trace output carries no other tenant's identifiers.

Logs are the isolation axis nobody designs and everybody leaks through. Nothing in the
system *intends* to write tenant B into tenant A's records; the ways it happens are all
accidents — a context variable that was bound and never unbound, a config the pipeline
cached from the previous lead, an exception whose ``repr`` drags a whole store along with
it. None of those is visible by reading the code, which is why this module runs the real
pipeline with the real formatter and searches the real bytes.

**The forbidden set is derived, never typed.** Every identifier tenant B has — slug, row
id, lead id, submission id, contact address, the SHA-256 of that address, routing
destination, the SHA-256 of *that*, company name, a distinctive phrase from the message —
is built from B's fixture data in :func:`forbidden_tokens`. Hardcoding the strings would
mean that renaming the fixture silently emptied the assertion, which is the way this kind
of test rots.

**Every absence assertion has a presence assertion next to it.** "B does not appear" is
true of an empty buffer, so each test also proves that A's own identifiers *are* there and
that the capture contains the events it should.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any, Final

import pytest

from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.adapters.store_postgres import PostgresLeadStore
from leadquali.app.assessment_result import AssessmentSucceeded, CallMetering
from leadquali.app.ingest import IngestRequest, IngestService
from leadquali.app.ports import RoutingOutcome
from leadquali.app.qualify import QualificationPipeline, QualificationRequest
from leadquali.domain.models import (
    Action,
    DimensionScores,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.tenant_config import TenantConfig
from leadquali.observability import contact_email_hash
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import (
    FakeClock,
    InMemoryLeadStore,
    RecordingNotifier,
    ScriptedAssessor,
    StaticConfigSource,
    StaticEnricher,
)
from tests.isolation.repositories import (
    LEAD_A,
    SUBMISSION_A,
    SUBMISSION_B,
    TENANT_A,
    TENANT_A_UUID,
    TENANT_B,
    TENANT_B_UUID,
)
from tests.logcapture import LogCapture, capture_json_logs
from tests.sqlcapture import CannedResult, SqlCapture

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)
OPERATOR_INBOX: Final[str] = "escalations@leadquali.invalid"


def digest_of(value: str) -> str:
    """:func:`~leadquali.observability.contact_email_hash`, narrowed to a present address.

    The real function answers ``None`` for a lead that arrived without one, which is a case
    every fixture here rules out by construction. Narrowing it once keeps the assertions
    below free of ``assert ... is not None`` noise.
    """
    digest = contact_email_hash(value)
    assert digest is not None, value
    return digest


class TenantFixture:
    """Everything one tenant is, in one object, so the forbidden set can be derived from it.

    Every string is distinctive and shares no substring with the other tenant's — a
    "does B appear in A's logs?" assertion where the two tenants were called ``acme`` and
    ``acme-demo`` would answer a question about substrings rather than about tenancy.

    The addresses are at plausible domains rather than at ``.invalid``, because the ingest
    spam pre-filter suppresses a reserved TLD before the pipeline ever runs and a suppressed
    lead emits half the events this module is searching. Nothing here resolves a name: the
    enricher is a double and no test in this package touches DNS.
    """

    def __init__(
        self,
        *,
        slug: str,
        row_id: object,
        submission_id: str,
        contact: str,
        company: str,
        phrase: str,
        destination: str,
    ) -> None:
        self.slug = slug
        self.row_id = str(row_id)
        self.submission_id = submission_id
        self.contact = contact
        self.company = company
        self.phrase = phrase
        self.destination = destination

    @property
    def submission(self) -> LeadSubmission:
        """The lead as it arrived from this tenant's form."""
        return LeadSubmission(
            full_name="Grace Hopper",
            email=self.contact,
            company=self.company,
            role="Director of Operations",
            message=f"We have a problem with {self.phrase} and need help this quarter.",
            extra={},
        )

    @property
    def config(self) -> TenantConfig:
        """A routing table that sends everything to this tenant's own inbox."""
        return TenantConfig.model_validate(
            {
                "tenant_id": self.slug,
                "name": self.company,
                "icp_description": f"Companies that care about {self.phrase}.",
                "routing_rules": {
                    tier.value: {
                        "action": Action.EMAIL_SALES.value,
                        "destination": self.destination,
                    }
                    for tier in Tier
                },
            }
        )

    def identifiers(self) -> frozenset[str]:
        """Every string that identifies this tenant or its lead, including the hashes.

        The hashes matter as much as the plain values. Invariant 5 says an address is
        logged as ``contact_email_hash(address)``, so the *correct* rendering of tenant B's
        contact is a 64-character digest — and a digest of B's address in A's log is a
        cross-tenant leak wearing the uniform of a privacy control.
        """
        return frozenset(
            {
                self.slug,
                self.row_id,
                self.submission_id,
                self.contact,
                digest_of(self.contact),
                self.company,
                self.phrase,
                self.destination,
                digest_of(self.destination),
            }
        )


ALPHA: Final[TenantFixture] = TenantFixture(
    slug=TENANT_A,
    row_id=TENANT_A_UUID,
    submission_id=SUBMISSION_A,
    contact="ada.q4x7@alpha-instruments-metrology.co.uk",
    company="Alpha Instruments Metrology",
    phrase="calibration drift on optical benches",
    destination="inbound-sales-7k2@alpha-instruments-metrology.co.uk",
)

ZENITH: Final[TenantFixture] = TenantFixture(
    slug=TENANT_B,
    row_id=TENANT_B_UUID,
    submission_id=SUBMISSION_B,
    contact="oskar.m9p3@zenith-freight-holdings.de",
    company="Zenith Freight Holdings",
    phrase="pallet throughput at cross-dock sites",
    destination="neugeschaeft-4b8@zenith-freight-holdings.de",
)

METERING: Final[CallMetering] = CallMetering(
    model_id="claude-opus-5",
    prompt_version="rubric_v1",
    effort="medium",
    input_tokens=480,
    output_tokens=190,
    cache_read_tokens=1024,
    cache_creation_tokens=0,
    cost_usd=Decimal("0.011000"),
    latency_ms=2800,
)

ASSESSMENT: Final[LeadAssessment] = LeadAssessment(
    dimension_scores=DimensionScores(
        icp_fit=24, intent=18, authority=11, urgency=8, budget_signal=9
    ),
    extracted=ExtractedFacts(
        company_name=None,
        industry=None,
        company_size_estimate=None,
        role_seniority="director",
        stated_use_case=None,
        stated_timeline="this quarter",
    ),
    reasoning="A senior operations contact with a concrete, in-quarter problem.",
    confidence=0.9,
    missing_information=[],
    suggested_first_question="Which site is worst affected?",
    spam_or_test_submission=False,
)


class Fleet:
    """One process serving both tenants, wired the way production is.

    Deliberately one store, one pipeline, one ingest service and one queue for both, since
    sharing them is the condition under which a leak is possible at all. A per-tenant
    process would make this module prove nothing.
    """

    def __init__(self) -> None:
        self.store = InMemoryLeadStore()
        self.notifier = RecordingNotifier()
        self.clock = FakeClock(start=NOW, step_ms=1)
        self.queue = InProcessLeadQueue()
        self.ingest = IngestService(store=self.store, queue=self.queue, clock=self.clock)
        self.pipeline = QualificationPipeline(
            config_source=StaticConfigSource(
                {ALPHA.slug: ALPHA.config, ZENITH.slug: ZENITH.config}
            ),
            assessor=ScriptedAssessor(
                AssessmentSucceeded(assessment=ASSESSMENT, metering=METERING)
            ),
            store=self.store,
            notifier=self.notifier,
            enricher=StaticEnricher(),
            clock=self.clock,
            escalation_destination=OPERATOR_INBOX,
        )

    def run(self, tenant: TenantFixture) -> str:
        """Ingest and qualify one lead end to end. Returns the run's trace id."""
        receipt = self.ingest.accept(
            IngestRequest(
                tenant_id=tenant.slug,
                submission_id=tenant.submission_id,
                submission=tenant.submission,
                source="web_form",
            )
        )
        result = self.pipeline.qualify(
            QualificationRequest(
                tenant_id=tenant.slug,
                submission_id=tenant.submission_id,
                submission=tenant.submission,
                received_at=NOW,
                trace_id=receipt.trace_id,
            )
        )
        return result.trace_id


def forbidden_tokens(absent: TenantFixture, *, present: TenantFixture) -> frozenset[str]:
    """``absent``'s identifiers, minus anything the two tenants happen to share.

    The subtraction is not a fudge: it is what keeps the assertion about tenancy. If a
    future fixture gave both tenants the same role or the same missing-information list,
    the shared value would fail this test for a reason that has nothing to do with
    isolation, and the failure would be dismissed rather than investigated.
    """
    return absent.identifiers() - present.identifiers()


def records_for(capture: LogCapture, trace_id: str) -> list[Mapping[str, Any]]:
    """Every record emitted under one run's trace id."""
    return [record for record in capture.records() if record.get("trace_id") == trace_id]


def assert_absent(haystack: str, tokens: Iterable[str], *, where: str) -> None:
    """Fail naming the token and the line it was found on."""
    for token in sorted(tokens):
        assert token not in haystack, f"{where}: {token!r} leaked into another tenant's output"


# -------------------------------------------------------------- one run, one tenant


def test_the_fixtures_share_nothing_that_would_make_this_module_lie() -> None:
    """The premise: two tenants with no string in common, and a forbidden set worth having."""
    assert not ALPHA.identifiers() & ZENITH.identifiers()
    for left in ALPHA.identifiers():
        for right in ZENITH.identifiers():
            assert left not in right and right not in left, (left, right)
    assert len(forbidden_tokens(ZENITH, present=ALPHA)) >= 9
    assert all(len(token) >= 8 for token in forbidden_tokens(ZENITH, present=ALPHA)), (
        "a short token would match by coincidence and make the assertion meaningless"
    )


def test_a_pipeline_run_for_one_tenant_logs_nothing_about_the_other() -> None:
    """The headline: B has been through this process, and A's run does not mention it.

    B is run first and outside the capture, so everything a leak could ride on — a bound
    context variable, a cached config, a store holding B's rows — is in place before A's
    lead arrives.
    """
    fleet = Fleet()
    fleet.run(ZENITH)

    with capture_json_logs() as capture:
        trace_id = fleet.run(ALPHA)

    text = capture.text
    assert text.strip(), "nothing was logged; the absence assertion would prove nothing"
    assert_absent(text, forbidden_tokens(ZENITH, present=ALPHA), where="alpha's run")

    # The positive control: A's own run is fully identified, hash and all.
    assert ALPHA.slug in text
    assert digest_of(ALPHA.contact) in text
    assert trace_id in text


def test_no_record_from_one_tenants_run_carries_the_others_tenant_id() -> None:
    """Field by field rather than by substring, which is the assertion with teeth.

    A leak into a *value* — ``tenant_id`` bound to the wrong slug by a context variable
    that outlived its block — is the realistic failure, and it is what a log aggregator
    would index and a dashboard would group by.
    """
    fleet = Fleet()
    with capture_json_logs() as capture:
        alpha_trace = fleet.run(ALPHA)
        zenith_trace = fleet.run(ZENITH)

    for trace_id, owner in ((alpha_trace, ALPHA), (zenith_trace, ZENITH)):
        records = records_for(capture, trace_id)
        assert records, f"{owner.slug} emitted nothing"
        for record in records:
            assert record.get("tenant_id", owner.slug) == owner.slug, record
            assert_absent(
                json.dumps(record),
                forbidden_tokens(ZENITH if owner is ALPHA else ALPHA, present=owner),
                where=f"{owner.slug} record {record.get('event')}",
            )


def test_running_the_tenants_in_either_order_leaks_neither_way() -> None:
    """Both directions, because a context variable leaks forwards only.

    Running A then B proves nothing about whether A's context survived into B's run unless
    B is also run first somewhere. This does both in one test so the pair cannot drift
    apart.
    """
    for first, second in ((ALPHA, ZENITH), (ZENITH, ALPHA)):
        fleet = Fleet()
        fleet.run(first)
        with capture_json_logs() as capture:
            fleet.run(second)
        assert_absent(
            capture.text,
            forbidden_tokens(first, present=second),
            where=f"{second.slug} after {first.slug}",
        )
        assert second.slug in capture.text


def test_every_event_a_run_emits_is_covered_by_the_assertion() -> None:
    """The capture is not accidentally one line.

    An isolation test over an empty or near-empty buffer is the commonest way this kind of
    module stops meaning anything. This names the events a successful lead produces, so a
    future change that stops emitting one has to come back through here.
    """
    fleet = Fleet()
    with capture_json_logs() as capture:
        trace_id = fleet.run(ALPHA)

    events = {record.get("event") for record in records_for(capture, trace_id)}
    assert {"lead.accepted", "assessment.completed", "lead.routed"} <= events, events


def test_the_leads_own_words_never_reach_the_log_for_either_tenant() -> None:
    """Invariant 5 restated as an isolation property.

    A lead's prose is not a pattern and no formatter can redact it, so if either tenant's
    message ever appeared it would be in full. Asserted for both tenants rather than one,
    because the interesting case is B's text in A's log and that only exists if the message
    reaches a log at all.
    """
    fleet = Fleet()
    with capture_json_logs() as capture:
        fleet.run(ALPHA)
        fleet.run(ZENITH)

    for tenant in (ALPHA, ZENITH):
        assert tenant.phrase not in capture.text
        assert tenant.contact not in capture.text
        assert tenant.company not in capture.text


# ------------------------------------------------------- the adapter's own log line


def test_the_store_logs_the_tenant_it_was_called_with_and_no_other() -> None:
    """``record_routing_event`` writes a log line of its own, so it gets its own assertion.

    It is the one place in the adapters that logs identifiers directly rather than through
    ``observability.events``, and it runs with a session, a tenant and a lead in scope —
    all three of which a sloppy edit could get from the wrong variable.
    """
    capture = SqlCapture()
    store = PostgresLeadStore(capture.sessions)

    with capture_json_logs() as logs:
        capture.run(
            lambda: store.record_routing_event(
                tenant_id=ALPHA.slug,
                lead_id=LEAD_A,
                action=Action.EMAIL_SALES,
                destination=ALPHA.destination,
                outcome=RoutingOutcome.DISPATCHED,
                provider_message_id="provider-msg-1",
                occurred_at=NOW,
                detail="scored 61.00/100 — warm",
            ),
            results=(CannedResult(), CannedResult()),
        )

    text = logs.text
    assert "routing event recorded" in text, text
    assert str(TENANT_A_UUID) in text
    assert_absent(text, forbidden_tokens(ZENITH, present=ALPHA), where="the store's log line")
    assert str(TENANT_B_UUID) not in text


@pytest.mark.parametrize("tenant", [ALPHA, ZENITH], ids=lambda item: item.slug)
def test_each_tenants_hash_is_its_own(tenant: TenantFixture) -> None:
    """The hashed form has to be tenant-distinguishing too, or the check above is vacuous.

    ``contact_email_hash`` is a function of the address alone. Two tenants with different
    addresses get different digests, which is what makes "B's digest in A's log" a
    detectable event rather than a coincidence nobody could tell from A's own.
    """
    other = ZENITH if tenant is ALPHA else ALPHA
    assert digest_of(tenant.contact) != digest_of(other.contact)
    assert digest_of(tenant.destination) != digest_of(other.destination)
    assert len(digest_of(tenant.contact)) == 64
