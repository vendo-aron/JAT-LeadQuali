"""The golden-set loader and validator, held to the standard the data has to meet.

These tests exist because the golden set is the only thing that will ever tell us whether
the rubric works, and a corrupt or unlabelled row in it is worse than a missing one: a
missing row lowers the sample size, a silently-wrong row moves the number the team steers
by. So every malformed shape below is an *error*, not a warning, and the loader refuses to
produce a partial corpus.

``tests/evals/test_golden_leads.py`` runs the same validator against the committed file.
Here we build minimal documents in a temp directory so each failure mode can be provoked
on its own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from leadquali.domain.models import Tier
from tests.evals.golden_set import (
    GOLDEN_LEADS_PATH,
    HEADER_KEY,
    SCHEMA_VERSION,
    SEED_LABELER,
    GoldenSet,
    GoldenSetError,
    Provenance,
    describe_golden_set,
    load_golden_set,
    parse_golden_set,
)
from tests.fixtures import load_injection_corpus

VALID_HEADER: dict[str, Any] = {
    HEADER_KEY: {
        "schema_version": SCHEMA_VERSION,
        "note": "Seed cases are synthetic; eval numbers measure self-consistency.",
        "min_total_cases": 1,
        "min_real_cases": 0,
        "acceptance_target_total_cases": 50,
        "acceptance_target_real_cases": 50,
    }
}


def _label(**overrides: Any) -> dict[str, Any]:
    label: dict[str, Any] = {
        "labeler": "icp_owner",
        "tier": "warm",
        "labeled_at": "2026-09-03",
        "notes": "Clear ICP fit but no stated timeline, so warm rather than hot.",
    }
    label.update(overrides)
    return label


def _case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case_id": "warm_baseline",
        "provenance": "synthetic",
        "expected_tier": "warm",
        "form": {
            "full_name": "Dana Prior",
            "email": "dana.prior@example.com",
            "company": "Prior Freight",
            "role": "Head of Operations",
            "message": "We have outgrown our spreadsheet and want to talk this quarter.",
        },
        "labels": [_label()],
    }
    case.update(overrides)
    if case.get("form", {}) is None:
        # ``form=None`` in a builder call means "this case has no inline payload".
        del case["form"]
    return case


def _write(tmp_path: Path, *records: Mapping[str, Any], name: str = "golden.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _load(tmp_path: Path, *records: Mapping[str, Any]) -> GoldenSet:
    return load_golden_set(_write(tmp_path, *records))


def _refuses(tmp_path: Path, *records: Mapping[str, Any]) -> str:
    with pytest.raises(GoldenSetError) as caught:
        load_golden_set(_write(tmp_path, *records))
    return str(caught.value)


# --------------------------------------------------------------------------------------
# The happy path, and what a loaded case exposes
# --------------------------------------------------------------------------------------


def test_a_minimal_valid_document_loads(tmp_path: Path) -> None:
    golden = _load(tmp_path, VALID_HEADER, _case())

    assert len(golden.cases) == 1
    case = golden.cases[0]
    assert case.case_id == "warm_baseline"
    assert case.provenance is Provenance.SYNTHETIC
    assert case.is_synthetic is True
    assert case.expected_tier is Tier.WARM
    assert case.labels[0].tier is Tier.WARM
    assert case.labels[0].labeled_at == date(2026, 9, 3)
    assert case.labels[0].labeler == "icp_owner"


def test_a_case_round_trips_through_the_ingest_schema_and_the_renderer(tmp_path: Path) -> None:
    """The payload has to be something #17 accepts and #12 can render, or it is fiction."""
    case = _load(tmp_path, VALID_HEADER, _case()).cases[0]

    form = case.to_lead_form()
    assert form.email == "dana.prior@example.com"

    submission = case.to_submission()
    assert submission.company == "Prior Freight"
    assert submission.message is not None
    assert "spreadsheet" in submission.message


