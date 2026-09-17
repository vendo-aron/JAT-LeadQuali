"""The admin's stores against real Postgres: atomicity, the query plan, idempotency.

Everything the unit suite could check without a database, it checks. What is left needs
one, and it is the half that decides whether the guarantees are real:

* the config write and its audit row are **one transaction** in Postgres, not merely in a
  fake that models one;
* ``UNIQUE (tenant_id, version)`` is what allocates a version number, so two writers cannot
  both take the same one;
* the feedback-review query uses an **index scan** rather than a sequential scan on
  ``assessments`` — the regression that makes the "under a second on a realistic dataset"
  criterion fail, checked from ``EXPLAIN`` rather than from a stopwatch, because a plan is
  checkable on a small dataset and a timing is not;
* promoting one lead twice inserts one row, enforced by the database rather than by a
  preceding ``SELECT``.

Skipped without Postgres, like every other test in this package. **They have never been
run in the environment this was written in** — there is no Docker there — so they are
written to be correct by reading and to fail loudly rather than silently if they are not.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Assessment, Feedback, Lead, Tenant, TenantConfigVersion
from leadquali.adapters.store_admin import (
    PostgresAdminQueryStore,
    PostgresConfigVersionStore,
    PostgresGoldenPromotionStore,
    tenant_uuid,
)
from leadquali.adapters.store_tenants import PostgresTenantAdminStore
from leadquali.adapters.unit_of_work import PostgresUnitOfWork
from leadquali.app.admin_views import LeadFilter
from leadquali.app.config_versions import ConfigEditor, UnknownConfigVersionError
from leadquali.app.feedback import Verdict
from leadquali.app.tenants import TenantService
from leadquali.domain.models import Tier
from tests.fakes import FakeClock, FakeSecretHasher, FakeTenantSecrets

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
SLUG = "acme"


def a_config(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "tenant_id": SLUG,
        "name": "Acme",
        "icp_description": "Mid-market logistics companies with a revenue team.",
        "thresholds": {"hot": 80.0, "warm": 55.0, "cold": 30.0},
        "routing_rules": {
            "hot": {"action": "email_sales", "destination": "hot@example.com"},
            "warm": {"action": "email_sales", "destination": "warm@example.com"},
            "cold": {"action": "email_sales", "destination": "cold@example.com"},
            "disqualified": {"action": "suppress"},
        },
    }
    document.update(overrides)
    return document


@pytest.fixture
def sessions(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A session factory over the migrated throwaway database, emptied afterwards.

    These tests commit — atomicity is the thing under test, so the connection-level
    rollback the ``db`` fixture gives would hide exactly what they are checking. Cleanup is
    therefore a truncate rather than a rollback.
    """
    factory = sessionmaker(migrated_engine)
    try:
        yield factory
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE golden_promotions, feedback, routing_events, assessments, "
                    "leads, tenant_config_versions, usage_daily, tenant_api_keys, tenants "
                    "RESTART IDENTITY CASCADE"
                )
            )


@pytest.fixture
def tenant(sessions: sessionmaker[Session]) -> uuid.UUID:
    """One tenant on file, with its seeded version 1."""
    service = TenantService(
        store=PostgresTenantAdminStore(sessions),
        hasher=FakeSecretHasher(),
        secrets=FakeTenantSecrets(),
        clock=FakeClock(start=NOW, step_ms=0),
    )
    service.create_tenant(slug=SLUG, name="Acme", config=a_config())
    PostgresConfigVersionStore(sessions).append(
        tenant_slug=SLUG,
        config=a_config(),
        changed_by="migration",
        changed_at=NOW,
        note="seeded",
    )
    return tenant_uuid(SLUG)


def editor(sessions: sessionmaker[Session], *, versions: Any = None) -> ConfigEditor:
    service = TenantService(
        store=PostgresTenantAdminStore(sessions),
        hasher=FakeSecretHasher(),
        secrets=FakeTenantSecrets(),
        clock=FakeClock(start=NOW, step_ms=0),
    )
    return ConfigEditor(
        tenants=service,
        versions=versions if versions is not None else PostgresConfigVersionStore(sessions),
        unit_of_work=PostgresUnitOfWork(sessions),
        clock=FakeClock(start=NOW, step_ms=0),
    )


def stored_config(sessions: sessionmaker[Session]) -> dict[str, Any]:
    with sessions.begin() as session:
        return dict(session.execute(select(Tenant.icp_config).where(Tenant.slug == SLUG)).one()[0])


# --------------------------------------------------------------------- the transaction


