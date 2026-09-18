"""Promotion strips PII, is idempotent, and produces a case #22 actually accepts.

The last one is the point of this file. There is no second PII rule in
``leadquali.app.golden_promotion``: the rendered case is fed to #22's own
:func:`~tests.evals.golden_set.parse_golden_set`, which is the check that fails the build
if a real address ever reaches ``golden_leads.jsonl``. Asserting against that parser rather
than against a copy of its rules is what stops the two from drifting until the copy is the
only one anybody runs.

Every assertion is on the **promoted record**, never on the fixture: a test that checked
the input was clean would pass just as happily against a promotion that did nothing.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from leadquali.app.golden_promotion import (
    MIN_PROMOTION_NOTE_CHARS,
    REDACTED_TEXT,
    GoldenPromotionError,
    GoldenPromotionService,
    case_id_for,
    render_case,
    strip_pii,
)
from leadquali.domain.models import Tier
from tests.evals.golden_set import (
    ACCEPTANCE_TARGET_REAL_CASES,
    ACCEPTANCE_TARGET_TOTAL_CASES,
    HEADER_KEY,
    MIN_NOTES_CHARS,
    SCHEMA_VERSION,
    GoldenSetError,
    parse_golden_set,
)
from tests.fakes import InMemoryGoldenPromotionStore

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
SLUG = "acme"
LEAD = "0f3c9a12-4d5e-4f60-9a1b-2c3d4e5f6071"
RATIONALE = (
    "Textbook ICP and a real deadline, but the contact is an analyst with no budget "
    "authority, so warm is the honest answer rather than hot."
)

#: A payload with a real-looking person in every field a form collects, including three
#: kinds of identifier buried inside the free text where no field rewrite would find them.
A_REAL_PAYLOAD: dict[str, Any] = {
    "full_name": "Priya Raghunathan",
    "email": "priya.raghunathan@northstar-logistics.co.uk",
    "company": "Northstar Logistics Ltd",
    "role": "VP Revenue Operations",
    "phone": "+44 20 7946 0958",
    "website": "https://www.northstar-logistics.co.uk/about",
    "message": (
        "We are a 300-person logistics firm replacing a spreadsheet process this quarter "
        "and have budget signed off. Reply to me at priya@northstar-logistics.co.uk or "
        "call 020 7946 0999, details at https://northstar-logistics.co.uk/rfp."
    ),
    "utm_source": "linkedin",
}

#: Every string in the payload that must not survive promotion, checked against the
#: rendered case rather than against the fields we happened to think of.
LEAKS = (
    "Priya",
    "Raghunathan",
    "northstar-logistics",
    "Northstar",
    "7946",
)


def service() -> tuple[GoldenPromotionService, InMemoryGoldenPromotionStore]:
    store = InMemoryGoldenPromotionStore()
    return GoldenPromotionService(store=store), store


def promote(
    tier: Tier = Tier.WARM, note: str = RATIONALE, lead_id: str = LEAD
) -> tuple[Any, bool, InMemoryGoldenPromotionStore]:
    promoter, store = service()
    promotion, created = promoter.promote(
        tenant_slug=SLUG,
        lead_id=lead_id,
        expected_tier=tier,
        promoted_by="icp_owner",
        note=note,
        now=NOW,
    )
    return promotion, created, store


def as_golden_file(case: dict[str, Any]) -> str:
    """One case, wrapped in the header #22's parser expects, as a JSONL document."""
    header = {
        HEADER_KEY: {
            "schema_version": SCHEMA_VERSION,
            "note": "promoted by the admin; measures self-consistency until labelled twice",
            "min_total_cases": 1,
            "min_real_cases": 0,
            "acceptance_target_total_cases": ACCEPTANCE_TARGET_TOTAL_CASES,
            "acceptance_target_real_cases": ACCEPTANCE_TARGET_REAL_CASES,
        }
    }
    return json.dumps(header) + "\n" + json.dumps(case, sort_keys=True) + "\n"


# --------------------------------------------------------------------- the PII rewrite