def test_optional_expectations_are_parsed_when_present(tmp_path: Path) -> None:
    case = _load(
        tmp_path,
        VALID_HEADER,
        _case(
            expected_min_tier="cold",
            expected_max_tier="hot",
            expect_escalation=True,
            hard_case=True,
            expected_dimension_ranges={"icp_fit": [18, 30], "urgency": [0, 6]},
            expected_extracted={"role_seniority": "manager", "stated_timeline": None},
            tags=["sparse", "no_budget"],
        ),
    ).cases[0]

    assert case.expected_min_tier is Tier.COLD
    assert case.expected_max_tier is Tier.HOT
    assert case.expect_escalation is True
    assert case.hard_case is True
    assert case.expected_dimension_ranges["icp_fit"] == (18, 30)
    assert case.expected_extracted == {"role_seniority": "manager", "stated_timeline": None}
    assert case.tags == ("sparse", "no_budget")


def test_allows_tier_reads_the_recorded_bounds(tmp_path: Path) -> None:
    """What #23 asks a case: is this tier acceptable, not merely was it exact?"""
    case = _load(
        tmp_path, VALID_HEADER, _case(expected_min_tier="cold", expected_max_tier="hot")
    ).cases[0]

    assert case.allows_tier(Tier.COLD) is True
    assert case.allows_tier(Tier.HOT) is True
    assert case.allows_tier(Tier.DISQUALIFIED) is False


def test_bounds_default_to_the_expected_tier_alone(tmp_path: Path) -> None:
    case = _load(tmp_path, VALID_HEADER, _case()).cases[0]

    assert case.allows_tier(Tier.WARM) is True
    assert case.allows_tier(Tier.HOT) is False
    assert case.allows_tier(Tier.COLD) is False


def test_blank_lines_are_ignored_but_the_header_must_come_first(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        json.dumps(VALID_HEADER) + "\n\n" + json.dumps(_case()) + "\n\n",
        encoding="utf-8",
    )
    assert len(load_golden_set(path).cases) == 1


# --------------------------------------------------------------------------------------
# Provenance — the field that stops synthetic cases being mistaken for evidence
# --------------------------------------------------------------------------------------


def test_provenance_is_required(tmp_path: Path) -> None:
    case = _case()
    del case["provenance"]
    assert "provenance" in _refuses(tmp_path, VALID_HEADER, case)


def test_provenance_must_be_synthetic_or_real(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(provenance="probably_real"))
    assert "provenance" in message
    assert "probably_real" in message


def test_a_real_case_must_say_where_it_was_promoted_from(tmp_path: Path) -> None:
    """A 'real' case with no origin is an unfalsifiable claim about the data."""
    message = _refuses(tmp_path, VALID_HEADER, _case(provenance="real"))
    assert "promoted_from" in message


def test_a_real_case_with_an_origin_is_accepted_and_counted(tmp_path: Path) -> None:
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(provenance="real", promoted_from="feedback:7f0d9c4e"),
        _case(case_id="synth_one"),
    )

    assert golden.real_count == 1
    assert golden.synthetic_count == 1
    assert golden.cases[0].promoted_from == "feedback:7f0d9c4e"


def test_a_synthetic_case_may_not_claim_an_origin(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(promoted_from="feedback:7f0d9c4e"))
    assert "promoted_from" in message
    assert "synthetic" in message


# --------------------------------------------------------------------------------------
# Labels — an unlabelled case is not a golden case
# --------------------------------------------------------------------------------------


def test_a_case_with_no_labels_is_refused(tmp_path: Path) -> None:
    assert "labels" in _refuses(tmp_path, VALID_HEADER, _case(labels=[]))


def test_a_label_needs_a_labeler_a_tier_a_date_and_notes(tmp_path: Path) -> None:
    for missing in ("labeler", "tier", "labeled_at", "notes"):
        label = _label()
        del label[missing]
        message = _refuses(tmp_path, VALID_HEADER, _case(labels=[label]))
        assert missing in message


