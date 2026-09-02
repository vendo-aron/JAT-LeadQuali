"""What the database refuses to store.

A constraint that has never been violated in a test is a constraint you are hoping exists.
Each test here provokes the failure the constraint is for, against real Postgres.

Tables are taken from :data:`leadquali.adapters.db_schema.metadata` rather than through
``Model.__table__`` so that they type as :class:`~sqlalchemy.Table`. The two are the same
objects; ``tests/unit/test_db_schema.py`` asserts that offline.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, Table, func, select
from sqlalchemy.exc import IntegrityError

from leadquali.adapters.db_schema import metadata

pytestmark = pytest.mark.integration

TENANTS = metadata.tables["tenants"]
LEADS = metadata.tables["leads"]
ASSESSMENTS = metadata.tables["assessments"]
ROUTING_EVENTS = metadata.tables["routing_events"]
FEEDBACK = metadata.tables["feedback"]

# Enough of a rubric to be a plausible config. `icp_config` has no server default, so every
# tenant insert has to carry one — which is the point of that change.
AN_ICP_CONFIG: dict[str, Any] = {
    "icp_description": "B2B software companies with inbound volume.",
    "weights": {"icp_fit": 1.0},
    "thresholds": {"hot": 80.0, "warm": 55.0, "cold": 30.0},
    "min_confidence": 0.6,
    "routing_rules": {"hot": {"action": "email_sales"}},
    "prompt_version": "rubric_v1",
}

# A complete, successful assessment minus its tenant_id/lead_id, reused by several tests.
AN_ASSESSMENT: dict[str, object] = {
    "status": "ok",
    "tier": "hot",
    "total_score": Decimal("88.50"),
    "dimension_scores": {"icp_fit": 28, "intent": 24},
    "extracted": {"company_name": "Acme"},
    "reasoning": "Enterprise buyer with a stated Q3 timeline.",
    "confidence": Decimal("0.910"),
    "model_id": "claude-opus-5",
    "prompt_version": "rubric_v1",
}

# An assessment that never happened: the call errored, so there is no model output at all.
A_FAILED_ASSESSMENT: dict[str, object] = {
    "status": "failed",
    "escalation_reason": "api_error",
    "model_id": "claude-opus-5",
    "prompt_version": "rubric_v1",
}

# feedback.rater is an opaque subject id, never a contact address. See db_schema.py.
A_RATER = "user_01hqzp4n8k"


def _new_tenant(db: Connection, name: str = "Acme") -> uuid.UUID:
    row: uuid.UUID = db.execute(
        TENANTS.insert().returning(TENANTS.c.id), {"name": name, "icp_config": AN_ICP_CONFIG}
    ).scalar_one()
    return row


def _new_lead(db: Connection, tenant_id: uuid.UUID, submission_id: str) -> uuid.UUID:
    row: uuid.UUID = db.execute(
        LEADS.insert().returning(LEADS.c.id),
        {
            "tenant_id": tenant_id,
            "submission_id": submission_id,
            "raw_payload": {"email": "buyer@example.com", "message": "pricing?"},
            "source": "web_form",
        },
    ).scalar_one()
    return row


def _rows_for(db: Connection, table: Table, lead_id: uuid.UUID) -> int:
    count: int = db.execute(
        select(func.count()).select_from(table).where(table.c.lead_id == lead_id)
    ).scalar_one()
    return count


def test_a_redelivered_submission_cannot_create_a_second_lead(db: Connection) -> None:
    """The idempotency guarantee. SQS is at-least-once; sales must be emailed once."""
    tenant_id = _new_tenant(db)
    _new_lead(db, tenant_id, "form-abc-123")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        _new_lead(db, tenant_id, "form-abc-123")

    assert "uq_leads_tenant_id_submission_id" in str(caught.value)


def test_two_tenants_may_use_the_same_submission_id(db: Connection) -> None:
    """The key is tenant-scoped, not global — two customers' form ids never collide."""
    first = _new_tenant(db, "Acme")
    second = _new_tenant(db, "Globex")

    _new_lead(db, first, "form-abc-123")
    _new_lead(db, second, "form-abc-123")

    assert db.execute(select(func.count()).select_from(LEADS)).scalar_one() == 2


