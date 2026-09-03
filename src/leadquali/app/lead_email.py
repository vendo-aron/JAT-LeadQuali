"""The routing email, rendered. No I/O, no SDK, no template engine.

This is what the product looks like to the only person who uses it. A rep opens twenty of
these at 9am, and the email has to answer *should I call this one, and what do I open with*
in about five seconds, without them opening a dashboard they do not have a login for. So
the order of the message is the order a decision gets made:

1. **The banner, if there is one.** "System could not assess" and "low model confidence"
   come first because they change how everything below is read — a score of 0 with no
   banner would look like a judgement about the lead rather than a failure of ours.
2. **Tier and score**, big, at the top.
3. **The one question to open with**, because it is the only line that turns reading into
   calling.
4. **Who they are and how to reach them.**
5. **Why the model said what it said**, then the five dimension scores, the facts it read
   off the submission, and what it could not find. This is the audit trail, and it is what a
   rep uses to disagree — which is the click this whole feature exists to collect.
6. **Good lead / bad lead.**

Both a plain-text and an HTML part are produced, and the plain-text one is not a courtesy:
it is what a screen reader, a locked-down Outlook and a preview pane show, and a message
with no text part is more likely to be scored as spam.

**HTML written for 2005, deliberately.** Outlook renders with Word's engine: no flexbox, no
grid, no ``<style>`` reliably, no external CSS, no web fonts. So the layout is nested
tables with inline styles, the "buttons" are padded table cells with an anchor inside, and
the whole thing is capped at 600px with ``width="100%"`` so it reflows on a phone. Colour
is never the only carrier of meaning — the tier is spelled out in words as well — because a
colourblind rep and a client that strips backgrounds must both still be able to read it.

**Everything interpolated is escaped.** The lead's message is attacker-controlled text from
a public form and the model's reasoning is model output; neither is trusted, both are
escaped with :func:`html.escape` including quotes, and no rendered value is ever placed
anywhere but in text content or in a fully-quoted attribute we constructed.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from leadquali.app.feedback import Verdict
from leadquali.domain.models import (
    MAX_TOTAL_SCORE,
    DimensionScores,
    ExtractedFacts,
    LeadAssessment,
    RoutingDecision,
    Tier,
)
from leadquali.domain.routing import SYSTEM_FAILURE_BANNER
from leadquali.domain.tenant_config import DIMENSION_MAXIMA
from leadquali.prompts.lead import LeadSubmission

#: Width every mail client agrees on. Wider than this and Outlook's preview pane clips.
_BODY_WIDTH_PX: Final[int] = 600

#: Tier colours, and their fallback words. The word is authoritative; the colour is decoration.
_TIER_COLOURS: Final[Mapping[Tier, str]] = {
    Tier.HOT: "#b42318",
    Tier.WARM: "#b54708",
    Tier.COLD: "#175cd3",
    Tier.DISQUALIFIED: "#475467",
}

_GOOD_COLOUR: Final[str] = "#067647"
_BAD_COLOUR: Final[str] = "#b42318"
_BANNER_BACKGROUND: Final[str] = "#fef3f2"
_BANNER_BORDER: Final[str] = "#f97066"
_MUTED: Final[str] = "#475467"
_RULE: Final[str] = "#e4e7ec"
_FONT: Final[str] = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

#: Longest single field rendered into the email. A form field can hold a novel; a routing
#: email that is a novel does not get read, and the full payload is in the database.
MAX_FIELD_CHARS: Final[int] = 2000

#: What a rep sees where a fact is missing. A blank cell reads as a rendering bug.
_ABSENT: Final[str] = "—"


@dataclass(frozen=True, slots=True)
class FeedbackLink:
    """One feedback button: a verdict and the signed URL that records it."""

    verdict: Verdict
    url: str


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """The three parts a multipart/alternative message is built from."""

    subject: str
    text_body: str
    html_body: str


def render_routing_email(
    *,
    submission: LeadSubmission,
    decision: RoutingDecision,
    assessment: LeadAssessment | None,
    links: Sequence[FeedbackLink] = (),
    lead_reference: str = "",
) -> RenderedEmail:
    """Render one routed lead as a subject line and two body parts.

    Args:
        submission: the lead as it was submitted. Untrusted text; escaped on the way in.
        decision: tier, score, note and escalation reason. The note is rendered verbatim —
            #9 owns that wording and an email that paraphrased it would drift from the
            phrase sales has learned to recognise.
        assessment: the model's judgment, or ``None`` for the system-failure path, which is
            not an error: the lead is emailed unqualified, banner first, for a person to
            qualify by hand.
        links: the feedback buttons, already signed and expiring. Empty renders no
            feedback section at all rather than a dead button.
        lead_reference: an opaque id shown in the footer so a rep can quote it in a support
            request. Never the submission's contents.
    """
    company = _clean(submission.company) or _clean(
        assessment.extracted.company_name if assessment else None
    )
    person = _clean(submission.full_name)
    return RenderedEmail(
        subject=_subject(decision=decision, assessment=assessment, company=company, person=person),
        text_body=_text_body(
            submission=submission,
            decision=decision,
            assessment=assessment,
            links=links,
            lead_reference=lead_reference,
        ),
        html_body=_html_body(
            submission=submission,
            decision=decision,
            assessment=assessment,
            links=links,
            lead_reference=lead_reference,
        ),
    )


# -------------------------------------------------------------------------- the subject


def _subject(
    *,
    decision: RoutingDecision,
    assessment: LeadAssessment | None,
    company: str,
    person: str,
) -> str:
    """One line that sorts and triages an inbox on its own.

    The tier and score lead because that is what a rep scans down the list for, and the
    company follows because that is what they recognise. A failed assessment says so in the
    first characters rather than showing a score of 0, which would read as a verdict.
    """
    who = company or person or "unknown company"
    if assessment is None:
        return f"[NEEDS REVIEW] Lead we could not assess — {who}"
    marker = f"[{decision.tier.value.upper()} {decision.total_score:.0f}]"
    if decision.escalated:
        return f"{marker} {who} — human review"
    return f"{marker} {who}"


# ------------------------------------------------------------------------ the text part


def _text_body(
    *,
    submission: LeadSubmission,
    decision: RoutingDecision,
    assessment: LeadAssessment | None,
    links: Sequence[FeedbackLink],
    lead_reference: str,
) -> str:
    lines: list[str] = []
    banner = _banner_text(decision=decision, assessment=assessment)
    if banner is not None:
        headline, detail = banner
        lines += ["!" * 60, headline.upper(), detail, "!" * 60, ""]

    if assessment is None:
        lines += ["This lead has NOT been scored. Qualify it by hand.", ""]
    else:
        # Only the headline. ``decision.note`` on a normally-scored lead is a restatement of
        # exactly this line ("scored 87.00/100 — hot"), and the notes that say something a
        # rep needs — the failure banner, the confidence gate — are rendered by the banner
        # above, verbatim. Printing it twice trains people to skim past it.
        lines += [
            f"{decision.tier.value.upper()} — {decision.total_score:.0f}/{MAX_TOTAL_SCORE:.0f}",
            "",
        ]
        question = _clean(assessment.suggested_first_question)
        if question:
            lines += ["OPEN WITH", f"  {question}", ""]

    lines += ["CONTACT"]
    lines += [f"  {label}: {value}" for label, value in _contact_rows(submission)]
    lines.append("")

    message = _clean(submission.message)
    if message:
        lines += ["WHAT THEY WROTE", _indent(message), ""]

    if assessment is not None:
        lines += ["WHY", _indent(_clean(assessment.reasoning)), ""]
        lines += ["SCORES"]
        lines += [
            f"  {_dimension_label(name)}: {value}/{maximum}"
            for name, value, maximum in _dimension_rows(assessment.dimension_scores)
        ]
        lines += [f"  confidence: {assessment.confidence:.0%}", ""]
        lines += ["WHAT WE READ OFF THE FORM"]
        lines += [f"  {label}: {value}" for label, value in _fact_rows(assessment.extracted)]
        lines.append("")
        missing = [_clean(item) for item in assessment.missing_information if _clean(item)]
        if missing:
            lines += ["WHAT WE DO NOT KNOW"]
            lines += [f"  - {item}" for item in missing]
            lines.append("")

    if links:
        lines += ["WAS THIS A GOOD LEAD? One click, no login — it tunes the scoring."]
        lines += [f"  {link.verdict.label.capitalize()}: {link.url}" for link in links]
        lines.append("")

    if lead_reference:
        lines.append(f"Lead reference: {lead_reference}")
    return "\n".join(lines).strip() + "\n"


# ------------------------------------------------------------------------ the HTML part


def _html_body(
    *,
    submission: LeadSubmission,
    decision: RoutingDecision,
    assessment: LeadAssessment | None,
    links: Sequence[FeedbackLink],
    lead_reference: str,
) -> str:
    blocks: list[str] = []
    banner = _banner_text(decision=decision, assessment=assessment)
    if banner is not None:
        headline, detail = banner
        blocks.append(_html_banner(headline, detail))

    blocks.append(_html_headline(decision=decision, assessment=assessment))

    if assessment is not None:
        question = _clean(assessment.suggested_first_question)
        if question:
            blocks.append(_html_callout("Open with", question))

    blocks.append(_html_section("Contact", _html_definition_table(_contact_rows(submission))))

    message = _clean(submission.message)
    if message:
        blocks.append(_html_section("What they wrote", _html_quote(message)))

    if assessment is not None:
        blocks.append(_html_section("Why", _html_paragraph(_clean(assessment.reasoning))))
        blocks.append(
            _html_section(
                "Scores",
                _html_score_table(assessment.dimension_scores, assessment.confidence),
            )
        )
        blocks.append(
            _html_section(
                "What we read off the form",
                _html_definition_table(_fact_rows(assessment.extracted)),
            )
        )
        missing = [_clean(item) for item in assessment.missing_information if _clean(item)]
        if missing:
            blocks.append(_html_section("What we do not know", _html_list(missing)))

    if links:
        blocks.append(_html_feedback(links))
    blocks.append(_html_footer(lead_reference))

    body = "\n".join(blocks)
    # A table-in-a-table with an explicit width: the outer one paints the page background in
    # clients that ignore body styles, the inner one is the 600px column.
    return (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{html.escape(_subject_placeholder(decision, assessment))}</title>"
        "</head>"
        '<body style="margin:0;padding:0;background-color:#f2f4f7;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#f2f4f7;"><tr><td align="center" style="padding:16px;">'
        f'<table role="presentation" width="{_BODY_WIDTH_PX}" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;max-width:{_BODY_WIDTH_PX}px;background-color:#ffffff;'
        f"border:1px solid {_RULE};border-radius:8px;font-family:{_FONT};font-size:15px;"
        'line-height:1.5;color:#101828;">'
        f"{body}"
        "</table></td></tr></table></body></html>"
    )


def _subject_placeholder(decision: RoutingDecision, assessment: LeadAssessment | None) -> str:
    if assessment is None:
        return "Lead we could not assess"
    return f"{decision.tier.value} lead — {decision.total_score:.0f}/100"


def _html_banner(headline: str, detail: str) -> str:
    return (
        f'<tr><td style="padding:20px 24px;background-color:{_BANNER_BACKGROUND};'
        f'border-bottom:3px solid {_BANNER_BORDER};border-radius:8px 8px 0 0;">'
        f'<div style="font-size:17px;font-weight:700;color:{_BAD_COLOUR};">'
        f"{html.escape(headline)}</div>"
        f'<div style="padding-top:4px;color:{_MUTED};">{html.escape(detail)}</div>'
        "</td></tr>"
    )


def _html_headline(*, decision: RoutingDecision, assessment: LeadAssessment | None) -> str:
    colour = _TIER_COLOURS[decision.tier]
    if assessment is None:
        left = (
            f'<div style="font-size:22px;font-weight:700;color:{_MUTED};">Not scored</div>'
            f'<div style="padding-top:4px;color:{_MUTED};">Qualify this one by hand.</div>'
        )
    else:
        left = (
            f'<div style="font-size:26px;font-weight:700;color:{colour};">'
            f"{html.escape(decision.tier.value.upper())} "
            f'<span style="color:#101828;">{decision.total_score:.0f}</span>'
            f'<span style="font-size:15px;color:{_MUTED};">/{MAX_TOTAL_SCORE:.0f}</span></div>'
        )
        # See ``_text_body``: the note is the banner's job, and on a normally-scored lead it
        # only restates the two numbers directly above it.
    return f'<tr><td style="padding:24px 24px 8px 24px;">{left}</td></tr>'


def _html_callout(title: str, body: str) -> str:
    return (
        '<tr><td style="padding:8px 24px 16px 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:#f8f9fc;border-left:4px solid {_MUTED};">'
        '<tr><td style="padding:12px 16px;">'
        f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:{_MUTED};">{html.escape(title)}</div>'
        f'<div style="padding-top:4px;font-size:16px;font-weight:600;">{html.escape(body)}</div>'
        "</td></tr></table></td></tr>"
    )


def _html_section(title: str, inner: str) -> str:
    return (
        '<tr><td style="padding:12px 24px;">'
        f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:{_MUTED};border-top:1px solid {_RULE};padding-top:12px;">'
        f"{html.escape(title)}</div>"
        f'<div style="padding-top:8px;">{inner}</div>'
        "</td></tr>"
    )


def _html_paragraph(text: str) -> str:
    return f'<div style="margin:0;">{html.escape(text)}</div>'


def _html_quote(text: str) -> str:
    escaped = html.escape(text).replace("\n", "<br />")
    return (
        f'<div style="white-space:normal;color:#344054;background-color:#f8f9fc;'
        f'padding:12px 16px;border-radius:6px;">{escaped}</div>'
    )


def _html_definition_table(rows: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        "<tr>"
        f'<td style="padding:2px 12px 2px 0;color:{_MUTED};white-space:nowrap;'
        f'vertical-align:top;">{html.escape(label)}</td>'
        f'<td style="padding:2px 0;vertical-align:top;">{_html_value(label, value)}</td>'
        "</tr>"
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"{cells}</table>"
    )


def _html_value(label: str, value: str) -> str:
    """Make an address or a URL clickable, and everything else plain escaped text.

    Only the two labels this module produced itself become links, and the href is built from
    the same escaped value — so a form field containing ``javascript:`` is rendered as the
    text it is rather than becoming an attribute we constructed for an attacker.
    """
    escaped = html.escape(value)
    if value == _ABSENT:
        return f'<span style="color:{_MUTED};">{escaped}</span>'
    if label == "email":
        return f'<a href="mailto:{escaped}" style="color:#175cd3;">{escaped}</a>'
    if label == "website" and value.startswith(("http://", "https://")):
        return f'<a href="{escaped}" style="color:#175cd3;">{escaped}</a>'
    return escaped


def _html_score_table(scores: DimensionScores, confidence: float) -> str:
    rows = ""
    for name, value, maximum in _dimension_rows(scores):
        filled = round((value / maximum) * 100) if maximum else 0
        rows += (
            "<tr>"
            f'<td style="padding:3px 12px 3px 0;color:{_MUTED};white-space:nowrap;">'
            f"{html.escape(_dimension_label(name))}</td>"
            f'<td style="padding:3px 8px 3px 0;width:100%;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="background-color:{_RULE};border-radius:3px;">'
            f'<tr><td width="{filled}%" style="background-color:#475467;border-radius:3px;'
            'font-size:1px;line-height:6px;">&nbsp;</td>'
            f'<td width="{100 - filled}%" style="font-size:1px;line-height:6px;">&nbsp;</td>'
            "</tr></table></td>"
            f'<td style="padding:3px 0;white-space:nowrap;font-variant-numeric:tabular-nums;">'
            f"{value}/{maximum}</td></tr>"
        )
    rows += (
        "<tr>"
        f'<td style="padding:8px 12px 0 0;color:{_MUTED};">model confidence</td>'
        f'<td colspan="2" style="padding:8px 0 0 0;">{confidence:.0%}</td></tr>'
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"{rows}</table>"
    )


def _html_list(items: Sequence[str]) -> str:
    entries = "".join(f'<li style="padding-bottom:2px;">{html.escape(item)}</li>' for item in items)
    return f'<ul style="margin:0;padding-left:20px;">{entries}</ul>'


def _html_feedback(links: Sequence[FeedbackLink]) -> str:
    """The two buttons the whole feature exists for.

    Bulletproof-button shape: a table cell carrying the background colour with a padded
    anchor inside it, so a client that drops the background still shows a legible link and
    Outlook still shows the block of colour.
    """
    buttons = ""
    for link in links:
        colour = _GOOD_COLOUR if link.verdict is Verdict.GOOD else _BAD_COLOUR
        buttons += (
            f'<td align="center" style="padding:0 6px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="background-color:{colour};border-radius:6px;"><tr>'
            f'<td align="center" style="padding:12px 20px;">'
            f'<a href="{html.escape(link.url, quote=True)}" '
            f'style="color:#ffffff;font-weight:700;font-size:16px;text-decoration:none;'
            f'display:inline-block;">{html.escape(link.verdict.label.capitalize())}</a>'
            "</td></tr></table></td>"
        )
    return (
        '<tr><td style="padding:20px 24px 8px 24px;">'
        f'<div style="border-top:1px solid {_RULE};padding-top:16px;text-align:center;">'
        '<div style="font-weight:600;padding-bottom:12px;">Was this a good lead?</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'align="center"><tr>'
        f"{buttons}"
        "</tr></table>"
        f'<div style="padding-top:10px;font-size:13px;color:{_MUTED};">'
        "One tap, no login. We ask you to confirm on the next screen, and you can change "
        "your answer at any time.</div>"
        "</div></td></tr>"
    )


def _html_footer(lead_reference: str) -> str:
    reference = (
        f"Lead reference {html.escape(lead_reference)}" if lead_reference else "Automated message"
    )
    return (
        f'<tr><td style="padding:16px 24px 24px 24px;color:{_MUTED};font-size:12px;">'
        f"{reference} · Scored automatically; the tier and score are computed from the "
        "rubric, not written by the model.</td></tr>"
    )


# ------------------------------------------------------------------------------ shared


def _banner_text(
    *, decision: RoutingDecision, assessment: LeadAssessment | None
) -> tuple[str, str] | None:
    """The banner headline and its detail, or ``None`` when the lead was scored normally.

    The detail is ``decision.note`` verbatim. #9 owns that wording — ``system could not
    assess: …`` and ``low model confidence — human review`` — and an email that paraphrased
    it would drift from the phrase a rep has learned to recognise.
    """
    if assessment is None:
        return ("The system could not assess this lead", decision.note or SYSTEM_FAILURE_BANNER)
    if decision.escalated:
        return ("Needs a human look", decision.note)
    return None


def _contact_rows(submission: LeadSubmission) -> list[tuple[str, str]]:
    """Name, role, company and the ways to reach them — in the order a rep reads them."""
    return [
        ("name", _clean(submission.full_name) or _ABSENT),
        ("role", _clean(submission.role) or _ABSENT),
        ("company", _clean(submission.company) or _ABSENT),
        ("email", _clean(submission.email) or _ABSENT),
        ("phone", _clean(submission.phone) or _ABSENT),
        ("website", _clean(submission.website) or _ABSENT),
    ]


def _fact_rows(extracted: ExtractedFacts) -> list[tuple[str, str]]:
    return [
        ("company", _clean(extracted.company_name) or _ABSENT),
        ("industry", _clean(extracted.industry) or _ABSENT),
        ("size", _clean(extracted.company_size_estimate) or _ABSENT),
        ("seniority", _clean(extracted.role_seniority) or _ABSENT),
        ("use case", _clean(extracted.stated_use_case) or _ABSENT),
        ("timeline", _clean(extracted.stated_timeline) or _ABSENT),
    ]


def _dimension_rows(scores: DimensionScores) -> list[tuple[str, int, int]]:
    """Each dimension with its value and its own maximum, in schema order.

    The maxima come from :data:`~leadquali.domain.tenant_config.DIMENSION_MAXIMA`, which is
    read off the assessment schema, so a rubric change cannot leave this email showing
    ``30/25``.
    """
    dumped = scores.model_dump()
    return [(name, int(dumped[name]), DIMENSION_MAXIMA[name]) for name in dumped]


def _dimension_label(name: str) -> str:
    return name.replace("_", " ")


def _clean(value: str | None) -> str:
    """Collapse a submitted or generated field into one safe, bounded line of display text.

    Control characters are dropped (a form field can carry them, and they corrupt both a
    plain-text part and a header), runs of whitespace are collapsed except for newlines in
    the lead's own message, and the result is truncated — the whole payload is in the
    database, and an email is not the archive.
    """
    if value is None:
        return ""
    text = "".join(character for character in value if character >= " " or character == "\n")
    text = "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()
    if len(text) > MAX_FIELD_CHARS:
        text = text[:MAX_FIELD_CHARS].rstrip() + "…"
    return text


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.split("\n"))


__all__ = [
    "MAX_FIELD_CHARS",
    "FeedbackLink",
    "RenderedEmail",
    "render_routing_email",
]
