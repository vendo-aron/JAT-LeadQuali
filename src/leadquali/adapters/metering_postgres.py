"""Postgres behind the usage meter: the rollup, and the reads that bill from it.

One adapter, :class:`PostgresMeteringStore`, implementing
:class:`~leadquali.app.metering.MeteringStorePort`. It is not on any request path — a
scheduled command drives it once a day and an operator drives it by hand — which is what
lets it do the one genuinely expensive thing in the system: recompute a day from ``leads``
and ``assessments``.

Why this is separate from ``store_postgres.py``
-----------------------------------------------

Same database, same session factory, different job and different blast radius. Everything
in ``store_postgres.py`` is on the path of a lead; everything here reads history in bulk
and writes a derived table. Keeping them apart means the worker's cold start does not
import the billing queries, and it means "what can touch the money numbers?" is one module.

The rollup, and why it is an upsert of a whole row
--------------------------------------------------

:meth:`PostgresMeteringStore.rollup_day` is a single ``INSERT ... SELECT ... ON CONFLICT
(tenant_id, usage_date) DO UPDATE`` that **replaces** every counter with the freshly
computed value. It never increments. That is the whole idempotency guarantee: running it
twice for the same day, or running it again a week later after a late SQS redelivery
inserted a lead into that day, leaves the row saying exactly what the source tables say —
and re-running it is therefore the ordinary way to correct a day rather than an
exceptional one. ``computed_at`` is the only column that changes on a re-run, which is
what ``tests/integration/test_metering_postgres.py`` asserts directly.

The day boundary, and the index
-------------------------------

A usage day is a **UTC calendar day** for every tenant (see
:mod:`leadquali.app.metering`). The definition is ``date_trunc('day', created_at AT TIME
ZONE 'UTC')``; what the queries actually emit is the equivalent half-open range
``created_at >= :midnight AND created_at < :next_midnight``, because a predicate wrapping
the column in a function cannot use ``ix_assessments_tenant_id_created_at`` and this one
can. Same days, same rows, one index scan instead of a sequential one.

``leads`` is counted by ``received_at``, not ``created_at``, and the two are different
columns on purpose: ``received_at`` is when the submission reached the ingest API and
``created_at`` is when the worker got round to writing the row, which after an SQS retry
can be much later. A customer's "leads on 3 September" means the ones they sent on
3 September, and ``ix_leads_tenant_id_received_at`` serves exactly that query.

Tenant scope
------------

Every tenant-scoped method takes ``tenant_id`` and filters on it (invariant 4). The two
fleet-wide methods — the ones reconciliation and cost allocation genuinely need — are
named ``fleet_*`` and return their results grouped **by tenant or by day**, so a total with
no tenant attached to it cannot be produced by accident at a call site.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Row, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Assessment, Lead, Tenant, UsageDaily
from leadquali.adapters.store_postgres import (
    ASSESSMENT_STATUS_FAILED,
    session_factory,
    session_factory_from_env,
    tenant_uuid,
)
from leadquali.app.metering import (
    BillingPeriod,
    DailySpend,
    MeteringError,
    TenantQuota,
    UsageTotals,
)
from leadquali.config import Settings

__all__ = ["PostgresMeteringStore"]

#: The columns of a ``usage_daily`` row, in the order the rollup writes them.
_ROLLUP_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "usage_date",
    "leads_ingested",
    "leads_assessed",
    "leads_billable",
    "assessments_failed",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "computed_at",
)

#: Everything a full recomputation replaces. ``tenant_id`` and ``usage_date`` are the
#: conflict target and cannot change; every other column is overwritten, which is what
#: makes the rollup a replacement rather than an increment.
_REPLACED_COLUMNS: tuple[str, ...] = _ROLLUP_COLUMNS[2:]


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The half-open UTC instant range covering ``day``.

    ``[midnight, next midnight)`` — the range form of ``date_trunc('day', ts AT TIME ZONE
    'UTC')``, written this way so the predicate is sargable against the composite indexes
    on ``(tenant_id, created_at)`` and ``(tenant_id, received_at)``.
    """
    start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


