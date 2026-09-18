"""Keyset pagination, and the SQL that has to implement the same rule.

The property is the one ``OFFSET`` cannot hold: **every row exactly once, even when rows
are inserted mid-traversal**. Offset paging shifts every later row down by one on each
insert, so the row at the top of page 4 moves to the bottom of page 3 and is never seen —
silently, with no error and no gap in the output. It is tested here against the in-memory
store, which resumes strictly after ``(created_at, id)`` exactly as the SQL's row-value
comparison does.

The second half of this file is structural. ``PostgresAdminQueryStore``'s statements cannot
run without a database, so what is checked here is that they are *shaped* the way the
guarantees require: the browser's predicate is a row-value comparison rather than an
``OFFSET``, every tenant-scoped statement names its tenant (invariant 4), and the admin
store joins the ambient transaction. Each has an ``integration`` counterpart that runs the
statement for real where Docker exists.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import textwrap
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters import store_admin
from leadquali.adapters.store_admin import (
    PostgresAdminQueryStore,
    PostgresConfigVersionStore,
    PostgresGoldenPromotionStore,
    tenant_uuid,
)
from leadquali.app.admin_views import LeadFilter, LeadRow
from leadquali.app.feedback import Verdict
from leadquali.app.tenants import UnknownTenantError
from leadquali.domain.models import Tier
from tests.fakes import AdminLead, InMemoryAdminQueryStore
from tests.sqlite_schema import shadow_metadata, sqlite_sessions

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
SLUG = "acme"


def lead(index: int, *, minutes: int | None = None, **overrides: object) -> AdminLead:
    offset = index if minutes is None else minutes
    return AdminLead(
        lead_id=f"lead-{index:04d}",
        tenant_slug=str(overrides.pop("tenant_slug", SLUG)),
        submission_id=f"sub-{index:04d}",
        created_at=NOW - dt.timedelta(minutes=offset),
        **overrides,  # type: ignore[arg-type]  # a test fixture's spread, checked by use
    )


def walk(store: InMemoryAdminQueryStore, *, page_size: int, insert_after: int = -1) -> list[str]:
    """Page all the way through, optionally inserting a row part way.

    Returns every lead id seen, in order, including duplicates — so a test can assert on
    both "every row once" and "no row twice" from the same traversal.
    """
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = store.browse_leads(
            criteria=LeadFilter(tenant_slug=SLUG), cursor=cursor, limit=page_size
        )
        seen.extend(row.lead_id for row in page.rows)
        pages += 1
        if pages == insert_after:
            # A lead arriving while somebody is mid-traversal: the case that breaks OFFSET.
            store.add(lead(999, minutes=0))
        if page.next_cursor is None:
            return seen
        cursor = page.next_cursor


# ------------------------------------------------------------------ keyset pagination


def test_paging_returns_every_row_exactly_once() -> None:
    store = InMemoryAdminQueryStore(lead(index) for index in range(10))

    seen = walk(store, page_size=3)

    assert seen == [f"lead-{index:04d}" for index in range(10)]


def test_a_row_inserted_mid_traversal_does_not_displace_the_rest() -> None:
    """The whole argument for a keyset cursor, stated as a test.

    The new row sorts to the very top — newer than everything — so a correct traversal
    never sees it, and, crucially, sees every row that was there when it started, once.
    Under ``OFFSET`` the insert would shift the remaining pages and one row would vanish.
    """
    store = InMemoryAdminQueryStore(lead(index) for index in range(10))

    seen = walk(store, page_size=3, insert_after=1)

    assert seen == [f"lead-{index:04d}" for index in range(10)]
    assert len(seen) == len(set(seen)), "a row was returned twice"


def test_rows_sharing_a_timestamp_are_still_paged_exactly_once() -> None:
    """A batch import gives many rows one second. Without the id in the cursor, a page
    boundary inside that batch either repeats rows or skips them."""
    store = InMemoryAdminQueryStore(lead(index, minutes=0) for index in range(7))

    seen = walk(store, page_size=2)

    assert sorted(seen) == sorted(f"lead-{index:04d}" for index in range(7))
    assert len(seen) == len(set(seen))


def test_the_last_page_has_no_cursor() -> None:
    store = InMemoryAdminQueryStore(lead(index) for index in range(4))

    page = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=10)

    assert len(page.rows) == 4
    assert page.next_cursor is None


def test_a_full_page_with_nothing_after_it_still_has_no_cursor() -> None:
    """The off-by-one that makes a browser show an empty "next page"."""
    store = InMemoryAdminQueryStore(lead(index) for index in range(4))

    page = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=4)

    assert page.next_cursor is None


def test_an_empty_result_has_no_cursor_and_no_rows() -> None:
    page = InMemoryAdminQueryStore().browse_leads(
        criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=10
    )

    assert page.rows == ()
    assert page.next_cursor is None


# ----------------------------------------------------------------------- the filters


def test_the_browser_is_scoped_to_one_tenant() -> None:
    store = InMemoryAdminQueryStore([lead(1), lead(2, tenant_slug="someone-else")])

    page = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=10)

    assert [row.lead_id for row in page.rows] == ["lead-0001"]


def test_filtering_by_tier_and_confidence() -> None:
    store = InMemoryAdminQueryStore(
        [
            lead(1, tier=Tier.HOT, confidence=Decimal("0.95")),
            lead(2, tier=Tier.HOT, confidence=Decimal("0.40")),
            lead(3, tier=Tier.COLD, confidence=Decimal("0.95")),
        ]
    )

    page = store.browse_leads(
        criteria=LeadFilter(tenant_slug=SLUG, tier=Tier.HOT, min_confidence=Decimal("0.5")),
        cursor=None,
        limit=10,
    )

    assert [row.lead_id for row in page.rows] == ["lead-0001"]


def test_a_listing_row_carries_the_hash_and_not_the_address() -> None:
    """The address belongs on the detail page, where a human is looking at one lead."""
    store = InMemoryAdminQueryStore([lead(1, raw_payload={"email": "ada@example.com"})])

    row = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=10).rows[0]

    assert isinstance(row, LeadRow)
    assert row.contact_email_hash is not None
    assert "ada@example.com" not in str(row)


def test_lead_detail_is_scoped_to_its_tenant() -> None:
    """Somebody else's lead arrives here as the same answer a stale bookmark does."""
    store = InMemoryAdminQueryStore([lead(1, tenant_slug="someone-else")])

    assert store.lead_detail(tenant_slug=SLUG, lead_id="lead-0001") is None
    assert store.lead_detail(tenant_slug="someone-else", lead_id="lead-0001") is not None


