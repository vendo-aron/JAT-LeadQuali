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
* Denormalised ``tenant_id`` is only safe if the database keeps it honest. ``leads`` carries
  ``UNIQUE (tenant_id, id)`` and every child table references it with a **composite**
  ``FOREIGN KEY (tenant_id, lead_id) REFERENCES leads (tenant_id, id)``. Two independent
  foreign keys would each pass while disagreeing with each other, letting tenant A's
  assessment be filed under tenant B — and because ``tenant_id`` is the only filter every
  repository method applies (invariant 4), the tenant filter would still return it and
  nothing would ever surface the mix-up.
* ``leads`` carries ``UNIQUE (tenant_id, submission_id)``. SQS is at-least-once, so the
  worker will see the same submission twice; that constraint is the idempotency guarantee.
* The ``tenants`` foreign key is ``ON DELETE RESTRICT``, the ``leads`` one ``ON DELETE
  CASCADE``. Deleting a lead is a scoped act and taking its assessment, routing and
  feedback rows with it is correct; deleting a *tenant* would otherwise destroy the entire
  invariant-3 audit trail as a side effect of one mistyped ``WHERE``. Erasure is deliberate:
  #37's purge routine deletes the tenant's leads first, then the tenant.
* An assessment records a **failure** as faithfully as a success. ``status`` says which,
  and a CHECK constraint keeps the two shapes from being mixed up. Invariant 3 makes an API
  error, a refusal, a timeout and a parse error first-class outcomes, so the schema has to
  have somewhere to put them; a row that cannot be written is a lead silently dropped.
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
    ForeignKeyConstraint,
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
    "ASSESSMENT_STATUSES",
    "ESCALATION_REASONS",
    "LEAD_STATUSES",
    "ROUTING_ACTIONS",
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

# --- vocabularies the database enforces -----------------------------------------------
#
# These mirror enums that live in ``leadquali.domain`` (#7). They are duplicated as plain
# strings on purpose: the adapters layer may not reach into the domain for a *migration*
# (the migration has to keep working when the domain moves on), and a CHECK constraint has
# to be a literal in the DDL anyway. ``tests/unit/test_db_schema.py`` pins the values, so a
# domain change that is not mirrored here fails a test rather than silently diverging.

LEAD_STATUSES: tuple[str, ...] = ("received", "qualified", "routed", "failed")
"""Lead lifecycle. Each state is produced by a documented pipeline step (plan §3):
``received`` by ingest, ``qualified`` once an assessment exists, ``failed`` when one could
not be produced, ``routed`` once a dispatch has been attempted."""

ASSESSMENT_STATUSES: tuple[str, ...] = ("ok", "failed")
"""Whether the model returned a usable assessment at all."""

ESCALATION_REASONS: tuple[str, ...] = (
    "low_confidence",
    "model_refusal",
    "parse_error",
    "api_error",
    "timeout",
)
"""Mirrors ``leadquali.domain.EscalationReason``. Note ``low_confidence`` accompanies a
*successful* assessment — the model answered, code just did not trust the answer — so an
escalation reason is not by itself evidence of a failure."""

