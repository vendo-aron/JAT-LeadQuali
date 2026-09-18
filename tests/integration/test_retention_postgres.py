"""Retention against a real Postgres.

``tests/unit/test_retention.py`` proves the *policy* — the tiers, the batching, the
receipt, the redaction of the model's prose — over an in-memory store, and
``tests/unit/test_retention_postgres.py`` proves the adapter's statements are well-formed
Postgres. This file is the half neither can reach: that the database agrees with both.

It is the issue's first acceptance criterion stated as a property of the server rather than
of a double — *"retention job runs on schedule and is verified against a seeded old
record"* — plus the things only a real server can settle:

* the ``@>`` tombstone filter really does make a second run a no-op;
* the composite ``ON DELETE CASCADE`` really does take the assessments, routing events,
  feedback and golden promotions with the lead;
* the ``tenants`` foreign key's ``RESTRICT`` really does refuse to let a purge remove a
  customer;
* the two ``CHECK`` constraints on the retention windows really are in the migrated
  database and not only in the models.

Skipped, never failed, when there is no database — see ``conftest.py``. Nothing that a
machine without Docker could have checked is left only to this file.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, create_engine, delete, insert, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
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
from leadquali.adapters.retention_postgres import PostgresRetentionStore
from leadquali.adapters.seed import seed_tenant
from leadquali.adapters.store_postgres import tenant_uuid
from leadquali.app.retention import (
    PurgedRecords,
    RetentionService,
    RetentionStorePort,
    is_tombstone,
)
from leadquali.observability import EMAIL_REDACTION, contact_email_hash
from tests.fakes import FakeClock
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Two tenants, because half of what retention must not do is touch the other one.
TENANT_A = "retention-tenant-a"
TENANT_B = "retention-tenant-b"

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)

SUBJECT = "ada.lovelace+jat37@analytical-engines-quali.co.uk"
BYSTANDER = "charles.babbage@difference-engines-quali.co.uk"

#: The model's prose, quoting the lead back — the behaviour #13 found, not an attack.
REASONING = (
    f"Strong fit. The enquiry came from {SUBJECT} at Analytical Engines Ltd, who needs "
    "routing before the Michaelmas board meeting."
)


# ------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def retention_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own, because these tests commit."""
    name = f"{_database_url.database}_retention_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def retention_engine(retention_database: URL) -> Iterator[Engine]:
    engine = create_engine(retention_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def seeded_tenants(retention_engine: Engine) -> tuple[str, str]:
    """Two committed tenant rows. A lead cannot exist without one."""
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    with retention_engine.begin() as connection:
        for slug in (TENANT_A, TENANT_B):
            seed_tenant(connection, {**document, "tenant_id": slug, "name": f"Tenant {slug}"})
    return (TENANT_A, TENANT_B)


@pytest.fixture
def connection(retention_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with retention_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    """Sessions joined to the test's transaction as savepoints.

    The adapter commits in every method — that is the behaviour under test — so the only
    way to keep tests from leaking rows is to make those commits release a savepoint inside
    a transaction the fixture rolls back.
    """
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def store(
    sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]
) -> PostgresRetentionStore:
    del seeded_tenants  # ordering only: the tenants must exist before any lead does.
    return PostgresRetentionStore(sessions)


@pytest.fixture
def service(store: PostgresRetentionStore) -> RetentionService:
    return RetentionService(store=store, clock=FakeClock(start=NOW, step_ms=0))


def seed_lead(
    connection: Connection,
    *,
    tenant: str,
    days_ago: int,
    email: str = SUBJECT,
    payload: dict[str, Any] | None = None,
    reasoning: str | None = REASONING,
    with_children: bool = True,
) -> uuid.UUID:
    """One lead, its assessment, and optionally a full set of children. Returns its id."""
    tenant_id = tenant_uuid(tenant)
    received = NOW - timedelta(days=days_ago)
    lead_id = connection.execute(
        insert(Lead)
        .values(
            tenant_id=tenant_id,
            submission_id=f"sub-{tenant}-{days_ago}-{uuid.uuid4().hex[:8]}",
            raw_payload=payload if payload is not None else {"email": email, "full_name": "Ada"},
            source="web_form",
            status="routed",
            contact_email_hash=contact_email_hash(email),
            received_at=received,
            created_at=received,
        )
        .returning(Lead.id)
    ).scalar_one()

    connection.execute(
        insert(Assessment).values(
            tenant_id=tenant_id,
            lead_id=lead_id,
            created_at=received,
            status="ok",
            tier="warm",
            total_score=Decimal("61.00"),
            dimension_scores={"icp_fit": 20},
            extracted={},
            reasoning=reasoning,
            confidence=Decimal("0.900"),
            missing_information=[],
            model_id="claude-opus-5",
            prompt_version="rubric_v1",
            effort="medium",
            input_tokens=512,
            output_tokens=101,
            cost_usd=Decimal("0.012345"),
            latency_ms=2000,
        )
    )
    if with_children:
        connection.execute(
            insert(RoutingEvent).values(
                tenant_id=tenant_id,
                lead_id=lead_id,
                action="email_sales",
                destination="sales@example.test",
                dispatched_at=received,
                provider_message_id="seeded",
                created_at=received,
            )
        )
        connection.execute(
            insert(Feedback).values(
                tenant_id=tenant_id,
                lead_id=lead_id,
                rater="dest:" + "f" * 32,
                verdict="good",
                notes="seeded",
                created_at=received,
            )
        )
        connection.execute(
            insert(GoldenPromotion).values(
                tenant_id=tenant_id,
                lead_id=lead_id,
                case_id=f"real_{tenant}_{uuid.uuid4().hex[:8]}",
                expected_tier="warm",
                promoted_by="ada",
                note="a rationale long enough to satisfy the check constraint",
                promoted_at=received,
                created_at=received,
            )
        )
    return lead_id


def payload_of(connection: Connection, lead_id: uuid.UUID) -> dict[str, Any]:
    row = connection.execute(select(Lead.raw_payload).where(Lead.id == lead_id)).scalar_one()
    assert isinstance(row, dict)
    return row


def reasoning_of(connection: Connection, lead_id: uuid.UUID) -> list[str | None]:
    return list(
        connection.execute(
            select(Assessment.reasoning).where(Assessment.lead_id == lead_id)
        ).scalars()
    )


def set_windows(connection: Connection, tenant: str, *, raw: int, assessment: int) -> None:
    connection.execute(
        update(Tenant)
        .where(Tenant.id == tenant_uuid(tenant))
        .values(raw_retention_days=raw, assessment_retention_days=assessment)
    )


# ------------------------------------------------------------ the store is the port


def test_the_adapter_satisfies_the_port(store: PostgresRetentionStore) -> None:
    """Structural, and it is what lets the unit tests' double stand in for this."""
    assert isinstance(store, RetentionStorePort)


def test_the_migrated_database_has_the_retention_defaults(
    store: PostgresRetentionStore,
) -> None:
    """A tenant seeded without naming a window gets the documented one from the server."""
    policy = store.retention_policy(tenant_id=TENANT_A)

    assert (policy.raw_retention_days, policy.assessment_retention_days) == (90, 730)


@pytest.mark.parametrize(("raw", "assessment"), [(0, 730), (90, 0), (400, 90)])
def test_the_database_refuses_an_impossible_window(
    connection: Connection, seeded_tenants: tuple[str, str], raw: int, assessment: int
) -> None:
    """The CHECKs are in the migrated database, not only in the models.

    A row written by ``psql`` during an incident has to be as well-formed as one written by
    the service, and the service's own validation cannot reach that path.
    """
    del seeded_tenants
    with pytest.raises(IntegrityError):
        set_windows(connection, TENANT_A, raw=raw, assessment=assessment)


# ------------------------------------------------------------------ tier 1, for real


def test_a_seeded_old_record_is_redacted_and_its_assessment_survives(
    connection: Connection, service: RetentionService
) -> None:
    """The acceptance criterion, against a real row.

    A lead received 200 days ago, a tenant whose tier-1 window is 90 days, and an assessment
    whose prose quotes the lead's address. After the run: a tombstone where the payload was,
    the score still there, the routing event still there, and no address anywhere.
    """
    lead_id = seed_lead(connection, tenant=TENANT_A, days_ago=200)

    report = service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 1
    assert report.count(PurgedRecords.ASSESSMENT_REASONING) == 1
    assert is_tombstone(payload_of(connection, lead_id))
    assert SUBJECT not in json.dumps(payload_of(connection, lead_id))
    stored = reasoning_of(connection, lead_id)
    assert stored and SUBJECT not in (stored[0] or "")
    assert EMAIL_REDACTION in (stored[0] or "")
    # The record it belongs to is untouched, which is the entire point of the tier split.
    assert connection.execute(
        select(Assessment.total_score).where(Assessment.lead_id == lead_id)
    ).scalar_one() == Decimal("61.00")
    assert (
        connection.execute(
            select(RoutingEvent.id).where(RoutingEvent.lead_id == lead_id)
        ).scalar_one_or_none()
        is not None
    )
    # And the pseudonym survives, which is what makes a later deletion request answerable.
    assert connection.execute(
        select(Lead.contact_email_hash).where(Lead.id == lead_id)
    ).scalar_one() == contact_email_hash(SUBJECT)


def test_the_tombstone_filter_makes_a_second_run_a_no_op(
    connection: Connection, service: RetentionService
) -> None:
    """Idempotence, as a property of the ``@>`` predicate rather than of a Python check.

    The second run reporting zero is what proves the marker is not rewritten nightly, which
    would make the date on every tombstone a lie about when the data actually went.
    """
    lead_id = seed_lead(connection, tenant=TENANT_A, days_ago=200)
    service.purge_tenant(tenant_id=TENANT_A, dry_run=False)
    stamped = payload_of(connection, lead_id)["redacted_at"]

    second = service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert not second.changed
    assert payload_of(connection, lead_id)["redacted_at"] == stamped


def test_a_lead_inside_the_window_keeps_its_payload(
    connection: Connection, service: RetentionService
) -> None:
    lead_id = seed_lead(connection, tenant=TENANT_A, days_ago=10)

    service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert payload_of(connection, lead_id)["email"] == SUBJECT


def test_one_tenants_purge_leaves_the_others_payload_alone(
    connection: Connection, service: RetentionService
) -> None:
    """Invariant 4 against the server: the predicate is in the statement, not in Python."""
    seed_lead(connection, tenant=TENANT_A, days_ago=200)
    theirs = seed_lead(connection, tenant=TENANT_B, days_ago=200, email=BYSTANDER)

    service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert payload_of(connection, theirs)["email"] == BYSTANDER


def test_the_batch_is_a_bound_and_the_job_loops_until_it_drains(
    connection: Connection, service: RetentionService
) -> None:
    """Three expired leads through a batch of one. All three go."""
    for age in (300, 250, 200):
        seed_lead(connection, tenant=TENANT_A, days_ago=age)

    report = service.purge_tenant(tenant_id=TENANT_A, batch_size=1, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 3


def test_a_dry_run_writes_nothing_and_reports_the_same_numbers(
    connection: Connection, service: RetentionService
) -> None:
    lead_id = seed_lead(connection, tenant=TENANT_A, days_ago=200)

    dry = service.purge_tenant(tenant_id=TENANT_A)

    assert dry.count(PurgedRecords.LEAD_PAYLOADS) == 1
    assert payload_of(connection, lead_id)["email"] == SUBJECT


# ------------------------------------------------------------------ tier 2, for real


def test_a_lead_past_tier_two_takes_every_child_with_it(
    connection: Connection, service: RetentionService
) -> None:
    """The composite ``ON DELETE CASCADE``, exercised rather than assumed.

    All four child tables at once, because a cascade that worked for three of them and had
    been forgotten for the fourth is exactly the shape of the bug: the purge would raise a
    foreign-key violation at 2:30am on the one tenant that had used the fourth feature.
    """
    set_windows(connection, TENANT_A, raw=90, assessment=180)
    lead_id = seed_lead(connection, tenant=TENANT_A, days_ago=400)

    report = service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert report.count(PurgedRecords.LEADS) == 1
    children: tuple[tuple[str, Any, Any], ...] = (
        ("leads", Lead.id, Lead.id),
        ("assessments", Assessment.id, Assessment.lead_id),
        ("routing_events", RoutingEvent.id, RoutingEvent.lead_id),
        ("feedback", Feedback.id, Feedback.lead_id),
        ("golden_promotions", GoldenPromotion.id, GoldenPromotion.lead_id),
    )
    for table, identity, reference in children:
        remaining = connection.execute(
            select(identity).where(reference == lead_id)
        ).scalar_one_or_none()
        assert remaining is None, f"{table} survived its lead"


def test_tier_two_deletes_a_lead_whose_payload_is_already_a_tombstone(
    connection: Connection, service: RetentionService
) -> None:
    """Otherwise the containment filter would make a redacted lead undeletable forever."""
    set_windows(connection, TENANT_A, raw=90, assessment=180)
    seed_lead(connection, tenant=TENANT_A, days_ago=400)

    service.purge_tenant(tenant_id=TENANT_A, dry_run=False)
    second = service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert not second.changed
    assert (
        connection.execute(
            select(Lead.id).where(Lead.tenant_id == tenant_uuid(TENANT_A))
        ).scalar_one_or_none()
        is None
    )


def test_no_purge_can_remove_a_tenant(
    connection: Connection, service: RetentionService, seeded_tenants: tuple[str, str]
) -> None:
    """``RESTRICT`` on ``leads.tenant_id``, and no code path that would try.

    Asserted twice: the tenant is still there after a purge that removed every one of its
    leads, and the database refuses a direct delete while a lead remains.
    """
    del seeded_tenants
    set_windows(connection, TENANT_A, raw=90, assessment=180)
    seed_lead(connection, tenant=TENANT_A, days_ago=400)

    service.purge_tenant(tenant_id=TENANT_A, dry_run=False)

    assert (
        connection.execute(
            select(Tenant.slug).where(Tenant.id == tenant_uuid(TENANT_A))
        ).scalar_one()
        == TENANT_A
    )
    seed_lead(connection, tenant=TENANT_A, days_ago=1)
    with pytest.raises(IntegrityError):
        connection.execute(delete(Tenant).where(Tenant.id == tenant_uuid(TENANT_A)))


# ------------------------------------------------------- deletion requests, for real


def test_an_erasure_removes_the_person_and_files_the_evidence(
    connection: Connection, service: RetentionService
) -> None:
    """End to end, and auditable — the issue's second acceptance criterion."""
    seed_lead(connection, tenant=TENANT_A, days_ago=10)
    seed_lead(connection, tenant=TENANT_A, days_ago=20)
    kept = seed_lead(connection, tenant=TENANT_A, days_ago=10, email=BYSTANDER)

    receipt = service.erase_subject(tenant_id=TENANT_A, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 2
    assert receipt.children.assessments == 2
    assert receipt.children.golden_promotions == 2
    assert SUBJECT not in receipt.render()
    remaining = list(
        connection.execute(select(Lead.id).where(Lead.tenant_id == tenant_uuid(TENANT_A))).scalars()
    )
    assert remaining == [kept]

    stored = connection.execute(
        select(
            ErasureLog.subject_hash,
            ErasureLog.leads_deleted,
            ErasureLog.assessments_deleted,
            ErasureLog.requested_by,
        ).where(ErasureLog.tenant_id == tenant_uuid(TENANT_A))
    ).one()
    assert stored == (contact_email_hash(SUBJECT), 2, 2, "SUP-4471")


def test_the_payload_scan_finds_an_address_the_hash_cannot(
    connection: Connection, service: RetentionService
) -> None:
    """The "please cc my colleague" case, against real JSONB.

    The contact field names somebody else, so ``contact_email_hash`` is the bystander's; the
    subject's address is in the message, and ``raw_payload::text ILIKE`` is what finds it.
    """
    seed_lead(
        connection,
        tenant=TENANT_A,
        days_ago=10,
        email=BYSTANDER,
        payload={
            "email": BYSTANDER,
            "message": f"Please copy my colleague {SUBJECT} on anything you send.",
        },
    )

    receipt = service.erase_subject(tenant_id=TENANT_A, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 1
    assert receipt.matched_by_hash == 0
    assert receipt.matched_by_payload_scan == 1


def test_an_erasure_for_somebody_we_hold_nothing_about_still_files_a_row(
    connection: Connection, service: RetentionService
) -> None:
    """ "We checked on this date and held nothing" is only evidence if it was written down."""
    receipt = service.erase_subject(tenant_id=TENANT_A, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 0
    assert (
        connection.execute(
            select(ErasureLog.leads_deleted).where(ErasureLog.tenant_id == tenant_uuid(TENANT_A))
        ).scalar_one()
        == 0
    )


def test_an_erasure_is_per_tenant(connection: Connection, service: RetentionService) -> None:
    """The same person can be a lead of two customers; each controller erases their own."""
    seed_lead(connection, tenant=TENANT_A, days_ago=10)
    theirs = seed_lead(connection, tenant=TENANT_B, days_ago=10)

    service.erase_subject(tenant_id=TENANT_A, email=SUBJECT, requested_by="SUP-4471")

    assert (
        connection.execute(
            select(Lead.id).where(Lead.tenant_id == tenant_uuid(TENANT_B))
        ).scalar_one()
        == theirs
    )


def test_the_database_refuses_an_audit_row_that_names_an_address(
    connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """The CHECK that keeps this table from becoming the last copy of what it destroyed."""
    del seeded_tenants
    with pytest.raises(IntegrityError):
        connection.execute(
            insert(ErasureLog).values(
                tenant_id=tenant_uuid(TENANT_A),
                subject_hash=SUBJECT,
                requested_by="SUP-4471",
                completed_at=NOW,
            )
        )
