"""Offline schema invariants, asserted from SQLAlchemy metadata alone.

These run without Docker and without a database. They exist so that the default test
suite still proves the product invariants that the schema is responsible for: multi-tenancy
on every table (CLAUDE.md invariant 4), the integrity that makes a denormalised
``tenant_id`` trustworthy, the idempotency key on ``leads``, the recordability of a failed
assessment (invariant 3), and the absence of any raw-email column (invariant 5).
"""

from __future__ import annotations

import decimal

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from leadquali.adapters.db_schema import (
    ASSESSMENT_STATUSES,
    ESCALATION_REASONS,
    LEAD_STATUSES,
    ROUTING_ACTIONS,
    Assessment,
    Base,
    Feedback,
    Lead,
    RoutingEvent,
    Tenant,
)

EXPECTED_TABLES = {"tenants", "leads", "assessments", "routing_events", "feedback"}

CHILD_TABLES = ("assessments", "routing_events", "feedback")
"""The tables that hang off a lead, and therefore off a tenant through it."""

# Every column this schema has, and what class of personal data it is allowed to hold.
#
#   "none"   — cannot contain personal data at all (ids, enums, counters, timestamps).
#   "hashed" — a one-way digest, safe to log and to keep after the raw data is purged.
#   "raw"    — free-form content that may contain anything a form submitter typed.
#
# Invariant 5 is a statement about *what is stored where*, so this is asserted as a
# complete inventory rather than as a search for suspicious column names. A substring
# check for "email" only catches a column whose author named it honestly; it would pass
# `rater`, `contact_details` or `notes_from_crm` without a murmur. Pinning the inventory
# means adding any column at all fails this test until someone has classified it, and the
# `raw` bucket having exactly one member is the property #37's retention job depends on.
COLUMN_PII_POLICY: dict[tuple[str, str], str] = {
    ("tenants", "id"): "none",
    ("tenants", "name"): "none",
    ("tenants", "status"): "none",
    ("tenants", "icp_config"): "none",
    ("tenants", "api_key_hash"): "hashed",
    ("tenants", "hmac_secret_ref"): "none",
    ("tenants", "created_at"): "none",
    ("leads", "id"): "none",
    ("leads", "tenant_id"): "none",
    ("leads", "submission_id"): "none",
    # The one and only place a lead's personal data lives.
    ("leads", "raw_payload"): "raw",
    ("leads", "source"): "none",
    ("leads", "status"): "none",
    ("leads", "contact_email_hash"): "hashed",
    ("leads", "received_at"): "none",
    ("leads", "created_at"): "none",
    ("assessments", "id"): "none",
    ("assessments", "tenant_id"): "none",
    ("assessments", "lead_id"): "none",
    ("assessments", "created_at"): "none",
    ("assessments", "status"): "none",
    ("assessments", "escalation_reason"): "none",
    ("assessments", "tier"): "none",
    ("assessments", "total_score"): "none",
    ("assessments", "dimension_scores"): "none",
    # Model-derived facts about the *company*, constrained by #7's ExtractedFacts schema —
    # not a copy of the submitter's contact details.
    ("assessments", "extracted"): "none",
    ("assessments", "reasoning"): "none",
    ("assessments", "confidence"): "none",
    ("assessments", "missing_information"): "none",
    ("assessments", "model_id"): "none",
    ("assessments", "prompt_version"): "none",
    ("assessments", "effort"): "none",
    ("assessments", "input_tokens"): "none",
    ("assessments", "output_tokens"): "none",
    ("assessments", "cache_read_tokens"): "none",
    ("assessments", "cache_creation_tokens"): "none",
    ("assessments", "cost_usd"): "none",
    ("assessments", "latency_ms"): "none",
    ("routing_events", "id"): "none",
    ("routing_events", "tenant_id"): "none",
    ("routing_events", "lead_id"): "none",
    ("routing_events", "action"): "none",
    # A configured sales inbox from TenantConfig — the tenant's own address, never the
    # lead's, and not submitter-controlled.
    ("routing_events", "destination"): "none",
    ("routing_events", "dispatched_at"): "none",
    ("routing_events", "provider_message_id"): "none",
    ("routing_events", "created_at"): "none",
    ("feedback", "id"): "none",
    ("feedback", "tenant_id"): "none",
    ("feedback", "lead_id"): "none",
    # An opaque subject id (an internal user id, or a hash of one) — deliberately not the
    # rep's email address or name. See the column comment in db_schema.py.
    ("feedback", "rater"): "none",
    ("feedback", "verdict"): "none",
    ("feedback", "notes"): "none",
    ("feedback", "created_at"): "none",
}


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _check_constraint_names(table_name: str) -> set[str]:
    # `Constraint.name` is typed as `str | _NoneName`, and the sentinel is not `None`, so
    # this filters on the type rather than on an identity check that would not narrow it.
    return {
        constraint.name
        for constraint in _table(table_name).constraints
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }


