"""Generate a realistic dataset and time the admin's two expensive queries.

#36's acceptance criterion is *"the hot-but-marked-bad view returns in under a second on a
realistic dataset"*. That is not measurable in CI and it is not measurable on a laptop with
fifty rows: it needs a database with a few hundred thousand leads in it, which is a minute
of writing and several hundred megabytes. So the criterion is **owner-verified**, and this
script is the one command that verifies it.

``tests/integration/test_store_admin.py`` checks the half that *is* checkable cheaply — the
query plan uses an index scan rather than a sequential scan on ``assessments`` — because
that is the regression which makes the timing fail, and a plan can be read off a small
table. This script is the other half: the number itself.

Usage
-----

.. code-block:: bash

    docker compose up -d
    export DATABASE_URL=postgresql+psycopg://leadquali:leadquali@localhost:5432/leadquali
    alembic upgrade head
    python scripts/seed_benchmark.py --leads 200000 --tenants 3

    # ... and to re-time without regenerating:
    python scripts/seed_benchmark.py --measure-only

It writes into whatever ``DATABASE_URL`` names, so point it at a scratch database. It
refuses to run against a database whose name does not contain ``bench`` or ``test`` unless
``--i-know-what-i-am-doing`` is passed, because the obvious mistake is running it against
the database you were developing on.

What is generated
-----------------

Leads spread over a year, with a tier distribution that resembles a real funnel (most leads
are cold), an industry drawn from a small vocabulary so that the "grouped by industry"
result has real groups, and feedback on about one lead in eight — which is roughly what a
one-click link in a routing email gets. The shape matters more than the volume: a dataset
where every lead is hot and every verdict is bad would make the index look better than it
is, because the filter would select everything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
import time
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.store_admin import PostgresAdminQueryStore, tenant_uuid
from leadquali.app.admin_views import LeadFilter
from leadquali.app.feedback import Verdict
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.config import Settings
from leadquali.domain.models import Tier

#: Default dataset size. 200k leads across three tenants is about what a busy first year
#: looks like, and it is large enough that a sequential scan is unmistakably slower than an
#: index scan rather than merely slower.
DEFAULT_LEADS: Final[int] = 200_000
DEFAULT_TENANTS: Final[int] = 3

#: Rows per INSERT. Large enough that the round trips do not dominate, small enough that
#: one statement's parameters stay well under Postgres's 65535 limit.
BATCH: Final[int] = 2_000

#: A real funnel: most leads are not hot. A uniform distribution would make the tier
#: predicate select a third of the table and flatter the index.
TIER_WEIGHTS: Final[Sequence[tuple[str, int]]] = (
    ("cold", 45),
    ("warm", 30),
    ("disqualified", 15),
    ("hot", 10),
)

#: Roughly what a one-click feedback link in a routing email gets.
FEEDBACK_RATE: Final[float] = 0.125

#: Enough industries for the "grouped by industry" view to have real groups, few enough
#: that the groups are not all of size one.
INDUSTRIES: Final[Sequence[str]] = (
    "logistics",
    "manufacturing",
    "retail",
    "healthcare",
    "financial services",
    "construction",
    "hospitality",
    "education",
)

#: The criterion, in seconds.
TARGET_SECONDS: Final[float] = 1.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--leads", type=int, default=DEFAULT_LEADS, help="leads to generate")
    parser.add_argument(
        "--tenants", type=int, default=DEFAULT_TENANTS, help="tenants to spread them across"
    )
    parser.add_argument("--seed", type=int, default=20260916, help="RNG seed, for repeatability")
    parser.add_argument(
        "--measure-only", action="store_true", help="skip generation and just time the queries"
    )
    parser.add_argument(
        "--i-know-what-i-am-doing",
        action="store_true",
        help="write to a database whose name does not look like a scratch one",
    )
    return parser.parse_args(argv)


def engine_for(*, allow_any_database: bool) -> Engine:
    """The configured engine, refusing an obviously non-scratch database."""
    url = Settings().require_database_url()
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not allow_any_database and not any(hint in name for hint in ("bench", "test")):
        raise SystemExit(
            f"refusing to write {DEFAULT_LEADS:,} rows into a database called {name!r}. "
            "Point DATABASE_URL at a scratch database (one with 'bench' or 'test' in its "
            "name), or pass --i-know-what-i-am-doing."
        )
    return create_engine(url)


def slug_for(index: int) -> str:
    """A generated tenant's slug."""
    return f"bench{index:02d}"