def test_notes_must_actually_say_something(tmp_path: Path) -> None:
    """'looks warm' is not a justification anyone can review a year from now."""
    assert "notes" in _refuses(tmp_path, VALID_HEADER, _case(labels=[_label(notes="warm")]))


def test_a_labeler_id_may_not_be_an_email_address(tmp_path: Path) -> None:
    """Invariant 5, and the same rule #15 puts on ``feedback.rater``: opaque ids only."""
    message = _refuses(
        tmp_path, VALID_HEADER, _case(labels=[_label(labeler="aron@vendoworks.com")])
    )
    assert "labeler" in message


def test_a_label_date_must_be_an_iso_date(tmp_path: Path) -> None:
    assert "labeled_at" in _refuses(
        tmp_path, VALID_HEADER, _case(labels=[_label(labeled_at="03/09/2026")])
    )


def test_the_same_labeler_may_not_label_one_case_twice(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(labels=[_label(), _label()]))
    assert "icp_owner" in message


def test_the_expected_tier_must_be_a_tier_a_human_actually_chose(tmp_path: Path) -> None:
    """Adjudication picks between the labels; it does not invent a third answer."""
    message = _refuses(tmp_path, VALID_HEADER, _case(expected_tier="hot"))
    assert "expected_tier" in message
    assert "hot" in message


def test_adjudicating_a_disagreement_to_one_of_the_two_labels_is_allowed(tmp_path: Path) -> None:
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(
            expected_tier="cold",
            labels=[
                _label(),
                _label(
                    labeler="eng_seed",
                    tier="cold",
                    notes="No budget signal at all; I read this as cold, not warm.",
                ),
            ],
        ),
    )

    case = golden.cases[0]
    assert case.expected_tier is Tier.COLD
    assert case.labelers == ("icp_owner", "eng_seed")
    assert case.labelers_agree is False


# --------------------------------------------------------------------------------------
# The payload — exactly one source, and one #17 would accept
# --------------------------------------------------------------------------------------


def test_a_case_needs_a_payload(tmp_path: Path) -> None:
    case = _case()
    del case["form"]
    message = _refuses(tmp_path, VALID_HEADER, case)
    assert "form" in message and "injection_case_id" in message


def test_a_case_may_not_have_two_payloads(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(injection_case_id="direct_override"))
    assert "form" in message and "injection_case_id" in message


def test_a_payload_the_ingest_endpoint_would_reject_is_refused(tmp_path: Path) -> None:
    """#17 forbids nested structure in a form field; a golden case may not contain it."""
    message = _refuses(
        tmp_path, VALID_HEADER, _case(form={"email": "x@example.com", "message": ["a", "b"]})
    )
    assert "form" in message


def test_an_empty_payload_is_refused(tmp_path: Path) -> None:
    assert "form" in _refuses(tmp_path, VALID_HEADER, _case(form={}))


def test_unknown_top_level_keys_are_refused(tmp_path: Path) -> None:
    """A typo in a key name would otherwise silently drop the expectation it carried."""
    message = _refuses(tmp_path, VALID_HEADER, _case(expected_teir="warm"))
    assert "expected_teir" in message


def test_duplicate_case_ids_are_refused(tmp_path: Path) -> None:
    assert "warm_baseline" in _refuses(tmp_path, VALID_HEADER, _case(), _case())


def test_a_case_id_must_be_a_stable_slug(tmp_path: Path) -> None:
    assert "case_id" in _refuses(tmp_path, VALID_HEADER, _case(case_id="Warm Baseline!"))


# --------------------------------------------------------------------------------------
# PII — this file lives in the repository
# --------------------------------------------------------------------------------------


def test_an_email_at_a_real_domain_is_refused(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path, VALID_HEADER, _case(form={"email": "priya@northwind-logistics.com"})
    )
    assert "northwind-logistics.com" in message