def _lead_ownership_fk(table_name: str) -> ForeignKeyConstraint | None:
    """The composite ``(tenant_id, lead_id)`` foreign key, if the table has one."""
    for constraint in _table(table_name).constraints:
        if isinstance(constraint, ForeignKeyConstraint) and tuple(
            column.name for column in constraint.columns
        ) == ("tenant_id", "lead_id"):
            return constraint
    return None


def test_metadata_declares_exactly_the_five_planned_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_model_classes_map_to_the_expected_table_names() -> None:
    """The names #16 imports. Renaming one of these is a breaking change for that issue."""
    for model, table_name in (
        (Tenant, "tenants"),
        (Lead, "leads"),
        (Assessment, "assessments"),
        (RoutingEvent, "routing_events"),
        (Feedback, "feedback"),
    ):
        assert model.__tablename__ == table_name
        # The class and the metadata entry are one object, so a repository written against
        # either sees the same columns.
        assert model.__table__ is _table(table_name)


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_every_table_carries_a_tenant_id(table_name: str) -> None:
    """Invariant 4: ``tenant_id`` on every table, from the first migration."""
    table = _table(table_name)
    if table_name == "tenants":
        # The tenant table is its own tenant scope: its primary key *is* the tenant id.
        assert "id" in table.c
        return
    column = table.c["tenant_id"]
    assert not column.nullable, f"{table_name}.tenant_id must be NOT NULL"


def test_leads_is_the_only_direct_reference_to_tenants() -> None:
    """And it restricts, so a tenant cannot be deleted out from under its data.

    ``ON DELETE CASCADE`` here would make ``DELETE FROM tenants WHERE id = ...`` destroy
    every lead, assessment, routing event and feedback row the customer ever had —
    including the invariant-3 audit trail — as a side effect of one over-broad ``WHERE``.
    Erasure is a deliberate operation (#37), so the database refuses to do it by accident.
    """
    foreign_keys = list(_table("leads").c["tenant_id"].foreign_keys)
    assert [fk.column.table.name for fk in foreign_keys] == ["tenants"]
    assert foreign_keys[0].ondelete == "RESTRICT"

    for table_name in CHILD_TABLES:
        # The composite key does put a ForeignKey on this column — pointing at
        # `leads.tenant_id`. What must not exist is a second, independent one to `tenants`,
        # because that is the one that would be satisfiable while the lead disagrees.
        referred = {fk.column.table.name for fk in _table(table_name).c["tenant_id"].foreign_keys}
        assert referred == {"leads"}, (
            f"{table_name}.tenant_id must reach tenants only through its lead; "
            f"an independent FK to {referred - {'leads'}} is what lets the two disagree"
        )


@pytest.mark.parametrize("table_name", CHILD_TABLES)
def test_a_child_row_is_tied_to_its_lead_and_that_lead_s_tenant(table_name: str) -> None:
    """The constraint that closes the cross-tenant hole.

    With two independent foreign keys, ``tenant_id`` and ``lead_id`` are each valid on
    their own while contradicting each other, so tenant A's assessment can be written
    against tenant B's lead. Because ``tenant_id`` is the only filter every repository
    method applies (invariant 4), that row then reads back cleanly under the wrong tenant
    and nothing in the system ever notices. One composite key states the real rule: the
    lead exists *and* it belongs to this tenant.
    """
    constraint = _lead_ownership_fk(table_name)
    assert constraint is not None, f"{table_name} has no composite (tenant_id, lead_id) FK"

    referred = [(element.column.table.name, element.column.name) for element in constraint.elements]
    assert referred == [("leads", "tenant_id"), ("leads", "id")]
    assert constraint.ondelete == "CASCADE"


def test_leads_can_be_the_target_of_the_composite_key() -> None:
    """Postgres requires a unique constraint on the referenced columns; without this the
    composite foreign keys above cannot be created at all."""
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in _table("leads").constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "id") in unique_column_sets