def test_the_promoted_case_is_accepted_by_the_golden_set_validator() -> None:
    """#22's parser is the authority, and it refuses any address outside its allowlist."""
    promotion, _, _ = promote()

    parsed = parse_golden_set(as_golden_file(render_case(promotion=promotion, form=A_REAL_PAYLOAD)))

    assert parsed.real_count == 1
    assert parsed.cases[0].case_id == promotion.case_id


@pytest.mark.parametrize("leak", LEAKS)
def test_nothing_identifying_survives_into_the_promoted_case(leak: str) -> None:
    """Asserted on the rendered case, so a promotion that did nothing fails this."""
    promotion, _, _ = promote()

    rendered = json.dumps(render_case(promotion=promotion, form=A_REAL_PAYLOAD))

    assert leak not in rendered


def test_the_signal_survives_the_rewrite() -> None:
    """A VP is still a VP and a 300-person logistics firm is still one; otherwise the case
    tests nothing the rubric cares about."""
    stripped = strip_pii(A_REAL_PAYLOAD, lead_id=LEAD)

    assert stripped["role"] == "VP Revenue Operations"
    assert stripped["message"] is not None
    assert "300-person logistics firm" in stripped["message"]
    assert "budget signed off" in stripped["message"]
    assert stripped["utm_source"] == "linkedin"


def test_identifiers_buried_in_free_text_are_removed() -> None:
    """The field rewrite cannot reach these; the scrub is what does."""
    message = strip_pii(A_REAL_PAYLOAD, lead_id=LEAD)["message"]

    assert message is not None
    assert message.count(REDACTED_TEXT) == 3, message


#: Identifiers the first version of the finder passed through untouched, **and** that #22's
#: gate then accepted — so the "the validator is the authority" argument did not hold for
#: them. The first two are how most of the non-English web writes an address; the third is
#: how a person is actually named in a sales note.
UNCAUGHT_BEFORE = [
    pytest.param("priya@n\u00f6rthstar-logistics.de", id="idn-domain"),
    pytest.param("priya@northstar\uff0ecom", id="fullwidth-dot"),
    pytest.param("priya\uff20northstar.com", id="fullwidth-at"),
    pytest.param("linkedin.com/in/priya-raghunathan", id="scheme-less-url"),
]


@pytest.mark.parametrize("identifier", UNCAUGHT_BEFORE)
def test_an_identifier_the_ascii_patterns_missed_is_stripped(identifier: str) -> None:
    """Under-matching costs a customer's contact in a file that goes into git."""
    message = strip_pii({"message": f"reach me at {identifier}"}, lead_id=LEAD)["message"]

    assert message is not None
    assert identifier not in message
    assert REDACTED_TEXT in message


@pytest.mark.parametrize("identifier", UNCAUGHT_BEFORE)
def test_the_golden_set_gate_would_also_have_refused_it(identifier: str) -> None:
    """The other half, and the one that broke the argument at ``golden_promotion.py``'s top.

    The finder and the gate failed on exactly the same inputs, so "#22's validator is the
    authority" was true only for addresses both already handled. Here the gate is fed the
    raw identifier directly — bypassing the pseudonymiser entirely — and must refuse it.
    """
    promotion, _, _ = promote()
    case = render_case(promotion=promotion, form=A_REAL_PAYLOAD)
    case["form"] = {**case["form"], "message": f"reach me at {identifier}"}

    if identifier.endswith("priya-raghunathan"):
        # A scheme-less URL in free text is the pseudonymiser's to catch; #22's gate checks
        # addresses everywhere and URLs only in the `website` field, which is its documented
        # scope. Put it where the gate looks.
        case["form"] = {**case["form"], "website": identifier, "message": "a normal message"}

    with pytest.raises(GoldenSetError):
        parse_golden_set(as_golden_file(case))


