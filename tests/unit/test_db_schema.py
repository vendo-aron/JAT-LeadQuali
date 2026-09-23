"""Offline schema invariants, asserted from SQLAlchemy metadata alone.

These run without Docker and without a database. They exist so that the default test
suite still proves the product invariants that the schema is responsible for: multi-tenancy
on every table (CLAUDE.md invariant 4), the integrity that makes a denormalised
``tenant_id`` trustworthy, the idempotency key on ``leads``, the recordability of a failed
assessment (invariant 3), and the absence of any raw-email column (invariant 5).
"""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from leadquali.adapters.db_schema import (
    ASSESSMENT_STATUSES,
    DEFAULT_QUOTA_ALERT_FRACTION,
    ESCALATION_REASONS,
    LEAD_STATUSES,
    ROUTING_ACTIONS,
    Assessment,
    Base,
    Feedback,
    GoldenPromotion,
    Lead,
    RoutingEvent,
    Tenant,
    TenantApiKey,
    TenantConfigVersion,
    UsageDaily,
)
from leadquali.app.metering import (
    DEFAULT_QUOTA_ALERT_FRACTION as METERING_DEFAULT_ALERT_FRACTION,
)

EXPECTED_TABLES = {
    "tenants",
    "tenant_api_keys",
    "leads",
    "assessments",
    "routing_events",
    "feedback",
    "usage_daily",
    "tenant_config_versions",
    "golden_promotions",
}

CHILD_TABLES = ("assessments", "routing_events", "feedback", "golden_promotions")
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
    ("tenants", "slug"): "none",
    ("tenants", "name"): "none",
    ("tenants", "status"): "none",
    ("tenants", "icp_config"): "none",
    ("tenants", "hmac_secret_ref"): "none",
    ("tenants", "rate_limit_per_minute"): "none",
    ("tenants", "rate_limit_burst"): "none",
    # The soft quota (#33). A plan size and an alert threshold: commercial policy about a
    # customer's *account*, with nothing of any lead in it.
    ("tenants", "monthly_lead_quota"): "none",
    ("tenants", "quota_alert_fraction"): "none",
    ("tenants", "created_at"): "none",
    ("tenants", "updated_at"): "none",
    ("tenant_api_keys", "id"): "none",
    ("tenant_api_keys", "tenant_id"): "none",
    # The clear-text lookup handle. Not a secret and not personal data: 64 bits of
    # machine randomness naming a row, which is why it is safe to log and to index.
    ("tenant_api_keys", "key_id"): "none",
    ("tenant_api_keys", "key_prefix"): "none",
    ("tenant_api_keys", "key_hash"): "hashed",
    # Operator-written, e.g. "acme marketing site". Free text, and classified "none" for
    # the same reason `tenants.name` is: it describes a customer's *integration*, and
    # nothing in the product ever puts a lead's data — or a person's — into it. It is not
    # in the "raw" bucket because that bucket is what #37's retention job purges, and a
    # purge that deleted key labels would erase the audit trail it is meant to preserve.
    ("tenant_api_keys", "label"): "none",
    ("tenant_api_keys", "created_at"): "none",
    ("tenant_api_keys", "expires_at"): "none",
    ("tenant_api_keys", "revoked_at"): "none",
    ("tenant_api_keys", "last_used_at"): "none",
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
    # usage_daily (#33) is counters and money, derived from the tables above. There is
    # nothing here that came from a person: it is how many leads there were, not who they
    # were, which is also why #37's retention purge can leave it alone.
    ("usage_daily", "tenant_id"): "none",
    ("usage_daily", "usage_date"): "none",
    ("usage_daily", "leads_ingested"): "none",
    ("usage_daily", "leads_assessed"): "none",
    ("usage_daily", "leads_billable"): "none",
    ("usage_daily", "assessments_failed"): "none",
    ("usage_daily", "input_tokens"): "none",
    ("usage_daily", "output_tokens"): "none",
    ("usage_daily", "cache_read_tokens"): "none",
    ("usage_daily", "cache_creation_tokens"): "none",
    ("usage_daily", "cost_usd"): "none",
    ("usage_daily", "computed_at"): "none",
    # tenant_config_versions (#36) is the rubric's edit history. A rubric is policy about
    # a *customer*, written by staff — the ICP prose, the weights, the thresholds and the
    # sales inboxes leads are routed to. Nothing a lead submitted reaches it, which is why
    # `tenants.icp_config` is classified the same way.
    ("tenant_config_versions", "id"): "none",
    ("tenant_config_versions", "tenant_id"): "none",
    ("tenant_config_versions", "version"): "none",
    ("tenant_config_versions", "config"): "none",
    # The staff subject from the admin session: an opaque username, never an address, for
    # the same reason `feedback.rater` is not one — an audit row outlives the payload.
    ("tenant_config_versions", "changed_by"): "none",
    ("tenant_config_versions", "changed_at"): "none",
    ("tenant_config_versions", "note"): "none",
    # golden_promotions (#36) records the *decision* to promote a lead into the eval set
    # and the human label that goes with it. It deliberately holds no copy of the payload:
    # a pseudonymised copy here would be a second home for data derived from
    # `leads.raw_payload`, outside the one place invariant 5 allows it and outside what
    # #37's retention job purges. The JSONL line is rendered from the lead on demand.
    ("golden_promotions", "id"): "none",
    ("golden_promotions", "tenant_id"): "none",
    ("golden_promotions", "lead_id"): "none",
    ("golden_promotions", "case_id"): "none",
    ("golden_promotions", "expected_tier"): "none",
    ("golden_promotions", "promoted_by"): "none",
    # The labeller's rationale for the tier, in their own words. Staff-written prose about
    # the *judgement*, classified like `feedback.notes` and for the same reason: #22's
    # runbook is explicit that a rep's notes are summarised here rather than pasted, and
    # that a labeler handle is never a person's name.
    ("golden_promotions", "note"): "none",
    ("golden_promotions", "promoted_at"): "none",
    ("golden_promotions", "created_at"): "none",
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


