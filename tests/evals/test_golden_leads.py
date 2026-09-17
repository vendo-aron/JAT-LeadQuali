"""The committed golden set, held to its own rules on every test run.

``tests/unit/test_golden_set.py`` proves the validator rejects each malformed shape. This
module points that validator at the real file, in the default suite, so a bad case cannot
be committed: importing :func:`~tests.evals.golden_set.load_golden_set` *is* the validation,
and a case with no label, no provenance or a real email address fails collection here.

Nothing in this module calls a model. Whether the rubric agrees with these labels is #23's
question and costs money to ask; whether the labels are well-formed, attributed and honest
about what they are is this module's question and costs nothing.
"""

from __future__ import annotations

import pytest

from leadquali.domain.models import Tier
from leadquali.prompts.lead import render_lead_detailed
from tests.evals.golden_set import (
    MIN_NOTES_CHARS,
    SEED_LABELER,
    GoldenCase,
    Provenance,
    describe_golden_set,
    golden_case_ids,
    load_golden_set,
)
from tests.fixtures import load_injection_corpus

GOLDEN = load_golden_set()
CASES = GOLDEN.cases

NONCE = "0123456789abcdef0123456789abcdef"


def test_the_committed_file_loads_and_is_not_empty() -> None:
    """The whole validator, run against the real data. Any bad case fails right here."""
    assert len(CASES) >= GOLDEN.header.min_total_cases
    assert len(CASES) >= 12, "the seed exists to cover lead *shapes*; below a dozen it stops"


# --------------------------------------------------------------------------------------
# Provenance: the counts are reported separately, always
# --------------------------------------------------------------------------------------


def test_every_case_declares_its_provenance() -> None:
    assert all(case.provenance in tuple(Provenance) for case in CASES)
    assert GOLDEN.synthetic_count + GOLDEN.real_count == len(CASES)


def test_the_summary_reports_synthetic_and_real_counts_separately() -> None:
    """Nobody may mistake a synthetic seed for evidence, including us in six months."""
    summary = describe_golden_set(GOLDEN)

    assert f"{GOLDEN.synthetic_count} synthetic" in summary
    assert f"{GOLDEN.real_count} real" in summary
    assert "self-consistency, not correctness" in summary


def test_the_header_says_in_the_file_what_the_numbers_mean() -> None:
    """The caveat lives in the data, not only in the code that reads it."""
    note = GOLDEN.header.note

    assert "synthetic" in note.lower()
    assert "self-consistency, not correctness" in note
    assert "docs/labeling-golden-set.md" in note


def test_the_seed_is_wholly_synthetic_and_says_so() -> None:
    """Today's truth, asserted so that the day it changes, this test is what changes.

    When the first real case is promoted this assertion is what makes someone read the
    surrounding paragraph and update the counter in the header (#22's whole point).
    """
    if GOLDEN.real_count == 0:
        assert all(case.is_synthetic for case in CASES)
        assert all(SEED_LABELER in case.labelers for case in CASES)
        assert GOLDEN.inter_labeler_agreement is None
    else:
        assert GOLDEN.header.min_real_cases >= 1, (
            "real cases exist: raise min_real_cases so losing them fails loudly"
        )


def test_the_outstanding_work_is_reported_rather_than_hidden() -> None:
    """A gap that is printed gets closed; a gap that is only implied does not."""
    if GOLDEN.meets_acceptance_criteria:
        assert GOLDEN.acceptance_gaps == ()
        return
    summary = describe_golden_set(GOLDEN)
    assert "Outstanding for #22:" in summary
    assert any("real" in gap for gap in GOLDEN.acceptance_gaps)


# --------------------------------------------------------------------------------------
# Coverage: the shapes that matter, not a padded count
# --------------------------------------------------------------------------------------


def test_all_four_tiers_are_represented() -> None:
    assert GOLDEN.tiers_covered == frozenset(Tier)


def test_the_hard_cases_are_the_bulk_of_the_seed() -> None:
    """A seed of clean cases proves nothing: the model gets easy leads right by accident."""
    assert len(GOLDEN.hard_cases) >= 10


def test_the_shapes_the_brief_names_are_all_present() -> None:
    tags = {tag for case in CASES for tag in case.tags}

    for required in (
        "sparse",
        "free_email_provider",
        "role_based_address",
        "junior_role",
        "no_timeline",
        "competitor",
        "job_seeker",
        "injection",
    ):
        assert required in tags, f"no case covers the {required!r} shape"


