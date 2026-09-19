"""What the admin screens read, stated as value types and one port.

Four views, and the shape of each one is decided by a query cost rather than by a wireframe:

**The lead browser** pages with a **keyset cursor** over ``(created_at, id)``, never
``OFFSET``. Offset paging over a growing table gets slower the further anybody looks — page
500 makes the database materialise and discard 10,000 rows — and, worse, it *silently skips
rows*: a lead inserted while somebody is on page 3 shifts every later row down by one, and
the row that moved from the top of page 4 to the bottom of page 3 is never seen. A keyset
cursor names the last row rather than counting rows, so it is stable under insertion and
costs the same on page 500 as on page 1.

The browser reads **assessments joined to their leads** rather than leads joined to their
assessments. Every filter it offers except the date range — tier, confidence — is an
``assessments`` column, and ``ix_assessments_tenant_id_tier_created_at`` serves exactly
``WHERE tenant_id = ? AND tier = ? AND created_at < ?`` with the sort already in index
order. The cost is that a lead with no assessment at all does not appear; that is a lead
the pipeline never reached, which is a queue problem and shows up in #21's alarms rather
than in a browser page.

**The feedback review** is the query the storage decision was made for: *every lead scored
hot last month that the rep marked bad, grouped by industry*. Industry comes from
``assessments.extracted``, which #7 constrains, so it needs no second table. The filter side
is ``ix_assessments_tenant_id_tier_created_at`` and the join side ``ix_feedback_lead_id`` —
the two indexes ``db_schema`` says are shaped for this. Grouping happens in Python, over a
result set the date range and the row cap have already bounded.

**The dashboards** read #33's ``usage_daily`` rollups for volume and cost — never
``assessments`` — and take tier mix and feedback agreement from two direct queries that are
bounded by a tenant and a date range. A chart whose query scans ``assessments`` unbounded
does not ship, because its cost grows forever while its answer stays the same size.

**Lead detail** renders the raw payload to a human, which is allowed and is the point: a
person triaging a lead needs to see what the lead said. What is *not* allowed is that
payload reaching a log line or an error page (invariant 5), which is a property of the
handler and of the logger rather than of this module, and is tested as such.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.feedback import Verdict
from leadquali.domain.models import Tier
from leadquali.prompts.lead import LeadSubmission

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "UNKNOWN_INDUSTRY",
    "AdminQueryPort",
    "AgreementPoint",
    "DashboardWindow",
    "FeedbackNote",
    "IndustryGroup",
    "LeadAssessmentRow",
    "LeadDetail",
    "LeadFilter",
    "LeadPage",
    "LeadRow",
    "PageCursor",
    "RerunCandidate",
    "ReviewRow",
    "RoutingRow",
    "TierCount",
    "group_by_industry",
]

#: Rows per page. Big enough to scan by eye, small enough that a page renders in one
#: round trip and a mistyped filter does not pull a month of leads into a template.
DEFAULT_PAGE_SIZE: Final[int] = 50

#: The ceiling a caller may ask for. A page size arrives from a query string, so it is a
#: number a stranger picks; without a cap, ``?limit=1000000`` is a denial of service
#: spelled as a preference.
MAX_PAGE_SIZE: Final[int] = 200

#: What an unlabelled industry is grouped under. A literal rather than ``None`` so the
#: grouping has one bucket for "the model could not tell", which is itself a finding: a
#: tier that disagrees with sales mostly among leads whose industry could not be read is a
#: different problem from one that disagrees within an industry.
UNKNOWN_INDUSTRY: Final[str] = "(industry not extracted)"


@dataclass(frozen=True, slots=True, order=True)
class PageCursor:
    """The last row of a page, and therefore where the next one starts.

    ``(created_at, id)`` rather than ``created_at`` alone: timestamps collide — a batch of
    leads imported in one second share one — and a cursor on a non-unique column either
    repeats rows or skips them at every boundary. The id breaks the tie deterministically.
    """

    created_at: dt.datetime
    row_id: str

    def encode(self) -> str:
        """Render the cursor for a URL. Opaque to the reader, not secret."""
        return (
            base64.urlsafe_b64encode(f"{self.created_at.isoformat()}|{self.row_id}".encode())
            .decode("ascii")
            .rstrip("=")
        )

    @classmethod
    def decode(cls, raw: str) -> PageCursor | None:
        """Parse a cursor from a query string, or ``None`` if it is not one.

        Never raises. The value arrives from a URL, so it is attacker-controlled text, and
        a stack trace on a mistyped cursor would be a 500 on a page anybody can request.
        It carries no authority — it only says where to resume within a query whose tenant
        predicate the handler has already fixed — so it is not signed.

        **Both halves are validated here**, which is the whole point of the contract above.
        The timestamp always was; the row id was not, so a well-formed cursor carrying a
        non-UUID id parsed cleanly and then raised ``ValueError`` one layer down, inside
        the adapter's ``uuid.UUID(cursor.row_id)`` — a 500 on a page anybody can request,
        from the one function that promises not to produce one.
        """
        if not raw:
            return None
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (binascii.Error, ValueError):
            return None
        stamp, separator, row_id = decoded.partition("|")
        if not separator or not row_id:
            return None
        try:
            created_at = dt.datetime.fromisoformat(stamp)
            # Parsed rather than pattern-matched, so the accepted spellings are exactly the
            # ones the adapter will accept when it builds the predicate.
            uuid.UUID(row_id)
        except ValueError:
            return None
        if created_at.tzinfo is None:
            return None
        return cls(created_at=created_at, row_id=row_id)


@dataclass(frozen=True, slots=True)
class LeadFilter:
    """What the operator narrowed the browser to. Every field is optional.

    **The tenant is deliberately not here.** It used to be, and #32's isolation sweep is
    what said otherwise: invariant 4 is "``tenant_id`` on every table and every repository
    method", and a method whose tenant arrives inside a value object does not name its
    tenant — the sweep could not see it, and neither could a reader of the signature. It is
    also one fewer way to go wrong, because a filter object carrying a tenant can disagree
    with the page that built it.

    So the scope is a parameter of
    :meth:`AdminQueryPort.browse_leads` and this type holds only what the operator chose.
    """

    tier: Tier | None = None
    start: dt.date | None = None
    """Inclusive lower bound on the assessment date, UTC."""

    end: dt.date | None = None
    """Inclusive upper bound on the assessment date, UTC. Compared as ``< end + 1 day`` so
    that "to the 14th" includes the 14th, which is what an operator typing a date means."""

    min_confidence: Decimal | None = None
    max_confidence: Decimal | None = None
    """Bounded above as well as below, because "show me the leads the model was unsure
    about" is the query that finds a broken rubric, and it is an upper bound."""

    def __post_init__(self) -> None:
        """Refuse a filter that can never match rather than returning an empty page.

        An operator who has typed an inverted range wants to be told, not shown nothing.
        """
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(f"the date range {self.start}..{self.end} ends before it starts")
        low, high = self.min_confidence, self.max_confidence
        if low is not None and high is not None and low > high:
            raise ValueError(f"the confidence range {low}..{high} ends before it starts")