def seed_tenants(sessions: sessionmaker[Session], count: int) -> list[str]:
    """Insert the tenants the leads will hang off, if they are not already there."""
    slugs = [slug_for(index) for index in range(count)]
    with sessions.begin() as session:
        for slug in slugs:
            session.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, icp_config) "
                    "VALUES (:id, :slug, :name, '{}'::jsonb) ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tenant_id_for(slug), "slug": slug, "name": slug},
            )
    return slugs


def generate(
    sessions: sessionmaker[Session], *, slugs: Sequence[str], leads: int, seed: int
) -> None:
    """Write ``leads`` leads, one assessment each, and feedback on some of them."""
    rng = random.Random(seed)  # noqa: S311 - a benchmark's shape, not a security decision
    tiers = [tier for tier, weight in TIER_WEIGHTS for _ in range(weight)]
    tenant_ids = {slug: tenant_uuid(slug) for slug in slugs}
    started = time.monotonic()
    written = 0

    while written < leads:
        size = min(BATCH, leads - written)
        lead_rows, assessment_rows, feedback_rows = [], [], []
        for index in range(written, written + size):
            slug = slugs[index % len(slugs)]
            tenant = tenant_ids[slug]
            lead_id = uuid.uuid4()
            # Spread over a year, newest first, so a date filter selects a real slice.
            created = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=index * 2)
            tier = rng.choice(tiers)
            lead_rows.append(
                {
                    "id": lead_id,
                    "tenant_id": tenant,
                    "submission_id": f"bench-{index:09d}",
                    "raw_payload": '{"message": "generated benchmark lead"}',
                    "source": "benchmark",
                    "received_at": created,
                    "created_at": created,
                }
            )
            assessment_rows.append(
                {
                    "tenant_id": tenant,
                    "lead_id": lead_id,
                    "created_at": created,
                    "status": "ok",
                    "tier": tier,
                    "total_score": Decimal(rng.randrange(0, 10000)) / 100,
                    "dimension_scores": '{"icp_fit": 20}',
                    "extracted": f'{{"industry": "{rng.choice(INDUSTRIES)}"}}',
                    "reasoning": "generated",
                    "confidence": Decimal(rng.randrange(500, 1000)) / 1000,
                    "model_id": "benchmark",
                    "prompt_version": "v1",
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "cost_usd": Decimal("0.018"),
                    "latency_ms": 900,
                }
            )
            if rng.random() < FEEDBACK_RATE:
                feedback_rows.append(
                    {
                        "tenant_id": tenant,
                        "lead_id": lead_id,
                        "rater": f"rep-{index % 7}",
                        # Reps disagree with hot leads more often than with cold ones,
                        # which is the pattern the review view exists to surface.
                        "verdict": rng.choice(
                            ["bad", "good", "good"] if tier != "hot" else ["bad", "bad", "good"]
                        ),
                        "created_at": created,
                    }
                )
        _write_batch(sessions, lead_rows, assessment_rows, feedback_rows)
        written += size
        print(f"  {written:,}/{leads:,} leads", end="\r", file=sys.stderr)

    print(f"\ngenerated {written:,} leads in {time.monotonic() - started:.1f}s", file=sys.stderr)


