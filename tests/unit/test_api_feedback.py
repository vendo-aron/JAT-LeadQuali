"""The feedback endpoint, from a mail client's point of view.

Three populations reach this URL and they must be told apart: a sales rep on a phone, a
security scanner that follows every link in every message before the rep sees it, and
somebody who found a link and would like to write whatever they want into the training set.
The tests are organised that way.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

# starlette's TestClient is built on httpx2 in this environment, and its Response is a
# different class from httpx 0.28's; take the type from the client that produces it.
from httpx2 import Response

from leadquali.api.feedback import (
    CONFIRM_FIELD,
    FEEDBACK_ROUTE,
    MAX_FORM_BYTES,
    NOTES_FIELD,
    FeedbackDeps,
    confirmation_code,
)
from leadquali.api.main import create_app
from leadquali.app.feedback import (
    MAX_NOTES_CHARS,
    MIN_TOKEN_SECRET_CHARS,
    TOKEN_VERSION,
    Verdict,
    mint_token,
    rater_id,
)
from tests.fakes import FakeClock, FeedbackRow, InMemoryFeedbackStore

SECRET = b"f" * MIN_TOKEN_SECRET_CHARS
OTHER_SECRET = b"g" * MIN_TOKEN_SECRET_CHARS
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
TENANT = "default"
LEAD = "8f14e45f-ceea-467a-9575-1b1e0a4d3c11"
OTHER_LEAD = "11111111-2222-3333-4444-555555555555"
RATER = rater_id("sales@northwind.example")


class Harness:
    """An app with the feedback routes wired to in-memory doubles."""

    def __init__(self, *, store: InMemoryFeedbackStore | None = None) -> None:
        self.store = store if store is not None else InMemoryFeedbackStore()
        self.deps = FeedbackDeps(
            store=self.store,
            token_secret=SECRET,
            clock=FakeClock(start=NOW, step_ms=0),
        )
        self.client = TestClient(create_app(feedback_deps=self.deps))

    def token(
        self,
        *,
        verdict: Verdict = Verdict.GOOD,
        lead_id: str = LEAD,
        tenant_id: str = TENANT,
        secret: bytes = SECRET,
        expires_at: datetime | None = None,
    ) -> str:
        return mint_token(
            secret=secret,
            tenant_id=tenant_id,
            lead_id=lead_id,
            verdict=verdict,
            rater=RATER,
            expires_at=expires_at if expires_at is not None else NOW + timedelta(days=30),
        )

    def url(self, token: str) -> str:
        return f"/feedback/{token}"

    def confirm(self, token: str, **fields: str) -> Response:
        form = {CONFIRM_FIELD: confirmation_code(secret=SECRET, token=token)}
        form.update(fields)
        return self.client.post(self.url(token), data=form)

    @property
    def rows(self) -> list[FeedbackRow]:
        return list(self.store.rows.values())


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --------------------------------------------------------------------------- the rep


def test_the_link_lands_on_a_human_page_not_json(harness: Harness) -> None:
    response = harness.client.get(harness.url(harness.token()))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "good lead" in response.text
    assert "<form" in response.text and 'method="post"' in response.text
    assert "{" not in response.text[:1], "a rep opens this in a browser, not a JSON viewer"


def test_confirming_writes_exactly_one_row_for_the_right_lead_and_tenant(
    harness: Harness,
) -> None:
    token = harness.token(verdict=Verdict.BAD)
    response = harness.confirm(token)

    assert response.status_code == 200
    assert "Recorded" in response.text
    assert len(harness.store.rows) == 1
    row = harness.rows[0]
    assert (row.tenant_id, row.lead_id, row.rater) == (TENANT, LEAD, RATER)
    assert row.verdict is Verdict.BAD
    assert row.created_at == NOW


def test_the_notes_box_is_saved_and_shown_as_saved(harness: Harness) -> None:
    token = harness.token()
    response = harness.confirm(token, **{NOTES_FIELD: "Wrong industry, right size."})

    assert "note was saved" in response.text
    assert harness.rows[0].notes == "Wrong industry, right size."


def test_clicking_the_same_link_twice_updates_rather_than_duplicates(harness: Harness) -> None:
    token = harness.token()
    harness.confirm(token)
    second = harness.confirm(token)

    assert len(harness.store.rows) == 1
    assert "Still recorded" in second.text


def test_a_rep_can_change_their_mind(harness: Harness) -> None:
    harness.confirm(harness.token(verdict=Verdict.GOOD))
    response = harness.confirm(harness.token(verdict=Verdict.BAD))

    assert len(harness.store.rows) == 1
    assert harness.rows[0].verdict is Verdict.BAD
    assert "Changed from good lead to" in response.text


def test_a_second_click_without_a_note_does_not_erase_the_first_one(harness: Harness) -> None:
    token = harness.token()
    harness.confirm(token, **{NOTES_FIELD: "Budget signed off."})
    harness.confirm(token)
    assert harness.rows[0].notes == "Budget signed off."


def test_the_thank_you_page_offers_the_opposite_verdict_in_one_tap(harness: Harness) -> None:
    """The change-of-mind path, and it must be a POST bound to the same lead and rater."""
    response = harness.confirm(harness.token(verdict=Verdict.GOOD))
    assert "Actually, this was a bad lead" in response.text

    action = response.text.split('action="', 1)[1].split('"', 1)[0]
    code = response.text.split(f'name="{CONFIRM_FIELD}" value="', 1)[1].split('"', 1)[0]
    changed = harness.client.post(action, data={CONFIRM_FIELD: code})

    assert changed.status_code == 200
    assert len(harness.store.rows) == 1
    assert harness.rows[0].verdict is Verdict.BAD


def test_an_enormous_note_is_truncated_rather_than_refused(harness: Harness) -> None:
    response = harness.confirm(harness.token(), **{NOTES_FIELD: "x" * (MAX_NOTES_CHARS * 2)})
    assert response.status_code == 200
    assert len(harness.rows[0].notes or "") == MAX_NOTES_CHARS


# ------------------------------------------------------------------------ the scanner


def test_a_scanner_prefetching_the_link_records_nothing(harness: Harness) -> None:
    """Outlook Safe Links, Proofpoint, Gmail: they GET every URL before a human sees it."""
    token = harness.token()
    for _ in range(5):
        assert harness.client.get(harness.url(token)).status_code == 200

    assert harness.store.calls == 0
    assert harness.store.rows == {}


def test_a_blind_post_without_the_page_records_nothing(harness: Harness) -> None:
    """The rarer scanner that POSTs a URL it has never rendered."""
    token = harness.token()
    response = harness.client.post(harness.url(token))

    assert response.status_code == 200
    assert "Confirm" in response.text, "it gets the button, not a write"
    assert harness.store.rows == {}


def test_a_post_with_a_forged_confirmation_records_nothing(harness: Harness) -> None:
    token = harness.token()
    response = harness.client.post(harness.url(token), data={CONFIRM_FIELD: "0" * 32})
    assert response.status_code == 200
    assert harness.store.rows == {}


def test_a_confirmation_code_from_another_token_does_not_work(harness: Harness) -> None:
    good = harness.token(verdict=Verdict.GOOD)
    bad = harness.token(verdict=Verdict.BAD)
    response = harness.client.post(
        harness.url(bad), data={CONFIRM_FIELD: confirmation_code(secret=SECRET, token=good)}
    )
    assert harness.store.rows == {}
    assert "Confirm" in response.text


def test_an_oversized_body_cannot_confirm_anything(harness: Harness) -> None:
    token = harness.token()
    response = harness.client.post(
        harness.url(token),
        data={
            CONFIRM_FIELD: confirmation_code(secret=SECRET, token=token),
            NOTES_FIELD: "x" * MAX_FORM_BYTES,
        },
    )
    assert response.status_code == 200
    assert harness.store.rows == {}


def test_the_pages_are_never_cached_or_indexed(harness: Harness) -> None:
    """A single-purpose capability URL must not sit in a proxy cache or a search index."""
    response = harness.client.get(harness.url(harness.token()))
    assert "no-store" in response.headers["cache-control"]
    assert "noindex" in response.headers["x-robots-tag"]
    assert response.headers["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------- the attacker


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_forged_token_is_refused_and_writes_nothing(harness: Harness, method: str) -> None:
    token = harness.token(secret=OTHER_SECRET)
    response = getattr(harness.client, method)(harness.url(token))

    assert response.status_code == 400
    assert "not valid" in response.text
    assert harness.store.rows == {}


def test_an_expired_token_is_refused_with_an_explanation(harness: Harness) -> None:
    token = harness.token(expires_at=NOW - timedelta(seconds=1))
    response = harness.client.get(harness.url(token))

    assert response.status_code == 410
    assert "expired" in response.text
    assert harness.store.rows == {}


def test_a_token_edited_to_name_another_lead_is_refused(harness: Harness) -> None:
    """The lead id is inside the signature, so this is the only way to try it."""
    valid = harness.token()
    other = harness.token(lead_id=OTHER_LEAD)
    spliced = f"{TOKEN_VERSION}.{other.split('.')[1]}.{valid.split('.')[2]}"

    assert harness.client.get(harness.url(spliced)).status_code == 400
    assert harness.confirm(spliced).status_code == 400
    assert harness.store.rows == {}


def test_a_token_edited_to_flip_the_verdict_is_refused(harness: Harness) -> None:
    good = harness.token(verdict=Verdict.GOOD)
    bad = harness.token(verdict=Verdict.BAD)
    spliced = f"{TOKEN_VERSION}.{bad.split('.')[1]}.{good.split('.')[2]}"

    assert harness.client.get(harness.url(spliced)).status_code == 400
    assert harness.store.rows == {}


@pytest.mark.parametrize("token", ["nonsense", "fb1.aaa", "fb9.aaa.bbb", "." * 10])
def test_a_malformed_token_is_refused(harness: Harness, token: str) -> None:
    assert harness.client.get(f"/feedback/{token}").status_code == 400


# ------------------------------------------------------------------- store failures


def test_a_lead_that_no_longer_exists_says_so_and_does_not_thank_anyone() -> None:
    """#37's retention job deletes leads; the link in the mailbox outlives the row."""
    harness = Harness(store=InMemoryFeedbackStore(known_leads=[(TENANT, OTHER_LEAD)]))
    response = harness.confirm(harness.token())

    assert response.status_code == 404
    assert "could not find that lead" in response.text
    assert "Thank" not in response.text


