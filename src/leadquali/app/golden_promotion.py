"""Turning one disagreement in the feedback review into one golden case (#22, #36).

The feedback table is the only place this product ever learns anything, and the golden set
is what turns that into a number. #22 built the file, the schema and the validator;
promotion is the missing half — the path from "a rep marked this hot lead bad" to a line
somebody can append to ``tests/evals/golden_leads.jsonl``.

Two things are not obvious and are both deliberate.

**Nothing here writes to the golden set.** The file lives in a git repository, is appended
to by a human, and is read by a Lambda whose filesystem is read-only. So a promotion
records the *decision* — which lead, which tier, who said so, and why — in
``golden_promotions``, and :func:`render_case` produces the JSONL line on demand from that
row plus the lead. The operator commits it. That keeps the label a reviewed change rather
than a row that appeared in a file nobody reads, and it keeps the payload out of a second
table (see ``db_schema.GoldenPromotion``).

**The payload is rewritten, not copied.** ``docs/labeling-golden-set.md`` is explicit that
promoting a feedback row is a *rewrite*: the semantics have to survive — a VP is still a VP,
a 300-person logistics firm is still a 300-person logistics firm — and the identity must
not, because that file may end up in a customer's hands. :func:`strip_pii` does the
rewrite deterministically, so promoting the same lead twice produces the same pseudonyms
and a re-render is a no-op rather than a diff.

**#22 stays the authority on whether that worked.** There is no second PII rule here.
``tests/unit/test_golden_promotion.py`` feeds the rendered case through
:func:`tests.evals.golden_set.parse_golden_set` and asserts *that* accepts it, which is the
same check that fails the build if a real address ever reaches the committed file. Writing
the rule twice is how the copy that is not the gate drifts and stops catching anything.

What :func:`strip_pii` cannot do
--------------------------------

It rewrites the identifying *fields* and scrubs anything address-, phone- or URL-shaped out
of the free text. It cannot tell that "spoke to Priya about this last Tuesday" names a
person, because no regular expression can. The runbook says the promoter reads the case
before committing it, and the admin renders the line for review rather than committing it
for them precisely so that there is a person in that loop.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.domain.models import Tier

__all__ = [
    "MIN_PROMOTION_NOTE_CHARS",
    "PSEUDONYM_EMAIL_DOMAIN",
    "REDACTED_TEXT",
    "GoldenPromotion",
    "GoldenPromotionError",
    "GoldenPromotionService",
    "GoldenPromotionStorePort",
    "case_id_for",
    "render_case",
    "strip_pii",
]

#: Where every pseudonymised address is put. ``.invalid`` is reserved by RFC 2606 and can
#: never resolve, and it is in #22's allowlist — which is the list that decides, not this
#: constant. Named here so the pseudonymiser has one answer rather than a choice.
PSEUDONYM_EMAIL_DOMAIN: Final[str] = "invalid"

#: Shortest rationale accepted, matching #22's ``MIN_NOTES_CHARS``. Restated rather than
#: imported because ``app`` may not import from ``tests``; the unit test pins the two
#: together so they cannot drift.
MIN_PROMOTION_NOTE_CHARS: Final[int] = 20

#: What an address, phone number or URL found inside free text is replaced by. Visible on
#: purpose: a promoter who sees it knows the lead said something identifying there and can
#: decide whether the sentence still carries the signal.
REDACTED_TEXT: Final[str] = "[removed]"

#: Fields rewritten wholesale rather than scrubbed. Everything else — role, message,
#: extras — keeps its words, because those words are the signal the case exists to test.
_IDENTITY_FIELDS: Final[tuple[str, ...]] = ("full_name", "email", "company", "phone", "website")

#: Surnames to build a pseudonym from. Deliberately bland and obviously invented; the
#: point of the name is that the case reads like a lead, not that it reads like anybody.
_PSEUDONYM_NAMES: Final[tuple[str, ...]] = (
    "Alex Rivera",
    "Sam Okafor",
    "Jordan Blake",
    "Riley Nakamura",
    "Casey Lindqvist",
    "Morgan Achebe",
    "Rowan Petrov",
    "Quinn Delacroix",
)

#: Company pseudonyms, same reasoning. The *industry* is carried by the message and by
#: ``assessments.extracted``, so the name itself has no signal to preserve.
_PSEUDONYM_COMPANIES: Final[tuple[str, ...]] = (
    "Northwind",
    "Contoso",
    "Fabrikam",
    "Tailspin",
    "Litware",
    "Proseware",
    "Adventure Works",
    "Wide World",
)

#: Addresses in free text. Loose on purpose: over-matching costs a ``[removed]``, and
#: under-matching costs a customer's contact in a file that goes into git.
_EMAIL_IN_TEXT: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}"
)

#: URLs in free text, with or without a scheme.
_URL_IN_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:https?://|www\.)[^\s<>\"']{1,300}", re.IGNORECASE
)

#: Anything long enough and digit-dense enough to be a phone number. Bounded quantifiers
#: keep it linear on hostile input, which a lead's message is.
_PHONE_IN_TEXT: Final[re.Pattern[str]] = re.compile(r"(?<!\w)\+?[\d][\d\s().-]{7,20}\d(?!\w)")

_SLUG_SAFE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9_]+")


class GoldenPromotionError(ValueError):
    """A lead cannot be promoted as asked, with a reason the operator can act on."""


@dataclass(frozen=True, slots=True)
class GoldenPromotion:
    """One ``golden_promotions`` row: the decision, not the case."""

    tenant_slug: str
    lead_id: str
    case_id: str
    expected_tier: Tier
    promoted_by: str
    note: str
    promoted_at: dt.datetime


@runtime_checkable
class GoldenPromotionStorePort(Protocol):
    """Which leads have been promoted, and the labels that went with them."""

    def record(
        self,
        *,
        tenant_slug: str,
        lead_id: str,
        case_id: str,
        expected_tier: Tier,
        promoted_by: str,
        note: str,
        promoted_at: dt.datetime,
    ) -> tuple[GoldenPromotion, bool]:
        """Record a promotion, or return the one already on file.

        Returns:
            The promotion and whether this call created it. **Idempotent**: a second
            promotion of the same ``(tenant_slug, lead_id)`` returns the first row with
            ``False`` rather than raising or inserting a second. A refreshed confirmation
            page and a double tap are both ordinary, and the eval harness would weigh a
            doubly-promoted lead twice.
        """
        ...

    def list_promotions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[GoldenPromotion]:
        """This tenant's promotions, newest first."""
        ...

    def promoted_lead_ids(self, *, tenant_slug: str, lead_ids: Sequence[str]) -> frozenset[str]:
        """Which of these leads are already promoted, in one query rather than N."""
        ...