def test_a_config_change_and_its_audit_row_commit_together(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    del tenant
    editor(sessions).apply(
        slug=SLUG, document=a_config(min_confidence=0.75), changed_by="ada", note="tighter"
    )

    assert stored_config(sessions)["min_confidence"] == 0.75
    history = PostgresConfigVersionStore(sessions).list_versions(tenant_slug=SLUG)
    assert [version.version for version in history] == [2, 1]
    assert history[0].changed_by == "ada"


class ExplodingVersions(PostgresConfigVersionStore):
    """The real store, whose append raises after the config write has happened.

    Subclassed rather than replaced so the surrounding transaction is the real one: the
    point of the test is that Postgres rolls the config write back, not that a double
    remembered to.
    """

    def append(self, **kwargs: Any) -> Any:
        raise RuntimeError("the version insert failed")


def test_a_failure_between_the_two_writes_leaves_neither(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """A saved config with no audit row must be impossible, not merely unlikely."""
    del tenant
    before = stored_config(sessions)

    with pytest.raises(RuntimeError, match="the version insert failed"):
        editor(sessions, versions=ExplodingVersions(sessions)).apply(
            slug=SLUG, document=a_config(min_confidence=0.75), changed_by="ada", note=None
        )

    assert stored_config(sessions) == before
    assert len(PostgresConfigVersionStore(sessions).list_versions(tenant_slug=SLUG)) == 1


def test_the_version_number_comes_from_the_table(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    del tenant
    store = PostgresConfigVersionStore(sessions)

    second = store.append(
        tenant_slug=SLUG, config=a_config(), changed_by="ada", changed_at=NOW, note=None
    )
    third = store.append(
        tenant_slug=SLUG, config=a_config(), changed_by="ada", changed_at=NOW, note=None
    )

    assert (second.version, third.version) == (2, 3)


def test_two_writers_cannot_take_the_same_version_number(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """``UNIQUE (tenant_id, version)`` is the mechanism, not belt and braces.

    Driven by inserting the row a concurrent writer would have inserted, which is the state
    the losing transaction finds itself in.
    """
    del tenant
    with sessions.begin() as session:
        session.execute(
            insert(TenantConfigVersion).values(
                tenant_id=tenant_uuid(SLUG),
                version=2,
                config=a_config(),
                changed_by="somebody-else",
                changed_at=NOW,
                note=None,
            )
        )
    with (
        # Any integrity failure is a pass: the point is that the database refuses,
        # not which of its errors surfaces through the driver.
        pytest.raises(Exception),  # noqa: B017
        sessions.begin() as session,
    ):
        session.execute(
            insert(TenantConfigVersion).values(
                tenant_id=tenant_uuid(SLUG),
                version=2,
                config=a_config(),
                changed_by="ada",
                changed_at=NOW,
                note=None,
            )
        )


def test_a_version_for_a_tenant_that_does_not_exist_is_refused(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    del tenant
    with pytest.raises(UnknownConfigVersionError):
        PostgresConfigVersionStore(sessions).get_version(tenant_slug=SLUG, version=99)


# ------------------------------------------------------------------------ the query plan


def seed_leads(sessions: sessionmaker[Session], *, count: int) -> list[uuid.UUID]:
    """Enough rows for the planner to have a choice, and every one of them a disagreement."""
    ids: list[uuid.UUID] = []
    with sessions.begin() as session:
        for index in range(count):
            lead_id = uuid.uuid4()
            ids.append(lead_id)
            session.execute(
                insert(Lead).values(
                    id=lead_id,
                    tenant_id=tenant_uuid(SLUG),
                    submission_id=f"sub-{index:05d}",
                    raw_payload={"email": f"lead{index}@example.invalid", "message": "hello"},
                    source="web_form",
                    received_at=NOW - dt.timedelta(minutes=index),
                )
            )
            session.execute(
                insert(Assessment).values(
                    tenant_id=tenant_uuid(SLUG),
                    lead_id=lead_id,
                    created_at=NOW - dt.timedelta(minutes=index),
                    status="ok",
                    tier="hot",
                    total_score=Decimal("82.00"),
                    dimension_scores={"icp_fit": 28},
                    extracted={"industry": "logistics", "company_name": "Northwind"},
                    reasoning="strong fit",
                    confidence=Decimal("0.900"),
                    model_id="claude-test",
                    prompt_version="v1",
                    input_tokens=1000,
                    cost_usd=Decimal("0.018"),
                )
            )
            session.execute(
                insert(Feedback).values(
                    tenant_id=tenant_uuid(SLUG),
                    lead_id=lead_id,
                    rater="rep-1",
                    verdict="bad",
                    created_at=NOW - dt.timedelta(minutes=index),
                )
            )
    return ids


def explain(sessions: sessionmaker[Session], statement: str, parameters: dict[str, Any]) -> str:
    with sessions.begin() as session:
        rows = session.execute(text(f"EXPLAIN {statement}"), parameters).all()
    return "\n".join(str(row[0]) for row in rows)


def test_the_hot_but_marked_bad_query_uses_an_index_rather_than_a_seq_scan(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """The regression that makes the sub-second criterion fail, caught without a stopwatch.

    A plan is checkable on a small dataset; a timing is not. ``enable_seqscan = off`` is
    *not* used — that would force the answer this test is trying to observe. Instead the
    table is given enough rows and analysed, so the planner makes a real choice.
    """
    seed_leads(sessions, count=2_000)
    with sessions.begin() as session:
        session.execute(text("ANALYZE assessments"))
        session.execute(text("ANALYZE feedback"))

    plan = explain(
        sessions,
        """
        SELECT a.lead_id FROM assessments a
        JOIN feedback f ON f.tenant_id = a.tenant_id AND f.lead_id = a.lead_id
        WHERE a.tenant_id = :tenant AND a.tier = 'hot'
          AND a.created_at >= :low AND a.created_at < :high
          AND f.verdict = 'bad'
        ORDER BY a.created_at DESC LIMIT 500
        """,
        {
            "tenant": tenant_uuid(SLUG),
            "low": NOW - dt.timedelta(days=30),
            "high": NOW + dt.timedelta(days=1),
        },
    )

    assert "Seq Scan on assessments" not in plan, plan
    assert "ix_assessments_tenant_id_tier_created_at" in plan, plan


def test_the_lead_browser_pages_off_the_same_index(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """A keyset page must be an index scan too; otherwise page 500 is a table scan."""
    del tenant
    seed_leads(sessions, count=2_000)
    with sessions.begin() as session:
        session.execute(text("ANALYZE assessments"))

    plan = explain(
        sessions,
        """
        SELECT a.id FROM assessments a
        WHERE a.tenant_id = :tenant AND a.tier = 'hot'
          AND (a.created_at, a.id) < (:created_at, :row_id)
        ORDER BY a.created_at DESC, a.id DESC LIMIT 51
        """,
        {
            "tenant": tenant_uuid(SLUG),
            "created_at": NOW - dt.timedelta(minutes=500),
            "row_id": uuid.uuid4(),
        },
    )

    assert "Seq Scan on assessments" not in plan, plan


def test_the_browser_and_the_review_return_what_the_unit_tests_assume(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """The in-memory double claims to behave like this store; here it is, for real."""
    del tenant
    seed_leads(sessions, count=5)
    store = PostgresAdminQueryStore(sessions)

    page = store.browse_leads(
        criteria=LeadFilter(tenant_slug=SLUG, tier=Tier.HOT), cursor=None, limit=3
    )
    assert len(page.rows) == 3
    assert page.next_cursor is not None
    assert page.rows[0].industry == "logistics"
    assert page.rows[0].verdict is Verdict.BAD

    resumed = store.browse_leads(
        criteria=LeadFilter(tenant_slug=SLUG, tier=Tier.HOT), cursor=page.next_cursor, limit=3
    )
    assert len(resumed.rows) == 2
    assert not {row.lead_id for row in page.rows} & {row.lead_id for row in resumed.rows}

    review = store.feedback_review(
        tenant_slug=SLUG,
        tier=Tier.HOT,
        verdict=Verdict.BAD,
        start=(NOW - dt.timedelta(days=30)).date(),
        end=NOW.date(),
        limit=50,
    )
    assert len(review) == 5


def test_a_lead_is_invisible_to_another_tenant(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """Invariant 4, at the store rather than in a docstring."""
    del tenant
    lead_ids = seed_leads(sessions, count=1)

    store = PostgresAdminQueryStore(sessions)

    assert store.lead_detail(tenant_slug=SLUG, lead_id=str(lead_ids[0])) is not None
    assert store.lead_detail(tenant_slug="someone-else", lead_id=str(lead_ids[0])) is None


# ------------------------------------------------------------------------ idempotency


def test_promoting_one_lead_twice_inserts_one_row(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    """Enforced by ``uq_golden_promotions_tenant_id_lead_id``, not by a preceding SELECT."""
    del tenant
    lead_id = str(seed_leads(sessions, count=1)[0])
    store = PostgresGoldenPromotionStore(sessions)
    arguments: dict[str, Any] = {
        "tenant_slug": SLUG,
        "lead_id": lead_id,
        "case_id": "real_acme_00000001",
        "expected_tier": Tier.WARM,
        "promoted_by": "icp_owner",
        "note": "warm is the honest answer here, the contact has no budget authority",
        "promoted_at": NOW,
    }

    first, created_first = store.record(**arguments)
    second, created_second = store.record(**{**arguments, "expected_tier": Tier.HOT})

    assert created_first is True
    assert created_second is False
    assert second.expected_tier is Tier.WARM, "the first, reviewed label must not be replaced"
    assert first == second
    assert len(store.list_promotions(tenant_slug=SLUG)) == 1


def test_promoted_lead_ids_answers_for_a_page_in_one_query(
    sessions: sessionmaker[Session], tenant: uuid.UUID
) -> None:
    del tenant
    lead_ids = [str(found) for found in seed_leads(sessions, count=3)]
    store = PostgresGoldenPromotionStore(sessions)
    store.record(
        tenant_slug=SLUG,
        lead_id=lead_ids[1],
        case_id="real_acme_00000002",
        expected_tier=Tier.COLD,
        promoted_by="icp_owner",
        note="cold is right: no intent at all beyond a price question",
        promoted_at=NOW,
    )

    assert store.promoted_lead_ids(tenant_slug=SLUG, lead_ids=lead_ids) == frozenset({lead_ids[1]})
    assert store.promoted_lead_ids(tenant_slug="other", lead_ids=lead_ids) == frozenset()