def test_an_email_pasted_into_the_message_body_is_refused(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(
            form={
                "email": "dana@example.com",
                "message": "Copying my colleague sam.reeve@acmemanufacturing.co.uk on this.",
            }
        ),
    )
    assert "acmemanufacturing.co.uk" in message


def test_a_website_at_a_real_domain_is_refused(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(form={"email": "dana@example.com", "website": "https://northwind-logistics.com"}),
    )
    assert "northwind-logistics.com" in message


def test_reserved_domains_and_the_free_provider_allowlist_are_accepted(tmp_path: Path) -> None:
    """A free-provider address is a lead shape we must be able to test, so it is allowed."""
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(form={"email": "t.arslan.demo@gmail.com", "message": "Keen to buy this quarter."}),
        _case(case_id="reserved_tld", form={"email": "ops@northwind.example", "message": "Hello."}),
        _case(
            case_id="reserved_invalid",
            form={"email": "info@prior-freight.invalid", "message": "Please send pricing."},
        ),
    )
    assert len(golden.cases) == 3


# --------------------------------------------------------------------------------------
# Optional expectations, checked against the domain's own bounds
# --------------------------------------------------------------------------------------


def test_a_dimension_range_must_name_a_real_dimension(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(expected_dimension_ranges={"vibes": [1, 2]}))
    assert "vibes" in message


def test_a_dimension_range_must_fit_inside_that_dimensions_bounds(tmp_path: Path) -> None:
    """``authority`` tops out at 15 in #7's schema; a range to 20 is unreachable."""
    message = _refuses(
        tmp_path, VALID_HEADER, _case(expected_dimension_ranges={"authority": [10, 20]})
    )
    assert "authority" in message


def test_a_dimension_range_must_not_be_inverted(tmp_path: Path) -> None:
    message = _refuses(tmp_path, VALID_HEADER, _case(expected_dimension_ranges={"intent": [20, 5]}))
    assert "intent" in message


def test_an_expected_extracted_field_must_be_a_real_field(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path, VALID_HEADER, _case(expected_extracted={"favourite_colour": "blue"})
    )
    assert "favourite_colour" in message


def test_the_expected_tier_must_sit_inside_the_recorded_bounds(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path, VALID_HEADER, _case(expected_tier="warm", expected_max_tier="cold")
    )
    assert "expected_max_tier" in message


def test_inverted_bounds_are_refused(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(expected_min_tier="hot", expected_max_tier="cold"),
    )
    assert "expected_min_tier" in message


# --------------------------------------------------------------------------------------
# Injection cases resolve from #12's corpus rather than being duplicated
# --------------------------------------------------------------------------------------


def test_an_injection_case_takes_its_payload_from_the_shared_corpus(tmp_path: Path) -> None:
    corpus = {case.id: case for case in load_injection_corpus()}
    expected = corpus["direct_override"]

    case = _load(
        tmp_path,
        VALID_HEADER,
        _case(
            case_id="injection_direct_override",
            form=None,
            injection_case_id="direct_override",
            expected_tier="cold",
            labels=[
                _label(
                    tier="cold",
                    notes="An instruction-override attempt carries no buying signal at all.",
                )
            ],
        ),
    ).cases[0]

    assert case.injection_case_id == "direct_override"
    assert dict(case.form) == dict(expected.fields)
    assert case.to_submission().message == expected.fields["message"]
    assert expected.canary in (case.to_submission().message or "")


def test_an_unknown_injection_case_id_is_refused(tmp_path: Path) -> None:
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(form=None, injection_case_id="no_such_attack"),
    )
    assert "no_such_attack" in message


def test_a_label_may_not_exceed_the_corpus_advisory_ceiling(tmp_path: Path) -> None:
    """#12 says ``direct_override`` must never beat ``cold``. The two files stay honest."""
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(
            form=None,
            injection_case_id="direct_override",
            expected_tier="hot",
            labels=[_label(tier="hot", notes="Scoring this hot would mean the attack worked.")],
        ),
    )
    assert "direct_override" in message
    assert "expected_max_tier" in message