def test_a_lead_cannot_belong_to_an_unknown_tenant(db: Connection) -> None:
    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        _new_lead(db, uuid.uuid4(), "orphan")

    assert "fk_leads_tenant_id_tenants" in str(caught.value)


# --- the cross-tenant integrity hole --------------------------------------------------


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        (ASSESSMENTS, AN_ASSESSMENT),
        (ROUTING_EVENTS, {"action": "email_sales"}),
        (FEEDBACK, {"rater": A_RATER, "verdict": "good"}),
    ],
    ids=["assessments", "routing_events", "feedback"],
)
def test_a_child_row_cannot_be_filed_under_a_tenant_that_does_not_own_the_lead(
    db: Connection, table: Table, extra: dict[str, object]
) -> None:
    """The one that matters most, and the one two independent foreign keys allowed.

    Both columns were individually valid — tenant B exists, the lead exists — while
    together they described something that cannot be true. Since ``tenant_id`` is the only
    filter every repository method applies (invariant 4), tenant B's queries then returned
    tenant A's lead data and the filter still "passed", so nothing anywhere surfaced it:
    the wrong rows just accumulated in the wrong customer's analytics and invoices.
    """
    owner = _new_tenant(db, "Acme")
    other = _new_tenant(db, "Globex")
    lead_id = _new_lead(db, owner, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(table.insert(), {"tenant_id": other, "lead_id": lead_id, **extra})

    assert f"fk_{table.name}_tenant_id_lead_id_leads" in str(caught.value)


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        (ASSESSMENTS, AN_ASSESSMENT),
        (ROUTING_EVENTS, {"action": "email_sales"}),
        (FEEDBACK, {"rater": A_RATER, "verdict": "good"}),
    ],
    ids=["assessments", "routing_events", "feedback"],
)
def test_a_child_row_whose_tenant_and_lead_agree_is_accepted(
    db: Connection, table: Table, extra: dict[str, object]
) -> None:
    """The other half: the composite key must not have made the normal case impossible."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(table.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **extra})

    assert _rows_for(db, table, lead_id) == 1


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        (ASSESSMENTS, AN_ASSESSMENT),
        (ROUTING_EVENTS, {"action": "email_sales"}),
        (FEEDBACK, {"rater": A_RATER, "verdict": "good"}),
    ],
    ids=["assessments", "routing_events", "feedback"],
)
def test_no_child_row_can_reference_an_unknown_tenant(
    db: Connection, table: Table, extra: dict[str, object]
) -> None:
    """Invariant 4 is only real if the database enforces it on every table.

    Plan section 4 leaves ``tenant_id`` off ``routing_events`` and ``feedback``; these two
    parameter cases are the reason it was added anyway. The composite key subsumes the old
    ``tenant_id -> tenants.id`` reference: an unknown tenant cannot own the lead either.
    """
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(table.insert(), {"tenant_id": uuid.uuid4(), "lead_id": lead_id, **extra})

    assert f"fk_{table.name}_tenant_id_lead_id_leads" in str(caught.value)


# --- deletion policy ------------------------------------------------------------------


def test_deleting_a_lead_deletes_its_assessment_routing_and_feedback(db: Connection) -> None:
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")
    owned = {"tenant_id": tenant_id, "lead_id": lead_id}

    db.execute(ASSESSMENTS.insert(), {**owned, **AN_ASSESSMENT})
    db.execute(ROUTING_EVENTS.insert(), {**owned, "action": "email_sales"})
    db.execute(FEEDBACK.insert(), {**owned, "rater": A_RATER, "verdict": "bad"})
    children = (ASSESSMENTS, ROUTING_EVENTS, FEEDBACK)
    assert [_rows_for(db, t, lead_id) for t in children] == [1, 1, 1]

    db.execute(LEADS.delete().where(LEADS.c.id == lead_id))

    assert [_rows_for(db, t, lead_id) for t in children] == [0, 0, 0]


def test_deleting_a_tenant_that_still_owns_data_is_refused(db: Connection) -> None:
    """Erasure is deliberate, not a side effect.

    With ``ON DELETE CASCADE`` on the tenant foreign keys, one over-broad
    ``DELETE FROM tenants WHERE ...`` silently destroyed every lead, assessment, routing
    event and feedback row the customer had — including the invariant-3 audit trail that
    exists precisely to prove no lead was dropped. Losing that to a typo is not a risk
    worth the convenience, so the database refuses and #37's purge routine has to say what
    it means.
    """
    tenant_id = _new_tenant(db)
    _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(TENANTS.delete().where(TENANTS.c.id == tenant_id))

    assert "fk_leads_tenant_id_tenants" in str(caught.value)


def test_an_explicit_purge_still_removes_everything_the_tenant_owns(db: Connection) -> None:
    """What #37 has to do instead: delete the leads, which cascades, then the tenant.

    Two statements rather than one, and no orphans left behind — the erasure request is
    still satisfiable, it just cannot happen by accident.
    """
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")
    db.execute(ASSESSMENTS.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **AN_ASSESSMENT})

    db.execute(LEADS.delete().where(LEADS.c.tenant_id == tenant_id))
    db.execute(TENANTS.delete().where(TENANTS.c.id == tenant_id))

    for table in (TENANTS, LEADS, ASSESSMENTS, ROUTING_EVENTS, FEEDBACK):
        assert db.execute(select(func.count()).select_from(table)).scalar_one() == 0


def test_a_tenant_with_no_data_can_still_be_deleted(db: Connection) -> None:
    """RESTRICT blocks destroying data, not tidying up a mistyped tenant."""
    tenant_id = _new_tenant(db, "created-by-mistake")

    db.execute(TENANTS.delete().where(TENANTS.c.id == tenant_id))

    assert db.execute(select(func.count()).select_from(TENANTS)).scalar_one() == 0


# --- the tenant rubric ----------------------------------------------------------------


def test_a_tenant_cannot_be_created_without_a_rubric(db: Connection) -> None:
    """Invariant 1, enforced at the insert rather than discovered at 3am.

    ``server_default='{}'`` made this succeed: the tenant existed, looked fine in ``\\dt``,
    and every config load rejected it — so the failure surfaced in the worker, against live
    traffic, a long way from the insert that caused it.
    """
    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(TENANTS.insert(), {"name": "no rubric"})

    assert "icp_config" in str(caught.value)


def test_a_tenant_created_with_a_rubric_round_trips(db: Connection) -> None:
    tenant_id = _new_tenant(db)

    stored = db.execute(select(TENANTS.c.icp_config).where(TENANTS.c.id == tenant_id)).scalar_one()

    assert stored == AN_ICP_CONFIG


# --- assessments: success, failure, and nothing in between ----------------------------


def test_a_successful_assessment_is_recorded(db: Connection) -> None:
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(ASSESSMENTS.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **AN_ASSESSMENT})

    row = db.execute(
        select(ASSESSMENTS.c.status, ASSESSMENTS.c.total_score, ASSESSMENTS.c.escalation_reason)
    ).one()
    assert row.status == "ok"
    assert row.total_score == Decimal("88.50")
    assert row.escalation_reason is None


def test_a_failed_assessment_is_recorded_too(db: Connection) -> None:
    """Invariant 3: an API error, refusal, timeout or parse error is a real outcome.

    While the model-output columns were NOT NULL there was nowhere to put one — so the
    only way to finish handling such a lead was to write nothing, and a lead with no row
    is a lead silently dropped.
    """
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(
        ASSESSMENTS.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **A_FAILED_ASSESSMENT}
    )

    row = db.execute(
        select(ASSESSMENTS.c.status, ASSESSMENTS.c.escalation_reason, ASSESSMENTS.c.reasoning)
    ).one()
    assert (row.status, row.escalation_reason, row.reasoning) == ("failed", "api_error", None)


def test_a_low_confidence_escalation_is_a_successful_assessment(db: Connection) -> None:
    """The model answered; code did not trust the answer. Output is present *and* a human
    is pulled in, so ``escalation_reason`` is not merely a failure code."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(
        ASSESSMENTS.insert(),
        {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            **AN_ASSESSMENT,
            "escalation_reason": "low_confidence",
        },
    )

    row = db.execute(select(ASSESSMENTS.c.status, ASSESSMENTS.c.escalation_reason)).one()
    assert (row.status, row.escalation_reason) == ("ok", "low_confidence")