@dataclass(frozen=True, slots=True)
class LeadRow:
    """One row of the lead browser: an assessment and enough of its lead to find it."""

    lead_id: str
    assessment_id: str
    submission_id: str
    created_at: dt.datetime
    """When the assessment was recorded — the column the keyset cursor pages on."""

    received_at: dt.datetime
    tier: Tier | None
    """``None`` for a failed assessment: there was no judgement to bin."""

    total_score: Decimal | None
    confidence: Decimal | None
    status: str
    escalation_reason: str | None
    company: str | None
    """From ``assessments.extracted``, which #7 constrains — a company name, not a person's."""

    industry: str | None
    contact_email_hash: str | None
    """The identifier a log line may carry. Rendered instead of the address in listings;
    the address itself is on the detail page, where a human is looking at one lead."""

    verdict: Verdict | None
    """What a rep said, if anyone has."""

    @property
    def cursor(self) -> PageCursor:
        """Where the next page resumes if this is the last row of this one."""
        return PageCursor(created_at=self.created_at, row_id=self.assessment_id)


@dataclass(frozen=True, slots=True)
class LeadPage:
    """One page of the lead browser, and where the next one starts."""

    rows: tuple[LeadRow, ...]
    next_cursor: PageCursor | None
    """``None`` when this is the last page. Derived from having fetched one row more than
    the page size, so "is there more?" costs one row rather than a ``COUNT(*)``."""