def test_a_label_at_or_below_the_corpus_ceiling_is_accepted(tmp_path: Path) -> None:
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(
            form=None,
            injection_case_id="injection_in_company_name",
            expected_tier="warm",
            labels=[
                _label(
                    tier="warm",
                    notes="A real enquiry whose company field carries a payload; still a lead.",
                )
            ],
        ),
    )
    assert golden.cases[0].expected_max_tier is Tier.WARM


# --------------------------------------------------------------------------------------
# The header: the file states its own thresholds, and the loader enforces them
# --------------------------------------------------------------------------------------


def test_a_document_with_no_header_is_refused(tmp_path: Path) -> None:
    assert HEADER_KEY in _refuses(tmp_path, _case())


def test_the_header_must_be_the_first_record(tmp_path: Path) -> None:
    assert HEADER_KEY in _refuses(tmp_path, _case(), VALID_HEADER)


def test_two_headers_are_refused(tmp_path: Path) -> None:
    assert HEADER_KEY in _refuses(tmp_path, VALID_HEADER, VALID_HEADER, _case())


def test_an_unknown_schema_version_is_refused(tmp_path: Path) -> None:
    header = {HEADER_KEY: {**VALID_HEADER[HEADER_KEY], "schema_version": SCHEMA_VERSION + 1}}
    assert "schema_version" in _refuses(tmp_path, header, _case())


def test_the_header_note_must_carry_the_self_consistency_warning(tmp_path: Path) -> None:
    """The one sentence in this PR that stops a number being believed too early."""
    header = {HEADER_KEY: {**VALID_HEADER[HEADER_KEY], "note": "Some labeled leads."}}
    assert "self-consistency" in _refuses(tmp_path, header, _case())


def test_falling_below_the_declared_total_floor_fails_loudly(tmp_path: Path) -> None:
    header = {HEADER_KEY: {**VALID_HEADER[HEADER_KEY], "min_total_cases": 5}}
    message = _refuses(tmp_path, header, _case())
    assert "min_total_cases" in message


def test_a_synthetic_only_set_fails_once_real_cases_are_expected(tmp_path: Path) -> None:
    """The counter, not a date: raising it is the act that turns the seed into a dataset."""
    header = {HEADER_KEY: {**VALID_HEADER[HEADER_KEY], "min_real_cases": 3}}
    message = _refuses(tmp_path, header, _case())
    assert "min_real_cases" in message
    assert "synthetic" in message


def test_promoting_a_real_case_is_one_appended_line(tmp_path: Path) -> None:
    """Decision record 0001 promises the file is append-only; the loader must not undo that.

    Real cases above the declared floor load without the header being touched, so the
    weekly ritual is ``>> golden_leads.jsonl`` and nothing else. Raising the floor to lock
    them in is then a separate, deliberate commit.
    """
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(provenance="real", promoted_from="feedback:1"),
        _case(case_id="second", provenance="real", promoted_from="feedback:2"),
    )

    assert golden.real_count == 2
    assert golden.header.min_real_cases == 0


def test_the_acceptance_targets_must_not_be_below_the_issues_numbers(tmp_path: Path) -> None:
    header = {HEADER_KEY: {**VALID_HEADER[HEADER_KEY], "acceptance_target_total_cases": 10}}
    assert "acceptance_target_total_cases" in _refuses(tmp_path, header, _case())


# --------------------------------------------------------------------------------------
# Malformed files, as opposed to malformed cases
# --------------------------------------------------------------------------------------


def test_a_line_that_is_not_json_names_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(VALID_HEADER) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(GoldenSetError) as caught:
        load_golden_set(path)
    assert "line 2" in str(caught.value)