def test_the_feedback_review_finds_the_query_the_schema_was_shaped_for() -> None:
    """ "Every lead scored hot last month that the rep marked bad"."""
    store = InMemoryAdminQueryStore(
        [
            lead(1, tier=Tier.HOT, verdict=Verdict.BAD),
            lead(2, tier=Tier.HOT, verdict=Verdict.GOOD),
            lead(3, tier=Tier.COLD, verdict=Verdict.BAD),
            lead(4, tier=Tier.HOT),
        ]
    )

    rows = store.feedback_review(
        tenant_slug=SLUG,
        tier=Tier.HOT,
        verdict=Verdict.BAD,
        start=NOW.date() - dt.timedelta(days=30),
        end=NOW.date(),
        limit=50,
    )

    assert [row.lead_id for row in rows] == ["lead-0001"]


# ------------------------------------------------------------- the SQL's shape (no DB)


def source_of(function: object) -> str:
    """One function's code, with its docstring removed.

    The docstrings in ``store_admin`` explain *why* there is no ``OFFSET`` and why the
    version number is not allocated in Python — so a check for those words has to look at
    the code and not at the prose that describes it.
    """
    source = textwrap.dedent(inspect.getsource(function))  # type: ignore[arg-type]
    body = ast.parse(source).body[0]
    assert isinstance(body, ast.FunctionDef)
    statements = body.body[1:] if ast.get_docstring(body) is not None else body.body
    return "\n".join(ast.unparse(statement) for statement in statements)


