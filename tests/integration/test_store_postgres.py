"""The Postgres store adapter against a real Postgres.

Two halves. The first is the shared contract from ``tests/contract/lead_store_contract.py``
— the same fourteen behaviours ``tests/unit/test_lead_store_inmemory.py`` runs against the
in-memory double, so the double and the adapter cannot drift apart unnoticed. The second is
everything that is only true of the database: the columns the rows actually carry, the
composite foreign key that stops a lead being filed under the wrong tenant, and the
concurrent redelivery that a ``SELECT``-then-``INSERT`` would lose.

Like the rest of ``tests/integration``, this skips — never fails — when ``DATABASE_URL`` is
unset or nothing is listening, so CI without a service container stays green. Bring one up
with ``docker compose up -d``; ``migrations`` are applied to a throwaway database of this
module's own, never to the one in ``DATABASE_URL``.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, Row, create_engine, delete, insert, select
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Assessment, Lead, RoutingEvent, Tenant
from leadquali.adapters.seed import seed_tenant, tenant_id_for
from leadquali.adapters.store_postgres import (
    UNKNOWN_MODEL_ID,
    UNKNOWN_PROMPT_VERSION,
    PostgresLeadStore,
    PostgresTenantConfigSource,
    contact_email_hash,
)
from leadquali.app.ports import LeadStorePort, RoutingOutcome, StoredLead
from leadquali.domain.models import Action, EscalationReason, Tier
from leadquali.domain.tenant_config import TenantConfigError, TenantNotFoundError
from tests.contract.lead_store_contract import (
    NOW,
    LeadStoreContract,
    make_decision,
    make_failed,
    make_metering,
    make_submission,
    make_succeeded,
)
from tests.integration.conftest import (
    alembic_config,
    database_url_in_environment,
    temporary_database,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Two tenants, both seeded, because half of what is under test here is what one of them
#: cannot see of the other.
TENANT_A = "store-tenant-a"
TENANT_B = "store-tenant-b"


# ------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def store_database(_database_url: URL) -> Iterator[URL]:
    """A migrated throwaway database of this module's own.

    Deliberately not the ``<db>_test`` one ``conftest.migrated_engine`` builds: these tests
    commit rows and run concurrent writers, and sharing a database with the schema suite
    would make either one able to disturb the other.
    """
    name = f"{_database_url.database}_store_test"
    with temporary_database(_database_url, name) as url, database_url_in_environment(url):
        command.upgrade(alembic_config(), "head")
        yield url


@pytest.fixture(scope="session")
def store_engine(store_database: URL) -> Iterator[Engine]:
    """A normally-pooled engine for the throwaway database.

    Not :func:`~leadquali.adapters.store_postgres.engine_for`: that one is deliberately
    capped at a single connection per process, which is right for a Lambda container and
    wrong for a test that needs two writers racing each other.
    """
    engine = create_engine(store_database)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def default_config_document() -> dict[str, Any]:
    """The shipped tenant config, used as the shape of every seeded tenant here."""
    raw = (REPO_ROOT / "tenants" / "default.json").read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(raw)
    return document


@pytest.fixture(scope="session")
def seeded_tenants(
    store_engine: Engine, default_config_document: dict[str, Any]
) -> tuple[str, str]:
    """Two committed tenant rows. Leads cannot exist without one (``ON DELETE RESTRICT``)."""
    with store_engine.begin() as connection:
        for slug in (TENANT_A, TENANT_B):
            seed_tenant(
                connection,
                {**default_config_document, "tenant_id": slug, "name": f"Tenant {slug}"},
            )
    return (TENANT_A, TENANT_B)


@pytest.fixture
def connection(store_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back, so tests cannot see each other."""
    with store_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def sessions(connection: Connection) -> sessionmaker[Session]:
    """Sessions joined to the test's transaction as savepoints.

    The adapter commits in every method — that is the behaviour under test — so the only
    way to keep tests from leaking rows into each other is to make those commits release a
    savepoint inside a transaction the fixture rolls back.
    """
    return sessionmaker(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def store(sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]) -> PostgresLeadStore:
    del seeded_tenants  # ordering only: the tenants must exist before any lead does.
    return PostgresLeadStore(sessions)


def lead_row(connection: Connection, lead_id: str) -> Row[Any]:
    return connection.execute(select(Lead).where(Lead.id == uuid.UUID(lead_id))).one()