@dataclass(frozen=True, slots=True)
class RoutingRow:
    """One ``routing_events`` row, as the detail page shows it."""

    action: str
    destination: str | None
    outcome: str
    provider_message_id: str | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class FeedbackNote:
    """One rep's verdict on this lead."""

    rater: str
    verdict: Verdict
    notes: str | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class LeadAssessmentRow:
    """One assessment of one lead, in full, for the detail page."""

    assessment_id: str
    created_at: dt.datetime
    status: str
    tier: Tier | None
    total_score: Decimal | None
    confidence: Decimal | None
    escalation_reason: str | None
    dimension_scores: Mapping[str, Any] | None
    extracted: Mapping[str, Any] | None
    reasoning: str | None
    missing_information: Sequence[Any]
    model_id: str
    prompt_version: str
    effort: str | None
    cost_usd: Decimal
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LeadDetail:
    """Everything known about one lead: the payload, the judgement, and what happened.

    ``raw_payload`` is the submitter's own words, including their contact details. This
    type exists to put that in front of a person, which invariant 5 allows — what it
    forbids is the same data reaching a log line or an error page, which is why the admin's
    exception handler renders a fixed page and never the row.
    """

    lead_id: str
    tenant_slug: str
    submission_id: str
    source: str
    received_at: dt.datetime
    contact_email_hash: str | None
    raw_payload: Mapping[str, Any]
    assessments: tuple[LeadAssessmentRow, ...]
    routing: tuple[RoutingRow, ...]
    feedback: tuple[FeedbackNote, ...]


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """One disagreement: what the system said, and what the rep said back."""

    lead_id: str
    assessed_at: dt.datetime
    tier: Tier | None
    total_score: Decimal | None
    confidence: Decimal | None
    industry: str | None
    company: str | None
    verdict: Verdict
    rater: str
    notes: str | None
    feedback_at: dt.datetime
    promoted: bool = False
    """Whether this lead is already in the golden set. Rendered so the button says
    "promoted" rather than offering an action the store would refuse."""


@dataclass(frozen=True, slots=True)
class IndustryGroup:
    """The review's rows for one industry, newest first."""

    industry: str
    rows: tuple[ReviewRow, ...]

    @property
    def count(self) -> int:
        """How many disagreements this industry accounts for."""
        return len(self.rows)


class DashboardWindow(StrEnum):
    """How wide a dashboard looks back. Named, because an operator picks one of these."""

    WEEK = "7d"
    MONTH = "30d"
    QUARTER = "90d"

    @property
    def days(self) -> int:
        """The window's length in days."""
        return {"7d": 7, "30d": 30, "90d": 90}[self.value]


@dataclass(frozen=True, slots=True)
class TierCount:
    """How many assessments landed in one tier over the dashboard's window."""

    tier: Tier | None
    """``None`` is the failed-assessment bucket: an attempt with no judgement to bin. It is
    shown rather than dropped, because a rising count here is a model or adapter incident
    and hiding it inside "other" is how it goes unnoticed."""

    count: int


@dataclass(frozen=True, slots=True)
class AgreementPoint:
    """One day of the feedback agreement rate."""

    day: dt.date
    good: int
    bad: int
    unsure: int

    @property
    def rated(self) -> int:
        """Verdicts given that day, all three kinds."""
        return self.good + self.bad + self.unsure

    @property
    def decisive(self) -> int:
        """Verdicts that took a side. ``unsure`` is excluded from the ratio below."""
        return self.good + self.bad

    @property
    def agreement(self) -> Decimal | None:
        """Share of decisive verdicts that agreed with the system, or ``None``.

        ``None`` rather than zero on a day with no decisive verdict: a day nobody rated
        and a day everybody disagreed are opposite facts, and a chart that draws both at
        0% invents a crisis every weekend. ``unsure`` is left out of the denominator
        because it is neither agreement nor disagreement.
        """
        if self.decisive == 0:
            return None
        return Decimal(self.good) / Decimal(self.decisive)