def test_a_broken_store_is_not_reported_as_success() -> None:
    """The rep must be able to try again: a lost verdict is unrecoverable signal."""
    harness = Harness(store=InMemoryFeedbackStore(fail=True))
    response = harness.confirm(harness.token())

    assert response.status_code == 503
    assert "not saved" in response.text
    assert "Recorded" not in response.text


# --------------------------------------------------------------------------- wiring


def test_the_routes_are_registered_on_the_one_app() -> None:
    """Extending the ingest app, not standing up a second one."""
    routes = {getattr(route, "path", None) for route in create_app().routes}
    assert FEEDBACK_ROUTE in routes
    assert "/leads" in routes


def test_a_short_secret_is_refused_when_the_endpoint_is_wired() -> None:
    with pytest.raises(ValueError, match="at least"):
        FeedbackDeps(store=InMemoryFeedbackStore(), token_secret=b"short", clock=FakeClock())


def test_building_the_app_touches_no_database_and_needs_no_secret() -> None:
    """Cold start imports the module; nothing may be resolved until a request arrives."""
    assert create_app() is not None


# ------------------------------------------------------------------------------ logs


def test_the_click_is_logged_without_an_address_or_the_note(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Invariant 5. The note is a rep writing freely about a person; it never leaves the row."""
    caplog.set_level(logging.DEBUG, logger="leadquali")
    harness.confirm(harness.token(), **{NOTES_FIELD: "Spoke to dana@northwind.example already"})

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged
    assert "@" not in logged
    assert "dana@northwind.example" not in logged
    assert "Spoke to" not in logged
    assert LEAD in logged
    assert RATER in logged
