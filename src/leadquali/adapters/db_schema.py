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
    BigInteger,
    CheckConstraint,
    Date,
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
    "DEFAULT_QUOTA_ALERT_FRACTION",
    "DEFAULT_RATE_LIMIT_BURST",
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "ESCALATION_REASONS",
    "LEAD_STATUSES",
    "ROUTING_ACTIONS",
    "TENANT_SLUG_SQL_PATTERN",
    "TENANT_STATUSES",
    "Assessment",
    "Base",
    "Feedback",
    "GoldenPromotion",
    "Lead",
    "RoutingEvent",
    "Tenant",
    "TenantApiKey",
    "TenantConfigVersion",
    "UsageDaily",
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

TENANT_STATUSES: tuple[str, ...] = ("active", "suspended", "disabled")
"""Mirrors ``leadquali.api.signing.TENANT_STATUSES``. Only ``active`` may ingest; the
difference between ``suspended`` (temporary, e.g. non-payment) and ``disabled`` (gone) is
policy rather than mechanism, and the ingest path treats them the same."""

TENANT_SLUG_SQL_PATTERN: str = "^[a-z0-9][a-z0-9_-]{0,62}$"
"""``leadquali.domain.tenant_config.TENANT_ID_PATTERN``, restated as a SQL literal.

Duplicated for the same reason the vocabularies above are: a CHECK constraint has to be a
literal in the DDL, and a migration must keep working when the domain moves on.
``tests/unit/test_db_schema.py`` pins the two together."""

DEFAULT_RATE_LIMIT_PER_MINUTE: int = 60
"""One lead per second, sustained. Comfortably above what an honest web form produces and
low enough that a runaway integration is throttled rather than billed for."""

DEFAULT_RATE_LIMIT_BURST: int = 10
"""How far above the sustained rate a tenant may spike — a marketing email landing at 9am
puts a handful of submissions in the same second, and refusing those would lose leads."""

DEFAULT_QUOTA_ALERT_FRACTION: str = "0.80"
"""How much of a monthly plan may be used before somebody is told (#33), as a SQL literal.

Mirrors ``leadquali.app.metering.DEFAULT_QUOTA_ALERT_FRACTION``; ``tests/unit/test_db_schema.py``
pins the two together. A fraction rather than a count so that it survives a plan change:
raising a customer's quota should not silently move their alert to 95% of the new one."""


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
    # The external identity, and the only one anything outside the database uses: it is
    # what a form sends in X-LeadQuali-Tenant and what an operator types. `id` is
    # `uuid5(TENANT_ID_NAMESPACE, slug)` (leadquali.app.tenant_ids), which the *service*
    # enforces on create because a database cannot compute a uuid5 in a CHECK. Before #31
    # the slug was only recoverable from `icp_config->>'tenant_id'`, so a config rewrite
    # could silently orphan every key and every lead the tenant owned.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    # ICP description, dimension weights, tier thresholds and routing rules. Invariant 1:
    # the rubric is tenant configuration, never code, so this column is not optional — and
    # deliberately has *no* server default. A default of '{}' would let a tenant be created
    # with no rubric: the row exists, every config load rejects it, and the failure surfaces
    # at 3am against live traffic instead of at the insert that caused it. Seed a tenant
    # with `scripts/seed.py`, which supplies a real config.
    icp_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Secrets Manager ARN for the webhook HMAC secret — a reference, not the secret.
    hmac_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-tenant throttle, enforced by api/ratelimit.TenantRateLimiter. On the row rather
    # than in an API Gateway usage plan: a usage plan is keyed by a *second* credential the
    # customer would have to embed in their page, the account default caps the customer
    # count at 300, and provisioning one is a control-plane call in the middle of
    # onboarding. See docs/tenant-onboarding.md for the whole argument.
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_RATE_LIMIT_PER_MINUTE))
    )
    rate_limit_burst: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_RATE_LIMIT_BURST))
    )
    # The plan allowance, in *billable* leads per calendar month (#33). NULL means
    # unlimited, and that is the default on purpose: a quota that appeared by accident
    # would start warning somebody about a customer who never agreed to one. Nothing in
    # the system refuses a lead because of this column — invariant 3 — it exists so that
    # `usagectl quota` and a CloudWatch metric can prompt a commercial conversation.
    monthly_lead_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # How much of that allowance may be used before the warning fires.
    quota_alert_fraction: Mapped[decimal.Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text(DEFAULT_QUOTA_ALERT_FRACTION)
    )
    created_at: Mapped[dt.datetime] = _created_at()
    # Touched by every admin write. "When did this tenant's rubric last change?" is the
    # first question after a routing surprise, and without this column the only answer is
    # a CloudTrail search.
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # The slug is the join between the outside world and this row, so it is unique and
        # shape-checked in the database as well as in TenantConfig: a row inserted by psql
        # during an incident has to be as well-formed as one the service wrote.
        UniqueConstraint("slug", name="uq_tenants_slug"),
        CheckConstraint(f"slug ~ '{TENANT_SLUG_SQL_PATTERN}'", name="slug_is_a_slug"),
        CheckConstraint(_sql_in("status", TENANT_STATUSES), name="status_known"),
        CheckConstraint(
            "rate_limit_per_minute > 0 AND rate_limit_burst > 0", name="rate_limits_are_positive"
        ),
        # A quota of zero would mean "this customer may send no leads", which is not a
        # plan — it is a suspension, and there is a status column for that.
        CheckConstraint(
            "monthly_lead_quota IS NULL OR monthly_lead_quota > 0",
            name="monthly_lead_quota_is_positive",
        ),
        # (0, 1]: an alert at 0% would fire on the first lead of every month, and one above
        # 100% could never fire at all — a setting that silently does nothing is worse than
        # one the database refuses.
        CheckConstraint(
            "quota_alert_fraction > 0 AND quota_alert_fraction <= 1",
            name="quota_alert_fraction_is_a_fraction",
        ),
    )