def _write_batch(
    sessions: sessionmaker[Session],
    leads: list[dict[str, object]],
    assessments: list[dict[str, object]],
    feedback: list[dict[str, object]],
) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO leads (id, tenant_id, submission_id, raw_payload, source, "
                "received_at, created_at) VALUES (:id, :tenant_id, :submission_id, "
                ":raw_payload::jsonb, :source, :received_at, :created_at)"
            ),
            leads,
        )
        session.execute(
            text(
                "INSERT INTO assessments (tenant_id, lead_id, created_at, status, tier, "
                "total_score, dimension_scores, extracted, reasoning, confidence, model_id, "
                "prompt_version, input_tokens, output_tokens, cost_usd, latency_ms) VALUES "
                "(:tenant_id, :lead_id, :created_at, :status, :tier, :total_score, "
                ":dimension_scores::jsonb, :extracted::jsonb, :reasoning, :confidence, "
                ":model_id, :prompt_version, :input_tokens, :output_tokens, :cost_usd, "
                ":latency_ms)"
            ),
            assessments,
        )
        if feedback:
            session.execute(
                text(
                    "INSERT INTO feedback (tenant_id, lead_id, rater, verdict, created_at) "
                    "VALUES (:tenant_id, :lead_id, :rater, :verdict, :created_at)"
                ),
                feedback,
            )


def measure(sessions: sessionmaker[Session], slug: str) -> int:
    """Time the two queries the criterion is about, and print the plans.

    Returns the process exit code: non-zero when the review query missed
    :data:`TARGET_SECONDS`, so this can be wired into a release check rather than only read.
    """
    store = PostgresAdminQueryStore(sessions)
    today = dt.datetime.now(dt.UTC).date()
    failures = 0

    print("\n=== hot leads the rep marked bad, last 30 days, grouped by industry ===")
    started = time.monotonic()
    rows = store.feedback_review(
        tenant_slug=slug,
        tier=Tier.HOT,
        verdict=Verdict.BAD,
        start=today - dt.timedelta(days=30),
        end=today,
        limit=500,
    )
    elapsed = time.monotonic() - started
    print(
        f"{len(rows)} rows in {elapsed * 1000:.0f} ms (target: under {TARGET_SECONDS * 1000:.0f})"
    )
    if elapsed > TARGET_SECONDS:
        print("FAIL: the acceptance criterion is not met on this dataset")
        failures += 1

    print("\n=== first page of the lead browser ===")
    started = time.monotonic()
    page = store.browse_leads(
        criteria=LeadFilter(tenant_slug=slug, tier=Tier.HOT), cursor=None, limit=50
    )
    print(f"{len(page.rows)} rows in {(time.monotonic() - started) * 1000:.0f} ms")

    print("\n=== deep page, to show a keyset cursor does not degrade ===")
    cursor = page.next_cursor
    for _ in range(20):
        if cursor is None:
            break
        page = store.browse_leads(
            criteria=LeadFilter(tenant_slug=slug, tier=Tier.HOT), cursor=cursor, limit=50
        )
        cursor = page.next_cursor
    started = time.monotonic()
    store.browse_leads(
        criteria=LeadFilter(tenant_slug=slug, tier=Tier.HOT), cursor=cursor, limit=50
    )
    print(f"page ~21 in {(time.monotonic() - started) * 1000:.0f} ms")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    """Generate, analyse and measure. Returns a process exit code."""
    args = parse_args(argv)
    engine = engine_for(allow_any_database=args.i_know_what_i_am_doing)
    sessions = sessionmaker(engine)
    try:
        slugs = seed_tenants(sessions, args.tenants)
        if not args.measure_only:
            generate(sessions, slugs=slugs, leads=args.leads, seed=args.seed)
            # Without this the planner is working from stale statistics and the timings
            # say more about ANALYZE than about the indexes.
            print("analysing…", file=sys.stderr)
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text("ANALYZE leads, assessments, feedback"))
        return measure(sessions, slugs[0])
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - a command, exercised by running it
    raise SystemExit(main())