def case_id_for(*, tenant_slug: str, lead_id: str) -> str:
    """The stable golden-set slug for one lead.

    Derived from the tenant and the lead id rather than minted randomly, so re-rendering a
    promotion produces the same case id and an operator who lost the page does not create a
    second case for the same lead. Shaped to #22's ``case_id`` pattern — a lowercase slug
    of letters, digits and underscores, starting with a letter.
    """
    cleaned = _SLUG_SAFE.sub("_", tenant_slug.lower()).strip("_") or "tenant"
    digest = hashlib.sha256(f"{tenant_slug}:{lead_id}".encode()).hexdigest()[:8]
    return f"real_{cleaned}_{digest}"[:64]


def strip_pii(form: Mapping[str, Any], *, lead_id: str) -> dict[str, str | None]:
    """Rewrite one submission's identity out of it, keeping everything that is signal.

    Deterministic in ``lead_id``: the same lead always yields the same pseudonyms, so a
    re-render is a no-op rather than a diff, and two cases promoted from two leads at the
    same company do not accidentally claim to be the same company.

    What changes:

    * ``full_name`` becomes an invented name, ``email`` an address at a reserved
      never-resolving domain derived from it, ``company`` an invented company, ``website``
      that company at a reserved domain, and ``phone`` a number in the reserved ``555``
      range. Each is rewritten only if the lead supplied it — inventing a phone number the
      lead never gave would add a signal that was not there.
    * Every other field keeps its words, with anything address-, URL- or phone-shaped
      inside it replaced by :data:`REDACTED_TEXT`. ``role``, the message, the headcount and
      the timeline are the whole reason the case is worth having.

    Args:
        form: The lead's raw payload.
        lead_id: What the pseudonyms are derived from.

    Returns:
        A payload of strings and ``None``s, in the shape ``POST /leads`` accepts.
    """
    seed = int(hashlib.sha256(lead_id.encode("utf-8")).hexdigest()[:8], 16)
    person = _PSEUDONYM_NAMES[seed % len(_PSEUDONYM_NAMES)]
    company = _PSEUDONYM_COMPANIES[(seed // len(_PSEUDONYM_NAMES)) % len(_PSEUDONYM_COMPANIES)]
    handle = person.lower().replace(" ", ".")
    domain = f"{company.lower().replace(' ', '-')}.{PSEUDONYM_EMAIL_DOMAIN}"

    replacements: dict[str, str] = {
        "full_name": person,
        "email": f"{handle}@{domain}",
        "company": company,
        "website": f"https://{domain}",
        # NANP reserves 555-0100..555-0199 for fiction, which is exactly this.
        "phone": f"+1-555-01{seed % 100:02d}",
    }

    stripped: dict[str, str | None] = {}
    for field, value in form.items():
        if value is None:
            stripped[field] = None
            continue
        text = value if isinstance(value, str) else str(value)
        if field in _IDENTITY_FIELDS:
            # Only rewrite what the lead actually gave. A blank stays blank.
            stripped[field] = replacements[field] if text.strip() else text
        else:
            stripped[field] = _scrub(text)
    return stripped


def render_case(
    *,
    promotion: GoldenPromotion,
    form: Mapping[str, Any],
    hard_case: bool = False,
) -> dict[str, Any]:
    """Build the #22 golden case for one promotion. Nothing is written.

    The result is a plain dict in #22's schema, ready to be serialised as one JSONL line.
    It is deliberately *not* validated here: #22's parser is the validator, it lives in the
    test tree, and duplicating its rules in the application layer would create a second
    answer to "is this case acceptable?" that nobody runs the build against.

    Args:
        promotion: The recorded decision, carrying the label and its author.
        form: The lead's raw payload, pseudonymised by :func:`strip_pii`.
        hard_case: Whether the promoter flagged this as genuinely difficult. #22 wants at
            least ten of these and reports the gap.

    Returns:
        The case document.
    """
    return {
        "case_id": promotion.case_id,
        "provenance": "real",
        # Eight characters of the lead id: enough to find the row, short enough that the
        # file does not become an index of this customer's leads.
        "promoted_from": f"feedback:{promotion.lead_id[:8]}",
        "expected_tier": promotion.expected_tier.value,
        "labels": [
            {
                "labeler": promotion.promoted_by,
                "tier": promotion.expected_tier.value,
                "labeled_at": promotion.promoted_at.date().isoformat(),
                "notes": promotion.note,
            }
        ],
        "form": strip_pii(form, lead_id=promotion.lead_id),
        **({"hard_case": True} if hard_case else {}),
    }


class GoldenPromotionService:
    """Promote a reviewed lead into the golden set, once.

    Args:
        store: Where the decision is recorded.
    """

    def __init__(self, *, store: GoldenPromotionStorePort) -> None:
        self._store = store

    def promote(
        self,
        *,
        tenant_slug: str,
        lead_id: str,
        expected_tier: Tier,
        promoted_by: str,
        note: str,
        now: dt.datetime,
    ) -> tuple[GoldenPromotion, bool]:
        """Record that this lead belongs in the golden set at ``expected_tier``.

        Args:
            tenant_slug: Whose lead this is.
            lead_id: The lead being promoted.
            expected_tier: The **human's** answer, not the model's. A golden case whose
                expectation came from the thing under test measures nothing.
            promoted_by: The staff subject, used as #22's ``labeler`` handle.
            note: Why this tier and not the adjacent one, in the labeller's words.
            now: Wall-clock time from the clock port.

        Returns:
            The promotion and whether this call created it.

        Raises:
            GoldenPromotionError: the label is unusable — a blank labeller, or a rationale
                shorter than :data:`MIN_PROMOTION_NOTE_CHARS`. Refused here rather than at
                the point the line is appended to the file, because by then the operator
                has already been told it worked.
        """
        labeler = promoted_by.strip()
        if not labeler:
            raise GoldenPromotionError(
                "a promotion needs a labeler: an unattributed label is not one"
            )
        rationale = " ".join(note.split())
        if len(rationale) < MIN_PROMOTION_NOTE_CHARS:
            raise GoldenPromotionError(
                f"the rationale is {len(rationale)} characters and the golden set requires "
                f"at least {MIN_PROMOTION_NOTE_CHARS}. Write why this tier and not the "
                "adjacent one — in six months that sentence is what says whether the label "
                "or the model was at fault"
            )
        return self._store.record(
            tenant_slug=tenant_slug,
            lead_id=lead_id,
            case_id=case_id_for(tenant_slug=tenant_slug, lead_id=lead_id),
            expected_tier=expected_tier,
            promoted_by=labeler,
            note=rationale,
            promoted_at=now,
        )

    def already_promoted(self, *, tenant_slug: str, lead_ids: Sequence[str]) -> frozenset[str]:
        """Which of these leads are already in the golden set.

        Asked once per review page rather than once per row, so the button can say
        "promoted" instead of offering an action the store would refuse.
        """
        return self._store.promoted_lead_ids(tenant_slug=tenant_slug, lead_ids=lead_ids)

    def promotions_for(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[GoldenPromotion]:
        """This tenant's promotions, newest first."""
        return self._store.list_promotions(tenant_slug=tenant_slug, limit=limit)

    def export_jsonl(
        self, *, promotions: Sequence[GoldenPromotion], payloads: Mapping[str, Mapping[str, Any]]
    ) -> str:
        """Render promotions as the lines to append to ``golden_leads.jsonl``.

        One JSON object per line, keys sorted, so two exports of the same promotions are
        byte-identical and a re-export produces no diff.

        Args:
            promotions: What to render, in the order the lines should appear.
            payloads: ``{lead_id: raw payload}``. A promotion whose lead is missing — #37's
                retention job has purged it — is skipped rather than rendered without a
                payload, because a case with no lead is not a case.

        Returns:
            The lines, newline-separated, with a trailing newline when there is anything.
        """
        lines = [
            json.dumps(
                render_case(promotion=promotion, form=payloads[promotion.lead_id]),
                sort_keys=True,
                ensure_ascii=False,
            )
            for promotion in promotions
            if promotion.lead_id in payloads
        ]
        return "".join(f"{line}\n" for line in lines)


def _scrub(text: str) -> str:
    """Replace anything address-, URL- or phone-shaped in free text.

    Order matters: addresses first, because an address contains something a URL matcher
    would otherwise chew into, and phones last, because the digits in a URL's port or path
    would otherwise be read as a number.
    """
    cleaned = _EMAIL_IN_TEXT.sub(REDACTED_TEXT, text)
    cleaned = _URL_IN_TEXT.sub(REDACTED_TEXT, cleaned)
    return _PHONE_IN_TEXT.sub(REDACTED_TEXT, cleaned)