def test_a_line_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(VALID_HEADER) + "\n[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(GoldenSetError, match="object"):
        load_golden_set(path)


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(GoldenSetError, match=re.escape("nope.jsonl")):
        load_golden_set(tmp_path / "nope.jsonl")


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(GoldenSetError, match=re.escape(HEADER_KEY)):
        load_golden_set(path)


def test_parse_reports_the_path_it_was_given(tmp_path: Path) -> None:
    with pytest.raises(GoldenSetError, match=re.escape("somewhere.jsonl")):
        parse_golden_set("", path=Path("somewhere.jsonl"))


# --------------------------------------------------------------------------------------
# Reporting: nobody may mistake a synthetic seed for evidence
# --------------------------------------------------------------------------------------


def test_the_summary_reports_synthetic_and_real_counts_separately(tmp_path: Path) -> None:
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(),
        _case(case_id="promoted", provenance="real", promoted_from="feedback:9"),
    )

    summary = describe_golden_set(golden)
    assert "2 cases" in summary
    assert "1 synthetic" in summary
    assert "1 real" in summary
    assert "self-consistency" in summary


def test_a_wholly_synthetic_set_says_so_in_its_summary(tmp_path: Path) -> None:
    summary = describe_golden_set(_load(tmp_path, VALID_HEADER, _case()))

    assert "0 real" in summary
    assert "self-consistency, not correctness" in summary


def test_acceptance_criteria_are_reported_and_not_pretended_to_be_met(tmp_path: Path) -> None:
    golden = _load(tmp_path, VALID_HEADER, _case())

    assert golden.meets_acceptance_criteria is False
    assert golden.acceptance_gaps
    assert any("real" in gap for gap in golden.acceptance_gaps)


def test_inter_labeler_agreement_is_none_until_a_case_has_two_labels(tmp_path: Path) -> None:
    golden = _load(tmp_path, VALID_HEADER, _case())

    assert golden.dual_labeled_cases == ()
    assert golden.inter_labeler_agreement is None


def test_inter_labeler_agreement_is_the_fraction_of_dual_labeled_cases_that_agree(
    tmp_path: Path,
) -> None:
    agreeing = _case(
        case_id="agreed",
        labels=[
            _label(),
            _label(labeler="eng_seed", notes="Same read: good fit, no timeline stated."),
        ],
    )
    disagreeing = _case(
        case_id="disputed",
        expected_tier="cold",
        labels=[
            _label(),
            _label(labeler="eng_seed", tier="cold", notes="I see no intent here at all."),
        ],
    )
    golden = _load(tmp_path, VALID_HEADER, agreeing, disagreeing, _case(case_id="single"))

    assert len(golden.dual_labeled_cases) == 2
    assert golden.inter_labeler_agreement == pytest.approx(0.5)


def test_the_default_path_points_at_the_file_the_plan_names() -> None:
    assert GOLDEN_LEADS_PATH.name == "golden_leads.jsonl"
    assert GOLDEN_LEADS_PATH.parent.name == "evals"


def test_a_real_case_may_not_reuse_the_seed_authors_handle(tmp_path: Path) -> None:
    """Growing the file is not the same as growing the evidence in it."""
    message = _refuses(
        tmp_path,
        VALID_HEADER,
        _case(
            provenance="real",
            promoted_from="feedback:3a9f",
            labels=[_label(labeler=SEED_LABELER)],
        ),
    )
    assert SEED_LABELER in message
    assert "real" in message


def test_a_real_case_labeled_by_a_human_alongside_the_seed_author_is_accepted(
    tmp_path: Path,
) -> None:
    golden = _load(
        tmp_path,
        VALID_HEADER,
        _case(
            provenance="real",
            promoted_from="feedback:3a9f",
            labels=[
                _label(labeler=SEED_LABELER),
                _label(labeler="icp_owner", notes="Agreed on review of the actual submission."),
            ],
        ),
    )
    assert golden.real_count == 1
