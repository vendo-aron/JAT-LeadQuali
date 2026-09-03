"""The routing email: what a rep must be able to see, and what must never reach them.

Never an assertion on model prose — the reasoning is passed through, so what is asserted is
that it *is* passed through, not what it says. Everything else here is about the five
seconds a rep spends deciding whether to call, and about the fact that both the lead's words
and the model's words are untrusted text going into HTML.
"""

from __future__ import annotations

import re

import pytest

from leadquali.app.feedback import Verdict
from leadquali.app.lead_email import (
    MAX_FIELD_CHARS,
    FeedbackLink,
    render_routing_email,
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
from leadquali.domain.routing import LOW_CONFIDENCE_NOTE, SYSTEM_FAILURE_BANNER, system_failure
from leadquali.prompts.lead import LeadSubmission

LINKS = (
    FeedbackLink(verdict=Verdict.GOOD, url="https://leads.example.com/feedback/fb1.good.mac"),
    FeedbackLink(verdict=Verdict.BAD, url="https://leads.example.com/feedback/fb1.bad.mac"),
)


def submission(**overrides: object) -> LeadSubmission:
    fields: dict[str, object] = {
        "full_name": "Dana Whitfield",
        "email": "dana@northwind.example",
        "company": "Northwind Logistics",
        "role": "VP Operations",
        "phone": "+44 20 7946 0000",
        "website": "https://northwind.example",
        "message": "We are replacing our routing spreadsheet before the Q4 peak.",
    }
    fields.update(overrides)
    return LeadSubmission(**fields)  # type: ignore[arg-type]


def assessment(**overrides: object) -> LeadAssessment:
    fields: dict[str, object] = {
        "dimension_scores": DimensionScores(
            icp_fit=27, intent=22, authority=12, urgency=13, budget_signal=11
        ),
        "extracted": ExtractedFacts(
            company_name="Northwind Logistics",
            industry="freight",
            company_size_estimate="200-500",
            role_seniority="vp",
            stated_use_case="replace a routing spreadsheet",
            stated_timeline="before Q4",
        ),
        "reasoning": "States a deadline and owns the budget line.",
        "confidence": 0.86,
        "missing_information": ["current tooling spend"],
        "suggested_first_question": "What breaks first when the spreadsheet is wrong?",
        "spam_or_test_submission": False,
    }
    fields.update(overrides)
    return LeadAssessment(**fields)  # type: ignore[arg-type]


HOT = RoutingDecision(
    tier=Tier.HOT, action=Action.EMAIL_SALES, total_score=87.0, note="scored 87.00/100 — hot"
)


# --------------------------------------------------------------------- the hot-lead email


def test_a_hot_lead_renders_both_parts_with_everything_a_rep_needs() -> None:
    email = render_routing_email(
        submission=submission(),
        decision=HOT,
        assessment=assessment(),
        links=LINKS,
        lead_reference="8f14e45f",
    )
    for part in (email.text_body, email.html_body):
        assert "HOT" in part or "hot" in part
        assert "87" in part
        assert "Northwind Logistics" in part
        assert "Dana Whitfield" in part
        assert "dana@northwind.example" in part
        assert "VP Operations" in part
        # the model's reasoning, its suggested opener, and what it could not find
        assert "States a deadline and owns the budget line." in part
        assert "What breaks first when the spreadsheet is wrong?" in part
        assert "current tooling spend" in part
        # every dimension, with its own maximum
        for label in ("icp", "intent", "authority", "urgency", "budget"):
            assert label in part
        assert "8f14e45f" in part


def test_the_subject_line_triages_the_inbox_on_its_own() -> None:
    email = render_routing_email(submission=submission(), decision=HOT, assessment=assessment())
    assert email.subject == "[HOT 87] Northwind Logistics"


def test_the_subject_falls_back_to_the_person_then_to_a_placeholder() -> None:
    named = render_routing_email(
        submission=submission(company=None), decision=HOT, assessment=assessment()
    )
    assert "Northwind Logistics" in named.subject, "the model's extracted company still names it"

    anonymous = render_routing_email(
        submission=LeadSubmission(full_name="Dana Whitfield"),
        decision=HOT,
        assessment=assessment(
            extracted=ExtractedFacts(**dict.fromkeys(ExtractedFacts.model_fields))
        ),
    )
    assert anonymous.subject == "[HOT 87] Dana Whitfield"

    nameless = render_routing_email(
        submission=LeadSubmission(),
        decision=HOT,
        assessment=assessment(
            extracted=ExtractedFacts(**dict.fromkeys(ExtractedFacts.model_fields))
        ),
    )
    assert nameless.subject == "[HOT 87] unknown company"


def test_the_scores_are_shown_against_their_own_maxima() -> None:
    email = render_routing_email(submission=submission(), decision=HOT, assessment=assessment())
    assert "icp fit: 27/30" in email.text_body
    assert "authority: 12/15" in email.text_body
    assert "27/30" in email.html_body


def test_a_missing_field_reads_as_missing_rather_than_blank() -> None:
    email = render_routing_email(
        submission=submission(phone=None, website=None), decision=HOT, assessment=assessment()
    )
    assert "phone: —" in email.text_body


# ------------------------------------------------------------------- the failure banner


def test_the_system_failure_email_carries_the_banner_and_no_score() -> None:
    """``assessment=None`` is the system-failure path, and it must not look like a verdict."""
    decision = system_failure(EscalationReason.API_ERROR, detail="connection reset")
    email = render_routing_email(
        submission=submission(), decision=decision, assessment=None, links=LINKS
    )

    assert "NEEDS REVIEW" in email.subject
    assert "Northwind Logistics" in email.subject
    for part in (email.text_body, email.html_body):
        assert SYSTEM_FAILURE_BANNER in part.lower()
        assert "could not assess" in part.lower()
        # the lead is still fully readable — that is the point of sending it
        assert "dana@northwind.example" in part
        assert "routing spreadsheet" in part
    assert "0/100" not in email.text_body, "an unscored lead must not be shown as scoring zero"
    assert "[HOT" not in email.subject


def test_the_low_confidence_gate_shows_its_note_verbatim() -> None:
    decision = RoutingDecision(
        tier=Tier.WARM,
        action=Action.EMAIL_SALES,
        total_score=64.0,
        note=LOW_CONFIDENCE_NOTE,
        escalation_reason=EscalationReason.LOW_CONFIDENCE,
    )
    email = render_routing_email(
        submission=submission(), decision=decision, assessment=assessment(confidence=0.31)
    )
    for part in (email.text_body, email.html_body):
        assert LOW_CONFIDENCE_NOTE in part
    assert "human review" in email.subject


def test_an_unscored_lead_says_so_where_the_score_would_be() -> None:
    decision = system_failure(EscalationReason.TIMEOUT)
    email = render_routing_email(submission=submission(), decision=decision, assessment=None)
    assert "NOT been scored" in email.text_body
    assert "Not scored" in email.html_body


# ---------------------------------------------------------------------- untrusted text


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        '" onmouseover="alert(1)',
        "<img src=x onerror=alert(1)>",
    ],
)
def test_the_leads_own_words_cannot_become_markup(hostile: str) -> None:
    email = render_routing_email(
        submission=submission(message=hostile, full_name=hostile),
        decision=HOT,
        assessment=assessment(),
    )
    # The payload must survive only as text: never as the bytes it was submitted as, which
    # is what would let it close an attribute or open a tag.
    assert hostile not in email.html_body
    assert "<script>" not in email.html_body
    assert ("&lt;" in email.html_body) or ("&quot;" in email.html_body)
    assert hostile in email.text_body, "the plain part is text, and a rep should see it"