@pytest.mark.parametrize(
    ("label", "values"),
    [
        ("ok_without_output", {"status": "ok", "model_id": "m", "prompt_version": "v"}),
        (
            "ok_with_only_half_its_output",
            {
                **AN_ASSESSMENT,
                "reasoning": None,
            },
        ),
        (
            "failed_but_carrying_output",
            {**AN_ASSESSMENT, "status": "failed", "escalation_reason": "api_error"},
        ),
        (
            "failed_without_a_reason",
            {"status": "failed", "model_id": "m", "prompt_version": "v"},
        ),
    ],
)
def test_an_assessment_that_is_neither_a_success_nor_a_failure_is_rejected(
    db: Connection, label: str, values: dict[str, object]
) -> None:
    """Nullable columns without this constraint would be worse than NOT NULL ones.

    "Successful, but with no reasoning" and "failed, but with scores" are both
    representable the moment the columns go nullable, and the feedback loop would average
    over rows that never held an assessment. The shape is all-or-nothing, and the database
    is what makes it so.
    """
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(ASSESSMENTS.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **values})

    assert "ck_assessments_output_present_iff_ok" in str(caught.value)


def test_an_unknown_escalation_reason_is_rejected(db: Connection) -> None:
    """The values must stay the domain's ``EscalationReason``: a rise in ``api_error`` and
    a rise in ``low_confidence`` wake different people, and grouping only works if the
    column holds one spelling of each."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            ASSESSMENTS.insert(),
            {
                "tenant_id": tenant_id,
                "lead_id": lead_id,
                **AN_ASSESSMENT,
                "escalation_reason": "the vibes were off",
            },
        )

    assert "ck_assessments_escalation_reason_known" in str(caught.value)


@pytest.mark.parametrize(
    "reason", ["low_confidence", "model_refusal", "parse_error", "api_error", "timeout"]
)
def test_every_domain_escalation_reason_is_accepted(db: Connection, reason: str) -> None:
    """A CHECK that rejected a value the domain can produce would be a worse bug than no
    CHECK at all, so each of the five is inserted rather than assumed."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(
        ASSESSMENTS.insert(),
        {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            **A_FAILED_ASSESSMENT,
            "escalation_reason": reason,
        },
    )

    assert _rows_for(db, ASSESSMENTS, lead_id) == 1


