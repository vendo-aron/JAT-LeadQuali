"""``PostgresFeedbackStore`` against a real Postgres.

The in-memory double in ``tests/fakes.py`` mirrors this adapter's uniqueness behaviour, and
that is exactly why the adapter needs its own tests: the double asserts what we *believe*
the constraint does. These assert what the database does — that the unique index exists at
all, that ``ON CONFLICT`` resolves against it, that the composite foreign key stops a
verdict being filed against another tenant's lead, and that two simultaneous clicks produce
one row rather than an ``IntegrityError`` at 9am.

Skips rather than fails without ``DATABASE_URL``, like the rest of ``tests/integration``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, Row, create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Feedback
from leadquali.adapters.seed import seed_tenant
from leadquali.adapters.store_postgres import (
    PostgresFeedbackStore,
    PostgresLeadStore,
    tenant_uuid,
)
from leadquali.app.feedback import UnknownLeadError, Verdict, rater_id
from leadquali.prompts.lead import LeadSubmission
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

TENANT_A = "feedback-tenant-a"
TENANT_B = "feedback-tenant-b"
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
RATER = rater_id("sales@example.com")
OTHER_RATER = rater_id("escalations@example.com")


@pytest.fixture(scope="session")
def feedback_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own."""
    name = f"{_database_url.database}_feedback_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def feedback_engine(feedback_database: URL) -> Iterator[Engine]:
    """A normally-pooled engine: one test needs two writers racing each other."""
    engine = create_engine(feedback_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def seeded_tenants(feedback_engine: Engine) -> tuple[str, str]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    )
    with feedback_engine.begin() as connection:
        for slug in (TENANT_A, TENANT_B):
            seed_tenant(connection, {**document, "tenant_id": slug, "name": f"Tenant {slug}"})
    return (TENANT_A, TENANT_B)


@pytest.fixture
def connection(feedback_engine: Engine) -> Iterator[Connection]:
    with feedback_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def store(
    sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]
) -> PostgresFeedbackStore:
    del seeded_tenants  # ordering only: a lead needs its tenant to exist first.
    return PostgresFeedbackStore(sessions)


@pytest.fixture
def lead_id(sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]) -> str:
    del seeded_tenants
    return (
        PostgresLeadStore(sessions)
        .upsert_lead(
            tenant_id=TENANT_A,
            submission_id="feedback-subject-1",
            submission=LeadSubmission(full_name="Dana", email="dana@example.com"),
            source="web_form",
            received_at=NOW,
        )
        .lead_id
    )


def rows(connection: Connection, lead: str) -> list[Row[Any]]:
    return list(connection.execute(select(Feedback).where(Feedback.lead_id == uuid.UUID(lead))))


# --------------------------------------------------------------------------- the write


def test_a_verdict_becomes_a_row_with_its_tenant_lead_rater_and_notes(
    store: PostgresFeedbackStore, connection: Connection, lead_id: str
) -> None:
    recorded = store.record_feedback(
        tenant_id=TENANT_A,
        lead_id=lead_id,
        rater=RATER,
        verdict=Verdict.GOOD,
        notes="Signed within the week.",
        recorded_at=NOW,
    )

    assert recorded.created is True
    assert recorded.previous_verdict is None
    (row,) = rows(connection, lead_id)
    assert row.tenant_id == tenant_uuid(TENANT_A), "the row is filed under the right tenant"
    assert str(row.lead_id) == lead_id
    assert row.rater == RATER
    assert row.verdict == "good"
    assert row.notes == "Signed within the week."
    assert row.created_at == NOW


def test_clicking_twice_updates_one_row(
    store: PostgresFeedbackStore, connection: Connection, lead_id: str
) -> None:
    """The acceptance criterion, against the constraint that actually enforces it."""
    for _ in range(3):
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=lead_id,
            rater=RATER,
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )
    assert len(rows(connection, lead_id)) == 1


def test_a_changed_verdict_replaces_the_old_one_and_reports_it(
    store: PostgresFeedbackStore, connection: Connection, lead_id: str
) -> None:
    store.record_feedback(
        tenant_id=TENANT_A,
        lead_id=lead_id,
        rater=RATER,
        verdict=Verdict.GOOD,
        notes="Looked promising.",
        recorded_at=NOW,
    )
    later = NOW + timedelta(days=3)
    recorded = store.record_feedback(
        tenant_id=TENANT_A,
        lead_id=lead_id,
        rater=RATER,
        verdict=Verdict.BAD,
        notes=None,
        recorded_at=later,
    )

    assert recorded.created is False
    assert recorded.previous_verdict is Verdict.GOOD
    assert recorded.changed is True
    (row,) = rows(connection, lead_id)
    assert row.verdict == "bad"
    assert row.created_at == later, "the row is the current verdict, so it carries its time"
    assert row.notes == "Looked promising.", "a click with no note must not erase one"