@pytest.mark.parametrize(
    "kept",
    [
        "We are a 300-person logistics firm replacing a spreadsheet this quarter.",
        "We are replacing acme.com internally and have budget signed off.",
        "Budget is $50k-$100k and the VP of RevOps has signed off.",
    ],
)
def test_the_widened_patterns_do_not_eat_the_signal(kept: str) -> None:
    """Over-matching is the cheap failure, but it is not free: a bare domain is the signal
    a case exists to test, while a domain with a path identifies somebody."""
    assert strip_pii({"message": kept}, lead_id=LEAD)["message"] == kept


def test_the_rewrite_is_deterministic_so_a_re_export_is_not_a_diff() -> None:
    assert strip_pii(A_REAL_PAYLOAD, lead_id=LEAD) == strip_pii(A_REAL_PAYLOAD, lead_id=LEAD)


def test_two_leads_get_different_pseudonyms() -> None:
    """Otherwise every promoted case would claim to be the same person at the same firm."""
    mine = strip_pii(A_REAL_PAYLOAD, lead_id=LEAD)
    theirs = strip_pii(A_REAL_PAYLOAD, lead_id="a different lead entirely")

    assert mine["email"] != theirs["email"]


def test_a_field_the_lead_never_filled_in_stays_empty() -> None:
    """Inventing a phone number adds a signal the lead did not give."""
    stripped = strip_pii({"email": "x@y.test", "phone": None, "company": ""}, lead_id=LEAD)

    assert stripped["phone"] is None
    assert stripped["company"] == ""


def test_the_minimum_rationale_matches_the_golden_set_s_own() -> None:
    """Two spellings of "long enough" is how a promotion the file refuses reports success."""
    assert MIN_PROMOTION_NOTE_CHARS == MIN_NOTES_CHARS


def test_a_case_whose_rationale_is_too_short_would_be_refused_by_the_validator() -> None:
    """The check above, demonstrated end to end rather than asserted as a constant."""
    promotion, _, _ = promote(note="a" * MIN_PROMOTION_NOTE_CHARS)
    short = render_case(promotion=promotion, form=A_REAL_PAYLOAD)
    short["labels"][0]["notes"] = "too short"

    with pytest.raises(GoldenSetError):
        parse_golden_set(as_golden_file(short))


# ---------------------------------------------------------------------------- the case


def test_the_case_records_where_it_came_from_and_who_labelled_it() -> None:
    promotion, _, _ = promote()

    case = render_case(promotion=promotion, form=A_REAL_PAYLOAD)

    assert case["provenance"] == "real"
    assert case["promoted_from"] == f"feedback:{LEAD[:8]}"
    assert case["labels"][0]["labeler"] == "icp_owner"
    assert case["labels"][0]["tier"] == "warm"
    assert case["expected_tier"] == "warm"


def test_the_expected_tier_is_the_human_s_answer_not_the_model_s() -> None:
    """A golden case whose expectation came from the thing under test measures nothing."""
    promotion, _, _ = promote(tier=Tier.COLD)

    case = render_case(promotion=promotion, form=A_REAL_PAYLOAD)

    assert case["expected_tier"] == "cold"
    assert {label["tier"] for label in case["labels"]} == {"cold"}


def test_a_hard_case_is_flagged_only_when_the_promoter_says_so() -> None:
    promotion, _, _ = promote()

    assert "hard_case" not in render_case(promotion=promotion, form=A_REAL_PAYLOAD)
    flagged = render_case(promotion=promotion, form=A_REAL_PAYLOAD, hard_case=True)
    assert flagged["hard_case"] is True


def test_the_case_id_is_derived_so_a_lost_page_does_not_create_a_second_case() -> None:
    mine = case_id_for(tenant_slug=SLUG, lead_id=LEAD)

    assert mine == case_id_for(tenant_slug=SLUG, lead_id=LEAD)
    assert mine != case_id_for(tenant_slug="other", lead_id=LEAD)


# -------------------------------------------------------------------------- idempotency