class TenantApiKey(Base):
    """One issued ingest API key, stored as a hash of its secret half and nothing more.

    A tenant may hold several rows at once, which is what makes rotation a non-event: the
    new key is issued, the old row gets an ``expires_at`` a week out, the customer redeploys
    their form whenever they like, and nothing is refused in between.

    ``key_id`` is the clear-text lookup handle carried inside the key itself (see
    :mod:`leadquali.app.api_keys`). It is not a secret, it is uniquely indexed, and it is
    what makes an argon2 verification affordable on the request path: the row is found by
    an indexed read, and the KDF runs only for a caller who already holds a real handle.

    ``key_hash`` is an encoded argon2id string over the key's **secret half only**. The key
    itself is shown once, at issue, and exists nowhere in this system afterwards — which is
    the acceptance criterion "keys are unrecoverable from the database", stated as a schema
    fact rather than as a promise.
    """

    __tablename__ = "tenant_api_keys"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # CASCADE, unlike `leads`: a key is not an audit record. #37's erasure deletes the
        # tenant, and leaving credentials behind that authenticate against nothing would be
        # the one kind of orphan row that is a security problem rather than a tidiness one.
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    # `lq_live_<key_id>`: everything about the key that is not secret, so a listing can show
    # an operator which key a customer is quoting without ever holding the key.
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Free text from whoever issued it, e.g. "acme marketing site". Optional, because a key
    # with no label is still a key, and refusing to issue one over a missing note would be
    # the sort of friction that gets worked around with a shared key.
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()
    # Set by rotation: the end of the overlap window during which both keys work.
    expires_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Set by revocation, and effective on the very next request: the row is read fresh from
    # Postgres every time, and nothing about it is cached anywhere.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Best-effort and deliberately coarse; written by
    # store_tenants.PostgresIngestCredentials._touch. A write per request would
    # put a row-level lock contended by every concurrent request for the same key on the
    # hot path, to answer a question nobody asks to the minute.
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        # The lookup. Unique across every tenant, because a key names its own row and two
        # rows answering to one handle would make "whose key is this" depend on scan order.
        UniqueConstraint("key_id", name="uq_tenant_api_keys_key_id"),
        # The listing: WHERE tenant_id = ? ORDER BY created_at DESC.
        Index("ix_tenant_api_keys_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint("key_id <> '' AND key_hash <> ''", name="key_material_not_blank"),
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


class UsageDaily(Base):
    """One tenant's usage for one UTC calendar day: the billing rollup (#33).

    Everything in here is **derived**. Every column can be recomputed from ``leads`` and
    ``assessments``, and ``PostgresMeteringStore.rollup_day`` does exactly that and
    replaces the whole row. The table exists so that a billing read
    for "September, tenant acme" is a range scan over thirty small rows instead of an
    aggregate over every assessment the tenant has ever had — a query whose cost otherwise
    grows forever while the answer stays the same size.

    Three decisions are load-bearing.

    **The primary key is ``(tenant_id, usage_date)``, not a surrogate id.** The natural key
    *is* the identity of the row: a second row for the same tenant-day is not a new fact,
    it is a bug, and a surrogate key would let two of them coexist while every read summed
    both. It is also the conflict target the idempotent upsert needs.

    **The day is a UTC calendar day**, for every tenant, wherever they are. A billing job
    that let a tenant in Sydney and a tenant in California each define "yesterday" would
    double-count one of them at every month boundary.

    **The token counters are ``bigint``.** ``assessments.input_tokens`` is an ``integer``
    and is right to be — no single call approaches 2^31 — but a busy tenant passes two
    billion input tokens inside a year, and a rollup that silently overflows would be
    discovered on an invoice.

    ``ON DELETE CASCADE`` to ``tenants``, unlike ``leads``, which restricts: this is a
    derived table, so losing it with the tenant destroys nothing that could not be
    recomputed, and the audit trail it would otherwise block the deletion of is in
    ``assessments`` where it belongs.
    """

    __tablename__ = "usage_daily"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usage_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    # --- what happened, in three deliberately different numbers -----------------------
    # `leads_ingested` counts submissions stored; `leads_assessed` counts attempts to
    # qualify them; `leads_billable` counts the attempts that actually cost us tokens.
    # The gap between the first and the third is the deterministic spam pre-filter, and
    # it is not billed. See leadquali.app.metering and docs/metering-and-billing.md.
    leads_ingested: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    leads_assessed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    leads_billable: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # A subset of `leads_assessed`, and mostly of `leads_billable` too: a refusal is an
    # HTTP 200 that Anthropic charges for. Kept because "how much of what we billed was a
    # failure?" is the first question of any billing dispute.
    assessments_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # --- what it cost -----------------------------------------------------------------
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Numeric(14, 6), two digits wider than `assessments.cost_usd`: this column holds a sum
    # of those, and a scale that fits one call does not necessarily fit a day of them.
    cost_usd: Mapped[decimal.Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, server_default=text("0")
    )

    # When this row was last recomputed. Not "when the day happened" — that is
    # `usage_date` — but the answer to "is this rollup stale?", which is the only question
    # an operator asks of a derived table.
    computed_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        # Every counter is a count of rows or of tokens. A negative one means the rollup
        # arithmetic is wrong, and it is far better to fail the write than to bill from it.
        CheckConstraint(
            "leads_ingested >= 0 AND leads_assessed >= 0 AND leads_billable >= 0"
            " AND assessments_failed >= 0 AND input_tokens >= 0 AND output_tokens >= 0"
            " AND cache_read_tokens >= 0 AND cache_creation_tokens >= 0 AND cost_usd >= 0",
            name="usage_is_non_negative",
        ),
        # Billing reads a period for one tenant: WHERE tenant_id = ? AND usage_date
        # BETWEEN ? AND ?. The primary key already serves that exactly, leading column
        # first, so there is deliberately no second index here.
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
        # One verdict per rater per lead, so a second click is an UPDATE and not a second
        # row. Mail clients prefetch links, phones register double taps, and a rep is
        # allowed to change their mind — without this, "how often does sales disagree with
        # the model" would measure how many times a link was clicked. It is also the
        # conflict target `PostgresFeedbackStore.record_feedback` upserts against, which is
        # what makes that a single statement and therefore safe under a race. Added by #19
        # with migration `20260903_feedback_one_verdict_per_rater`.
        UniqueConstraint(
            "tenant_id", "lead_id", "rater", name="uq_feedback_tenant_id_lead_id_rater"
        ),
        # Join side of the feedback-loop analytics query.
        Index("ix_feedback_lead_id", "lead_id"),
        # ... and its filter side: WHERE tenant_id = ? AND verdict = 'bad'
        # AND created_at >= now() - interval '1 month'.
        Index("ix_feedback_tenant_id_verdict_created_at", "tenant_id", "verdict", "created_at"),
        CheckConstraint("verdict IN ('good', 'bad', 'unsure')", name="verdict_known"),
    )


