"""The email enricher, exercised without touching a nameserver.

Every test here drives an injected :class:`~leadquali.adapters.enrich_email.MxLookup`, so
the suite is offline, deterministic and fast. That is not only a CI convenience: the seam
that makes it possible is the same one that lets a deployment swap DNS for an internal
resolution service, and the same one that lets the failure paths — timeout, SERVFAIL,
NXDOMAIN — be tested at all, which is where the interesting behaviour lives.

The invariant under test throughout is that this adapter *cannot* break qualification. It
classifies what it can, marks what it could not, and never raises.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import dns.exception
import dns.resolver
import pytest

from leadquali.adapters.enrich_email import (
    DEFAULT_BREAKER_COOLDOWN_SECONDS,
    DEFAULT_CACHE_MAX_DOMAINS,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    FACT_ADDRESS_TYPE,
    FACT_COMPANY_MATCH,
    FACT_DOMAIN,
    FACT_DOMAIN_HAS_MX,
    FACT_DOMAIN_RESOLVES,
    FACT_DOMAIN_TYPE,
    NO,
    NOT_APPLICABLE,
    NOT_CHECKED,
    UNKNOWN,
    YES,
    AddressType,
    DnsPythonMxLookup,
    DomainType,
    EmailEnricher,
    MxResult,
    default_disposable_domains_path,
    default_free_mail_domains_path,
)
from leadquali.app.assessment_result import AssessmentSucceeded, CallMetering
from leadquali.app.enrichment import Enrichment, enrichment_block
from leadquali.app.ports import EnricherPort
from leadquali.app.qualify import (
    Disposition,
    QualificationPipeline,
    QualificationRequest,
)
from leadquali.domain.models import DimensionScores, ExtractedFacts, LeadAssessment
from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import FakeClock, InMemoryLeadStore, RecordingNotifier, ScriptedAssessor

TENANT = "acme"


# --------------------------------------------------------------------------- doubles


class FakeMxLookup:
    """An :class:`MxLookup` answering from a dict, and counting every call.

    ``calls`` is the point of it: "one lookup per lead, and none at all when the cache
    already knows" is a behavioural claim, and the only way to assert it is to count.
    """

    def __init__(
        self,
        results: Mapping[str, MxResult] | None = None,
        *,
        default: MxResult = MxResult.HAS_MX,
        raises: Exception | None = None,
        raise_times: int | None = None,
    ) -> None:
        self.results = dict(results or {})
        self.default = default
        self.raises = raises
        self.raise_times = raise_times
        self.calls: list[str] = []

    def lookup_mx(self, domain: str) -> MxResult:
        self.calls.append(domain)
        if self.raises is not None and (
            self.raise_times is None or len(self.calls) <= self.raise_times
        ):
            raise self.raises
        return self.results.get(domain, self.default)


@dataclass
class FakeMonotonic:
    """An injectable monotonic clock, so cache expiry and cooldowns need no sleeping."""

    value: float = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# --------------------------------------------------------------------------- helpers


def enrich(
    email: str | None,
    *,
    company: str | None = None,
    enricher: EmailEnricher | None = None,
    lookup: FakeMxLookup | None = None,
) -> Enrichment:
    """Enrich one submission built from just an address and (optionally) a company."""
    the_enricher = enricher or EmailEnricher(lookup=lookup or FakeMxLookup())
    return the_enricher.enrich(
        tenant_id=TENANT, submission=LeadSubmission(email=email, company=company)
    )


def facts(result: Enrichment) -> dict[str, str]:
    return dict(result.facts)


# --------------------------------------------------------------------------- the port


def test_it_satisfies_the_enricher_port() -> None:
    """Structural conformance, checked rather than assumed."""
    assert isinstance(EmailEnricher(lookup=FakeMxLookup()), EnricherPort)


def test_the_bundled_lists_exist_and_are_loaded() -> None:
    """A path typo would otherwise turn every classification into ``corporate``."""
    assert default_disposable_domains_path().is_file()
    assert default_free_mail_domains_path().is_file()
    enricher = EmailEnricher(lookup=FakeMxLookup())
    assert len(enricher.disposable_domains) >= 100
    assert len(enricher.free_mail_domains) >= 50
    assert "mailinator.com" in enricher.disposable_domains
    assert "gmail.com" in enricher.free_mail_domains
    # The two lists are different signals; a domain in both would make them one flag.
    assert not (enricher.disposable_domains & enricher.free_mail_domains)


# --------------------------------------------------------------- domain classification


def test_a_corporate_domain_with_mx_is_reported_as_corporate() -> None:
    lookup = FakeMxLookup({"acme.test": MxResult.HAS_MX})
    result = enrich("dana.reed@acme.test", company="Acme", lookup=lookup)

    assert result.available is True
    assert facts(result) == {
        FACT_DOMAIN: "acme.test",
        FACT_DOMAIN_TYPE: DomainType.CORPORATE.value,
        FACT_ADDRESS_TYPE: AddressType.PERSONAL.value,
        FACT_DOMAIN_RESOLVES: YES,
        FACT_DOMAIN_HAS_MX: YES,
        FACT_COMPANY_MATCH: YES,
    }
    assert lookup.calls == ["acme.test"]


def test_a_free_mail_provider_is_not_called_corporate_and_not_called_disposable() -> None:
    """gmail is a real reachable person; the throwaway signal must not be borrowed for it."""
    result = enrich("dana.reed@gmail.com", company="Acme Ltd")

    assert facts(result)[FACT_DOMAIN_TYPE] == DomainType.FREE_MAIL.value
    assert facts(result)[FACT_DOMAIN_TYPE] != DomainType.DISPOSABLE.value
    assert result.available is True


def test_a_disposable_provider_is_reported_as_disposable() -> None:
    result = enrich("someone@mailinator.com", company="Acme")

    assert facts(result)[FACT_DOMAIN_TYPE] == DomainType.DISPOSABLE.value


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("dana@acme.test", DomainType.CORPORATE),
        ("dana@gmail.com", DomainType.FREE_MAIL),
        ("dana@GMAIL.COM", DomainType.FREE_MAIL),
        ("dana@yopmail.com", DomainType.DISPOSABLE),
        ("dana@mail.acme.test", DomainType.CORPORATE),
    ],
)
def test_domain_type_classification(address: str, expected: DomainType) -> None:
    assert facts(enrich(address))[FACT_DOMAIN_TYPE] == expected.value


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("info@acme.test", AddressType.ROLE_ACCOUNT),
        ("sales@acme.test", AddressType.ROLE_ACCOUNT),
        ("admin@acme.test", AddressType.ROLE_ACCOUNT),
        ("no-reply@acme.test", AddressType.ROLE_ACCOUNT),
        ("No.Reply@acme.test", AddressType.ROLE_ACCOUNT),
        ("hello+tag@acme.test", AddressType.ROLE_ACCOUNT),
        ("dana.reed@acme.test", AddressType.PERSONAL),
        ("d.reed2@acme.test", AddressType.PERSONAL),
        ("infomatics@acme.test", AddressType.PERSONAL),
    ],
)
def test_role_addresses_are_told_apart_from_personal_ones(
    address: str, expected: AddressType
) -> None:
    assert facts(enrich(address))[FACT_ADDRESS_TYPE] == expected.value


def test_the_domain_is_normalised_out_of_a_display_name_and_case() -> None:
    """A form field holds whatever the browser autofilled into it."""
    result = enrich("  Dana Reed <Dana.Reed@ACME.test.> ")

    assert facts(result)[FACT_DOMAIN] == "acme.test"


# ------------------------------------------------------------------------ MX outcomes


def test_a_domain_that_resolves_without_mx_records_says_so() -> None:
    lookup = FakeMxLookup({"acme.test": MxResult.NO_MX_RECORDS})
    result = enrich("dana@acme.test", lookup=lookup)

    assert result.available is True
    assert facts(result)[FACT_DOMAIN_RESOLVES] == YES
    assert facts(result)[FACT_DOMAIN_HAS_MX] == NO


def test_nxdomain_reports_a_domain_that_does_not_resolve() -> None:
    lookup = FakeMxLookup({"acme.test": MxResult.NO_SUCH_DOMAIN})
    result = enrich("dana@acme.test", lookup=lookup)

    assert result.available is True
    assert facts(result)[FACT_DOMAIN_RESOLVES] == NO
    assert facts(result)[FACT_DOMAIN_HAS_MX] == NO


# ------------------------------------------------------------------- failing outcomes


def test_a_dns_timeout_degrades_to_unavailable_and_keeps_the_offline_facts() -> None:
    """The classification needs no network, so a timeout must not throw it away."""
    lookup = FakeMxLookup(raises=dns.exception.Timeout())
    result = enrich("info@mailinator.com", lookup=lookup)

    assert result.available is False
    assert result.unavailable_reason
    assert facts(result)[FACT_DOMAIN_TYPE] == DomainType.DISPOSABLE.value
    assert facts(result)[FACT_ADDRESS_TYPE] == AddressType.ROLE_ACCOUNT.value
    assert facts(result)[FACT_DOMAIN_RESOLVES] == UNKNOWN
    assert facts(result)[FACT_DOMAIN_HAS_MX] == UNKNOWN


@pytest.mark.parametrize(
    "exception",
    [
        dns.exception.Timeout(),
        dns.resolver.NoNameservers(),
        dns.resolver.LifetimeTimeout(timeout=2.0, errors=[]),
        OSError("network is unreachable"),
        RuntimeError("resolver misconfigured"),
        ValueError("nonsense"),
        MemoryError(),
    ],
    ids=lambda exception: type(exception).__name__,
)
def test_any_resolver_exception_becomes_unavailable_rather_than_propagating(
    exception: Exception,
) -> None:
    """Invariant: enrichment is an optimisation. Nothing it does may reach the caller."""
    result = enrich("dana@acme.test", lookup=FakeMxLookup(raises=exception))

    assert result.available is False
    assert type(exception).__name__ in result.unavailable_reason


def test_a_missing_address_is_unavailable_with_no_facts_at_all() -> None:
    result = enrich(None)

    assert result == Enrichment.unavailable(result.unavailable_reason)
    assert not result.facts
    assert "no email address" in result.unavailable_reason


@pytest.mark.parametrize(
    "address",
    ["", "   ", "not-an-email", "@acme.test", "dana@", "dana@@acme.test", "dana@localhost"],
)
def test_a_malformed_address_is_unavailable_with_no_facts_at_all(address: str) -> None:
    result = enrich(address)

    assert result == Enrichment.unavailable(result.unavailable_reason)
    assert not result.facts
    assert result.unavailable_reason


def test_a_malformed_address_never_costs_a_lookup() -> None:
    lookup = FakeMxLookup()
    enrich("not-an-email", lookup=lookup)

    assert lookup.calls == []


# ------------------------------------------------------------------------- the cache


def test_a_second_lead_from_the_same_domain_does_not_re_resolve_it() -> None:
    lookup = FakeMxLookup({"acme.test": MxResult.HAS_MX})
    enricher = EmailEnricher(lookup=lookup)

    first = enrich("dana@acme.test", enricher=enricher)
    second = enrich("sam@acme.test", enricher=enricher)

    assert lookup.calls == ["acme.test"]
    assert facts(first)[FACT_DOMAIN_HAS_MX] == YES
    assert facts(second)[FACT_DOMAIN_HAS_MX] == YES


def test_the_cache_entry_expires() -> None:
    clock = FakeMonotonic()
    lookup = FakeMxLookup()
    enricher = EmailEnricher(lookup=lookup, cache_ttl_seconds=60.0, monotonic=clock)

    enrich("dana@acme.test", enricher=enricher)
    clock.advance(59.0)
    enrich("sam@acme.test", enricher=enricher)
    assert lookup.calls == ["acme.test"]

    clock.advance(2.0)
    enrich("kim@acme.test", enricher=enricher)
    assert lookup.calls == ["acme.test", "acme.test"]


def test_the_cache_is_bounded_and_evicts_the_least_recently_used_domain() -> None:
    """A flood of unique domains must not become a memory leak."""
    lookup = FakeMxLookup()
    enricher = EmailEnricher(lookup=lookup, cache_max_domains=2)

    enrich("a@one.test", enricher=enricher)
    enrich("a@two.test", enricher=enricher)
    enrich("a@one.test", enricher=enricher)  # refreshes one.test, so two.test is oldest
    enrich("a@three.test", enricher=enricher)  # evicts two.test
    enrich("a@two.test", enricher=enricher)  # must be looked up again

    assert lookup.calls == ["one.test", "two.test", "three.test", "two.test"]
    assert enricher.cached_domains == 2


def test_a_negative_result_is_cached_too() -> None:
    lookup = FakeMxLookup({"gone.test": MxResult.NO_SUCH_DOMAIN})
    enricher = EmailEnricher(lookup=lookup)

    enrich("a@gone.test", enricher=enricher)
    second = enrich("b@gone.test", enricher=enricher)

    assert lookup.calls == ["gone.test"]
    assert facts(second)[FACT_DOMAIN_RESOLVES] == NO


# ---------------------------------------------------------------- the circuit breaker


def test_repeated_failures_stop_the_adapter_from_paying_the_timeout_every_lead() -> None:
    """A DNS outage costs `failure_threshold` timeouts per cooldown, not one per lead."""
    lookup = FakeMxLookup(raises=dns.exception.Timeout())
    enricher = EmailEnricher(lookup=lookup, failure_threshold=2, monotonic=FakeMonotonic())

    results = [enrich(f"a@company{n}.test", enricher=enricher) for n in range(6)]

    assert len(lookup.calls) == 2
    assert all(result.available is False for result in results)
    assert "paused" in results[-1].unavailable_reason


def test_the_breaker_closes_again_after_its_cooldown() -> None:
    clock = FakeMonotonic()
    lookup = FakeMxLookup(raises=dns.exception.Timeout())
    enricher = EmailEnricher(
        lookup=lookup, failure_threshold=1, breaker_cooldown_seconds=30.0, monotonic=clock
    )

    enrich("a@one.test", enricher=enricher)
    enrich("a@two.test", enricher=enricher)
    assert len(lookup.calls) == 1

    clock.advance(31.0)
    enrich("a@three.test", enricher=enricher)
    assert len(lookup.calls) == 2


def test_a_success_clears_the_failure_count() -> None:
    lookup = FakeMxLookup(raises=dns.exception.Timeout(), raise_times=1)
    enricher = EmailEnricher(lookup=lookup, failure_threshold=2)

    enrich("a@one.test", enricher=enricher)
    assert enricher.consecutive_failures == 1

    enrich("a@two.test", enricher=enricher)
    assert enricher.consecutive_failures == 0


# ---------------------------------------------------------------- company/domain match


@pytest.mark.parametrize(
    ("company", "domain"),
    [
        ("Acme", "acme.test"),
        ("Acme Widgets Ltd", "acme.test"),
        ("acme", "acme.co.uk"),
        ("Acme", "mail.acme.test"),
        ("Vendo Works", "vendoworks.io"),
        ("International Business Machines", "ibm.test"),
    ],
)
def test_a_company_that_plausibly_matches_its_email_domain(company: str, domain: str) -> None:
    result = enrich(f"dana@{domain}", company=company)

    assert facts(result)[FACT_COMPANY_MATCH] == YES


@pytest.mark.parametrize(
    ("company", "domain"),
    [
        ("Globex", "acme.test"),
        ("Acme", "initech.test"),
        ("Northwind Traders", "contoso.test"),
    ],
)
def test_a_company_that_does_not_match_its_email_domain(company: str, domain: str) -> None:
    result = enrich(f"dana@{domain}", company=company)

    assert facts(result)[FACT_COMPANY_MATCH] == NO


def test_no_company_means_the_match_was_not_checked() -> None:
    assert facts(enrich("dana@acme.test"))[FACT_COMPANY_MATCH] == NOT_CHECKED


@pytest.mark.parametrize("domain", ["gmail.com", "mailinator.com"])
def test_the_match_is_not_applicable_on_a_consumer_or_throwaway_domain(domain: str) -> None:
    """ "Acme does not match gmail.com" is true and worthless; asserting it would mislead."""
    result = enrich(f"dana@{domain}", company="Acme")

    assert facts(result)[FACT_COMPANY_MATCH] == NOT_APPLICABLE


# ------------------------------------------------------------------------ invariant 5


def test_the_local_part_never_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Invariant 5: the domain is fine, the address is not."""
    caplog.set_level(logging.DEBUG, logger="leadquali.adapters.enrich_email")
    enrich("dana.the.secret.person@acme.test", lookup=FakeMxLookup(raises=OSError("down")))
    enrich("also.secret@nope", lookup=FakeMxLookup())

    assert "secret" not in caplog.text
    assert "acme.test" in caplog.text