def test_the_browser_pages_by_row_value_and_never_by_offset() -> None:
    """``OFFSET`` is the bug this whole design avoids; it must not be in the statement."""
    source = source_of(PostgresAdminQueryStore.browse_leads)

    assert "tuple_(" in source, "the keyset predicate is a row-value comparison"
    assert ".offset(" not in source
    assert "offset" not in source.lower()


def test_the_browser_fetches_one_row_more_than_the_page_rather_than_counting() -> None:
    source = source_of(PostgresAdminQueryStore.browse_leads)

    assert "limit + 1" in source
    assert "func.count" not in source


def test_the_admin_stores_join_the_ambient_transaction() -> None:
    """``session_scope`` is what lets the config write and its audit row be one commit.

    Checked on the source rather than by running a statement, because the alternative —
    ``self._sessions.begin()`` — is correct-looking, passes every test that does not have a
    database, and silently opens a second transaction. The behaviour itself is asserted in
    ``tests/unit/test_unit_of_work.py`` against a real engine.
    """
    source = Path(inspect.getfile(store_admin)).read_text(encoding="utf-8")
    attributes = [
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert "begin" not in attributes, (
        "an admin store opened its own transaction instead of joining session_scope"
    )
    assert "session_scope" in source


def test_the_version_number_is_allocated_from_the_table_and_not_from_python() -> None:
    """Two admin processes saving at once must not be handed the same version."""
    source = source_of(PostgresConfigVersionStore.append)

    assert "func.max" in source or "MAX(" in source.upper()
    assert "INSERT" in source.upper() or "insert(" in source


# ------------------------------------------- the SQL, actually executed (real engine, no Docker)


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    """The shipping schema on in-memory SQLite. See ``tests/sqlite_schema.py``."""
    with sqlite_sessions() as factory:
        yield factory


def seed_tenant(sessions: sessionmaker[Session], slug: str, *, count: int = 2) -> list[uuid.UUID]:
    """One tenant's leads, each with an assessment and a rep's verdict on it.

    Two tenants are always seeded by the tests below, because a tenant predicate is only
    observable when there is somebody else's row available to leak.
    """
    tables = shadow_metadata().tables
    tenant = tenant_uuid(slug)
    lead_ids: list[uuid.UUID] = []
    with sessions.begin() as session:
        session.execute(
            insert(tables["tenants"]).values(
                id=tenant,
                slug=slug,
                name=slug,
                status="active",
                icp_config={},
                rate_limit_per_minute=60,
                rate_limit_burst=10,
                quota_alert_fraction=Decimal("0.80"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        for index in range(count):
            lead_id = uuid.uuid4()
            lead_ids.append(lead_id)
            session.execute(
                insert(tables["leads"]).values(
                    id=lead_id,
                    tenant_id=tenant,
                    submission_id=f"{slug}-{index}",
                    raw_payload={"email": f"{slug}{index}@example.invalid", "message": "hello"},
                    source="web_form",
                    status="received",
                    contact_email_hash="h" * 64,
                    received_at=NOW,
                    created_at=NOW,
                )
            )
            session.execute(
                insert(tables["assessments"]).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant,
                    lead_id=lead_id,
                    created_at=NOW - dt.timedelta(minutes=index),
                    status="ok",
                    tier="hot",
                    total_score=Decimal("82.00"),
                    dimension_scores={"icp_fit": 28},
                    extracted={"industry": slug, "company_name": slug.title()},
                    reasoning="strong fit",
                    confidence=Decimal("0.900"),
                    missing_information=[],
                    model_id="m",
                    prompt_version="v1",
                    input_tokens=1,
                    output_tokens=1,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=Decimal("0.018"),
                    latency_ms=1,
                )
            )
            session.execute(
                insert(tables["feedback"]).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant,
                    lead_id=lead_id,
                    rater="rep-1",
                    verdict="bad",
                    notes=None,
                    created_at=NOW,
                )
            )
    return lead_ids


@pytest.fixture
def two_tenants(
    sessions: sessionmaker[Session],
) -> tuple[PostgresAdminQueryStore, list[uuid.UUID], list[uuid.UUID]]:
    """``acme``'s leads and somebody else's, in one database, with the real store over it."""
    mine = seed_tenant(sessions, SLUG)
    theirs = seed_tenant(sessions, "someone-else")
    return PostgresAdminQueryStore(sessions), mine, theirs


Seeded = tuple[PostgresAdminQueryStore, list[uuid.UUID], list[uuid.UUID]]


def test_the_harness_really_runs_the_statements(two_tenants: Seeded) -> None:
    """Guards the three tests below against passing because nothing was seeded."""
    store, mine, theirs = two_tenants

    assert len(mine) == 2
    assert not set(mine) & set(theirs)
    assert store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=10).rows, (
        "the browser returned nothing at all, so a tenant check below proves nothing"
    )


def test_the_browser_returns_only_this_tenants_rows(two_tenants: Seeded) -> None:
    """Invariant 4, executed rather than read.

    The previous version of this test grepped ``browse_leads`` for ``tenant_uuid(...)``,
    which survives deleting the predicate that *uses* it — a mutation that deleted exactly
    that left all 2184 unit tests green. Seeding a second tenant is what makes the leak
    observable.
    """
    store, mine, theirs = two_tenants

    rows = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=None, limit=50).rows

    assert {row.lead_id for row in rows} == {str(found) for found in mine}
    assert not {row.lead_id for row in rows} & {str(found) for found in theirs}