class TenantConfigVersion(Base):
    """One edit to one tenant's rubric: the whole config after it, and who made it (#36).

    Invariant 1 makes the rubric configuration rather than code, which is what lets a
    customer be onboarded without a deploy — and which also means the single
    highest-risk action in the product has no build, no code review and no ``git revert``
    behind it. This table is all three.

    **Full snapshots, not patches.** ``config`` is the complete document as it stood after
    this change. A chain of patches is one bad apply away from being unreplayable, and the
    entire point of the table is that a bad rubric can be undone at 3am by somebody who is
    not the person who wrote it. The storage cost is a few kilobytes per edit of a document
    that changes a handful of times a year.

    **``version`` is allocated from this table, inside the writing transaction**, against
    ``UNIQUE (tenant_id, version)``. A counter held in Python would hand the same number to
    two admin processes; the constraint is what settles the race.

    **Nothing ever deletes or updates a row here.** Reverting appends a new version whose
    ``config`` is an old one's — the history only answers "who changed this, and to what?"
    if it is append-only, and a revert is itself a change somebody made and should have to
    account for.

    ``ON DELETE CASCADE`` to ``tenants``, like ``tenant_api_keys`` and unlike ``leads``:
    this is a record *about* the customer's configuration and it is meaningless without
    them, so #37's deliberate erasure takes it along rather than being blocked by it.
    """

    __tablename__ = "tenant_config_versions"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Monotonic per tenant, starting at 1. The migration seeds version 1 for every existing
    # tenant from its current `icp_config`, so the first real edit has something to diff
    # against rather than a blank page.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The **full** config after this change. Same shape as `tenants.icp_config`, and
    # validated by the same `TenantConfig` before it is written.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The staff subject from the admin session — an opaque username, never an address
    # (invariant 5), and never blank: an audit row with no author is not an audit row.
    # `'migration'` on the rows the seeding migration writes, so the answer to "who did
    # this?" is visibly not a person rather than absent.
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[dt.datetime] = _created_at()
    # Why, in the operator's words. Optional: refusing to save a rubric fix during an
    # incident over a missing note is the sort of friction that gets worked around by
    # editing the row in psql, which is the one outcome this table exists to prevent.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The identity of a version, and the conflict target that settles two operators
        # saving at once: one of them gets a constraint violation and retries against the
        # number the other took.
        UniqueConstraint(
            "tenant_id", "version", name="uq_tenant_config_versions_tenant_id_version"
        ),
        # The history screen: WHERE tenant_id = ? ORDER BY version DESC. The unique
        # constraint above already serves it, leading column first, so there is no second
        # index here.
        CheckConstraint("version > 0", name="version_is_positive"),
        CheckConstraint("changed_by <> ''", name="changed_by_not_blank"),
    )