def _as_int(value: Any) -> int:
    """Coerce an aggregate back to ``int``.

    ``SUM`` over a ``bigint`` column returns ``numeric`` in Postgres, so a token total
    arrives as a :class:`~decimal.Decimal`. Counts of rows are already integers. Both are
    counts of whole things by the time they reach the application.
    """
    return int(value or 0)


def _as_decimal(value: Any) -> Decimal:
    """Coerce a money aggregate to ``Decimal``, treating an empty period as zero."""
    return Decimal(0) if value is None else Decimal(value)


class PostgresMeteringStore:
    """The usage meter's store. Every tenant-scoped statement names the tenant."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use.

        See :func:`leadquali.adapters.store_postgres.session_factory`.
        """
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresMeteringStore:
        """A store over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresMeteringStore:
        """A store over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    # ------------------------------------------------------------------------- rollup

    def rollup_day(self, *, tenant_id: str, day: dt.date) -> UsageTotals:
        """Recompute one tenant-day from source and replace its ``usage_daily`` row.

        One statement, and it is a whole-row replacement rather than an increment — see
        the module docstring. The counting rule is
        :func:`leadquali.app.metering.is_billable`, restated here as
        ``input_tokens > 0`` because a ``FILTER`` clause has to be SQL; the unit tests pin
        the two together.

        Returns:
            The row as it now stands, read back from ``RETURNING`` rather than from the
            values sent, so a CHECK constraint or a type coercion cannot make the returned
            totals disagree with what is stored.
        """
        tenant = tenant_uuid(tenant_id)
        start, end = _day_bounds(day)

        # Submissions stored for this tenant on this day, counted by when they arrived.
        # A scalar subquery rather than a join: the two tables have nothing to join on
        # here (a lead received today may be assessed tomorrow, and both facts belong to
        # the day they happened), and a join would multiply leads by their assessments.
        leads_ingested = (
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.tenant_id == tenant,
                Lead.received_at >= start,
                Lead.received_at < end,
            )
            .scalar_subquery()
        )

        source = (
            select(
                literal(tenant).label("tenant_id"),
                # Cast explicitly rather than relying on Postgres to resolve an
                # unknown-typed parameter from the target column of the INSERT: the
                # inference does hold, and a billing statement is the wrong place to
                # depend on it.
                cast(literal(day), Date).label("usage_date"),
                leads_ingested.label("leads_ingested"),
                func.count().label("leads_assessed"),
                # The billing rule, in SQL. An attempt that burned input tokens reached
                # Anthropic and is on our invoice, so it is on the customer's.
                func.count().filter(Assessment.input_tokens > 0).label("leads_billable"),
                func.count()
                .filter(Assessment.status == ASSESSMENT_STATUS_FAILED)
                .label("assessments_failed"),
                func.coalesce(func.sum(Assessment.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(Assessment.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(Assessment.cache_read_tokens), 0).label("cache_read_tokens"),
                func.coalesce(func.sum(Assessment.cache_creation_tokens), 0).label(
                    "cache_creation_tokens"
                ),
                func.coalesce(func.sum(Assessment.cost_usd), 0).label("cost_usd"),
                func.now().label("computed_at"),
            )
            .select_from(Assessment)
            .where(
                Assessment.tenant_id == tenant,
                Assessment.created_at >= start,
                Assessment.created_at < end,
            )
        )

        insertion = insert(UsageDaily).from_select(list(_ROLLUP_COLUMNS), source)
        statement = insertion.on_conflict_do_update(
            index_elements=[UsageDaily.tenant_id, UsageDaily.usage_date],
            set_={name: getattr(insertion.excluded, name) for name in _REPLACED_COLUMNS},
        ).returning(*(getattr(UsageDaily, name) for name in _ROLLUP_COLUMNS))

        with self._sessions.begin() as session:
            row = session.execute(statement).one()
        return self._totals_from_row(tenant_id=tenant_id, row=row)

    # --------------------------------------------------------------------------- reads

    def usage_for_period(self, *, tenant_id: str, period: BillingPeriod) -> UsageTotals:
        """Billable usage for one tenant and period, from the rollup table alone.

        The acceptance criterion "a single query returns billable usage for any tenant and
        period", and it is a range scan over the primary key: ``assessments`` is not named
        in this statement at all, so the cost of a month's billing read does not grow with
        the history behind it.
        """
        tenant = tenant_uuid(tenant_id)
        statement = select(
            func.coalesce(func.sum(UsageDaily.leads_ingested), 0),
            func.coalesce(func.sum(UsageDaily.leads_assessed), 0),
            func.coalesce(func.sum(UsageDaily.leads_billable), 0),
            func.coalesce(func.sum(UsageDaily.assessments_failed), 0),
            func.coalesce(func.sum(UsageDaily.input_tokens), 0),
            func.coalesce(func.sum(UsageDaily.output_tokens), 0),
            func.coalesce(func.sum(UsageDaily.cache_read_tokens), 0),
            func.coalesce(func.sum(UsageDaily.cache_creation_tokens), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0),
            func.max(UsageDaily.computed_at),
        ).where(
            UsageDaily.tenant_id == tenant,
            UsageDaily.usage_date >= period.start,
            UsageDaily.usage_date <= period.end,
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one()
        return UsageTotals(
            tenant_id=tenant_id,
            period=period,
            leads_ingested=_as_int(row[0]),
            leads_assessed=_as_int(row[1]),
            leads_billable=_as_int(row[2]),
            assessments_failed=_as_int(row[3]),
            input_tokens=_as_int(row[4]),
            output_tokens=_as_int(row[5]),
            cache_read_tokens=_as_int(row[6]),
            cache_creation_tokens=_as_int(row[7]),
            cost_usd=_as_decimal(row[8]),
            computed_at=row[9],
        )

    def daily_usage(self, *, tenant_id: str, period: BillingPeriod) -> Sequence[UsageTotals]:
        """The stored rollup rows for a period, oldest first.

        Days with no row are absent rather than zero-filled: "we never rolled this day up"
        and "nothing happened that day" are different facts, and only the caller knows
        which of them matters.
        """
        tenant = tenant_uuid(tenant_id)
        statement = (
            select(*(getattr(UsageDaily, name) for name in _ROLLUP_COLUMNS))
            .where(
                UsageDaily.tenant_id == tenant,
                UsageDaily.usage_date >= period.start,
                UsageDaily.usage_date <= period.end,
            )
            .order_by(UsageDaily.usage_date)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [self._totals_from_row(tenant_id=tenant_id, row=row) for row in rows]

    def quota_for(self, *, tenant_id: str) -> TenantQuota:
        """The tenant's plan allowance.

        Raises:
            MeteringError: no such tenant. Raised rather than defaulted, because an
                unlimited quota reported for a tenant that does not exist would read as
                "this customer is fine" forever.
        """
        tenant = tenant_uuid(tenant_id)
        statement = select(Tenant.monthly_lead_quota, Tenant.quota_alert_fraction).where(
            Tenant.id == tenant
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise MeteringError(f"no tenant '{tenant_id}' (id {tenant})")
        return TenantQuota(
            tenant_id=tenant_id,
            monthly_lead_quota=row[0],
            alert_fraction=Decimal(row[1]),
        )

    def set_quota(
        self, *, tenant_id: str, monthly_lead_quota: int | None, alert_fraction: Decimal
    ) -> TenantQuota:
        """Write a tenant's plan allowance and stamp ``updated_at``.

        ``updated_at`` moves because a plan change is an administrative write like any
        other, and "when did this customer's plan change?" is the first question after a
        surprising invoice.

        Raises:
            MeteringError: no such tenant.
        """
        tenant = tenant_uuid(tenant_id)
        statement = (
            update(Tenant)
            .where(Tenant.id == tenant)
            .values(
                monthly_lead_quota=monthly_lead_quota,
                quota_alert_fraction=alert_fraction,
                updated_at=func.now(),
            )
            .returning(Tenant.monthly_lead_quota, Tenant.quota_alert_fraction)
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise MeteringError(f"no tenant '{tenant_id}' (id {tenant})")
        return TenantQuota(
            tenant_id=tenant_id,
            monthly_lead_quota=row[0],
            alert_fraction=Decimal(row[1]),
        )

    # ---------------------------------------------------------------------- fleet-wide

    def fleet_billable_leads(self, *, period: BillingPeriod) -> Mapping[str, int]:
        """Billable leads per tenant, across every tenant, for cost allocation.

        Keyed by slug rather than by row id so that the allocation a margin report prints
        names customers an operator recognises. Tenants with no usage in the period are
        absent, which keeps them out of the denominator — allocating a share of the
        infrastructure bill to a customer who sent nothing would make everyone else's
        margin look better than it is.
        """
        statement = (
            select(Tenant.slug, func.sum(UsageDaily.leads_billable))
            .join(Tenant, Tenant.id == UsageDaily.tenant_id)
            .where(
                UsageDaily.usage_date >= period.start,
                UsageDaily.usage_date <= period.end,
            )
            .group_by(Tenant.slug)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return {row[0]: _as_int(row[1]) for row in rows}

    def fleet_daily_spend(self, *, period: BillingPeriod) -> Sequence[DailySpend]:
        """Token spend per day across every tenant, for invoice reconciliation.

        Fleet-wide because that is what Anthropic bills: the invoice is for the workspace,
        and comparing one tenant's share of it against the whole thing would fail every
        month by construction.
        """
        statement = (
            select(
                UsageDaily.usage_date,
                func.coalesce(func.sum(UsageDaily.input_tokens), 0),
                func.coalesce(func.sum(UsageDaily.output_tokens), 0),
                func.coalesce(func.sum(UsageDaily.cache_read_tokens), 0),
                func.coalesce(func.sum(UsageDaily.cache_creation_tokens), 0),
                func.coalesce(func.sum(UsageDaily.cost_usd), 0),
            )
            .where(
                UsageDaily.usage_date >= period.start,
                UsageDaily.usage_date <= period.end,
            )
            .group_by(UsageDaily.usage_date)
            .order_by(UsageDaily.usage_date)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [
            DailySpend(
                usage_date=row[0],
                input_tokens=_as_int(row[1]),
                output_tokens=_as_int(row[2]),
                cache_read_tokens=_as_int(row[3]),
                cache_creation_tokens=_as_int(row[4]),
                cost_usd=_as_decimal(row[5]),
            )
            for row in rows
        ]

    # ----------------------------------------------------------------------- internals

    @staticmethod
    def _totals_from_row(*, tenant_id: str, row: Row[Any]) -> UsageTotals:
        """Map one ``usage_daily`` row onto the type the application layer speaks in."""
        return UsageTotals(
            tenant_id=tenant_id,
            period=BillingPeriod.of_day(row.usage_date),
            leads_ingested=_as_int(row.leads_ingested),
            leads_assessed=_as_int(row.leads_assessed),
            leads_billable=_as_int(row.leads_billable),
            assessments_failed=_as_int(row.assessments_failed),
            input_tokens=_as_int(row.input_tokens),
            output_tokens=_as_int(row.output_tokens),
            cache_read_tokens=_as_int(row.cache_read_tokens),
            cache_creation_tokens=_as_int(row.cache_creation_tokens),
            cost_usd=_as_decimal(row.cost_usd),
            computed_at=row.computed_at,
        )

    def __repr__(self) -> str:
        """Render the shape, never a connection string."""
        return "PostgresMeteringStore()"