def test_leads_has_the_tenant_scoped_submission_idempotency_key() -> None:
    """The unique key that stops SQS at-least-once delivery from emailing sales twice."""
    unique_column_sets = {
        tuple(c.name for c in constraint.columns)
        for constraint in _table("leads").constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "submission_id") in unique_column_sets


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_no_table_stores_a_raw_contact_email(table_name: str) -> None:
    """Invariant 5: only ``contact_email_hash``; raw PII lives solely in ``raw_payload``."""
    for column in _table(table_name).c:
        assert column.name != "contact_email"
        assert "email" not in column.name or column.name == "contact_email_hash"


def test_every_column_is_classified_against_the_pii_policy() -> None:
    """Invariant 5, stated as a complete inventory rather than a name search.

    A column-name check passes anything named innocuously. This fails the moment a column
    is added or removed without someone deciding, in :data:`COLUMN_PII_POLICY`, whether it
    can hold personal data.
    """
    actual = {
        (table.name, column.name) for table in Base.metadata.tables.values() for column in table.c
    }
    assert actual == set(COLUMN_PII_POLICY), (
        "the column inventory changed; classify the new column(s) in COLUMN_PII_POLICY "
        "and check the result against CLAUDE.md invariant 5"
    )


def test_raw_payload_is_the_only_column_that_may_hold_personal_data() -> None:
    """What ``contact_email_hash`` is *for*, and what #37's retention job relies on:
    purging one column removes the personal data while the assessments survive."""
    raw = {key for key, policy in COLUMN_PII_POLICY.items() if policy == "raw"}
    assert raw == {("leads", "raw_payload")}
    assert set(COLUMN_PII_POLICY.values()) <= {"none", "hashed", "raw"}


def test_the_rater_is_an_opaque_subject_id_not_a_contact() -> None:
    """``feedback.rater`` is grouped by and joined in the plan §4 analytics, and outlives
    the raw lead payload. It holds an internal id, never an address or a display name — so
    it is classified as carrying no personal data, and that classification is the promise
    #25 has to keep when it writes the column."""
    assert COLUMN_PII_POLICY[("feedback", "rater")] == "none"
    assert not _table("feedback").c["rater"].nullable


def test_leads_hashes_the_contact_email() -> None:
    assert "contact_email_hash" in _table("leads").c
    assert "raw_payload" in _table("leads").c
    assert isinstance(_table("leads").c["raw_payload"].type, JSONB)


def test_assessments_records_everything_the_feedback_loop_and_billing_need() -> None:
    columns = set(_table("assessments").c.keys())
    assert {
        "model_id",
        "prompt_version",
        "effort",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "latency_ms",
        "confidence",
        "dimension_scores",
        "extracted",
        "missing_information",
        "reasoning",
        "tier",
        "total_score",
        "status",
        "escalation_reason",
    } <= columns


def test_a_failed_assessment_has_somewhere_to_go() -> None:
    """Invariant 3: an API error, refusal, timeout or parse error is a first-class outcome.

    If the model-output columns were NOT NULL, a lead whose assessment failed could not be
    written at all — and a lead that cannot be recorded is a lead silently dropped, which
    is the one thing the product promises never happens.
    """
    assessments = _table("assessments")
    for column_name in ("dimension_scores", "extracted", "reasoning", "confidence"):
        assert assessments.c[column_name].nullable, (
            f"assessments.{column_name} must be nullable so a failed run can be recorded"
        )
    # tier and total_score are code's verdict *about* that output. With no assessment there
    # is nothing to tier, and inventing one would be exactly the silent disqualification
    # invariant 3 forbids — so they are absent on a failure too.
    for column_name in ("tier", "total_score"):
        assert assessments.c[column_name].nullable

    assert not assessments.c["status"].nullable
    assert assessments.c["escalation_reason"].nullable
    # The constraint that stops the two shapes being mixed into a half-written row.
    assert "ck_assessments_output_present_iff_ok" in _check_constraint_names("assessments")


def test_total_score_is_a_two_decimal_number_on_a_zero_to_hundred_scale() -> None:
    """#9's ``weighted_total`` is a float rounded to 2dp, and tenant thresholds are floats.

    As an ``Integer`` this column could not represent a threshold of 55.1 at all — the
    stored score would silently disagree with the tier that was computed from it.
    """
    column = _table("assessments").c["total_score"]
    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale) == (5, 2)
    assert "ck_assessments_total_score_in_range" in _check_constraint_names("assessments")
    # Representable at the stored precision, which an Integer column is not.
    assert decimal.Decimal("55.10") == decimal.Decimal("55.1")


