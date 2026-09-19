"""Postgres behind the retention job and the deletion-request path.

One adapter, :class:`PostgresRetentionStore`, implementing
:class:`~leadquali.app.retention.RetentionStorePort`. It is not on any request path: a
daily EventBridge schedule drives it, and an operator drives it by hand with
``python -m leadquali.retentionctl``.

Why this is separate from ``store_postgres.py``
------------------------------------------------

Same database, same session factory, very different blast radius. ``store_postgres.py``
writes one lead at a time on the path of a customer's form; this module is the only place
in the codebase that issues a ``DELETE``. Keeping them apart means "what can destroy
customer data?" is one file, and it means the ingest Lambda's cold start does not import
it.

Every destructive statement is bounded
---------------------------------------

Both batched statements have the same shape:

.. code-block:: sql

    UPDATE/DELETE leads
    WHERE tenant_id = :tenant AND id IN (
        SELECT id FROM leads
        WHERE tenant_id = :tenant AND received_at < :cutoff AND …
        ORDER BY received_at
        LIMIT :batch FOR UPDATE SKIP LOCKED
    )

Three things are doing work there. ``LIMIT`` bounds the transaction, so a first run against
a year of leads is a sequence of short transactions rather than one that holds locks for
minutes. ``ORDER BY received_at`` makes it drain oldest-first, so a run that is cancelled
half way has still deleted the most overdue data. ``FOR UPDATE SKIP LOCKED`` means a
scheduled run and an operator running the same command during an incident do not block each
other — they take different rows and both make progress, which is the behaviour you want
from the command somebody reaches for when the pager has gone off.

The tenant predicate is repeated on the inner select and the outer statement. The inner one
is the one that matters and the outer one is redundant against it; both are there because
invariant 4 is "every statement filters on the tenant" with no exceptions for the ones that
are provably safe, and ``tests/isolation`` reads the compiled SQL rather than taking the
subquery's word for it.

The tombstone predicate
------------------------

``NOT (raw_payload @> '{"redacted": true}')`` is what makes the payload redaction
idempotent: a row already tombstoned is not selected, so the second run reports zero and
``redacted_at`` is never rewritten. ``@>`` is exact here rather than a heuristic, because a
stored payload's values are all strings or null (see
:mod:`leadquali.app.retention`) and a JSON ``true`` at the top level is something the ingest
path cannot produce.

The payload scan, and why it is allowed to be slow
---------------------------------------------------

:meth:`PostgresRetentionStore.leads_mentioning` compiles to
``raw_payload::text ILIKE '%…%'``, which no index can serve. That is deliberate and it is
documented on the port: it is the second net for an address sitting in a form field nobody
modelled, it runs once per deletion request rather than once per lead, and it is bounded to
one tenant. Making it fast would mean a trigram index on a cast of the column holding every
tenant's personal data, which is a worse trade than a query that takes a few seconds.

The address does travel to the server as a bound parameter on that one statement.
``sqlalchemy.engine`` is pinned to ``WARNING`` by ``observability.logs.QUIET_LOGGERS``
precisely so that an echo of bound parameters cannot put it in a log line.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import Row, ScalarSelect, Text, cast, delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import (
    Assessment,
    ErasureLog,
    Feedback,
    GoldenPromotion,
    Lead,
    RoutingEvent,
    Tenant,
)
from leadquali.adapters.store_postgres import (
    lead_uuid,
    session_factory,
    session_factory_from_env,
    tenant_uuid,
)
from leadquali.app.retention import (
    TOMBSTONE_KEY,
    ChildCounts,
    ErasureReceipt,
    ErasureRequest,
    ExpiredCounts,
    RetentionError,
    RetentionPolicy,
    UnknownTenantError,
)
from leadquali.config import Settings

__all__ = ["PostgresRetentionStore"]

#: The containment document the tombstone filter tests against. A module constant so the
#: predicate and :func:`~leadquali.app.retention.payload_tombstone` cannot drift into
#: disagreeing about what a tombstone is.
_TOMBSTONE_MATCH: Final[dict[str, Any]] = {TOMBSTONE_KEY: True}

#: Characters ``LIKE`` gives a meaning to. An address may legitimately contain ``_`` and
#: ``%`` in its local part, so the scan escapes them rather than letting a stranger's
#: address become a wildcard that matches every lead the tenant has.
_LIKE_ESCAPE: Final[str] = "\\"


def _escape_like(value: str) -> str:
    """Quote ``LIKE`` metacharacters in a literal that is being searched for."""
    for special in (_LIKE_ESCAPE, "%", "_"):
        value = value.replace(special, _LIKE_ESCAPE + special)
    return value


def _lead_uuids(lead_ids: Sequence[str]) -> list[uuid.UUID]:
    """Parse lead ids once, loudly, before any of them reaches a statement."""
    return [lead_uuid(lead_id) for lead_id in lead_ids]


class PostgresRetentionStore:
    """The retention store. Every tenant-scoped statement names the tenant."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use.

        See :func:`leadquali.adapters.store_postgres.session_factory`.
        """
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresRetentionStore:
        """A store over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresRetentionStore:
        """A store over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    # ------------------------------------------------------------------------- policies

    def fleet_retention_policies(self) -> Sequence[RetentionPolicy]:
        """Every tenant's windows, oldest tenant first.

        The scheduled sweep's worklist, and the one method here that takes no tenant. It
        returns the per-tenant breakdown — never a count, never a total — which is the same
        rule #33's ``fleet_*`` methods follow.
        """
        statement = select(
            Tenant.slug, Tenant.raw_retention_days, Tenant.assessment_retention_days
        ).order_by(Tenant.created_at, Tenant.slug)
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [_policy_from_row(row) for row in rows]

    def retention_policy(self, *, tenant_id: str) -> RetentionPolicy:
        """One tenant's windows.

        Raises:
            UnknownTenantError: no such tenant.
        """
        statement = select(
            Tenant.slug, Tenant.raw_retention_days, Tenant.assessment_retention_days
        ).where(Tenant.id == tenant_uuid(tenant_id))
        with self._sessions.begin() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise UnknownTenantError(f"no such tenant: {tenant_id}")
        return _policy_from_row(row)

    def set_retention_policy(
        self, *, tenant_id: str, raw_retention_days: int, assessment_retention_days: int
    ) -> RetentionPolicy:
        """Write one tenant's windows and return them as stored.

        Read back from ``RETURNING`` rather than echoed from the arguments, so a CHECK
        constraint the service did not anticipate cannot make the answer disagree with the
        row.

        Raises:
            UnknownTenantError: no such tenant.
            RetentionError: the database refused the windows.
        """
        statement = (
            update(Tenant)
            .where(Tenant.id == tenant_uuid(tenant_id))
            .values(
                raw_retention_days=raw_retention_days,
                assessment_retention_days=assessment_retention_days,
                updated_at=func.now(),
            )
            .returning(Tenant.slug, Tenant.raw_retention_days, Tenant.assessment_retention_days)
        )
        try:
            with self._sessions.begin() as session:
                row = session.execute(statement).one_or_none()
        except SQLAlchemyError as error:
            raise RetentionError(f"could not set retention for {tenant_id}: {error}") from error
        if row is None:
            raise UnknownTenantError(f"no such tenant: {tenant_id}")
        return _policy_from_row(row)

    # --------------------------------------------------------------------------- tier 1

    def count_expired(
        self, *, tenant_id: str, payload_cutoff: dt.datetime, lead_cutoff: dt.datetime
    ) -> ExpiredCounts:
        """How much a run would touch, writing nothing.

        One statement with two scalar sub-selects rather than two round trips, so the two
        numbers a dry run prints are read at the same instant and cannot describe two
        different states of the table.
        """
        tenant = tenant_uuid(tenant_id)
        payloads = (
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.tenant_id == tenant,
                Lead.received_at < payload_cutoff,
                ~Lead.raw_payload.contains(_TOMBSTONE_MATCH),
            )
            .scalar_subquery()
        )
        leads = (
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant, Lead.received_at < lead_cutoff)
            .scalar_subquery()
        )
        statement = select(payloads.label("payloads"), leads.label("leads"))
        with self._sessions.begin() as session:
            row = session.execute(statement).one()
        return ExpiredCounts(tenant_id=tenant_id, payloads=int(row[0]), leads=int(row[1]))

    def redact_expired_payloads(
        self,
        *,
        tenant_id: str,
        cutoff: dt.datetime,
        tombstone: Mapping[str, Any],
        batch_size: int,
    ) -> Sequence[str]:
        """Tombstone at most ``batch_size`` expired payloads. Returns the lead ids touched.

        The assessment, its scores and the routing events are untouched — that is the
        entire point of the tier split, and it is why this is an ``UPDATE`` of one column
        and not a ``DELETE``.
        """
        tenant = tenant_uuid(tenant_id)
        statement = (
            update(Lead)
            .where(
                Lead.tenant_id == tenant, Lead.id.in_(self._expiring(tenant, cutoff, batch_size))
            )
            .values(raw_payload=dict(tombstone))
            .returning(Lead.id)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [str(row[0]) for row in rows]

    def reasoning_for_leads(self, *, tenant_id: str, lead_ids: Sequence[str]) -> Mapping[str, str]:
        """Every non-empty ``assessments.reasoning`` for these leads, by assessment id."""
        if not lead_ids:
            return {}
        statement = select(Assessment.id, Assessment.reasoning).where(
            Assessment.tenant_id == tenant_uuid(tenant_id),
            Assessment.lead_id.in_(_lead_uuids(lead_ids)),
            Assessment.reasoning.is_not(None),
            Assessment.reasoning != "",
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return {str(row[0]): str(row[1]) for row in rows}

    def replace_reasoning(self, *, tenant_id: str, replacements: Mapping[str, str]) -> int:
        """Overwrite the named assessments' ``reasoning``. Returns rows changed.

        One statement per row, inside one transaction. A single ``UPDATE … FROM (VALUES …)``
        would be one round trip, and it would put the redacted prose of a whole batch into
        one statement's parameters; the per-row form keeps each statement small and each one
        individually tenant-scoped, which is what the isolation sweep reads.
        """
        if not replacements:
            return 0
        tenant = tenant_uuid(tenant_id)
        changed = 0
        with self._sessions.begin() as session:
            for assessment_id, reasoning in replacements.items():
                # RETURNING rather than ``rowcount``: the count is then read off rows the
                # server sent back, which is a fact, and it types as a Result like every
                # other statement here instead of needing a cast to CursorResult.
                rows = session.execute(
                    update(Assessment)
                    .where(
                        Assessment.tenant_id == tenant,
                        Assessment.id == uuid.UUID(assessment_id),
                    )
                    .values(reasoning=reasoning)
                    .returning(Assessment.id)
                ).all()
                changed += len(rows)
        return changed

    # --------------------------------------------------------------------------- tier 2

    def purge_expired_leads(self, *, tenant_id: str, cutoff: dt.datetime, batch_size: int) -> int:
        """Delete at most ``batch_size`` leads past tier 2. Returns how many went.

        The composite ``(tenant_id, lead_id)`` foreign keys cascade, so the assessments,
        routing events, feedback and golden promotions go with them. The ``tenants``
        foreign key is ``RESTRICT``: this statement cannot remove a customer.
        """
        tenant = tenant_uuid(tenant_id)
        statement = (
            delete(Lead)
            .where(
                Lead.tenant_id == tenant,
                Lead.id.in_(self._expiring(tenant, cutoff, batch_size, tombstoned_too=True)),
            )
            .returning(Lead.id)
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return len(rows)

    def _expiring(
        self,
        tenant: uuid.UUID,
        cutoff: dt.datetime,
        batch_size: int,
        *,
        tombstoned_too: bool = False,
    ) -> ScalarSelect[uuid.UUID]:
        """One batch of expired lead ids, oldest first, locked and skippable.

        ``tombstoned_too`` is the difference between the two tiers: tier 1 must skip rows it
        has already redacted or it never terminates, and tier 2 deletes the row whatever its
        payload looks like.
        """
        conditions = [Lead.tenant_id == tenant, Lead.received_at < cutoff]
        if not tombstoned_too:
            conditions.append(~Lead.raw_payload.contains(_TOMBSTONE_MATCH))
        return (
            select(Lead.id)
            .where(*conditions)
            .order_by(Lead.received_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )

    # ----------------------------------------------------------------- deletion requests

    def leads_for_subject(self, *, tenant_id: str, subject_hash: str) -> Sequence[str]:
        """Lead ids whose ``contact_email_hash`` is this subject. The indexed path."""
        statement = select(Lead.id).where(
            Lead.tenant_id == tenant_uuid(tenant_id),
            Lead.contact_email_hash == subject_hash,
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [str(row[0]) for row in rows]

    def leads_mentioning(self, *, tenant_id: str, needle: str) -> Sequence[str]:
        """Lead ids whose stored payload contains ``needle`` anywhere. Slow by design.

        ``ILIKE`` over ``raw_payload::text`` — the whole JSON document, keys included, for
        one tenant, with no index that can serve it. See the module docstring.
        """
        pattern = f"%{_escape_like(needle)}%"
        statement = select(Lead.id).where(
            Lead.tenant_id == tenant_uuid(tenant_id),
            cast(Lead.raw_payload, Text).ilike(pattern, escape=_LIKE_ESCAPE),
        )
        with self._sessions.begin() as session:
            rows = session.execute(statement).all()
        return [str(row[0]) for row in rows]

    def count_lead_children(self, *, tenant_id: str, lead_ids: Sequence[str]) -> ChildCounts:
        """Rows that will cascade when these leads go, per table.

        The four tables whose composite foreign key to ``leads`` is ``ON DELETE CASCADE``,
        named one at a time rather than derived from the metadata: a table added later with
        a different delete rule must show up as a missing count here, not as a silent zero.
        """
        if not lead_ids:
            return ChildCounts()
        tenant = tenant_uuid(tenant_id)
        leads = _lead_uuids(lead_ids)
        statement = select(
            *(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == tenant, model.lead_id.in_(leads))
                .scalar_subquery()
                .label(label)
                for model, label in _CHILD_TABLES
            )
        )
        with self._sessions.begin() as session:
            row = session.execute(statement).one()
        return ChildCounts(
            assessments=int(row[0]),
            routing_events=int(row[1]),
            feedback=int(row[2]),
            golden_promotions=int(row[3]),
        )

    def erase(self, *, tenant_id: str, request: ErasureRequest) -> ErasureReceipt:
        """Delete these leads and write the audit row, in one transaction.

        Three statements, and the order is the argument for keeping them together: count
        the children (the cascade reports nothing back, so afterwards is too late), delete
        the leads, file the evidence. A failure anywhere rolls back all three, so there is
        never a deletion nobody can prove or a proof of one that did not happen.

        Raises:
            RetentionError: the delete or the audit write was refused.
        """
        tenant = tenant_uuid(tenant_id)
        leads = _lead_uuids(request.lead_ids)
        children_statement = select(
            *(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == tenant, model.lead_id.in_(leads))
                .scalar_subquery()
                .label(label)
                for model, label in _CHILD_TABLES
            )
        )
        try:
            with self._sessions.begin() as session:
                row = session.execute(children_statement).one()
                children = ChildCounts(
                    assessments=int(row[0]),
                    routing_events=int(row[1]),
                    feedback=int(row[2]),
                    golden_promotions=int(row[3]),
                )
                deleted = (
                    len(
                        session.execute(
                            delete(Lead)
                            .where(Lead.tenant_id == tenant, Lead.id.in_(leads))
                            .returning(Lead.id)
                        ).all()
                    )
                    if leads
                    else 0
                )
                session.execute(
                    insert(ErasureLog).values(
                        tenant_id=tenant,
                        subject_hash=request.subject_hash,
                        leads_deleted=deleted,
                        assessments_deleted=children.assessments,
                        routing_events_deleted=children.routing_events,
                        feedback_deleted=children.feedback,
                        golden_promotions_deleted=children.golden_promotions,
                        matched_by_hash=request.matched_by_hash,
                        matched_by_payload_scan=request.matched_by_payload_scan,
                        requested_by=request.requested_by,
                        completed_at=request.completed_at,
                    )
                )
        except SQLAlchemyError as error:
            raise RetentionError(f"erasure for {tenant_id} was rolled back: {error}") from error
        return ErasureReceipt(
            tenant_id=tenant_id,
            subject_hash=request.subject_hash,
            leads_deleted=deleted,
            children=children,
            matched_by_hash=request.matched_by_hash,
            matched_by_payload_scan=request.matched_by_payload_scan,
            requested_by=request.requested_by,
            completed_at=request.completed_at,
        )


#: The child tables a lead's deletion cascades to, and the order the counts are read in.
#: Written out rather than walked from the metadata: a table added later whose foreign key
#: is ``RESTRICT`` rather than ``CASCADE`` — #35's ``usage_reports`` is one — must be a
#: deliberate decision here, not a zero that appears on an erasure receipt by default.
_CHILD_TABLES: Final[tuple[tuple[Any, str], ...]] = (
    (Assessment, "assessments"),
    (RoutingEvent, "routing_events"),
    (Feedback, "feedback"),
    (GoldenPromotion, "golden_promotions"),
)


def _policy_from_row(row: Row[Any]) -> RetentionPolicy:
    """One ``tenants`` row's retention columns as a policy."""
    return RetentionPolicy(
        tenant_id=str(row[0]),
        raw_retention_days=int(row[1]),
        assessment_retention_days=int(row[2]),
    )