ROUTING_ACTIONS: tuple[str, ...] = ("email_sales", "escalate_human", "suppress")
"""Mirrors ``leadquali.domain.Action``."""


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` from a fixed vocabulary.

    The values are module constants, never user input; this only exists so the vocabulary
    is written down once instead of once per constraint.
    """
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class Base(DeclarativeBase):
    """Declarative base carrying the project's metadata and naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


metadata = Base.metadata
"""The metadata Alembic migrates against."""


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=_UUID_PK)


def _tenant_id() -> Mapped[uuid.UUID]:
    """The NOT NULL tenant discriminator every table carries (invariant 4).

    On the child tables this column deliberately has **no** foreign key of its own: it is
    validated by the composite ``(tenant_id, lead_id)`` constraint below, which is strictly
    stronger — it proves the tenant exists *and* that it is the lead's tenant.
    """
    return mapped_column(UUID(as_uuid=True), nullable=False)


def _lead_id() -> Mapped[uuid.UUID]:
    """The NOT NULL lead reference. Its foreign key is composite; see :func:`_owned_lead_fk`."""
    return mapped_column(UUID(as_uuid=True), nullable=False)


def _owned_lead_fk() -> ForeignKeyConstraint:
    """``(tenant_id, lead_id) -> leads (tenant_id, id) ON DELETE CASCADE``.

    One constraint doing two jobs: the lead exists, and it belongs to the same tenant as
    the row pointing at it. The second half is the one that cannot be expressed with two
    independent foreign keys, and it is the half that stops a worker bug from writing
    tenant A's assessment against tenant B.
    """
    return ForeignKeyConstraint(
        ["tenant_id", "lead_id"],
        ["leads.tenant_id", "leads.id"],
        ondelete="CASCADE",
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
    # the rubric is tenant configuration, never code, so this column is not optional — and
    # deliberately has *no* server default. A default of '{}' would let a tenant be created
    # with no rubric: the row exists, every config load rejects it, and the failure surfaces
    # at 3am against live traffic instead of at the insert that caused it. Seed a tenant
    # with `scripts/seed.py`, which supplies a real config.
    icp_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
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
    # The only direct reference to ``tenants``. ON DELETE RESTRICT: removing a customer has
    # to be a deliberate purge (#37), not a side effect of a DELETE that matched more rows
    # than its author expected.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
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
        # Redundant against the primary key on its own, and that is not why it is here: it
        # is the target every child table's composite (tenant_id, lead_id) foreign key
        # needs. Postgres requires a unique constraint on the referenced columns.
        UniqueConstraint("tenant_id", "id", name="uq_leads_tenant_id_id"),
        # Per-tenant recent-leads listing: WHERE tenant_id = ? ORDER BY received_at DESC.
        # The unique constraint above cannot serve it — its second column is not a date.
        Index("ix_leads_tenant_id_received_at", "tenant_id", "received_at"),
        CheckConstraint("submission_id <> ''", name="submission_id_not_blank"),
        CheckConstraint(_sql_in("status", LEAD_STATUSES), name="status_known"),
    )


class Assessment(Base):
    """One qualification run over one lead — successful or not.

    ``tier`` and ``total_score`` are stored here because they are *computed in Python* from
    the dimension scores and the tenant's thresholds, then recorded. That is invariant 2
    working as intended: the model's output schema has no tier — this table is the audit
    trail of what code decided, which is exactly what the feedback loop needs to query.

    A run that produced no model output at all — API error, refusal, timeout, parse error —
    is recorded here too, with ``status = 'failed'`` and an ``escalation_reason``. Invariant
    3 says such a lead escalates to a human and is never dropped, and "never dropped" is
    only auditable if the attempt leaves a row. The model-output columns are therefore
    nullable, with a CHECK constraint making them all-present-or-all-absent so that a
    half-written assessment cannot masquerade as a real one.
    """

    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    lead_id: Mapped[uuid.UUID] = _lead_id()
    created_at: Mapped[dt.datetime] = _created_at()

    # --- did this run produce anything? -----------------------------------------------
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ok'"))
    # Why a human was pulled in. Set on every failure, and also on a *successful* assessment
    # the confidence gate rejected (`low_confidence`) — which is why it is not simply
    # "the failure reason".
    escalation_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- what code decided (absent when status = 'failed') ----------------------------
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Float, not integer: the domain's thresholds and weighted_total (#9) are floats on a
    # 0-100 scale rounded to 2dp, so a tenant threshold of 55.1 has to be representable.
    # Numeric rather than double precision keeps SUM/AVG over the column exact and matches
    # how `confidence` and `cost_usd` are already stored.
    total_score: Mapped[decimal.Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # --- what the model returned (absent when status = 'failed') ----------------------
    dimension_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[decimal.Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Not part of the all-or-nothing group: an empty list is a truthful reading of "no
    # missing information was reported", which is as true of a failed run as of a clean one.
    missing_information: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # --- how it was produced ----------------------------------------------------------
    # Answers "did last Tuesday's prompt change make things worse?" without a migration.
    # Recorded even for a failure: "which model version started refusing?" is the question
    # an incident actually asks.
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- what it cost -----------------------------------------------------------------
    # Per-tenant usage metering is then a SUM, not a later migration. A failed call still
    # burned input tokens and still took time, so these stay NOT NULL with a zero default.
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
        _owned_lead_fk(),
        # The feedback-loop analytics query (plan §4): "every lead scored hot last month
        # the rep marked bad, grouped by industry" filters assessments by tenant and tier
        # over a date window before joining feedback. This composite serves that directly.
        Index("ix_assessments_tenant_id_tier_created_at", "tenant_id", "tier", "created_at"),
        # Billing (#33) sums tokens and cost per tenant over a period. That query has no
        # tier predicate, so the index above cannot serve it — its second column is wrong.
        Index("ix_assessments_tenant_id_created_at", "tenant_id", "created_at"),
        # A lead's assessment history — the join side of the query above, and the
        # "show me this lead" screen.
        Index("ix_assessments_lead_id", "lead_id"),
        CheckConstraint("tier IN ('hot', 'warm', 'cold', 'disqualified')", name="tier_known"),
        CheckConstraint(_sql_in("status", ASSESSMENT_STATUSES), name="status_known"),
        CheckConstraint(
            f"escalation_reason IS NULL OR {_sql_in('escalation_reason', ESCALATION_REASONS)}",
            name="escalation_reason_known",
        ),
        CheckConstraint("total_score >= 0 AND total_score <= 100", name="total_score_in_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_a_probability"),
        # The shape rule. A successful assessment carries every model-output column and the
        # verdict code derived from them; a failed one carries none of them and must say
        # why. Without this, "status = 'ok' with a NULL reasoning" is representable and the
        # feedback loop silently averages over rows that never had an assessment.
        CheckConstraint(
            "(status = 'ok'"
            " AND dimension_scores IS NOT NULL AND extracted IS NOT NULL"
            " AND reasoning IS NOT NULL AND confidence IS NOT NULL"
            " AND tier IS NOT NULL AND total_score IS NOT NULL)"
            " OR (status = 'failed'"
            " AND dimension_scores IS NULL AND extracted IS NULL"
            " AND reasoning IS NULL AND confidence IS NULL"
            " AND tier IS NULL AND total_score IS NULL"
            " AND escalation_reason IS NOT NULL)",
            name="output_present_iff_ok",
        ),
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
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    lead_id: Mapped[uuid.UUID] = _lead_id()
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # e.g. the SES message id, so a delivery complaint can be traced back to a lead.
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        _owned_lead_fk(),
        # "What happened to this lead?" — the audit trail for one lead.
        Index("ix_routing_events_lead_id", "lead_id"),
        # Per-tenant dispatch log, newest first (ops screens, delivery incident triage).
        Index("ix_routing_events_tenant_id_created_at", "tenant_id", "created_at"),
        # "How many leads did we suppress last week?" is only answerable if the column
        # holds the three actions the domain defines and not a fourth spelling of one.
        CheckConstraint(_sql_in("action", ROUTING_ACTIONS), name="action_known"),
    )


class Feedback(Base):
    """A human's verdict on a routed lead. The training signal for rubric tuning."""

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    lead_id: Mapped[uuid.UUID] = _lead_id()
    # An **opaque subject id** — an internal user id, or a hash of one. Deliberately not an
    # email address and not a display name: this column is grouped by and joined in the
    # analytics of plan §4, so whatever goes in it is retained for as long as the feedback
    # is useful, which is longer than the raw lead payload is kept (#37). Storing a rep's
    # address here would put personal data outside `leads.raw_payload`, which is the one
    # place invariant 5 allows it to live. The database cannot tell an opaque id from an
    # address, so the writer (#25) owns this; see docs/local-database.md.
    rater: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        _owned_lead_fk(),
        # Join side of the feedback-loop analytics query.
        Index("ix_feedback_lead_id", "lead_id"),
        # ... and its filter side: WHERE tenant_id = ? AND verdict = 'bad'
        # AND created_at >= now() - interval '1 month'.
        Index("ix_feedback_tenant_id_verdict_created_at", "tenant_id", "verdict", "created_at"),
        CheckConstraint("verdict IN ('good', 'bad', 'unsure')", name="verdict_known"),
    )