def test_metadata_declares_exactly_the_planned_tables() -> None:
    """Plan §4's five, plus ``tenant_api_keys`` (#31).

    Credentials get a table of their own rather than a column on ``tenants`` because a
    tenant holds several keys at once during a rotation, and because a key row carries its
    own revocation and expiry.
    """
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_model_classes_map_to_the_expected_table_names() -> None:
    """The names #16 imports. Renaming one of these is a breaking change for that issue."""
    for model, table_name in (
        (Tenant, "tenants"),
        (Lead, "leads"),
        (Assessment, "assessments"),
        (RoutingEvent, "routing_events"),
        (Feedback, "feedback"),
        (TenantApiKey, "tenant_api_keys"),
        (UsageDaily, "usage_daily"),
        (TenantConfigVersion, "tenant_config_versions"),
        (GoldenPromotion, "golden_promotions"),
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
        ("tenants", "ck_tenants_slug_is_a_slug"),
        ("tenants", "ck_tenants_rate_limits_are_positive"),
        ("tenants", "ck_tenants_monthly_lead_quota_is_positive"),
        ("tenants", "ck_tenants_quota_alert_fraction_is_a_fraction"),
        ("usage_daily", "ck_usage_daily_usage_is_non_negative"),
        ("feedback", "ck_feedback_verdict_known"),
        ("golden_promotions", "ck_golden_promotions_expected_tier_known"),
        ("tenant_config_versions", "ck_tenant_config_versions_version_is_positive"),
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
        ("tenants", "updated_at"),
        ("tenant_api_keys", "created_at"),
        ("leads", "received_at"),
        ("leads", "created_at"),
        ("assessments", "created_at"),
        ("routing_events", "created_at"),
        ("feedback", "created_at"),
        ("usage_daily", "computed_at"),
        ("tenant_config_versions", "changed_at"),
        ("golden_promotions", "promoted_at"),
        ("golden_promotions", "created_at"),
    ],
)
def test_timestamps_are_timezone_aware_with_a_server_default(
    table_name: str, column_name: str
) -> None:
    column = _table(table_name).c[column_name]
    assert isinstance(column.type, TIMESTAMP)
    assert column.type.timezone is True, f"{table_name}.{column_name} must be timestamptz"
    assert column.server_default is not None


#: The one table whose primary key is a natural key rather than a server-generated UUID.
#: ``usage_daily`` is a rollup: ``(tenant_id, usage_date)`` *is* the identity of a row, a
#: second row for one tenant-day is a bug rather than a new fact, and the composite key is
#: also the conflict target the idempotent upsert needs. A surrogate ``id`` would let two
#: rows for one day coexist while every billing read summed both.
NATURAL_KEY_TABLES = {"usage_daily"}


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES - NATURAL_KEY_TABLES))
def test_primary_keys_are_uuids_generated_by_the_server(table_name: str) -> None:
    primary_key = list(_table(table_name).primary_key.columns)
    assert [c.name for c in primary_key] == ["id"]
    assert primary_key[0].server_default is not None


def test_the_usage_rollup_is_keyed_by_the_tenant_and_the_day() -> None:
    """The exception to the rule above, and the reason a re-run cannot double count."""
    primary_key = [column.name for column in _table("usage_daily").primary_key.columns]
    assert primary_key == ["tenant_id", "usage_date"]


def test_the_usage_rollup_counts_tokens_in_bigints() -> None:
    """``assessments.input_tokens`` is an ``integer`` and is right to be — no single call
    approaches 2^31 — but a busy tenant passes two billion input tokens inside a year, and
    a rollup that silently overflowed would be discovered on an invoice."""
    table = _table("usage_daily")
    for column_name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        assert isinstance(table.c[column_name].type, BigInteger), column_name
    # Counts of rows stay `integer`: two billion leads in a day from one tenant is not a
    # capacity question, it is an incident.
    assert isinstance(table.c["leads_ingested"].type, Integer)