@dataclass(frozen=True, slots=True)
class RerunCandidate:
    """One historical lead, ready to be re-assessed against a candidate rubric.

    Carries a parsed :class:`~leadquali.prompts.lead.LeadSubmission` rather than the raw
    payload. The adapter builds it through #17's own ``LeadForm``, so a lead stored before
    a field was renamed goes through exactly the normalisation ingest applies at the door —
    a re-run that read old rows more leniently than ingest reads new ones would be
    comparing against a pipeline that does not exist. It also keeps ``app`` free of an
    import from ``api``, where that schema lives.
    """

    lead_id: str
    submission_id: str
    submission: LeadSubmission
    assessed_at: dt.datetime
    previous_tier: Tier | None
    previous_score: Decimal | None


@runtime_checkable
class AdminQueryPort(Protocol):
    """Everything the admin reads that is not already a #31 or #33 service call.

    Read-only by construction: there is no method here that writes anything. The admin's
    two writes are the config editor's (through
    :class:`~leadquali.app.tenants.TenantService`) and a golden-set promotion (through
    :class:`~leadquali.app.golden_promotion.GoldenPromotionStorePort`), and keeping them
    out of this port is what makes "the browser cannot change anything" a property of the
    type rather than of the handlers.
    """

    def browse_leads(
        self,
        *,
        tenant_slug: str,
        criteria: LeadFilter,
        cursor: PageCursor | None,
        limit: int,
    ) -> LeadPage:
        """One page of assessed leads, newest first, resuming after ``cursor``.

        ``tenant_slug`` is a parameter of its own rather than a field of ``criteria``, like
        every other method on this port: invariant 4 asks each one to name its tenant, and
        #32's sweep reads these signatures to decide what to check.

        Implementations fetch ``limit + 1`` rows and return the extra one as
        :attr:`LeadPage.next_cursor` rather than counting the whole result set.
        """
        ...

    def lead_detail(self, *, tenant_slug: str, lead_id: str) -> LeadDetail | None:
        """One lead in full, or ``None`` if this tenant has no such lead.

        ``None`` rather than an exception: a stale bookmark is expected, not exceptional,
        and the tenant predicate means "somebody else's lead" arrives here as the same
        answer — which is the point.
        """
        ...

    def feedback_review(
        self,
        *,
        tenant_slug: str,
        tier: Tier,
        verdict: Verdict,
        start: dt.date,
        end: dt.date,
        limit: int,
    ) -> Sequence[ReviewRow]:
        """Leads binned in ``tier`` over a date range that a rep gave ``verdict`` to."""
        ...

    def tier_mix(self, *, tenant_slug: str, start: dt.date, end: dt.date) -> Sequence[TierCount]:
        """How this tenant's assessments were distributed across the tiers."""
        ...

    def feedback_agreement(
        self, *, tenant_slug: str, start: dt.date, end: dt.date
    ) -> Sequence[AgreementPoint]:
        """Day-by-day verdict counts for this tenant, oldest first."""
        ...

    def rerun_candidates(self, *, tenant_slug: str, limit: int) -> Sequence[RerunCandidate]:
        """The most recently assessed leads, with their payload and the tier they got.

        The input to #36's re-run: what the rubric said about these leads before, and
        enough of each lead to ask the model again.
        """
        ...


def group_by_industry(rows: Sequence[ReviewRow]) -> tuple[IndustryGroup, ...]:
    """Group review rows by industry, biggest group first.

    Biggest first because the view exists to answer "where is the rubric wrong?", and the
    industry with eleven disagreements is the answer — it should not be below the one with
    a single row because of alphabetical order. Ties break on the industry name so two
    renders of the same data are identical.
    """
    buckets: dict[str, list[ReviewRow]] = {}
    for row in rows:
        industry = (row.industry or "").strip() or UNKNOWN_INDUSTRY
        buckets.setdefault(industry, []).append(row)
    ordered = sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
    return tuple(
        IndustryGroup(
            industry=industry,
            rows=tuple(sorted(found, key=lambda row: row.assessed_at, reverse=True)),
        )
        for industry, found in ordered
    )