def test_model_output_is_escaped_too() -> None:
    email = render_routing_email(
        submission=submission(),
        decision=HOT,
        assessment=assessment(reasoning="<b>very</b> promising"),
    )
    assert "<b>very</b>" not in email.html_body
    assert "&lt;b&gt;very&lt;/b&gt;" in email.html_body


def test_a_hostile_website_field_never_becomes_an_href() -> None:
    email = render_routing_email(
        submission=submission(website="javascript:alert(1)"), decision=HOT, assessment=assessment()
    )
    assert 'href="javascript:' not in email.html_body


def test_an_enormous_field_is_truncated_rather_than_sent_whole() -> None:
    email = render_routing_email(
        submission=submission(message="x" * (MAX_FIELD_CHARS * 3)),
        decision=HOT,
        assessment=assessment(),
    )
    assert len(email.text_body) < MAX_FIELD_CHARS * 2
    assert "…" in email.text_body


def test_control_characters_are_stripped_from_the_rendered_fields() -> None:
    email = render_routing_email(
        submission=submission(full_name="Dana\x00\x07 Whitfield"),
        decision=HOT,
        assessment=assessment(),
    )
    assert "\x00" not in email.text_body
    assert "\x07" not in email.html_body


# ------------------------------------------------------------------- the feedback links


def test_both_feedback_links_appear_in_both_parts() -> None:
    email = render_routing_email(
        submission=submission(), decision=HOT, assessment=assessment(), links=LINKS
    )
    for link in LINKS:
        assert link.url in email.text_body
        assert f'href="{link.url}"' in email.html_body
    assert "Was this a good lead?" in email.html_body
    assert "Good lead" in email.html_body
    assert "Bad lead" in email.html_body


def test_no_links_means_no_dead_feedback_section() -> None:
    email = render_routing_email(submission=submission(), decision=HOT, assessment=assessment())
    assert "Was this a good lead?" not in email.html_body
    assert "good lead" not in email.text_body.lower()


# ----------------------------------------------------------- rendering in real clients


def test_the_html_is_table_based_and_inline_styled_for_outlook() -> None:
    """Word's rendering engine: no <style>, no flexbox, no floats, no external assets."""
    html_body = render_routing_email(
        submission=submission(), decision=HOT, assessment=assessment(), links=LINKS
    ).html_body
    assert "<table" in html_body
    assert "<style" not in html_body
    assert "display:flex" not in html_body
    assert "<link" not in html_body
    assert "<img" not in html_body, "no remote images: they are blocked and they track people"
    assert re.search(r"max-width:\s*600px", html_body), "must reflow on a phone"


def test_the_html_declares_a_doctype_and_a_charset() -> None:
    html_body = render_routing_email(
        submission=submission(message="Prêt à démarrer — 5 000 €"),
        decision=HOT,
        assessment=assessment(),
    ).html_body
    assert html_body.startswith("<!DOCTYPE html")
    assert "charset=UTF-8" in html_body
    assert "Prêt à démarrer" in html_body


def test_the_text_part_is_readable_on_its_own() -> None:
    """The alternative part is what a preview pane and a screen reader get."""
    text = render_routing_email(
        submission=submission(), decision=HOT, assessment=assessment(), links=LINKS
    ).text_body
    assert "<" not in text and ">" not in text
    assert text.endswith("\n")
    assert "CONTACT" in text
    assert "OPEN WITH" in text