def test_the_usage_rollup_holds_a_wider_money_column_than_one_assessment() -> None:
    """It holds a sum of them, and a scale that fits one call need not fit a day of them."""
    day = _table("usage_daily").c["cost_usd"].type
    call = _table("assessments").c["cost_usd"].type
    assert isinstance(day, Numeric) and isinstance(call, Numeric)
    assert day.precision is not None and call.precision is not None
    assert day.precision > call.precision
    assert day.scale == call.scale


def test_the_usage_rollup_goes_when_its_tenant_does() -> None:
    """CASCADE, unlike ``leads``, which restricts. Nothing here is a record of anything —
    it is a cache of a SUM — so blocking a deletion on it would be theatre."""
    foreign_keys = list(_table("usage_daily").c["tenant_id"].foreign_keys)
    assert [fk.column.table.name for fk in foreign_keys] == ["tenants"]
    assert foreign_keys[0].ondelete == "CASCADE"


def test_a_quota_is_optional_and_a_tenant_without_one_is_unlimited() -> None:
    """A quota that appeared by accident would start warning somebody about a customer who
    never agreed to one, so NULL is both the default and the meaning "unlimited"."""
    quota = _table("tenants").c["monthly_lead_quota"]
    assert quota.nullable
    assert quota.server_default is None
    fraction = _table("tenants").c["quota_alert_fraction"]
    assert not fraction.nullable
    assert fraction.server_default is not None


def test_the_alert_fraction_default_matches_the_application_constant() -> None:
    """The database writes this default for a row inserted by psql; the service applies the
    same number in Python. Two spellings of "alert at 80%" is how a tenant's alert ends up
    depending on how its row was created."""
    assert Decimal(DEFAULT_QUOTA_ALERT_FRACTION) == METERING_DEFAULT_ALERT_FRACTION


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
    # #36's promotion listing and "has this lead already been promoted?" for a page of
    # review rows: WHERE tenant_id = ? ORDER BY promoted_at DESC.
    assert ("tenant_id", "promoted_at") in index_columns["golden_promotions"]


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


def test_a_config_version_is_identified_by_its_tenant_and_number() -> None:
    """``UNIQUE (tenant_id, version)`` is what settles two operators saving at once.

    The version is allocated from this table inside the writing transaction, so the
    constraint is not belt and braces — it is the mechanism. A counter kept in Python
    would hand the same number to two admin processes and one edit would overwrite the
    other's audit row.
    """
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in _table("tenant_config_versions").constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "version") in unique_column_sets


def test_a_config_version_holds_a_whole_config_and_an_author() -> None:
    """Full snapshots, not patches, and never an anonymous change."""
    table = _table("tenant_config_versions")
    assert isinstance(table.c["config"].type, JSONB)
    assert not table.c["config"].nullable
    assert not table.c["changed_by"].nullable
    assert "ck_tenant_config_versions_changed_by_not_blank" in _check_constraint_names(
        "tenant_config_versions"
    )
    # The reason is optional: refusing to save a rubric fix during an incident over a
    # missing note is friction that gets worked around by editing the row in psql.
    assert table.c["note"].nullable


def test_a_config_version_goes_when_its_tenant_does() -> None:
    """Like ``tenant_api_keys`` and unlike ``leads``: this is a record *about* the
    customer's configuration, meaningless without them, so #37's deliberate erasure takes
    it along rather than being blocked by it."""
    foreign_keys = list(_table("tenant_config_versions").c["tenant_id"].foreign_keys)
    assert [fk.column.table.name for fk in foreign_keys] == ["tenants"]
    assert foreign_keys[0].ondelete == "CASCADE"


def test_promoting_one_lead_twice_is_impossible() -> None:
    """#36's acceptance criterion, stated in the database rather than in the handler.

    A refreshed confirmation page, a double tap or a second operator must not put the same
    lead in the eval set twice; the harness would then weigh that one lead twice.
    """
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in _table("golden_promotions").constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "lead_id") in unique_column_sets
    assert ("case_id",) in unique_column_sets


def test_a_promotion_carries_no_copy_of_the_payload() -> None:
    """The golden case is rendered from the lead on demand and never stored here.

    A pseudonymised copy would be a second home for data derived from
    ``leads.raw_payload`` — outside the one place invariant 5 allows personal data to live,
    and outside what #37's retention job purges.
    """
    columns = {column.name for column in _table("golden_promotions").c}
    assert columns == {
        "id",
        "tenant_id",
        "lead_id",
        "case_id",
        "expected_tier",
        "promoted_by",
        "note",
        "promoted_at",
        "created_at",
    }
    assert not any(isinstance(column.type, JSONB) for column in _table("golden_promotions").c)


def test_a_promotion_must_carry_a_rationale_long_enough_for_the_golden_set() -> None:
    """#22 refuses a label whose notes are shorter than 20 characters. Staging a promotion
    that the file would then refuse tells the operator it worked when it did not."""
    assert "ck_golden_promotions_note_is_a_rationale" in _check_constraint_names(
        "golden_promotions"
    )
    assert not _table("golden_promotions").c["note"].nullable