class GoldenPromotion(Base):
    """One lead promoted into the eval golden set (#22), recorded so it happens once (#36).

    The golden set itself is ``tests/evals/golden_leads.jsonl`` — a file in a git
    repository, appended to by a human, because the labels in it are human judgements and
    because a Lambda's filesystem is read-only. So this table does not hold the golden
    case: it holds the **decision** to promote, plus the human label that goes with it, and
    the admin renders the JSONL line from those and the lead on demand.

    That split is deliberate rather than incidental. A copy of the (pseudonymised) payload
    here would be a second home for data derived from ``leads.raw_payload``, which
    invariant 5 says is the one place personal data lives and which #37's retention job
    purges. Rendering on demand means the promoted case is always derived from the row the
    retention policy governs, and disappears with it.

    ``UNIQUE (tenant_id, lead_id)`` is what makes promotion idempotent: a rep's second
    click, a double-tap or a refreshed confirmation page must not add the same lead to the
    golden set twice, because the eval harness would then weigh that one lead twice.
    """

    __tablename__ = "golden_promotions"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    lead_id: Mapped[uuid.UUID] = _lead_id()
    # The stable slug the golden case is filed under, e.g. `real_acme_0f3c9a12`. Unique
    # across every tenant because it is a case id in one shared file, and two cases
    # answering to one id would make a failing eval unattributable.
    case_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The tier the human says this lead should have been. The label, not the model's answer
    # — a golden case whose expectation came from the thing under test measures nothing.
    expected_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    # The staff subject who labelled it, used as #22's `labeler` handle. An opaque
    # username, never an address: the golden set outlives the raw payload.
    promoted_by: Mapped[str] = mapped_column(Text, nullable=False)
    # Why this tier and not the adjacent one, in the labeller's words. #22 requires at
    # least 20 characters and says why: in six months this sentence is what tells somebody
    # whether the label or the model is at fault.
    note: Mapped[str] = mapped_column(Text, nullable=False)
    promoted_at: Mapped[dt.datetime] = _created_at()
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        _owned_lead_fk(),
        # Idempotency. Promoting the same lead twice must not add it twice.
        UniqueConstraint("tenant_id", "lead_id", name="uq_golden_promotions_tenant_id_lead_id"),
        UniqueConstraint("case_id", name="uq_golden_promotions_case_id"),
        # The review screen needs "has this lead already been promoted?" for a page of
        # leads at a time, and the export needs this tenant's promotions newest first.
        Index("ix_golden_promotions_tenant_id_promoted_at", "tenant_id", "promoted_at"),
        CheckConstraint(
            "expected_tier IN ('hot', 'warm', 'cold', 'disqualified')",
            name="expected_tier_known",
        ),
        CheckConstraint("case_id <> '' AND promoted_by <> ''", name="promotion_fields_not_blank"),
        # #22 refuses a label whose notes are shorter than this, and a promotion staged
        # here would then be refused at the point it was appended to the file — after the
        # operator had been told it worked.
        CheckConstraint("length(note) >= 20", name="note_is_a_rationale"),
    )
