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

# A complete assessment row minus its tenant_id/lead_id, reused by several tests.
AN_ASSESSMENT: dict[str, object] = {
    "tier": "hot",
    "total_score": 88,
    "dimension_scores": {"icp_fit": 28, "intent": 24},
    "extracted": {"company_name": "Acme"},
    "reasoning": "Enterprise buyer with a stated Q3 timeline.",
    "confidence": Decimal("0.910"),
    "model_id": "claude-opus-5",
    "prompt_version": "rubric_v1",
}


def _new_tenant(db: Connection, name: str = "Acme") -> uuid.UUID:
    row: uuid.UUID = db.execute(
        TENANTS.insert().returning(TENANTS.c.id), {"name": name}
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


@pytest.mark.parametrize(
    ("table", "extra"),
    [
        (ASSESSMENTS, AN_ASSESSMENT),
        (ROUTING_EVENTS, {"action": "email_sales"}),
        (FEEDBACK, {"rater": "rep@example.com", "verdict": "good"}),
    ],
    ids=["assessments", "routing_events", "feedback"],
)
def test_no_child_row_can_reference_an_unknown_tenant(
    db: Connection, table: Table, extra: dict[str, object]
) -> None:
    """Invariant 4 is only real if the database enforces it on every table.

    Plan section 4 leaves ``tenant_id`` off ``routing_events`` and ``feedback``; these two
    parameter cases are the reason it was added anyway.
    """
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        db.execute(table.insert(), {"tenant_id": uuid.uuid4(), "lead_id": lead_id, **extra})

    assert f"fk_{table.name}_tenant_id_tenants" in str(caught.value)


def test_deleting_a_lead_deletes_its_assessment_routing_and_feedback(db: Connection) -> None:
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")
    owned = {"tenant_id": tenant_id, "lead_id": lead_id}

    db.execute(ASSESSMENTS.insert(), {**owned, **AN_ASSESSMENT})
    db.execute(ROUTING_EVENTS.insert(), {**owned, "action": "email_sales"})
    db.execute(FEEDBACK.insert(), {**owned, "rater": "rep@example.com", "verdict": "bad"})
    children = (ASSESSMENTS, ROUTING_EVENTS, FEEDBACK)
    assert [_rows_for(db, t, lead_id) for t in children] == [1, 1, 1]

    db.execute(LEADS.delete().where(LEADS.c.id == lead_id))

    assert [_rows_for(db, t, lead_id) for t in children] == [0, 0, 0]


def test_deleting_a_tenant_removes_everything_it_owns(db: Connection) -> None:
    """What an erasure request needs: one delete, no orphans left behind."""
    tenant_id = _new_tenant(db)
    lead_id = _new_lead(db, tenant_id, "form-1")
    db.execute(ASSESSMENTS.insert(), {"tenant_id": tenant_id, "lead_id": lead_id, **AN_ASSESSMENT})

    db.execute(TENANTS.delete().where(TENANTS.c.id == tenant_id))

    assert db.execute(select(func.count()).select_from(LEADS)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(ASSESSMENTS)).scalar_one() == 0


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


def test_a_blank_submission_id_is_rejected(db: Connection) -> None:
    """An empty idempotency key is not an idempotency key — reject it at the boundary."""
    tenant_id = _new_tenant(db)

    with pytest.raises(IntegrityError) as caught, db.begin_nested():
        _new_lead(db, tenant_id, "")

    assert "ck_leads_submission_id_not_blank" in str(caught.value)
