"""Postgres behind billing: the webhook inbox, the usage ledger, the tenant link.

One adapter, :class:`PostgresBillingStore`, implementing
:class:`~leadquali.app.billing.BillingStorePort`. It is separate from
``store_postgres.py`` and from ``store_tenants.py`` for the same reason
``metering_postgres.py`` is: same database, same session factory, different blast radius.
Everything here can change what a customer is charged, and "what can touch the money
numbers?" should be one module.

Three statements in this file are load-bearing, and each is written as **one** statement on
purpose, because each is the point where two schedulers, two containers or a Stripe retry
can race.

``insert_event`` — ``INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id``
    The webhook idempotency guarantee. Not a ``SELECT`` then an ``INSERT``: two deliveries
    of the same event arriving milliseconds apart would both see no row and both insert,
    and the primary key would turn the second into an ``IntegrityError`` in the middle of
    the request instead of a quiet no-op. ``RETURNING`` is how the caller learns which of
    the two happened, and ``DO NOTHING`` is what makes a retry cost one round trip.

``mark_event_attempt_failed`` — one ``UPDATE`` that both counts and decides
    ``attempts = attempts + 1`` and the pending/failed decision are in the same statement,
    computed from the column rather than from a value the application read a moment ago.
    A read-modify-write would let two overlapping drains each read ``attempts = 4`` and
    each write ``5``, so an event would be retried for ever while looking like it had
    failed five times.

``record_usage_report`` — ``INSERT ... ON CONFLICT (tenant_id, usage_date) DO NOTHING``
    The one that stops a customer being billed twice. Same shape and same reason: the
    conflict target is the natural key, and ``RETURNING`` says whether this call was the
    one that recorded the day.

Tenant scope
------------

Every method about a tenant takes ``tenant_id`` and filters on it (invariant 4). The
resolver methods take a Stripe identifier instead — a customer id, an event id — and those
are the deliberate exception documented in :mod:`leadquali.app.billing`: a webhook is
received before there is a tenant to scope it to. ``tenant_for_customer`` is the function
that turns the one into the other, and every read *after* it is tenant-scoped.

The port speaks slugs, the database keys on UUIDs, and
:func:`~leadquali.adapters.store_postgres.tenant_uuid` is the single mapping between them —
imported rather than restated, because two spellings of "which row is this tenant" is how
a billing write lands on the wrong customer.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, case, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import StripeEventRow, Tenant, UsageReportRecord
from leadquali.adapters.store_postgres import (
    session_factory,
    session_factory_from_env,
    tenant_uuid,
)
from leadquali.app.billing import (
    BillingTenant,
    EventStatus,
    StripeEvent,
    UsageReport,
)
from leadquali.app.tenants import TenantStatus
from leadquali.config import Settings

__all__ = ["PostgresBillingStore"]

#: The ``tenants`` columns billing reads. Named rather than ``select(Tenant)`` so that the
#: billing path never loads a tenant's ``icp_config`` — a rubric document it has no use for
#: and that would be carried across the wire on every webhook.
_TENANT_COLUMNS = (
    Tenant.slug,
    Tenant.status,
    Tenant.stripe_customer_id,
    Tenant.stripe_subscription_id,
    Tenant.dunning_until,
)

#: The ``stripe_events`` columns the drain reads. ``payload`` is in here and is the only
#: large one; the drain needs it, because it is what the handlers dispatch on.
_EVENT_COLUMNS = (
    StripeEventRow.event_id,
    StripeEventRow.event_type,
    StripeEventRow.payload,
    StripeEventRow.received_at,
    StripeEventRow.status,
    StripeEventRow.attempts,
    StripeEventRow.tenant_id,
    StripeEventRow.processed_at,
    StripeEventRow.last_error,
)


def _tenant_from_row(row: Row[Any]) -> BillingTenant:
    """Map one ``tenants`` row onto the billing value type."""
    return BillingTenant(
        tenant_id=row.slug,
        status=TenantStatus(row.status),
        stripe_customer_id=row.stripe_customer_id,
        stripe_subscription_id=row.stripe_subscription_id,
        dunning_until=row.dunning_until,
    )


def _event_from_row(row: Row[Any], *, tenant_slug: str | None) -> StripeEvent:
    """Map one ``stripe_events`` row onto the billing value type.

    ``tenant_slug`` is resolved by the caller when it needs it; the column holds the UUID
    and the port speaks slugs, and inventing a mapping here would mean a second one.
    """
    return StripeEvent(
        event_id=row.event_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        received_at=row.received_at,
        status=EventStatus(row.status),
        attempts=row.attempts,
        tenant_id=tenant_slug,
        processed_at=row.processed_at,
        last_error=row.last_error,
    )


class PostgresBillingStore:
    """``stripe_events``, ``usage_reports`` and the billing columns on ``tenants``."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use.

        See :func:`leadquali.adapters.store_postgres.session_factory`.
        """
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresBillingStore:
        """A store over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresBillingStore:
        """A store over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    # --------------------------------------------------------------------- the inbox

    def insert_event(self, *, event: StripeEvent) -> bool:
        """Store a verified webhook, and say whether it was new.

        One statement, ``ON CONFLICT (event_id) DO NOTHING``: see the module docstring.
        ``tenant_id`` is deliberately not resolved here — the route inserts and returns
        200, and attributing the event is the drain's job.

        Returns:
            ``True`` when this call inserted the row; ``False`` when we already held the
            event id, which is a Stripe retry and means the caller must do nothing else.
        """
        statement = (
            insert(StripeEventRow)
            .values(
                event_id=event.event_id,
                event_type=event.event_type,
                payload=dict(event.payload),
                received_at=event.received_at,
                status=EventStatus.PENDING.value,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(StripeEventRow.event_id)
        )
        with self._sessions.begin() as session:
            return session.execute(statement).one_or_none() is not None

    def pending_events(self, *, limit: int) -> Sequence[StripeEvent]:
        """Events still to apply, oldest first, served by the partial index.

        A ``failed`` row is never returned: it is out of attempts and an operator has to
        look at it, and retrying it automatically would hide it rather than fix it.
        """
        statement = (
            select(*_EVENT_COLUMNS, Tenant.slug)
            .outerjoin(Tenant, Tenant.id == StripeEventRow.tenant_id)
            .where(StripeEventRow.status == EventStatus.PENDING.value)
            .order_by(StripeEventRow.received_at, StripeEventRow.event_id)
            .limit(limit)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_event_from_row(row, tenant_slug=row.slug) for row in rows]

    def mark_event_processed(
        self, *, event_id: str, processed_at: dt.datetime, tenant_id: str | None
    ) -> None:
        """Record that an event has been applied, and who it turned out to be about."""
        statement = (
            update(StripeEventRow)
            .where(StripeEventRow.event_id == event_id)
            .values(
                status=EventStatus.PROCESSED.value,
                processed_at=processed_at,
                tenant_id=None if tenant_id is None else tenant_uuid(tenant_id),
                last_error=None,
            )
        )
        with self._sessions.begin() as session:
            session.execute(statement)

    def mark_event_attempt_failed(
        self, *, event_id: str, error: str, attempted_at: dt.datetime, max_attempts: int
    ) -> EventStatus:
        """Count a failed attempt and return the status the row now has.

        The increment and the decision are one statement computed from the column, never
        from a value the application read a moment ago — see the module docstring.
        ``attempted_at`` is recorded in ``processed_at`` so an operator can see *when* the
        last attempt was without a second column; the row is still ``pending``, and
        ``status`` is what says so.
        """
        attempts = StripeEventRow.attempts + 1
        statement = (
            update(StripeEventRow)
            .where(StripeEventRow.event_id == event_id)
            .values(
                attempts=attempts,
                last_error=error,
                processed_at=attempted_at,
                # A SQL CASE over the incremented column, not a Python conditional: the
                # comparison has to be made by the database, on the value it is about to
                # store, or the count and the decision are two facts that can disagree.
                status=case(
                    (attempts >= max_attempts, EventStatus.FAILED.value),
                    else_=EventStatus.PENDING.value,
                ),
            )
            .returning(StripeEventRow.status)
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        # A missing row can only mean the event was deleted under us, which nothing in this
        # system does. Reported as failed rather than pending so it is not retried forever.
        return EventStatus.FAILED if row is None else EventStatus(row.status)

    # ------------------------------------------------------------------- the tenants

    def tenant_for_customer(self, *, stripe_customer_id: str) -> str | None:
        """Which of our tenants this Stripe customer is, if any.

        ``None`` is a normal answer, not an error: a Stripe account holds customers that
        are not our tenants — a test-mode experiment, another product on the same account —
        and an event about one of them is something to record and ignore.
        """
        statement = select(Tenant.slug).where(Tenant.stripe_customer_id == stripe_customer_id)
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        return None if row is None else str(row.slug)

    def billing_tenant(self, *, tenant_id: str) -> BillingTenant | None:
        """One tenant's billing columns, or ``None``."""
        statement = select(*_TENANT_COLUMNS).where(Tenant.id == tenant_uuid(tenant_id))
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        return None if row is None else _tenant_from_row(row)

    def billable_tenants(self) -> Sequence[BillingTenant]:
        """Every tenant with a Stripe customer, oldest first.

        Not filtered by status. A suspended tenant's usage from before it was suspended is
        still owed, and a job that skipped them would write off exactly the customers who
        are not paying.
        """
        statement = (
            select(*_TENANT_COLUMNS)
            .where(Tenant.stripe_customer_id.is_not(None))
            .order_by(Tenant.created_at, Tenant.slug)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_tenant_from_row(row) for row in rows]

    def link_customer(self, *, tenant_id: str, stripe_customer_id: str) -> None:
        """Record which Stripe customer a tenant is billed as."""
        self._update_tenant(tenant_id, {"stripe_customer_id": stripe_customer_id})

    def set_subscription(self, *, tenant_id: str, stripe_subscription_id: str | None) -> None:
        """Record (or clear) the tenant's current subscription."""
        self._update_tenant(tenant_id, {"stripe_subscription_id": stripe_subscription_id})

    def set_status(self, *, tenant_id: str, status: TenantStatus) -> None:
        """Set ``tenants.status``.

        The only write in billing that can stop new leads — and it stops *new* ones only.
        A lead already on the queue is assessed and delivered regardless: the qualification
        worker does not read this column.
        """
        self._update_tenant(tenant_id, {"status": status.value})

    def set_dunning_until(self, *, tenant_id: str, until: dt.datetime | None) -> None:
        """Start, extend or clear a tenant's grace period."""
        self._update_tenant(tenant_id, {"dunning_until": until})

    def tenants_in_expired_dunning(self, *, now: dt.datetime) -> Sequence[BillingTenant]:
        """Active tenants whose grace period has run out.

        Filtered on ``status = 'active'`` as well as on the deadline, so the sweep cannot
        re-suspend a tenant that is already suspended — which would be a second log line, a
        second metric and a second alert about one event.
        """
        statement = (
            select(*_TENANT_COLUMNS)
            .where(
                Tenant.dunning_until.is_not(None),
                Tenant.dunning_until <= now,
                Tenant.status == TenantStatus.ACTIVE.value,
            )
            .order_by(Tenant.dunning_until, Tenant.slug)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_tenant_from_row(row) for row in rows]

    # --------------------------------------------------------------- the usage ledger

    def record_usage_report(self, *, report: UsageReport, reported_at: dt.datetime) -> bool:
        """Record that a tenant-day has been reported, and say whether it was new.

        ``ON CONFLICT (tenant_id, usage_date) DO NOTHING``. ``False`` means the day was
        already recorded — by an overlapping run, by a retry, or by a second container —
        and the caller must send nothing further.
        """
        statement = (
            insert(UsageReportRecord)
            .values(
                tenant_id=tenant_uuid(report.tenant_id),
                usage_date=report.usage_date,
                reported_at=reported_at,
                external_id=report.external_id,
                quantity=report.quantity,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "usage_date"])
            .returning(UsageReportRecord.usage_date)
        )
        with self._sessions.begin() as session:
            return session.execute(statement).one_or_none() is not None

    def usage_reported(self, *, tenant_id: str, usage_date: dt.date) -> bool:
        """Whether this tenant-day has already been reported."""
        statement = select(UsageReportRecord.usage_date).where(
            UsageReportRecord.tenant_id == tenant_uuid(tenant_id),
            UsageReportRecord.usage_date == usage_date,
        )
        with self._sessions.begin() as session:
            return session.execute(statement).one_or_none() is not None

    # ------------------------------------------------------------------------ plumbing

    def _update_tenant(self, tenant_id: str, values: dict[str, Any]) -> None:
        """One tenant, filtered on its id (invariant 4). A missing tenant is a silent no-op
        here and a loud one in the service, which reads the row before it decides anything.
        """
        statement = (
            update(Tenant)
            .where(Tenant.id == tenant_uuid(tenant_id))
            .values(**values, updated_at=dt.datetime.now(dt.UTC))
        )
        with self._sessions.begin() as session:
            session.execute(statement)

    def __repr__(self) -> str:
        """Recognisable in a traceback without naming a connection string."""
        return "PostgresBillingStore(stripe_events, usage_reports, tenants)"