def test_lead_detail_will_not_serve_another_tenants_lead(two_tenants: Seeded) -> None:
    """Asking for somebody else's lead id under your own tenant is "no such lead"."""
    store, mine, theirs = two_tenants

    assert store.lead_detail(tenant_slug=SLUG, lead_id=str(mine[0])) is not None
    assert store.lead_detail(tenant_slug=SLUG, lead_id=str(theirs[0])) is None
    assert store.lead_detail(tenant_slug="someone-else", lead_id=str(mine[0])) is None


def test_the_feedback_review_returns_only_this_tenants_disagreements(
    two_tenants: Seeded,
) -> None:
    store, mine, theirs = two_tenants

    rows = store.feedback_review(
        tenant_slug=SLUG,
        tier=Tier.HOT,
        verdict=Verdict.BAD,
        start=(NOW - dt.timedelta(days=30)).date(),
        end=NOW.date(),
        limit=50,
    )

    assert {row.lead_id for row in rows} == {str(found) for found in mine}
    assert not {row.lead_id for row in rows} & {str(found) for found in theirs}
    assert {row.industry for row in rows} == {SLUG}


def test_the_remaining_reads_are_tenant_scoped_too(two_tenants: Seeded) -> None:
    """``tier_mix`` and ``rerun_candidates``; ``feedback_agreement`` needs ``date_trunc``
    and is covered in the integration suite instead."""
    store, mine, _ = two_tenants

    assert sum(
        row.count
        for row in store.tier_mix(
            tenant_slug=SLUG, start=(NOW - dt.timedelta(days=30)).date(), end=NOW.date()
        )
    ) == len(mine)
    candidates = store.rerun_candidates(tenant_slug=SLUG, limit=50)
    assert {candidate.lead_id for candidate in candidates} == {str(found) for found in mine}