def test_the_tenant_rubric_has_no_usable_default() -> None:
    """Invariant 1 has teeth only if a tenant cannot exist without a rubric.

    ``server_default='{}'`` let a tenant be inserted with no config: the row exists, every
    config load rejects it, and the failure lands on the worker at 3am instead of on the
    insert that caused it.
    """
    icp_config = _table("tenants").c["icp_config"]
    assert icp_config.server_default is None
    assert not icp_config.nullable


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    [
        ("leads", "ck_leads_status_known"),
        ("routing_events", "ck_routing_events_action_known"),
        ("assessments", "ck_assessments_tier_known"),
        ("assessments", "ck_assessments_status_known"),
        ("assessments", "ck_assessments_escalation_reason_known"),
        ("tenants", "ck_tenants_status_known"),
        ("feedback", "ck_feedback_verdict_known"),
    ],
)
def test_every_enumerated_column_is_constrained(table_name: str, constraint_name: str) -> None:
    """A column holding one of a fixed set of words either says so or collects typos.

    ``leads.status`` and ``routing_events.action`` were the two that did not, while their
    neighbours did — and an unconstrained ``action`` makes "how many did we suppress?"
    quietly wrong the first time something writes ``"suppressed"``.
    """
    assert constraint_name in _check_constraint_names(table_name)


def test_the_enforced_vocabularies_match_the_domain() -> None:
    """These CHECK values duplicate enums in ``leadquali.domain`` (#7), which the adapters
    layer deliberately does not import into a migration. Pinning them here means a domain
    change that is not mirrored fails a test instead of drifting silently."""
    assert ROUTING_ACTIONS == ("email_sales", "escalate_human", "suppress")
    assert ESCALATION_REASONS == (
        "low_confidence",
        "model_refusal",
        "parse_error",
        "api_error",
        "timeout",
    )
    assert ASSESSMENT_STATUSES == ("ok", "failed")
    assert LEAD_STATUSES == ("received", "qualified", "routed", "failed")


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("tenants", "created_at"),
        ("leads", "received_at"),
        ("leads", "created_at"),
        ("assessments", "created_at"),
        ("routing_events", "created_at"),
        ("feedback", "created_at"),
    ],
)
def test_timestamps_are_timezone_aware_with_a_server_default(
    table_name: str, column_name: str
) -> None:
    column = _table(table_name).c[column_name]
    assert isinstance(column.type, TIMESTAMP)
    assert column.type.timezone is True, f"{table_name}.{column_name} must be timestamptz"
    assert column.server_default is not None


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_primary_keys_are_uuids_generated_by_the_server(table_name: str) -> None:
    primary_key = list(_table(table_name).primary_key.columns)
    assert [c.name for c in primary_key] == ["id"]
    assert primary_key[0].server_default is not None


def test_indexes_cover_the_queries_the_product_actually_runs() -> None:
    index_columns = {
        table.name: {tuple(c.name for c in index.columns) for index in table.indexes}
        for table in Base.metadata.tables.values()
    }
    # Per-tenant recent-leads listing.
    assert ("tenant_id", "received_at") in index_columns["leads"]
    # "Every lead scored hot last month the rep marked bad, grouped by industry".
    assert ("tenant_id", "tier", "created_at") in index_columns["assessments"]
    assert ("tenant_id", "verdict", "created_at") in index_columns["feedback"]
    # Billing (#33): SUM(tokens), SUM(cost_usd) per tenant over a period. No tier predicate,
    # so the tier index above cannot serve it — its second column is the wrong one.
    assert ("tenant_id", "created_at") in index_columns["assessments"]
    # Join paths from a lead to its history.
    assert ("lead_id",) in index_columns["feedback"]
    assert ("lead_id",) in index_columns["routing_events"]
    assert ("lead_id",) in index_columns["assessments"]


def test_child_rows_are_deleted_with_their_lead() -> None:
    """Deleting one lead is scoped and deliberate, so cascading from it is right — unlike
    cascading from a tenant, which is why that side restricts."""
    for table_name in CHILD_TABLES:
        constraint = _lead_ownership_fk(table_name)
        assert constraint is not None
        assert constraint.ondelete == "CASCADE"


def test_constraint_names_are_deterministic() -> None:
    """A naming convention is what lets Alembic autogenerate and downgrade stay stable."""
    convention = Base.metadata.naming_convention
    assert {"ix", "uq", "ck", "fk", "pk"} <= set(convention)
