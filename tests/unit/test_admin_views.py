"""The value types the admin screens are built from: cursors, filters, grouping, rates.

All pure functions, so all of it runs without a database. The keyset-pagination property
that actually matters — every row exactly once even when a row is inserted mid-traversal —
is asserted in ``tests/unit/test_admin_store.py`` against the in-memory query store, which
implements the same cursor rule the SQL does.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from leadquali.app.admin_views import (
    UNKNOWN_INDUSTRY,
    AgreementPoint,
    DashboardWindow,
    LeadFilter,
    PageCursor,
    ReviewRow,
    group_by_industry,
)
from leadquali.app.feedback import Verdict
from leadquali.domain.models import Tier

NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)


# ----------------------------------------------------------------------------- cursor


def test_a_cursor_round_trips() -> None:
    cursor = PageCursor(created_at=NOW, row_id="0f3c9a12")

    assert PageCursor.decode(cursor.encode()) == cursor


def test_a_cursor_keeps_its_timezone() -> None:
    """A naive timestamp compared against a timestamptz column is a silent day's drift."""
    decoded = PageCursor.decode(PageCursor(created_at=NOW, row_id="x").encode())

    assert decoded is not None
    assert decoded.created_at.tzinfo is not None
    assert decoded.created_at == NOW


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not base64!!",
        "YWJj",  # decodes, but has no separator
        "MjAyNi0wOS0xNnw=",  # a timestamp and an empty id
        "bm90LWEtZGF0ZXwx",  # "not-a-date|1"
        "MjAyNi0wOS0xNlQwOTowMDowMHwx",  # naive timestamp: no offset
    ],
)
def test_a_cursor_that_is_not_one_is_none_rather_than_an_exception(raw: str) -> None:
    """The value arrives from a URL. A stack trace here is a 500 anybody can request."""
    assert PageCursor.decode(raw) is None


def test_cursors_order_by_time_then_id() -> None:
    """Timestamps collide — a batch import shares one — and the id breaks the tie."""
    earlier = PageCursor(created_at=NOW, row_id="a")
    later = PageCursor(created_at=NOW, row_id="b")

    assert earlier < later
    assert PageCursor(created_at=NOW - dt.timedelta(seconds=1), row_id="z") < earlier


# ----------------------------------------------------------------------------- filter


def test_a_filter_accepts_a_sensible_range() -> None:
    criteria = LeadFilter(
        tenant_slug="acme",
        tier=Tier.HOT,
        start=dt.date(2026, 9, 1),
        end=dt.date(2026, 9, 30),
        min_confidence=Decimal("0.2"),
        max_confidence=Decimal("0.8"),
    )

    assert criteria.tier is Tier.HOT


def test_an_inverted_date_range_is_refused_rather_than_returning_nothing() -> None:
    """An operator who has typed the dates the wrong way round wants to be told."""
    with pytest.raises(ValueError, match="ends before it starts"):
        LeadFilter(tenant_slug="acme", start=dt.date(2026, 9, 30), end=dt.date(2026, 9, 1))


def test_an_inverted_confidence_range_is_refused() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        LeadFilter(tenant_slug="acme", min_confidence=Decimal("0.9"), max_confidence=Decimal("0.1"))


# --------------------------------------------------------------------------- grouping


def review_row(industry: str | None, *, minutes: int = 0) -> ReviewRow:
    return ReviewRow(
        lead_id=f"lead-{industry}-{minutes}",
        assessed_at=NOW + dt.timedelta(minutes=minutes),
        tier=Tier.HOT,
        total_score=Decimal("82.00"),
        confidence=Decimal("0.900"),
        industry=industry,
        company="Acme",
        verdict=Verdict.BAD,
        rater="rep-1",
        notes=None,
        feedback_at=NOW,
    )


def test_grouping_puts_the_biggest_industry_first() -> None:
    """The view answers "where is the rubric wrong?", and that is the biggest group."""
    groups = group_by_industry(
        [
            review_row("logistics"),
            review_row("retail", minutes=1),
            review_row("logistics", minutes=2),
            review_row("logistics", minutes=3),
            review_row("retail", minutes=4),
        ]
    )

    assert [(group.industry, group.count) for group in groups] == [("logistics", 3), ("retail", 2)]


def test_groups_of_equal_size_are_ordered_by_name_so_two_renders_agree() -> None:
    groups = group_by_industry([review_row("retail"), review_row("logistics", minutes=1)])

    assert [group.industry for group in groups] == ["logistics", "retail"]


def test_rows_inside_a_group_are_newest_first() -> None:
    groups = group_by_industry(
        [review_row("logistics", minutes=0), review_row("logistics", minutes=5)]
    )

    assert [row.assessed_at for row in groups[0].rows] == [
        NOW + dt.timedelta(minutes=5),
        NOW,
    ]


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_an_unreadable_industry_gets_its_own_bucket(missing: str | None) -> None:
    """Disagreement concentrated in leads whose industry could not be read is a different
    finding from disagreement inside an industry, so it is not folded into "other"."""
    groups = group_by_industry([review_row(missing)])

    assert groups[0].industry == UNKNOWN_INDUSTRY


def test_grouping_nothing_is_no_groups() -> None:
    assert group_by_industry([]) == ()


# -------------------------------------------------------------------------- agreement


def test_the_agreement_rate_ignores_unsure() -> None:
    """``unsure`` is neither agreement nor disagreement, so it is not in the denominator."""
    point = AgreementPoint(day=dt.date(2026, 9, 16), good=3, bad=1, unsure=6)

    assert point.agreement == Decimal(3) / Decimal(4)
    assert point.rated == 10
    assert point.decisive == 4


def test_a_day_with_no_decisive_verdict_has_no_rate() -> None:
    """A day nobody rated and a day everybody disagreed are opposite facts; drawing both
    at 0% invents a crisis every weekend."""
    assert AgreementPoint(day=dt.date(2026, 9, 16), good=0, bad=0, unsure=2).agreement is None
    assert AgreementPoint(day=dt.date(2026, 9, 16), good=0, bad=0, unsure=0).agreement is None


def test_total_disagreement_is_zero_and_not_none() -> None:
    assert AgreementPoint(day=dt.date(2026, 9, 16), good=0, bad=4, unsure=0).agreement == 0


# ----------------------------------------------------------------------------- window


@pytest.mark.parametrize(("window", "days"), [("7d", 7), ("30d", 30), ("90d", 90)])
def test_every_dashboard_window_names_its_own_length(window: str, days: int) -> None:
    assert DashboardWindow(window).days == days