# --- scores, tiers and other vocabularies ---------------------------------------------


@pytest.mark.parametrize("bad_score", [Decimal("-5"), Decimal("100.01"), Decimal("999")])
def test_a_total_score_outside_the_zero_to_hundred_scale_is_rejected(
    db: Connection, bad_score: Decimal
) -> None:
    """``9999`` and ``-5`` both inserted before. A score is a percentage of a rubric; a
    number outside the scale is a bug in whatever computed it, and storing it silently
    corrupts every average taken over the column afterwards."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            ASSESSMENTS.insert(),
            {"tenant_id": tenant_id, "lead_id": lead_id, **AN_ASSESSMENT, "total_score": bad_score},
        )

    assert "ck_assessments_total_score_in_range" in str(caught.value)


def test_a_fractional_score_survives_the_round_trip(db: Connection) -> None:
    """#9's ``weighted_total`` is a float rounded to 2dp and tenant thresholds are floats,
    so ``55.1`` has to come back as ``55.1`` — as an ``Integer`` column it could not."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(
        ASSESSMENTS.insert(),
        {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            **AN_ASSESSMENT,
            "tier": "warm",
            "total_score": Decimal("55.1"),
        },
    )

    stored = db.execute(select(ASSESSMENTS.c.total_score)).scalar_one()
    assert stored == Decimal("55.1")
    assert float(stored) == 55.1