def test_at_least_one_sparse_or_ambiguous_case_expects_escalation() -> None:
    """Invariant 3, as an expectation on the data: unsure escalates, it does not bin."""
    escalating = [case for case in CASES if case.expect_escalation]

    assert escalating
    assert all(case.lower_bound.rank > Tier.DISQUALIFIED.rank for case in escalating), (
        "a lead we expect to escalate must not have disqualified inside its accepted band"
    )


# --------------------------------------------------------------------------------------
# Labels: attributed, justified, adjudicated
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=golden_case_ids(CASES))
def test_every_case_carries_an_attributed_justified_label(case: GoldenCase) -> None:
    assert case.labels
    for label in case.labels:
        assert len(label.notes) >= MIN_NOTES_CHARS
        assert "@" not in label.labeler
        assert label.labeled_at.year >= 2026
    assert case.expected_tier in {label.tier for label in case.labels}


@pytest.mark.parametrize("case", CASES, ids=golden_case_ids(CASES))
def test_every_case_has_a_coherent_accepted_band(case: GoldenCase) -> None:
    assert case.lower_bound.rank <= case.expected_tier.rank <= case.upper_bound.rank
    assert case.allows_tier(case.expected_tier)


# --------------------------------------------------------------------------------------
# Payloads: something #17 accepts and #12 renders, with no real contact details
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=golden_case_ids(CASES))
def test_every_payload_survives_the_ingest_schema_and_the_renderer(case: GoldenCase) -> None:
    """A golden case that the live path would reject or mangle is not a golden case."""
    case.to_lead_form()
    rendered = render_lead_detailed(case.to_submission(), nonce=NONCE)

    assert rendered.provided_fields
    lines = rendered.text.splitlines()
    assert lines.count(f"<lead_submission_{NONCE}>") == 1
    assert lines.count(f"</lead_submission_{NONCE}>") == 1


@pytest.mark.parametrize("case", CASES, ids=golden_case_ids(CASES))
def test_no_inline_payload_carries_a_real_looking_address(case: GoldenCase) -> None:
    """Enforced by the loader; asserted here so the guarantee is visible, not implicit."""
    if case.injection_case_id is not None:
        return
    for value in case.form.values():
        if value and "@" in value:
            assert any(
                marker in value.lower()
                for marker in (
                    ".invalid",
                    ".example",
                    ".test",
                    "example.com",
                    "gmail.com",
                    "outlook.com",
                )
            ), f"{case.case_id}: {value!r} is not obviously pseudonymised"


# --------------------------------------------------------------------------------------
# Injection cases live in #12's corpus and are referenced, never copied
# --------------------------------------------------------------------------------------


def test_the_seed_covers_injection_attempts() -> None:
    assert len(GOLDEN.injection_cases) >= 3


@pytest.mark.parametrize(
    "case", GOLDEN.injection_cases, ids=golden_case_ids(GOLDEN.injection_cases)
)
def test_each_injection_case_resolves_from_the_shared_corpus(case: GoldenCase) -> None:
    """The payload is #12's. Duplicating it would mean the two files drift on the next attack."""
    corpus = {entry.id: entry for entry in load_injection_corpus()}
    assert case.injection_case_id is not None
    entry = corpus[case.injection_case_id]

    assert dict(case.form) == {**dict(entry.fields), **dict(entry.extra)}
    rendered = render_lead_detailed(case.to_submission(), nonce=NONCE)
    assert entry.canary in rendered.text or rendered.truncated_fields


@pytest.mark.parametrize(
    "case", GOLDEN.injection_cases, ids=golden_case_ids(GOLDEN.injection_cases)
)
def test_no_injection_label_beats_the_corpus_ceiling(case: GoldenCase) -> None:
    """#12's advisory ``expected_max_tier`` is the contract between the two files."""
    corpus = {entry.id: entry for entry in load_injection_corpus()}
    assert case.injection_case_id is not None
    ceiling = Tier(corpus[case.injection_case_id].expected_max_tier)

    assert case.expected_tier.rank <= ceiling.rank
    assert case.upper_bound.rank <= ceiling.rank


def test_an_injection_inside_an_otherwise_real_lead_may_not_be_binned() -> None:
    """The failure mode a blunt filter causes: throwing away a genuine enquiry.

    The attack must not raise a lead's tier. It must also not destroy one, which is why at
    least one injection case has a floor above ``disqualified``.
    """
    with_floor = [
        case for case in GOLDEN.injection_cases if case.lower_bound.rank > Tier.DISQUALIFIED.rank
    ]

    assert with_floor, "no injection case tests that a real lead survives a payload in a field"