def test_the_keyset_cursor_pages_the_real_statement_exactly_once(
    sessions: sessionmaker[Session],
) -> None:
    """The row-value predicate, executed. SQLite has supported row values since 3.15."""
    seeded = seed_tenant(sessions, SLUG, count=7)
    store = PostgresAdminQueryStore(sessions)

    seen: list[str] = []
    cursor = None
    while True:
        page = store.browse_leads(criteria=LeadFilter(tenant_slug=SLUG), cursor=cursor, limit=3)
        seen.extend(row.lead_id for row in page.rows)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert sorted(seen) == sorted(str(found) for found in seeded)
    assert len(seen) == len(set(seen)), "a row came back twice"


def test_promoting_one_lead_twice_inserts_one_row(sessions: sessionmaker[Session]) -> None:
    """§4's idempotency requirement, against a database rather than against a fake.

    Deleting ``.on_conflict_do_nothing(...)`` from ``record`` left the whole suite green,
    because the only test that ran the statement needed Docker. Here the second call raises
    without it.
    """
    lead_id = str(seed_tenant(sessions, SLUG, count=1)[0])
    store = PostgresGoldenPromotionStore(sessions)
    arguments: dict[str, Any] = {
        "tenant_slug": SLUG,
        "lead_id": lead_id,
        "case_id": "real_acme_00000001",
        "promoted_by": "icp_owner",
        "note": "warm is honest: the contact has no budget authority at all",
        "promoted_at": NOW,
    }

    first, created_first = store.record(expected_tier=Tier.WARM, **arguments)
    second, created_second = store.record(expected_tier=Tier.HOT, **arguments)

    assert created_first is True
    assert created_second is False
    assert first == second
    assert second.expected_tier is Tier.WARM, "the first, reviewed label must not be replaced"
    assert len(store.list_promotions(tenant_slug=SLUG)) == 1


def test_a_promotion_is_scoped_to_its_tenant(sessions: sessionmaker[Session]) -> None:
    mine = str(seed_tenant(sessions, SLUG, count=1)[0])
    theirs = str(seed_tenant(sessions, "someone-else", count=1)[0])
    store = PostgresGoldenPromotionStore(sessions)
    store.record(
        tenant_slug=SLUG,
        lead_id=mine,
        case_id="c1",
        expected_tier=Tier.WARM,
        promoted_by="icp_owner",
        note="a rationale long enough for the golden set",
        promoted_at=NOW,
    )

    assert store.promoted_lead_ids(tenant_slug=SLUG, lead_ids=[mine, theirs]) == frozenset({mine})
    assert store.promoted_lead_ids(tenant_slug="someone-else", lead_ids=[mine]) == frozenset()
    assert store.list_promotions(tenant_slug="someone-else") == []


def test_the_config_version_store_allocates_from_the_table(
    sessions: sessionmaker[Session],
) -> None:
    """``MAX(version) + 1`` inside the insert, executed — and scoped to one tenant."""
    seed_tenant(sessions, SLUG, count=0)
    seed_tenant(sessions, "someone-else", count=0)
    store = PostgresConfigVersionStore(sessions)

    for slug in (SLUG, "someone-else"):
        first = store.append(
            tenant_slug=slug, config={"a": 1}, changed_by="ada", changed_at=NOW, note=None
        )
        second = store.append(
            tenant_slug=slug, config={"a": 2}, changed_by="ada", changed_at=NOW, note=None
        )
        assert (first.version, second.version) == (1, 2), f"{slug} did not start at 1"

    assert [version.version for version in store.list_versions(tenant_slug=SLUG)] == [2, 1]
    assert store.get_version(tenant_slug=SLUG, version=1).config == {"a": 1}


def test_appending_a_version_for_a_tenant_that_does_not_exist_writes_nothing(
    sessions: sessionmaker[Session],
) -> None:
    """The ``SELECT ... FROM tenants`` the insert is sourced from is what refuses it."""
    store = PostgresConfigVersionStore(sessions)

    with pytest.raises(UnknownTenantError):
        store.append(tenant_slug="nobody", config={}, changed_by="ada", changed_at=NOW, note=None)

    assert store.list_versions(tenant_slug="nobody") == []