def test_the_local_part_never_reaches_the_prompt() -> None:
    result = enrich("dana.the.secret.person@acme.test")

    assert "secret" not in enrichment_block(result)
    assert "acme.test" in enrichment_block(result)


def test_the_reason_carries_no_address_either() -> None:
    result = enrich("dana.secret@acme.test", lookup=FakeMxLookup(raises=OSError("boom")))

    assert "secret" not in result.unavailable_reason


# ------------------------------------------------------------------------- the block


def test_the_facts_render_into_the_prompt_block() -> None:
    block = enrichment_block(enrich("info@acme.test", company="Acme"))

    assert "email_domain: acme.test" in block
    assert f"{FACT_DOMAIN_TYPE}: corporate" in block
    assert "verified_facts" in block


def test_an_unavailable_enrichment_tells_the_model_to_record_the_gap() -> None:
    block = enrichment_block(enrich("dana@acme.test", lookup=FakeMxLookup(raises=OSError("x"))))

    assert "missing_information" in block


# ------------------------------------------------------------------- the dns adapter


def make_dns_lookup(monkeypatch: pytest.MonkeyPatch, outcome: object) -> DnsPythonMxLookup:
    """A real :class:`DnsPythonMxLookup` whose resolver answers from memory."""
    resolver = dns.resolver.Resolver(configure=False)

    def fake_resolve(*args: object, **kwargs: object) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    return DnsPythonMxLookup(resolver=resolver)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ([object()], MxResult.HAS_MX),
        ([], MxResult.NO_MX_RECORDS),
        (dns.resolver.NoAnswer(), MxResult.NO_MX_RECORDS),
        (dns.resolver.NXDOMAIN(), MxResult.NO_SUCH_DOMAIN),
    ],
)
def test_the_dnspython_lookup_translates_resolver_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcome: object, expected: MxResult
) -> None:
    assert make_dns_lookup(monkeypatch, outcome).lookup_mx("acme.test") == expected