def test_a_new_note_replaces_the_old_note(
    store: PostgresFeedbackStore, connection: Connection, lead_id: str
) -> None:
    for note in ("First thought.", "Second thought."):
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=lead_id,
            rater=RATER,
            verdict=Verdict.BAD,
            notes=note,
            recorded_at=NOW,
        )
    (row,) = rows(connection, lead_id)
    assert row.notes == "Second thought."


def test_two_raters_on_one_lead_are_two_rows(
    store: PostgresFeedbackStore, connection: Connection, lead_id: str
) -> None:
    """Uniqueness is per rater: two people disagreeing is data, not a duplicate."""
    for rater, verdict in ((RATER, Verdict.GOOD), (OTHER_RATER, Verdict.BAD)):
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=lead_id,
            rater=rater,
            verdict=verdict,
            notes=None,
            recorded_at=NOW,
        )
    assert len(rows(connection, lead_id)) == 2


@pytest.mark.parametrize("verdict", list(Verdict))
def test_every_verdict_the_domain_defines_is_one_the_schema_accepts(
    store: PostgresFeedbackStore, lead_id: str, verdict: Verdict
) -> None:
    """``Verdict`` and the ``verdict_known`` CHECK constraint must not drift apart."""
    store.record_feedback(
        tenant_id=TENANT_A,
        lead_id=lead_id,
        rater=RATER,
        verdict=verdict,
        notes=None,
        recorded_at=NOW,
    )


# ------------------------------------------------------------------- tenant isolation


def test_another_tenant_cannot_file_a_verdict_against_this_lead(
    store: PostgresFeedbackStore, lead_id: str
) -> None:
    """Invariant 4, enforced by the composite foreign key rather than by good intentions."""
    with pytest.raises(UnknownLeadError):
        store.record_feedback(
            tenant_id=TENANT_B,
            lead_id=lead_id,
            rater=RATER,
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )


def test_a_lead_that_does_not_exist_raises_unknown_lead(store: PostgresFeedbackStore) -> None:
    """The path a link takes once #37's retention job has deleted the lead."""
    with pytest.raises(UnknownLeadError):
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=str(uuid.uuid4()),
            rater=RATER,
            verdict=Verdict.BAD,
            notes=None,
            recorded_at=NOW,
        )


@pytest.mark.parametrize(
    ("tenant_id", "lead", "match"),
    [("not a tenant!", "irrelevant", "tenant"), (TENANT_A, "not-a-uuid", "lead id")],
)
def test_an_unusable_identifier_is_refused_before_the_query(
    store: PostgresFeedbackStore, tenant_id: str, lead: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        store.record_feedback(
            tenant_id=tenant_id,
            lead_id=lead,
            rater=RATER,
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )


def test_a_blank_rater_is_refused(store: PostgresFeedbackStore, lead_id: str) -> None:
    with pytest.raises(ValueError, match="rater"):
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=lead_id,
            rater="",
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )


# ------------------------------------------------------------------------ concurrency


def test_two_simultaneous_clicks_produce_one_row_and_no_error(
    feedback_engine: Engine, seeded_tenants: tuple[str, str]
) -> None:
    """A double tap on a phone, or a client that retries a POST. Both must survive.

    Committing writers, so this one cannot use the rolled-back connection fixture; it
    cleans up after itself instead.
    """
    del seeded_tenants
    sessions = sessionmaker(bind=feedback_engine)
    lead = PostgresLeadStore(sessions).upsert_lead(
        tenant_id=TENANT_A,
        submission_id=f"feedback-race-{uuid.uuid4()}",
        submission=LeadSubmission(full_name="Race"),
        source="web_form",
        received_at=NOW,
    )
    store = PostgresFeedbackStore(sessions)

    def click() -> None:
        store.record_feedback(
            tenant_id=TENANT_A,
            lead_id=lead.lead_id,
            rater=RATER,
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(click), pool.submit(click)]:
                future.result()  # raises here if either writer hit a uniqueness error

        with feedback_engine.connect() as connection:
            assert len(rows(connection, lead.lead_id)) == 1
    finally:
        with feedback_engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM leads WHERE id = %s", (uuid.UUID(lead.lead_id),)
            )