def assessment_rows(connection: Connection, lead_id: str) -> list[Row[Any]]:
    return list(
        connection.execute(select(Assessment).where(Assessment.lead_id == uuid.UUID(lead_id)))
    )


def routing_rows(connection: Connection, lead_id: str) -> list[Row[Any]]:
    return list(
        connection.execute(
            select(RoutingEvent)
            .where(RoutingEvent.lead_id == uuid.UUID(lead_id))
            .order_by(RoutingEvent.created_at)
        )
    )


# --------------------------------------------------------------- the shared contract


class TestPostgresLeadStoreContract(LeadStoreContract):
    """Every behaviour the port promises, checked against Postgres."""

    @pytest.fixture
    def store(self, store: PostgresLeadStore) -> LeadStorePort:
        return store

    @pytest.fixture
    def tenants(self, seeded_tenants: tuple[str, str]) -> tuple[str, str]:
        return seeded_tenants


# ------------------------------------------------------------------- what rows carry


def test_a_stored_lead_carries_the_payload_the_hash_and_the_timestamps(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    submission = make_submission(email="Ada@Example.com")
    stored = store.upsert_lead(
        tenant_id=tenant,
        submission_id="row-shape",
        submission=submission,
        source="webhook",
        received_at=NOW,
    )

    row = lead_row(connection, stored.lead_id)
    assert row.tenant_id == tenant_id_for(tenant)
    assert row.source == "webhook"
    assert row.received_at == NOW
    assert row.status == "received"
    # Invariant 5: the address lives in raw_payload and nowhere else, and the column that
    # logs may quote is its hash of the *normalised* address.
    assert row.contact_email_hash == contact_email_hash("ada@example.com")
    assert row.raw_payload["email"] == "Ada@Example.com"


def test_a_lead_with_no_email_hashes_to_null(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    stored = store.upsert_lead(
        tenant_id=tenant,
        submission_id="no-email",
        submission=make_submission(email=None),
        source="web_form",
        received_at=NOW,
    )
    assert lead_row(connection, stored.lead_id).contact_email_hash is None


def test_a_redelivery_does_not_overwrite_what_first_arrived(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """The first delivery is the record of what arrived and when. A retry that rewrote it
    would erase the queue latency ``received_at`` exists to measure."""
    tenant, _ = seeded_tenants
    first = store.upsert_lead(
        tenant_id=tenant,
        submission_id="no-clobber",
        submission=make_submission(message="original"),
        source="web_form",
        received_at=NOW,
    )
    later = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
    second = store.upsert_lead(
        tenant_id=tenant,
        submission_id="no-clobber",
        submission=make_submission(message="rewritten"),
        source="csv_import",
        received_at=later,
    )

    assert second.lead_id == first.lead_id
    row = lead_row(connection, first.lead_id)
    assert row.raw_payload["message"] == "original"
    assert row.source == "web_form"
    assert row.received_at == NOW


def test_a_blank_submission_id_is_refused_before_it_reaches_the_database(
    store: PostgresLeadStore, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    with pytest.raises(ValueError, match="submission_id"):
        store.upsert_lead(
            tenant_id=tenant,
            submission_id="",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )


def test_a_lead_for_an_unseeded_tenant_is_refused_by_the_foreign_key(
    store: PostgresLeadStore,
) -> None:
    with pytest.raises(IntegrityError):
        store.upsert_lead(
            tenant_id="no-such-tenant",
            submission_id="orphan",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )


def test_a_tenant_id_that_is_neither_a_uuid_nor_a_slug_is_refused(
    store: PostgresLeadStore,
) -> None:
    """Tenant ids arrive from queue messages and request payloads."""
    with pytest.raises(ValueError, match="neither a UUID nor a valid slug"):
        store.already_routed(tenant_id="../../etc/passwd", lead_id=str(uuid.uuid4()))


# -------------------------------------------------------------------- assessments


def test_a_successful_assessment_records_the_judgment_the_verdict_and_the_cost(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="assess-ok",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_assessment(
        tenant_id=tenant,
        lead_id=lead,
        outcome=make_succeeded(confidence=0.815),
        decision=make_decision(tier=Tier.HOT, total_score=88.25),
        recorded_at=NOW,
    )

    (row,) = assessment_rows(connection, lead)
    assert row.status == "ok"
    assert row.escalation_reason is None
    assert row.tier == "hot"
    assert row.total_score == Decimal("88.25")
    assert row.confidence == Decimal("0.815")
    assert row.dimension_scores["icp_fit"] == 24
    assert row.extracted["industry"] == "B2B software"
    assert row.reasoning
    assert row.missing_information == ["budget"]
    assert row.created_at == NOW
    # Provenance and cost, so "did Tuesday's prompt change make things worse?" and the
    # per-tenant billing SUM are both queries rather than opinions.
    assert (row.model_id, row.prompt_version, row.effort) == (
        "claude-opus-5",
        "rubric_v1",
        "medium",
    )
    assert (row.input_tokens, row.output_tokens) == (1_200, 380)
    assert (row.cache_read_tokens, row.cache_creation_tokens) == (4_800, 0)
    assert row.cost_usd == Decimal("0.012345")
    assert row.latency_ms == 2_100
    # An assessment exists, so the lead is qualified — not yet routed.
    assert lead_row(connection, lead).status == "qualified"


def test_a_failed_assessment_records_the_reason_and_leaves_the_model_columns_null(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """The row #15's ``output_present_iff_ok`` CHECK exists to make possible: an attempt
    that produced no judgment is still auditable, which is what invariant 3 needs."""
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="assess-failed",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_assessment(
        tenant_id=tenant,
        lead_id=lead,
        outcome=make_failed(reason=EscalationReason.TIMEOUT, latency_ms=30_000),
        decision=make_decision(
            escalation_reason=EscalationReason.TIMEOUT,
            total_score=0.0,
            note="The system could not assess this lead.",
        ),
        recorded_at=NOW,
    )

    (row,) = assessment_rows(connection, lead)
    assert row.status == "failed"
    assert row.escalation_reason == "timeout"
    assert row.tier is None
    assert row.total_score is None
    assert row.dimension_scores is None
    assert row.extracted is None
    assert row.reasoning is None
    assert row.confidence is None
    assert row.missing_information == []
    # An attempt that never reached the model has no provenance to report, and the columns
    # are NOT NULL: a sentinel is honest, a guess would be a lie in the billing table.
    assert (row.model_id, row.prompt_version) == (UNKNOWN_MODEL_ID, UNKNOWN_PROMPT_VERSION)
    assert row.effort is None
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, Decimal("0.000000"))
    # The attempt still took time, and how long is the operator's first question.
    assert row.latency_ms == 30_000
    assert lead_row(connection, lead).status == "failed"


def test_a_billed_refusal_is_metered_even_though_it_produced_nothing(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """A refusal is an HTTP 200 and Anthropic charges for it; so is a ``max_tokens``
    truncation. A billed failure that left no metering is money the business cannot see."""
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="assess-refusal",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_assessment(
        tenant_id=tenant,
        lead_id=lead,
        outcome=make_failed(
            reason=EscalationReason.MODEL_REFUSAL,
            metering=make_metering(model_id="claude-opus-5", latency_ms=1_500),
        ),
        decision=make_decision(escalation_reason=EscalationReason.MODEL_REFUSAL),
        recorded_at=NOW,
    )

    (row,) = assessment_rows(connection, lead)
    assert row.status == "failed"
    assert row.escalation_reason == "model_refusal"
    assert row.model_id == "claude-opus-5"
    assert row.cost_usd == Decimal("0.012345")
    assert row.input_tokens == 1_200
    assert row.latency_ms == 1_500


def test_a_low_confidence_escalation_keeps_the_model_output_and_the_reason(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="assess-low-confidence",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_assessment(
        tenant_id=tenant,
        lead_id=lead,
        outcome=make_succeeded(confidence=0.2),
        decision=make_decision(escalation_reason=EscalationReason.LOW_CONFIDENCE),
        recorded_at=NOW,
    )

    (row,) = assessment_rows(connection, lead)
    assert row.status == "ok"
    assert row.escalation_reason == "low_confidence"
    assert row.confidence == Decimal("0.200")
    assert row.tier == "warm"


# ----------------------------------------------------------------- routing events


def test_a_dispatch_stamps_the_time_and_the_receipt_and_routes_the_lead(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="route-dispatched",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_routing_event(
        tenant_id=tenant,
        lead_id=lead,
        action=Action.EMAIL_SALES,
        destination="sales@example.invalid",
        outcome=RoutingOutcome.DISPATCHED,
        provider_message_id="ses-42",
        occurred_at=NOW,
        detail="Routed to sales.",
    )

    (row,) = routing_rows(connection, lead)
    assert row.action == "email_sales"
    assert row.destination == "sales@example.invalid"
    assert row.provider_message_id == "ses-42"
    assert row.dispatched_at == NOW
    assert lead_row(connection, lead).status == "routed"


def test_a_suppression_is_recorded_with_no_destination_and_no_dispatch_time(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """ "We never contacted this lead, and here is why" is an answer the business needs."""
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="route-suppressed",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    store.record_routing_event(
        tenant_id=tenant,
        lead_id=lead,
        action=Action.SUPPRESS,
        destination=None,
        outcome=RoutingOutcome.SUPPRESSED,
        provider_message_id=None,
        occurred_at=NOW,
        detail="Spam or test submission.",
    )

    (row,) = routing_rows(connection, lead)
    assert row.action == "suppress"
    assert row.destination is None
    assert row.dispatched_at is None
    assert store.already_routed(tenant_id=tenant, lead_id=lead) is True


def test_a_failed_send_is_visible_but_leaves_the_lead_unrouted(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    """A lead stuck behind a broken notifier has to be visible rather than merely absent —
    and still retryable."""
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id="route-failed",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id
    store.record_assessment(
        tenant_id=tenant,
        lead_id=lead,
        outcome=make_succeeded(),
        decision=make_decision(),
        recorded_at=NOW,
    )

    store.record_routing_event(
        tenant_id=tenant,
        lead_id=lead,
        action=Action.EMAIL_SALES,
        destination="sales@example.invalid",
        outcome=RoutingOutcome.FAILED,
        provider_message_id=None,
        occurred_at=NOW,
        detail="dispatch failed: ConnectionError",
    )

    (row,) = routing_rows(connection, lead)
    assert row.dispatched_at is None
    assert row.provider_message_id is None
    assert store.already_routed(tenant_id=tenant, lead_id=lead) is False
    # Still 'qualified': a lead that never went anywhere has not been routed.
    assert lead_row(connection, lead).status == "qualified"


@pytest.mark.parametrize(
    ("action", "outcome"),
    [
        (Action.EMAIL_SALES, RoutingOutcome.SUPPRESSED),
        (Action.SUPPRESS, RoutingOutcome.DISPATCHED),
        (Action.SUPPRESS, RoutingOutcome.FAILED),
    ],
)
def test_an_action_and_outcome_that_disagree_about_suppression_are_refused(
    store: PostgresLeadStore,
    connection: Connection,
    seeded_tenants: tuple[str, str],
    action: Action,
    outcome: RoutingOutcome,
) -> None:
    """Suppression is encoded *as* the action, so a row where the two disagree would be
    read back wrongly by ``already_routed`` — as a routed lead that was never sent, or a
    suppressed one still waiting. Refused rather than silently mis-recorded."""
    tenant, _ = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant,
        submission_id=f"route-mismatch-{action.value}-{outcome.value}",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    with pytest.raises(ValueError, match="disagree"):
        store.record_routing_event(
            tenant_id=tenant,
            lead_id=lead,
            action=action,
            destination=None,
            outcome=outcome,
            provider_message_id=None,
            occurred_at=NOW,
            detail="nonsense",
        )
    assert routing_rows(connection, lead) == []


# --------------------------------------------------------------- tenant isolation


def test_one_tenant_cannot_read_another_tenants_lead(
    store: PostgresLeadStore, seeded_tenants: tuple[str, str]
) -> None:
    """The seed of #32's isolation suite. Tenant B holds tenant A's lead id — the strongest
    thing a confused or hostile caller could hold — and still learns nothing."""
    tenant_a, tenant_b = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant_a,
        submission_id="isolation",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id
    store.record_routing_event(
        tenant_id=tenant_a,
        lead_id=lead,
        action=Action.EMAIL_SALES,
        destination="sales@example.invalid",
        outcome=RoutingOutcome.DISPATCHED,
        provider_message_id="ses-private",
        occurred_at=NOW,
        detail="Routed to sales.",
    )

    assert store.already_routed(tenant_id=tenant_b, lead_id=lead) is False


def test_one_tenant_cannot_write_against_another_tenants_lead(
    store: PostgresLeadStore, seeded_tenants: tuple[str, str]
) -> None:
    """The composite ``(tenant_id, lead_id)`` foreign key, doing the job two independent
    foreign keys could not: the lead exists *and* it belongs to the writing tenant."""
    tenant_a, tenant_b = seeded_tenants
    lead = store.upsert_lead(
        tenant_id=tenant_a,
        submission_id="isolation-write",
        submission=make_submission(),
        source="web_form",
        received_at=NOW,
    ).lead_id

    with pytest.raises(IntegrityError):
        store.record_assessment(
            tenant_id=tenant_b,
            lead_id=lead,
            outcome=make_succeeded(),
            decision=make_decision(),
            recorded_at=NOW,
        )


def test_a_tenants_lead_listing_never_includes_another_tenants_rows(
    store: PostgresLeadStore, connection: Connection, seeded_tenants: tuple[str, str]
) -> None:
    tenant_a, tenant_b = seeded_tenants
    for tenant in (tenant_a, tenant_b):
        store.upsert_lead(
            tenant_id=tenant,
            submission_id="same-id-both-tenants",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )

    rows = connection.execute(
        select(Lead.id).where(
            Lead.tenant_id == tenant_id_for(tenant_a),
            Lead.submission_id == "same-id-both-tenants",
        )
    ).all()
    assert len(rows) == 1


# ----------------------------------------------------------- concurrent redelivery


def test_two_concurrent_deliveries_of_one_lead_produce_one_row_and_one_new(
    store_engine: Engine, seeded_tenants: tuple[str, str]
) -> None:
    """The race a ``SELECT``-then-``INSERT`` loses.

    Two workers pick up the same redelivered SQS message and reach ``upsert_lead`` at the
    same instant. Exactly one row must exist, both callers must get its id, and exactly one
    of them may be told the lead is new — otherwise two workers both believe they are the
    first, and sales is emailed twice.

    Real connections and real threads: the whole point is what the *database* does when two
    transactions conflict, which a fixture that shares one transaction cannot show.
    """
    tenant, _ = seeded_tenants
    store = PostgresLeadStore(sessionmaker(bind=store_engine))
    submission_id = f"concurrent-{uuid.uuid4()}"
    barrier = threading.Barrier(2)

    def deliver() -> tuple[str, bool]:
        barrier.wait(timeout=10)
        stored = store.upsert_lead(
            tenant_id=tenant,
            submission_id=submission_id,
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        return (stored.lead_id, stored.is_new)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=30) for future in [pool.submit(deliver) for _ in range(2)]
            ]

        lead_ids = {lead_id for lead_id, _ in results}
        assert len(lead_ids) == 1, "the two deliveries disagree about the lead's identity"
        assert [is_new for _, is_new in results].count(True) == 1, (
            "exactly one delivery may be told the lead is new"
        )

        with store_engine.connect() as conn:
            rows = conn.execute(
                select(Lead.id).where(
                    Lead.tenant_id == tenant_id_for(tenant), Lead.submission_id == submission_id
                )
            ).all()
        assert len(rows) == 1
    finally:
        with store_engine.begin() as conn:
            conn.execute(
                delete(Lead).where(
                    Lead.tenant_id == tenant_id_for(tenant), Lead.submission_id == submission_id
                )
            )


def test_a_redelivery_arriving_mid_insert_waits_and_reports_the_existing_lead(
    store_engine: Engine, seeded_tenants: tuple[str, str]
) -> None:
    """The same race as above, made deterministic rather than hoped for.

    One transaction has inserted the lead and not yet committed when the redelivery
    arrives. ``ON CONFLICT DO UPDATE`` must *wait* for it and then report the row it finds:
    a ``SELECT``-then-``INSERT`` would raise a uniqueness error here, and ``DO NOTHING``
    would return no row at all because the winner's insert is not visible yet — either way
    a worker would be told a stored lead had failed, and the lead would be processed twice
    or not at all.
    """
    tenant, _ = seeded_tenants
    store = PostgresLeadStore(sessionmaker(bind=store_engine))
    submission_id = f"midflight-{uuid.uuid4()}"
    redelivered: list[StoredLead] = []
    errors: list[BaseException] = []

    def redeliver() -> None:
        try:
            redelivered.append(
                store.upsert_lead(
                    tenant_id=tenant,
                    submission_id=submission_id,
                    submission=make_submission(),
                    source="web_form",
                    received_at=NOW,
                )
            )
        except BaseException as error:  # reported on the main thread, where it fails the test
            errors.append(error)

    try:
        with store_engine.connect() as writer:
            transaction = writer.begin()
            first_id = writer.execute(
                insert(Lead)
                .values(
                    tenant_id=tenant_id_for(tenant),
                    submission_id=submission_id,
                    raw_payload={"source": "first delivery"},
                    source="web_form",
                    received_at=NOW,
                )
                .returning(Lead.id)
            ).scalar_one()

            thread = threading.Thread(target=redeliver, daemon=True)
            thread.start()
            thread.join(timeout=2)
            assert thread.is_alive(), "the redelivery did not wait for the in-flight insert"

            transaction.commit()
            thread.join(timeout=30)
            assert not thread.is_alive(), "the redelivery never finished"

        assert errors == []
        (stored,) = redelivered
        assert stored.lead_id == str(first_id)
        assert stored.is_new is False
    finally:
        with store_engine.begin() as conn:
            conn.execute(
                delete(Lead).where(
                    Lead.tenant_id == tenant_id_for(tenant), Lead.submission_id == submission_id
                )
            )


def test_the_lambda_shaped_engine_really_connects(
    store_database: URL, seeded_tenants: tuple[str, str]
) -> None:
    """``from_url`` builds the engine the Lambda uses — single connection, pre-ping,
    prepared statements off for RDS Proxy. Those settings are only worth having if a real
    server accepts them."""
    tenant, _ = seeded_tenants
    store = PostgresLeadStore.from_url(store_database.render_as_string(hide_password=False))
    submission_id = f"lambda-engine-{uuid.uuid4()}"
    try:
        stored = store.upsert_lead(
            tenant_id=tenant,
            submission_id=submission_id,
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )
        assert stored.is_new is True
        assert store.already_routed(tenant_id=tenant, lead_id=stored.lead_id) is False
    finally:
        engine = create_engine(store_database)
        with engine.begin() as conn:
            conn.execute(
                delete(Lead).where(
                    Lead.tenant_id == tenant_id_for(tenant), Lead.submission_id == submission_id
                )
            )
        engine.dispose()


# ------------------------------------------------------------- tenant config source


def test_the_config_source_loads_a_seeded_tenants_rubric(
    sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    config = PostgresTenantConfigSource(sessions).get(tenant)
    assert config.tenant_id == tenant
    assert config.routing_rules[Tier.HOT].destination == "sales@example.invalid"
    assert config.prompt_version == "rubric_v1"


def test_the_config_source_accepts_the_row_id_as_well_as_the_slug(
    sessions: sessionmaker[Session], seeded_tenants: tuple[str, str]
) -> None:
    tenant, _ = seeded_tenants
    config = PostgresTenantConfigSource(sessions).get(str(tenant_id_for(tenant)))
    assert config.tenant_id == tenant


def test_an_unknown_tenant_is_not_found(sessions: sessionmaker[Session]) -> None:
    with pytest.raises(TenantNotFoundError):
        PostgresTenantConfigSource(sessions).get("never-seeded-tenant")


def test_an_invalid_rubric_is_a_config_error_not_a_silent_default(
    connection: Connection, sessions: sessionmaker[Session]
) -> None:
    """A tenant whose policy cannot be loaded must not have its leads routed by someone
    else's policy — so this raises rather than falling back to the defaults."""
    slug = "broken-rubric-tenant"
    connection.execute(
        insert(Tenant).values(
            id=tenant_id_for(slug),
            name="Broken",
            icp_config={"tenant_id": slug, "name": "Broken"},
        )
    )
    with pytest.raises(TenantConfigError):
        PostgresTenantConfigSource(sessions).get(slug)


def test_a_row_whose_config_names_a_different_tenant_is_refused(
    connection: Connection, sessions: sessionmaker[Session], default_config_document: dict[str, Any]
) -> None:
    slug = "mislabelled-tenant"
    connection.execute(
        insert(Tenant).values(
            id=tenant_id_for(slug),
            name="Mislabelled",
            icp_config={**default_config_document, "tenant_id": "someone-else"},
        )
    )
    with pytest.raises(TenantConfigError, match="must agree"):
        PostgresTenantConfigSource(sessions).get(slug)