def test_the_dnspython_lookup_lets_a_timeout_through_to_the_enricher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translating a timeout into a *fact* would be a lie; it is a failure, and it raises."""
    lookup = make_dns_lookup(monkeypatch, dns.exception.Timeout())

    with pytest.raises(dns.exception.Timeout):
        lookup.lookup_mx("acme.test")


def test_the_dnspython_lookup_bounds_every_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """One bounded attempt: ``lifetime`` caps the whole query, retries included."""
    seen: dict[str, object] = {}
    resolver = dns.resolver.Resolver(configure=False)

    def fake_resolve(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return [object()]

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    DnsPythonMxLookup(resolver=resolver, timeout_seconds=1.5).lookup_mx("acme.test")

    assert seen["lifetime"] == 1.5


def test_the_defaults_are_the_documented_ones() -> None:
    """These numbers are a deployment contract; a silent change deserves a failing test."""
    assert DEFAULT_TIMEOUT_SECONDS == 2.0
    assert DEFAULT_CACHE_TTL_SECONDS == 900.0
    assert DEFAULT_CACHE_MAX_DOMAINS == 1024
    assert DEFAULT_BREAKER_COOLDOWN_SECONDS == 60.0


# ---------------------------------------------------------------- the pipeline holds


def build_pipeline(
    enricher: EnricherPort,
) -> tuple[QualificationPipeline, RecordingNotifier, ScriptedAssessor]:
    config = TenantConfig.model_validate(
        {
            "tenant_id": TENANT,
            "name": "Acme Corp",
            "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
            "routing_rules": {
                "hot": {"action": "email_sales", "destination": "hot@acme.test"},
                "warm": {"action": "email_sales", "destination": "sales@acme.test"},
                "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
                "disqualified": {"action": "suppress"},
            },
        }
    )
    assessment = LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=25, intent=25, authority=15, urgency=15, budget_signal=15
        ),
        extracted=ExtractedFacts(
            company_name="Acme",
            industry="saas",
            company_size_estimate="120",
            role_seniority="vp",
            stated_use_case="lead routing",
            stated_timeline="this quarter",
        ),
        reasoning="Fits the profile.",
        confidence=0.9,
        missing_information=[],
        suggested_first_question=None,
        spam_or_test_submission=False,
    )
    metering = CallMetering(
        model_id="claude-opus-5",
        prompt_version="rubric_v1",
        effort="medium",
        input_tokens=500,
        output_tokens=800,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=Decimal("0.02"),
        latency_ms=3200,
    )
    notifier = RecordingNotifier()
    assessor = ScriptedAssessor(AssessmentSucceeded(assessment=assessment, metering=metering))

    class OneConfig:
        def get(self, tenant_id: str) -> TenantConfig:
            del tenant_id
            return config

    pipeline = QualificationPipeline(
        config_source=OneConfig(),
        assessor=assessor,
        store=InMemoryLeadStore(),
        notifier=notifier,
        enricher=enricher,
        clock=FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC)),
        escalation_destination="ops@vendoworks.test",
    )
    return pipeline, notifier, assessor


def test_a_dns_outage_still_produces_a_complete_assessment() -> None:
    """#18's acceptance criterion: the lead is qualified and dispatched anyway."""
    enricher = EmailEnricher(lookup=FakeMxLookup(raises=dns.exception.Timeout()))
    pipeline, notifier, _ = build_pipeline(enricher)

    result = pipeline.qualify(
        QualificationRequest(
            tenant_id=TENANT,
            submission_id="sub-1",
            submission=LeadSubmission(
                email="dana@acme.test", company="Acme", message="We need lead routing."
            ),
        )
    )

    assert result.disposition is Disposition.DISPATCHED
    assert result.enrichment_available is False
    assert len(notifier.dispatches) == 1


def test_a_working_enricher_puts_its_facts_in_front_of_the_model() -> None:
    enricher = EmailEnricher(lookup=FakeMxLookup())
    pipeline, _, assessor = build_pipeline(enricher)

    pipeline.qualify(
        QualificationRequest(
            tenant_id=TENANT,
            submission_id="sub-1",
            submission=LeadSubmission(email="info@acme.test", company="Acme"),
        )
    )

    assert "email_domain: acme.test" in assessor.prompts[0]
