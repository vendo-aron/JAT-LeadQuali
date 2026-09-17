"""Postgres behind the staff admin: the config history, the read views, the promotions.

Three stores over five tables, in one file because they are one surface with one blast
radius — everything here is reached only from ``/admin``, by a handful of staff, and never
from the request path a customer's form posts to.

* :class:`PostgresConfigVersionStore` — the rubric's append-only edit history (#36's
  ``tenant_config_versions``).
* :class:`PostgresAdminQueryStore` — the lead browser, the lead detail page, the feedback
  review and the two dashboard queries that #33's rollups cannot answer.
* :class:`PostgresGoldenPromotionStore` — which leads are already in the eval set.

Every statement resolves its session through
:func:`~leadquali.adapters.unit_of_work.session_scope` rather than opening one itself. On
its own that behaves exactly like a transaction per call; inside a
:meth:`~leadquali.adapters.unit_of_work.PostgresUnitOfWork.atomic` block it joins that
transaction, which is what makes the config write and its audit row commit together or not
at all.

Why the version number comes out of the table
---------------------------------------------

:meth:`PostgresConfigVersionStore.append` is one ``INSERT ... SELECT`` that computes
``MAX(version) + 1`` for the tenant in the same statement that inserts the row. A
``SELECT`` followed by an ``INSERT`` would hand two concurrent admin processes the same
number, and a counter held in Python would do it across restarts as well.
``UNIQUE (tenant_id, version)`` then settles the race the statement cannot: one of the two
transactions gets a constraint violation and can retry, rather than one edit's audit row
overwriting the other's.

Why the browser pages on ``(created_at, id)``
---------------------------------------------

``OFFSET`` over a growing table is slow at depth and, worse, *lossy*: a lead inserted while
somebody is on page 3 shifts every later row down by one, and the row that moved from the
top of page 4 to the bottom of page 3 is never returned. The predicate here is a row-value
comparison — ``(created_at, id) < (:created_at, :id)`` — which Postgres can drive straight
off ``ix_assessments_tenant_id_tier_created_at`` and which names a row rather than counting
them, so it is stable under insertion and costs the same on page 500 as on page 1.

Why the dashboards are two queries and not five
-----------------------------------------------

Volume and cost come from #33's ``usage_daily``, through
:class:`~leadquali.app.metering.MeteringService`, and are not in this file at all. What is
here is the tier mix and the day-by-day verdict counts, both bounded by a tenant and a date
range and both served by the composite indexes ``db_schema`` already carries. A chart whose
query scans ``assessments`` unbounded does not ship.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Row, and_, func, insert, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import (
    Assessment,
    Feedback,
    Lead,
    RoutingEvent,
    Tenant,
    TenantConfigVersion,
)
from leadquali.adapters.db_schema import (
    GoldenPromotion as GoldenPromotionRow,
)
from leadquali.adapters.unit_of_work import session_scope
from leadquali.api.schemas import LeadForm
from leadquali.app.admin_views import (
    AgreementPoint,
    FeedbackNote,
    LeadAssessmentRow,
    LeadDetail,
    LeadFilter,
    LeadPage,
    LeadRow,
    PageCursor,
    RerunCandidate,
    ReviewRow,
    RoutingRow,
    TierCount,
)
from leadquali.app.config_versions import ConfigVersion, UnknownConfigVersionError
from leadquali.app.feedback import Verdict
from leadquali.app.golden_promotion import GoldenPromotion
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import UnknownTenantError
from leadquali.config import Settings
from leadquali.domain.models import Tier

__all__ = [
    "PostgresAdminQueryStore",
    "PostgresConfigVersionStore",
    "PostgresGoldenPromotionStore",
    "tenant_uuid",
]

#: How many feedback rows the detail page and the review will render for one lead. A lead
#: has one verdict per rater and a handful of raters; the cap is there so a pathological
#: row count cannot become a pathological page.
_MAX_ROWS_PER_LEAD: Final[int] = 50


def tenant_uuid(tenant_slug: str) -> uuid.UUID:
    """The row id for a tenant slug.

    Derived rather than looked up — ``tenants.id`` is ``uuid5(TENANT_ID_NAMESPACE, slug)``
    by construction (#31) — so a tenant-scoped read is one statement rather than two, and
    a slug that names no tenant simply matches no rows instead of needing its own branch.
    """
    return tenant_id_for(tenant_slug)


def _as_tier(value: str | None) -> Tier | None:
    """A ``tier`` column as the domain's enum, or ``None`` for a failed assessment."""
    return Tier(value) if value else None


def _day_range(start: dt.date, end: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The half-open UTC instant range covering ``start..end`` inclusive.

    Half-open and expressed as instants, exactly as ``metering_postgres._day_bounds`` does
    it, so the predicate stays sargable against the ``(tenant_id, ..., created_at)``
    composites rather than wrapping the column in a ``date()`` call the index cannot serve.
    """
    low = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    return low, dt.datetime.combine(end, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(days=1)


# ------------------------------------------------------------------- the edit history


class PostgresConfigVersionStore:
    """The rubric's append-only history. Implements ``ConfigVersionStorePort``.

    Args:
        sessions: The session factory; see
            :func:`leadquali.adapters.store_postgres.session_factory`.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresConfigVersionStore:
        """A store over the memoised engine for ``url``."""
        from leadquali.adapters.store_postgres import session_factory

        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresConfigVersionStore:
        """A store over the configured ``DATABASE_URL``."""
        from leadquali.adapters.store_postgres import session_factory_from_env

        return cls(session_factory_from_env(settings))

    def append(
        self,
        *,
        tenant_slug: str,
        config: Mapping[str, Any],
        changed_by: str,
        changed_at: dt.datetime,
        note: str | None,
    ) -> ConfigVersion:
        """Append the next version for this tenant, allocating the number from the table.

        One statement: the ``SELECT`` that finds ``MAX(version)`` is the same statement
        that inserts, so there is no window in which a second process can read the same
        maximum. It is sourced from ``tenants`` so that a slug naming no tenant inserts
        nothing and is reported as such, rather than writing an orphan row against a
        plausible-looking UUID.

        Raises:
            UnknownTenantError: no such tenant, or the version number was taken by a
                concurrent write. Both mean "this did not happen"; the caller's
                transaction is rolled back either way.
        """
        tenant = tenant_uuid(tenant_slug)
        next_version = func.coalesce(
            select(func.max(TenantConfigVersion.version))
            .where(TenantConfigVersion.tenant_id == tenant)
            .scalar_subquery(),
            0,
        ) + literal(1)
        statement = (
            insert(TenantConfigVersion)
            .from_select(
                ["tenant_id", "version", "config", "changed_by", "changed_at", "note"],
                select(
                    Tenant.id,
                    next_version,
                    literal(dict(config), type_=TenantConfigVersion.__table__.c.config.type),
                    literal(changed_by),
                    literal(changed_at),
                    literal(note),
                ).where(Tenant.id == tenant),
            )
            .returning(
                TenantConfigVersion.version,
                TenantConfigVersion.config,
                TenantConfigVersion.changed_by,
                TenantConfigVersion.changed_at,
                TenantConfigVersion.note,
            )
        )
        try:
            with session_scope(self._sessions) as session:
                row = session.execute(statement).one_or_none()
        except IntegrityError as error:
            raise UnknownTenantError(
                f"another change to tenant '{tenant_slug}' took this version number; "
                "nothing was written — reload the config and try again"
            ) from error
        if row is None:
            raise UnknownTenantError(f"no tenant '{tenant_slug}'")
        return self._version_from_row(tenant_slug, row)

    def list_versions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[ConfigVersion]:
        """This tenant's history, newest first."""
        statement = (
            select(
                TenantConfigVersion.version,
                TenantConfigVersion.config,
                TenantConfigVersion.changed_by,
                TenantConfigVersion.changed_at,
                TenantConfigVersion.note,
            )
            .where(TenantConfigVersion.tenant_id == tenant_uuid(tenant_slug))
            .order_by(TenantConfigVersion.version.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        return [self._version_from_row(tenant_slug, row) for row in rows]

    def get_version(self, *, tenant_slug: str, version: int) -> ConfigVersion:
        """One version.

        Raises:
            UnknownConfigVersionError: this tenant has no such version.
        """
        statement = select(
            TenantConfigVersion.version,
            TenantConfigVersion.config,
            TenantConfigVersion.changed_by,
            TenantConfigVersion.changed_at,
            TenantConfigVersion.note,
        ).where(
            TenantConfigVersion.tenant_id == tenant_uuid(tenant_slug),
            TenantConfigVersion.version == version,
        )
        with session_scope(self._sessions) as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise UnknownConfigVersionError(
                f"tenant '{tenant_slug}' has no config version {version}"
            )
        return self._version_from_row(tenant_slug, row)

    @staticmethod
    def _version_from_row(tenant_slug: str, row: Row[Any]) -> ConfigVersion:
        return ConfigVersion(
            tenant_slug=tenant_slug,
            version=row.version,
            config=dict(row.config),
            changed_by=row.changed_by,
            changed_at=row.changed_at,
            note=row.note,
        )

    def __repr__(self) -> str:
        """Render the shape, never the connection string."""
        return "PostgresConfigVersionStore()"


# ------------------------------------------------------------------------- the reads


class PostgresAdminQueryStore:
    """The admin's read side. Implements ``AdminQueryPort``. Writes nothing, ever.

    Args:
        sessions: The session factory.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresAdminQueryStore:
        """A store over the memoised engine for ``url``."""
        from leadquali.adapters.store_postgres import session_factory

        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresAdminQueryStore:
        """A store over the configured ``DATABASE_URL``."""
        from leadquali.adapters.store_postgres import session_factory_from_env

        return cls(session_factory_from_env(settings))

    # ------------------------------------------------------------------------ browsing

    def browse_leads(
        self, *, criteria: LeadFilter, cursor: PageCursor | None, limit: int
    ) -> LeadPage:
        """One page of assessed leads, newest first, resuming strictly after ``cursor``.

        The keyset predicate is a row-value comparison rather than ``OFFSET``: see the
        module docstring for why that is a correctness decision and not a performance one.
        ``limit + 1`` rows are fetched so that "is there another page?" costs one row
        rather than a ``COUNT(*)`` over the whole filtered set.
        """
        tenant = tenant_uuid(criteria.tenant_slug)
        # The rep's verdict, if any, as a correlated scalar rather than a join: a lead can
        # have several raters, and joining would multiply the page's rows by them.
        verdict = (
            select(Feedback.verdict)
            .where(Feedback.tenant_id == tenant, Feedback.lead_id == Assessment.lead_id)
            .order_by(Feedback.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                Assessment.id,
                Assessment.lead_id,
                Assessment.created_at,
                Assessment.tier,
                Assessment.total_score,
                Assessment.confidence,
                Assessment.status,
                Assessment.escalation_reason,
                Assessment.extracted,
                Lead.submission_id,
                Lead.received_at,
                Lead.contact_email_hash,
                verdict.label("verdict"),
            )
            .join(Lead, and_(Lead.tenant_id == Assessment.tenant_id, Lead.id == Assessment.lead_id))
            .where(Assessment.tenant_id == tenant)
            .order_by(Assessment.created_at.desc(), Assessment.id.desc())
            .limit(limit + 1)
        )
        if criteria.tier is not None:
            statement = statement.where(Assessment.tier == criteria.tier.value)
        if criteria.start is not None or criteria.end is not None:
            low, high = _day_range(
                criteria.start or dt.date.min, criteria.end or dt.date(9999, 12, 30)
            )
            statement = statement.where(Assessment.created_at >= low, Assessment.created_at < high)
        if criteria.min_confidence is not None:
            statement = statement.where(Assessment.confidence >= criteria.min_confidence)
        if criteria.max_confidence is not None:
            statement = statement.where(Assessment.confidence <= criteria.max_confidence)
        if cursor is not None:
            statement = statement.where(
                tuple_(Assessment.created_at, Assessment.id)
                < tuple_(literal(cursor.created_at), literal(uuid.UUID(cursor.row_id)))
            )

        with session_scope(self._sessions) as session:
            fetched = session.execute(statement).all()
        rows = tuple(self._lead_row(row) for row in fetched[:limit])
        return LeadPage(
            rows=rows,
            next_cursor=rows[-1].cursor if len(fetched) > limit and rows else None,
        )

    def lead_detail(self, *, tenant_slug: str, lead_id: str) -> LeadDetail | None:
        """One lead in full, or ``None`` if this tenant has no such lead."""
        tenant = tenant_uuid(tenant_slug)
        try:
            lead_key = uuid.UUID(lead_id)
        except ValueError:
            # A malformed id from a URL is "no such lead", not a 500.
            return None
        lead_statement = select(
            Lead.id,
            Lead.submission_id,
            Lead.source,
            Lead.received_at,
            Lead.contact_email_hash,
            Lead.raw_payload,
        ).where(Lead.tenant_id == tenant, Lead.id == lead_key)
        assessment_statement = (
            select(
                Assessment.id,
                Assessment.created_at,
                Assessment.status,
                Assessment.tier,
                Assessment.total_score,
                Assessment.confidence,
                Assessment.escalation_reason,
                Assessment.dimension_scores,
                Assessment.extracted,
                Assessment.reasoning,
                Assessment.missing_information,
                Assessment.model_id,
                Assessment.prompt_version,
                Assessment.effort,
                Assessment.cost_usd,
                Assessment.latency_ms,
            )
            .where(Assessment.tenant_id == tenant, Assessment.lead_id == lead_key)
            .order_by(Assessment.created_at.desc())
            .limit(_MAX_ROWS_PER_LEAD)
        )
        routing_statement = (
            select(
                RoutingEvent.action,
                RoutingEvent.destination,
                RoutingEvent.dispatched_at,
                RoutingEvent.provider_message_id,
                RoutingEvent.created_at,
            )
            .where(RoutingEvent.tenant_id == tenant, RoutingEvent.lead_id == lead_key)
            .order_by(RoutingEvent.created_at.desc())
            .limit(_MAX_ROWS_PER_LEAD)
        )
        feedback_statement = (
            select(Feedback.rater, Feedback.verdict, Feedback.notes, Feedback.created_at)
            .where(Feedback.tenant_id == tenant, Feedback.lead_id == lead_key)
            .order_by(Feedback.created_at.desc())
            .limit(_MAX_ROWS_PER_LEAD)
        )
        with session_scope(self._sessions) as session:
            lead = session.execute(lead_statement).one_or_none()
            if lead is None:
                return None
            assessments = session.execute(assessment_statement).all()
            routing = session.execute(routing_statement).all()
            feedback = session.execute(feedback_statement).all()
        return LeadDetail(
            lead_id=str(lead.id),
            tenant_slug=tenant_slug,
            submission_id=lead.submission_id,
            source=lead.source,
            received_at=lead.received_at,
            contact_email_hash=lead.contact_email_hash,
            raw_payload=dict(lead.raw_payload),
            assessments=tuple(self._assessment_row(row) for row in assessments),
            routing=tuple(
                RoutingRow(
                    action=row.action,
                    destination=row.destination,
                    # ``routing_events`` records the attempt; a row with no dispatch time
                    # is one that failed, which is exactly what the detail page must show.
                    outcome="dispatched" if row.dispatched_at is not None else "not dispatched",
                    provider_message_id=row.provider_message_id,
                    created_at=row.created_at,
                )
                for row in routing
            ),
            feedback=tuple(
                FeedbackNote(
                    rater=row.rater,
                    verdict=Verdict(row.verdict),
                    notes=row.notes,
                    created_at=row.created_at,
                )
                for row in feedback
            ),
        )

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
        """The query the storage decision was made for (plan §4).

        Filter side: ``ix_assessments_tenant_id_tier_created_at``, which is
        ``(tenant_id, tier, created_at)`` and therefore serves this predicate leading
        column first. Join side: ``ix_feedback_lead_id``. Industry comes off
        ``assessments.extracted``, which #7 constrains, so there is no third table.
        """
        tenant = tenant_uuid(tenant_slug)
        low, high = _day_range(start, end)
        statement = (
            select(
                Assessment.lead_id,
                Assessment.created_at,
                Assessment.tier,
                Assessment.total_score,
                Assessment.confidence,
                Assessment.extracted,
                Feedback.verdict,
                Feedback.rater,
                Feedback.notes,
                Feedback.created_at.label("feedback_at"),
            )
            .join(
                Feedback,
                and_(
                    Feedback.tenant_id == Assessment.tenant_id,
                    Feedback.lead_id == Assessment.lead_id,
                ),
            )
            .where(
                Assessment.tenant_id == tenant,
                Assessment.tier == tier.value,
                Assessment.created_at >= low,
                Assessment.created_at < high,
                Feedback.verdict == verdict.value,
            )
            .order_by(Assessment.created_at.desc())
            .limit(limit)
        )
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        return [
            ReviewRow(
                lead_id=str(row.lead_id),
                assessed_at=row.created_at,
                tier=_as_tier(row.tier),
                total_score=row.total_score,
                confidence=row.confidence,
                industry=self._extracted(row.extracted, "industry"),
                company=self._extracted(row.extracted, "company_name"),
                verdict=Verdict(row.verdict),
                rater=row.rater,
                notes=row.notes,
                feedback_at=row.feedback_at,
            )
            for row in rows
        ]

    # ---------------------------------------------------------------------- dashboards

    def tier_mix(self, *, tenant_slug: str, start: dt.date, end: dt.date) -> Sequence[TierCount]:
        """How this tenant's assessments were distributed across the tiers.

        Bounded by tenant and date, so it is a range scan over
        ``ix_assessments_tenant_id_created_at`` rather than an aggregate over the table's
        whole history. A failed assessment has a ``NULL`` tier and gets its own bucket.
        """
        low, high = _day_range(start, end)
        statement = (
            select(Assessment.tier, func.count())
            .where(
                Assessment.tenant_id == tenant_uuid(tenant_slug),
                Assessment.created_at >= low,
                Assessment.created_at < high,
            )
            .group_by(Assessment.tier)
        )
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        counts = [TierCount(tier=_as_tier(row[0]), count=int(row[1])) for row in rows]
        return sorted(counts, key=lambda found: found.tier.rank if found.tier else -1, reverse=True)

    def feedback_agreement(
        self, *, tenant_slug: str, start: dt.date, end: dt.date
    ) -> Sequence[AgreementPoint]:
        """Day-by-day verdict counts for this tenant, oldest first.

        Grouped in SQL rather than in Python so the result set is one row per day and
        verdict rather than one per click. The predicate leads on ``tenant_id``, which
        ``ix_feedback_tenant_id_verdict_created_at`` serves; the index's second column is
        ``verdict`` and this query has no verdict predicate, so Postgres filters the range
        rather than seeking within it. That is bounded work over a small table — ``feedback``
        grows with clicks, not with leads — and it is the reason this reads ``feedback`` and
        never ``assessments``.
        """
        low, high = _day_range(start, end)
        day = func.date_trunc("day", Feedback.created_at).label("day")
        statement = (
            select(day, Feedback.verdict, func.count())
            .where(
                Feedback.tenant_id == tenant_uuid(tenant_slug),
                Feedback.created_at >= low,
                Feedback.created_at < high,
            )
            .group_by(day, Feedback.verdict)
            .order_by(day)
        )
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        tallies: dict[dt.date, dict[str, int]] = {}
        for stamp, found, count in rows:
            tallies.setdefault(stamp.date(), {})[found] = int(count)
        return [
            AgreementPoint(
                day=when,
                good=tallies[when].get(Verdict.GOOD.value, 0),
                bad=tallies[when].get(Verdict.BAD.value, 0),
                unsure=tallies[when].get(Verdict.UNSURE.value, 0),
            )
            for when in sorted(tallies)
        ]

    def rerun_candidates(self, *, tenant_slug: str, limit: int) -> Sequence[RerunCandidate]:
        """The most recently assessed leads, with their payload and the tier they got.

        ``DISTINCT ON (lead_id)`` would be the tidier statement; this takes the most recent
        assessments and de-duplicates in Python instead, because a lead with two assessments
        is rare (it means a redelivery after a dispatch failure) and the ``ORDER BY`` that
        ``DISTINCT ON`` requires would put ``lead_id`` first, which no index leads on.
        """
        tenant = tenant_uuid(tenant_slug)
        statement = (
            select(
                Assessment.lead_id,
                Assessment.created_at,
                Assessment.tier,
                Assessment.total_score,
                Lead.submission_id,
                Lead.raw_payload,
            )
            .join(Lead, and_(Lead.tenant_id == Assessment.tenant_id, Lead.id == Assessment.lead_id))
            .where(Assessment.tenant_id == tenant)
            .order_by(Assessment.created_at.desc(), Assessment.id.desc())
            .limit(limit * 2)
        )
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        seen: set[uuid.UUID] = set()
        candidates: list[RerunCandidate] = []
        for row in rows:
            if row.lead_id in seen:
                continue
            seen.add(row.lead_id)
            candidates.append(
                RerunCandidate(
                    lead_id=str(row.lead_id),
                    submission_id=row.submission_id,
                    # Through #17's own form schema, so a lead stored before a field was
                    # renamed goes through exactly the normalisation ingest applies.
                    submission=LeadForm.model_validate(dict(row.raw_payload)).to_submission(),
                    assessed_at=row.created_at,
                    previous_tier=_as_tier(row.tier),
                    previous_score=row.total_score,
                )
            )
            if len(candidates) == limit:
                break
        return candidates

    # ----------------------------------------------------------------------- internals

    @staticmethod
    def _extracted(extracted: Mapping[str, Any] | None, key: str) -> str | None:
        """One field off ``assessments.extracted``, or ``None``.

        The column is nullable (a failed assessment has none) and the model may have
        answered ``null`` for the field, so both absences collapse to the same answer.
        """
        if not extracted:
            return None
        value = extracted.get(key)
        return str(value) if isinstance(value, str) and value.strip() else None

    def _lead_row(self, row: Row[Any]) -> LeadRow:
        return LeadRow(
            lead_id=str(row.lead_id),
            assessment_id=str(row.id),
            submission_id=row.submission_id,
            created_at=row.created_at,
            received_at=row.received_at,
            tier=_as_tier(row.tier),
            total_score=row.total_score,
            confidence=row.confidence,
            status=row.status,
            escalation_reason=row.escalation_reason,
            company=self._extracted(row.extracted, "company_name"),
            industry=self._extracted(row.extracted, "industry"),
            contact_email_hash=row.contact_email_hash,
            verdict=Verdict(row.verdict) if row.verdict else None,
        )

    @staticmethod
    def _assessment_row(row: Row[Any]) -> LeadAssessmentRow:
        return LeadAssessmentRow(
            assessment_id=str(row.id),
            created_at=row.created_at,
            status=row.status,
            tier=_as_tier(row.tier),
            total_score=row.total_score,
            confidence=row.confidence,
            escalation_reason=row.escalation_reason,
            dimension_scores=dict(row.dimension_scores) if row.dimension_scores else None,
            extracted=dict(row.extracted) if row.extracted else None,
            reasoning=row.reasoning,
            missing_information=list(row.missing_information or ()),
            model_id=row.model_id,
            prompt_version=row.prompt_version,
            effort=row.effort,
            cost_usd=row.cost_usd if row.cost_usd is not None else Decimal(0),
            latency_ms=row.latency_ms,
        )

    def __repr__(self) -> str:
        """Render the shape, never the connection string."""
        return "PostgresAdminQueryStore()"


# --------------------------------------------------------------------- the promotions


class PostgresGoldenPromotionStore:
    """Which leads are already in the eval golden set. Implements the promotion port.

    Args:
        sessions: The session factory.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresGoldenPromotionStore:
        """A store over the memoised engine for ``url``."""
        from leadquali.adapters.store_postgres import session_factory

        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresGoldenPromotionStore:
        """A store over the configured ``DATABASE_URL``."""
        from leadquali.adapters.store_postgres import session_factory_from_env

        return cls(session_factory_from_env(settings))

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

        ``ON CONFLICT DO NOTHING`` against ``uq_golden_promotions_tenant_id_lead_id``,
        followed by a read when nothing was inserted. One statement decides, so two
        operators clicking at once cannot both create a case — and the second one gets the
        first one's label rather than an error page about a constraint.
        """
        tenant = tenant_uuid(tenant_slug)
        columns = (
            GoldenPromotionRow.case_id,
            GoldenPromotionRow.expected_tier,
            GoldenPromotionRow.promoted_by,
            GoldenPromotionRow.note,
            GoldenPromotionRow.promoted_at,
        )
        statement = (
            pg_insert(GoldenPromotionRow)
            .values(
                tenant_id=tenant,
                lead_id=uuid.UUID(lead_id),
                case_id=case_id,
                expected_tier=expected_tier.value,
                promoted_by=promoted_by,
                note=note,
                promoted_at=promoted_at,
            )
            .on_conflict_do_nothing(constraint="uq_golden_promotions_tenant_id_lead_id")
            .returning(*columns)
        )
        existing_statement = select(*columns).where(
            GoldenPromotionRow.tenant_id == tenant,
            GoldenPromotionRow.lead_id == uuid.UUID(lead_id),
        )
        with session_scope(self._sessions) as session:
            inserted = session.execute(statement).one_or_none()
            if inserted is not None:
                return self._promotion_from_row(tenant_slug, lead_id, inserted), True
            found = session.execute(existing_statement).one()
        return self._promotion_from_row(tenant_slug, lead_id, found), False

    def list_promotions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[GoldenPromotion]:
        """This tenant's promotions, newest first."""
        statement = (
            select(
                GoldenPromotionRow.lead_id,
                GoldenPromotionRow.case_id,
                GoldenPromotionRow.expected_tier,
                GoldenPromotionRow.promoted_by,
                GoldenPromotionRow.note,
                GoldenPromotionRow.promoted_at,
            )
            .where(GoldenPromotionRow.tenant_id == tenant_uuid(tenant_slug))
            .order_by(GoldenPromotionRow.promoted_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        return [self._promotion_from_row(tenant_slug, str(row.lead_id), row) for row in rows]

    def promoted_lead_ids(self, *, tenant_slug: str, lead_ids: Sequence[str]) -> frozenset[str]:
        """Which of these leads are already promoted, in one query rather than N.

        An unparseable id is dropped rather than raising: the list comes from a page of
        rows this process just rendered, but a caller that passed something else deserves
        "not promoted" rather than a 500 on a review screen.
        """
        keys: list[uuid.UUID] = []
        for lead_id in lead_ids:
            try:
                keys.append(uuid.UUID(lead_id))
            except ValueError:
                continue
        if not keys:
            return frozenset()
        statement = select(GoldenPromotionRow.lead_id).where(
            GoldenPromotionRow.tenant_id == tenant_uuid(tenant_slug),
            or_(*[GoldenPromotionRow.lead_id == key for key in keys]),
        )
        with session_scope(self._sessions) as session:
            rows = session.execute(statement).all()
        return frozenset(str(row[0]) for row in rows)

    @staticmethod
    def _promotion_from_row(tenant_slug: str, lead_id: str, row: Row[Any]) -> GoldenPromotion:
        return GoldenPromotion(
            tenant_slug=tenant_slug,
            lead_id=lead_id,
            case_id=row.case_id,
            expected_tier=Tier(row.expected_tier),
            promoted_by=row.promoted_by,
            note=row.note,
            promoted_at=row.promoted_at,
        )

    def __repr__(self) -> str:
        """Render the shape, never the connection string."""
        return "PostgresGoldenPromotionStore()"