def test_promoting_the_same_lead_twice_does_not_add_it_twice() -> None:
    """The eval harness would weigh a doubly-promoted lead twice."""
    promoter, store = service()
    arguments: dict[str, Any] = {
        "tenant_slug": SLUG,
        "lead_id": LEAD,
        "expected_tier": Tier.WARM,
        "promoted_by": "icp_owner",
        "note": RATIONALE,
        "now": NOW,
    }

    first, created_first = promoter.promote(**arguments)
    second, created_second = promoter.promote(**arguments)

    assert created_first is True
    assert created_second is False
    assert first == second
    assert len(store.list_promotions(tenant_slug=SLUG)) == 1


def test_a_second_promotion_does_not_overwrite_the_first_label() -> None:
    """The first label is the one that was reviewed; a later click must not silently
    replace the tier it recorded."""
    promoter, store = service()
    promoter.promote(
        tenant_slug=SLUG,
        lead_id=LEAD,
        expected_tier=Tier.WARM,
        promoted_by="icp_owner",
        note=RATIONALE,
        now=NOW,
    )

    again, created = promoter.promote(
        tenant_slug=SLUG,
        lead_id=LEAD,
        expected_tier=Tier.HOT,
        promoted_by="someone_else",
        note=RATIONALE,
        now=NOW + dt.timedelta(days=1),
    )

    assert created is False
    assert again.expected_tier is Tier.WARM
    assert again.promoted_by == "icp_owner"
    assert store.list_promotions(tenant_slug=SLUG)[0].expected_tier is Tier.WARM


def test_promotions_are_scoped_to_their_tenant() -> None:
    promoter, store = service()
    promoter.promote(
        tenant_slug=SLUG,
        lead_id=LEAD,
        expected_tier=Tier.WARM,
        promoted_by="icp_owner",
        note=RATIONALE,
        now=NOW,
    )

    assert store.promoted_lead_ids(tenant_slug=SLUG, lead_ids=[LEAD]) == frozenset({LEAD})
    assert store.promoted_lead_ids(tenant_slug="other", lead_ids=[LEAD]) == frozenset()


def test_a_blank_labeler_is_refused() -> None:
    promoter, _ = service()

    with pytest.raises(GoldenPromotionError, match="labeler"):
        promoter.promote(
            tenant_slug=SLUG,
            lead_id=LEAD,
            expected_tier=Tier.WARM,
            promoted_by="   ",
            note=RATIONALE,
            now=NOW,
        )


def test_a_rationale_the_golden_set_would_refuse_is_refused_here_first() -> None:
    """By the time the line is appended, the operator has already been told it worked."""
    promoter, store = service()

    with pytest.raises(GoldenPromotionError, match="at least"):
        promoter.promote(
            tenant_slug=SLUG,
            lead_id=LEAD,
            expected_tier=Tier.WARM,
            promoted_by="icp_owner",
            note="looks warm",
            now=NOW,
        )

    assert store.list_promotions(tenant_slug=SLUG) == []


# ------------------------------------------------------------------------------ export


def test_the_export_renders_one_line_per_promotion() -> None:
    promotion, _, store = promote()

    exported = GoldenPromotionService(store=store).export_jsonl(
        promotions=[promotion], payloads={LEAD: A_REAL_PAYLOAD}
    )

    assert exported.endswith("\n")
    assert len(exported.splitlines()) == 1
    assert json.loads(exported)["case_id"] == promotion.case_id


def test_the_export_is_byte_identical_between_runs() -> None:
    promotion, _, store = promote()
    promoter = GoldenPromotionService(store=store)

    first = promoter.export_jsonl(promotions=[promotion], payloads={LEAD: A_REAL_PAYLOAD})
    second = promoter.export_jsonl(promotions=[promotion], payloads={LEAD: A_REAL_PAYLOAD})

    assert first == second


def test_a_promotion_whose_lead_has_been_purged_is_skipped_not_half_rendered() -> None:
    """#37's retention job removes the payload; a case with no lead is not a case."""
    promotion, _, store = promote()

    exported = GoldenPromotionService(store=store).export_jsonl(promotions=[promotion], payloads={})

    assert exported == ""