def test_an_unknown_tier_is_rejected(db: Connection) -> None:
    """Tier is computed by code (invariant 2); the database keeps typos out of analytics."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            ASSESSMENTS.insert(),
            {"tenant_id": tenant_id, "lead_id": lead_id, **AN_ASSESSMENT, "tier": "scorching"},
        )

    assert "ck_assessments_tier_known" in str(caught.value)


def test_an_unknown_routing_action_is_rejected(db: Connection) -> None:
    """``tenants.status``, ``assessments.tier`` and ``feedback.verdict`` were constrained
    and ``action`` was not. "How many leads did we suppress last week?" is quietly wrong
    the first time something writes ``"suppressed"``."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            ROUTING_EVENTS.insert(),
            {"tenant_id": tenant_id, "lead_id": lead_id, "action": "teleport_to_sales"},
        )

    assert "ck_routing_events_action_known" in str(caught.value)


@pytest.mark.parametrize("action", ["email_sales", "escalate_human", "suppress"])
def test_every_domain_action_is_accepted(db: Connection, action: str) -> None:
    """Including ``suppress``: invariant 3 says even a suppression leaves a row."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    db.execute(
        ROUTING_EVENTS.insert(),
        {"tenant_id": tenant_id, "lead_id": lead_id, "action": action},
    )

    assert _rows_for(db, ROUTING_EVENTS, lead_id) == 1


def test_an_unknown_lead_status_is_rejected(db: Connection) -> None:
    tenant_id = _new_tenant(db)

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            LEADS.insert(),
            {
                "tenant_id": tenant_id,
                "submission_id": "form-1",
                "raw_payload": {},
                "source": "web_form",
                "status": "banana",
            },
        )

    assert "ck_leads_status_known" in str(caught.value)


@pytest.mark.parametrize("status", ["received", "qualified", "routed", "failed"])
def test_every_lead_lifecycle_state_is_accepted(db: Connection, status: str) -> None:
    tenant_id = _new_tenant(db)

    db.execute(
        LEADS.insert(),
        {
            "tenant_id": tenant_id,
            "submission_id": f"form-{status}",
            "raw_payload": {},
            "source": "web_form",
            "status": status,
        },
    )

    assert db.execute(select(func.count()).select_from(LEADS)).scalar_one() == 1


def test_the_server_supplies_uuids_timestamps_and_the_initial_status(db: Connection) -> None:
    """A row inserted by psql during an incident is as well-formed as one from the app."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    row = db.execute(select(LEADS.c.received_at, LEADS.c.status).where(LEADS.c.id == lead_id)).one()

    assert isinstance(lead_id, uuid.UUID)
    assert row.received_at.tzinfo is not None, "received_at must come back timezone-aware"
    assert row.status == "received"


@pytest.mark.parametrize("bad_confidence", [Decimal("-0.001"), Decimal("1.001")])
def test_confidence_outside_zero_to_one_is_rejected(
    db: Connection, bad_confidence: Decimal
) -> None:
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(
            ASSESSMENTS.insert(),
            {
                "tenant_id": tenant_id,
                "lead_id": lead_id,
                **AN_ASSESSMENT,
                "confidence": bad_confidence,
            },
        )

    assert "ck_assessments_confidence_is_a_probability" in str(caught.value)


def test_a_blank_submission_id_is_rejected(db: Connection) -> None:
    """An empty idempotency key is not an idempotency key — reject it at the boundary."""
    tenant_id = _new_tenant(db)

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        _new_lead(db, tenant_id, "")

    assert "ck_leads_submission_id_not_blank" in str(caught.value)
