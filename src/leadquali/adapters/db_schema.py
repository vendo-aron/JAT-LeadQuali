"""Postgres schema: SQLAlchemy 2.0 declarative table definitions.

This module is the single source of truth for the database shape. Alembic's
``migrations/env.py`` targets :data:`Base.metadata`, so a model change that is not
accompanied by a migration shows up as an autogenerate diff.

It is deliberately free of behaviour: no sessions, no queries, no engine. The repository
implementations live in ``store_postgres.py``; keeping the tables separate means the
migration environment can import the schema without importing connection handling.

Schema decisions worth knowing about:

* ``tenant_id`` is on **every** table, including ``feedback`` and ``routing_events`` where
  it is reachable via ``lead_id``. Denormalising it is what lets every repository method
  filter on the tenant directly, and lets the analytics queries below use a single
  composite index instead of a join to ``leads``. Retrofitting multi-tenancy is a rewrite.
* ``leads`` carries ``UNIQUE (tenant_id, submission_id)``. SQS is at-least-once, so the
  worker will see the same submission twice; that constraint is the idempotency guarantee.
* There is no raw email column anywhere. ``leads.contact_email_hash`` gives log correlation
  without PII, and the address itself lives only inside ``leads.raw_payload``.
* Primary keys are server-generated UUIDs (``gen_random_uuid()``, built into Postgres 13+,
  so no extension is needed) and every timestamp is ``timestamptz`` defaulted by the server.
  A row inserted by ``psql`` during an incident is as well-formed as one inserted by the app.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Assessment",
    "Base",
    "Feedback",
    "Lead",
    "RoutingEvent",
    "Tenant",
    "metadata",
]

# Deterministic constraint and index names. Without this, Alembic autogenerate proposes
# renames on every run and `downgrade` cannot drop constraints Postgres named itself.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_UUID_PK = text("gen_random_uuid()")
_NOW = text("now()")


class Base(DeclarativeBase):
    """Declarative base carrying the project's metadata and naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


metadata = Base.metadata
"""The metadata Alembic migrates against."""


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=_UUID_PK)


def _tenant_fk() -> Mapped[uuid.UUID]:
    """A NOT NULL tenant reference. Deleting a tenant removes everything it owns."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def _lead_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=_NOW)


class Tenant(Base):
    """A customer. Its rubric lives in ``icp_config``, so onboarding is a config write."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    # ICP description, dimension weights, tier thresholds and routing rules. Invariant 1:
    # the rubric is tenant configuration, never code, so this column is not optional.
    icp_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Argon2 hash of the tenant's API key. The key itself is never stored.
    api_key_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Secrets Manager ARN for the webhook HMAC secret — a reference, not the secret.
    hmac_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'disabled')", name="status_known"),
    )


class Lead(Base):
    """A raw inbound submission, stored before anything is done with it."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # Caller-supplied identity of the submission; unique within the tenant.
    submission_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'received'")
    )
    # SHA-256 of the lowercased contact address. Correlates log lines to a person without
    # ever putting the address in a log; the address itself stays inside ``raw_payload``.
    contact_email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Two timestamps, not one, because they answer different questions. ``received_at`` is
    # when the submission reached the ingest API; ``created_at`` is when this row was
    # written, which the worker may do much later after an SQS retry. Their gap is queue
    # latency, and "how stale was this lead when sales saw it?" is a real question.
    received_at: Mapped[dt.datetime] = _created_at()
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # The idempotency guarantee. SQS delivers at least once; without this the worker
        # would create a second lead for a redelivered submission and sales would be
        # emailed twice. #16's upsert_lead() resolves conflicts against this constraint.
        UniqueConstraint("tenant_id", "submission_id", name="uq_leads_tenant_id_submission_id"),
        # Per-tenant recent-leads listing: WHERE tenant_id = ? ORDER BY received_at DESC.
        # The unique constraint above cannot serve it — its second column is not a date.
        Index("ix_leads_tenant_id_received_at", "tenant_id", "received_at"),
        CheckConstraint("submission_id <> ''", name="submission_id_not_blank"),
    )


class Assessment(Base):
    """One qualification run over one lead.

    ``tier`` and ``total_score`` are stored here because they are *computed in Python* from
    the dimension scores and the tenant's thresholds, then recorded. That is invariant 2
    working as intended: the model's output schema has no tier — this table is the audit
    trail of what code decided, which is exactly what the feedback loop needs to query.
    """

    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    lead_id: Mapped[uuid.UUID] = _lead_fk()
    created_at: Mapped[dt.datetime] = _created_at()

    # --- what code decided ------------------------------------------------------------
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    total_score: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- what the model returned ------------------------------------------------------
    dimension_scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[decimal.Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    missing_information: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # --- how it was produced ----------------------------------------------------------
    # Answers "did last Tuesday's prompt change make things worse?" without a migration.
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- what it cost -----------------------------------------------------------------
    # Per-tenant usage metering is then a SUM, not a later migration.
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cost_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        # The feedback-loop analytics query (plan §4): "every lead scored hot last month
        # the rep marked bad, grouped by industry" filters assessments by tenant and tier
        # over a date window before joining feedback. This composite serves that directly.
        Index("ix_assessments_tenant_id_tier_created_at", "tenant_id", "tier", "created_at"),
        # A lead's assessment history — the join side of the query above, and the
        # "show me this lead" screen.
        Index("ix_assessments_lead_id", "lead_id"),
        CheckConstraint("tier IN ('hot', 'warm', 'cold', 'disqualified')", name="tier_known"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_a_probability"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_creation_tokens >= 0 AND cost_usd >= 0 AND latency_ms >= 0",
            name="usage_is_non_negative",
        ),
    )


class RoutingEvent(Base):
    """A dispatch attempt: what code decided to do with a lead, and what happened.

    Invariant 3 — a lead is never silently dropped — is only auditable if every outcome,
    including a suppression, leaves a row here.
    """

    __tablename__ = "routing_events"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    lead_id: Mapped[uuid.UUID] = _lead_fk()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # e.g. the SES message id, so a delivery complaint can be traced back to a lead.
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # "What happened to this lead?" — the audit trail for one lead.
        Index("ix_routing_events_lead_id", "lead_id"),
        # Per-tenant dispatch log, newest first (ops screens, delivery incident triage).
        Index("ix_routing_events_tenant_id_created_at", "tenant_id", "created_at"),
    )


class Feedback(Base):
    """A human's verdict on a routed lead. The training signal for rubric tuning."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    lead_id: Mapped[uuid.UUID] = _lead_fk()
    rater: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # Join side of the feedback-loop analytics query.
        Index("ix_feedback_lead_id", "lead_id"),
        # ... and its filter side: WHERE tenant_id = ? AND verdict = 'bad'
        # AND created_at >= now() - interval '1 month'.
        Index("ix_feedback_tenant_id_verdict_created_at", "tenant_id", "verdict", "created_at"),
        CheckConstraint("verdict IN ('good', 'bad', 'unsure')", name="verdict_known"),
    )
