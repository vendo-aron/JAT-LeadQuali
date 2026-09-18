"""The admin surface: every route behind a session, every write behind a CSRF token.

Nothing here needs a database. That is the whole point — #31's and #33's reviews each
found a security- or money-critical property asserted only by a Docker-gated test, which
meant a mutation could break it while the suite stayed green. "Every admin route requires
a session" and "a config editor without CSRF is a one-click rubric rewrite" are exactly
that kind of property, so they are checked against the real ASGI app over in-memory
collaborators.

The route enumeration is the load-bearing part. It reads the paths and methods out of the
application itself rather than from a list somebody maintains, so a route added tomorrow
to the wrong router fails this file instead of shipping open.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leadquali.api.admin import ADMIN_PREFIX, LOGIN_PATH, AdminDeps
from leadquali.api.main import create_app
from leadquali.app.admin_auth import (
    CSRF_FIELD,
    SESSION_COOKIE,
    SESSION_TTL,
    StaffAuthenticator,
    csrf_token,
    mint_session,
)
from leadquali.app.config_versions import ConfigEditor, ConfigVersionConflictError
from leadquali.app.feedback import Verdict
from leadquali.app.golden_promotion import GoldenPromotionService
from leadquali.app.metering import MeteringService
from leadquali.app.rerun import RerunService
from leadquali.app.tenants import TenantService
from leadquali.domain.models import Tier
from leadquali.observability import EVENT_ADMIN_LOGIN_FAILED, MAX_LOGGED_USERNAME_CHARS
from tests.fakes import (
    AdminLead,
    FakeClock,
    FakeSecretHasher,
    FakeTenantSecrets,
    FakeUnitOfWork,
    InMemoryAdminQueryStore,
    InMemoryConfigVersionStore,
    InMemoryGoldenPromotionStore,
    InMemoryMeteringStore,
    InMemoryTenantAdminStore,
    ScriptedAssessor,
    StaticEnricher,
    StaticRevenue,
)
from tests.logcapture import capture_json_logs
from tests.unit.test_rerun import METERING, assessment

SECRET = b"s" * 32
NOW = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
SLUG = "acme"
STAFF = "ada"
PASSWORD = "correct horse battery staple"
RATIONALE = "The contact is an analyst with no budget authority, so warm is the honest answer."

#: A lead payload with a real-looking person in it, so a test can assert that nothing in
#: the logs or on an error page ever carries one (invariant 5).
A_PAYLOAD: dict[str, Any] = {
    "full_name": "Priya Raghunathan",
    "email": "priya@northstar-logistics.example",
    "company": "Northstar Logistics",
    "role": "VP Revenue Operations",
    "message": "We need this replaced this quarter and have budget signed off.",
}


def config_document(**overrides: Any) -> dict[str, Any]:
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


class Harness:
    """The real ASGI app over in-memory collaborators, plus the doubles for assertions."""

    def __init__(self) -> None:
        clock = FakeClock(start=NOW, step_ms=0)
        self.tenant_store = InMemoryTenantAdminStore()
        self.tenants = TenantService(
            store=self.tenant_store,
            hasher=FakeSecretHasher(),
            secrets=FakeTenantSecrets(),
            clock=clock,
        )
        self.tenants.create_tenant(slug=SLUG, name="Acme", config=config_document())
        self.versions = InMemoryConfigVersionStore()
        self.versions.seed(tenant_slug=SLUG, config=config_document(), changed_by="migration")
        self.queries = InMemoryAdminQueryStore(
            [
                AdminLead(
                    lead_id=f"lead-{index:04d}",
                    tenant_slug=SLUG,
                    submission_id=f"sub-{index:04d}",
                    created_at=NOW - dt.timedelta(days=index),
                    tier=Tier.HOT,
                    verdict=Verdict.BAD if index == 1 else None,
                    raw_payload=A_PAYLOAD,
                )
                for index in range(1, 4)
            ]
        )
        self.promotion_store = InMemoryGoldenPromotionStore()
        self.assessor = ScriptedAssessor(_succeeded())
        self.deps = AdminDeps(
            tenants=self.tenants,
            config_editor=ConfigEditor(
                tenants=self.tenants,
                versions=self.versions,
                unit_of_work=FakeUnitOfWork(self.tenant_store, self.versions),
                clock=clock,
            ),
            queries=self.queries,
            promotions=GoldenPromotionService(store=self.promotion_store),
            metering=MeteringService(
                store=InMemoryMeteringStore(), clock=clock, revenue=StaticRevenue()
            ),
            rerun=RerunService(assessor=self.assessor, enricher=StaticEnricher(), clock=clock),
            authenticator=StaffAuthenticator(
                credentials={STAFF: f"$argon2id$fake${PASSWORD}"}, verifier=FakeSecretHasher2()
            ),
            session_secret=SECRET,
            clock=clock,
        )
        self.app = create_app(admin_deps=self.deps)

    def client(self, *, signed_in: bool = True, token: str | None = None) -> TestClient:
        """A client over HTTPS, because the session cookie is ``Secure`` and ``__Host-``.

        The cookie is handed to the constructor rather than set on the jar afterwards: a
        ``__Host-`` cookie set by hand does not satisfy httpx's own prefix rules, and a
        client that silently sent nothing would make every "requires a session" test pass
        for the wrong reason.
        """
        jar = {SESSION_COOKIE: token or self.token} if signed_in or token else {}
        return TestClient(self.app, base_url="https://testserver", cookies=jar)

    @property
    def token(self) -> str:
        return mint_session(secret=SECRET, subject=STAFF, now=NOW)

    @property
    def csrf(self) -> str:
        return csrf_token(secret=SECRET, session_token=self.token)

    def form(self, **fields: str) -> dict[str, str]:
        """A form body carrying this session's CSRF token."""
        return {CSRF_FIELD: self.csrf, **fields}


class FakeSecretHasher2:
    """A verifier with argon2's interface and none of its cost."""

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        del key_id
        return key_hash == f"$argon2id$fake${secret}"


def _succeeded() -> Any:
    from leadquali.app.assessment_result import AssessmentSucceeded

    return AssessmentSucceeded(assessment(), METERING)


@pytest.fixture
def harness() -> Iterator[Harness]:
    yield Harness()


def admin_routes(app: Any) -> list[tuple[str, str]]:
    """Every registered ``/admin`` route, read out of the application itself.

    From the OpenAPI document rather than from ``app.routes``: FastAPI's router inclusion
    is an implementation detail that has changed shape between versions, while the schema
    is the app's own statement of what it serves. Either way the point is that nothing here
    is a hand-maintained list — a route added tomorrow appears in this test automatically.
    """
    return sorted(
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/admin")
        for method in operations
    )


def fill(path: str) -> str:
    """Substitute a plausible value for every path parameter."""
    return path.replace("{slug}", SLUG).replace("{lead_id}", "lead-0001")


# -------------------------------------------------- every route requires a session


def test_the_app_registers_the_admin_routes(harness: Harness) -> None:
    """Guards against this whole file silently testing nothing."""
    routes = admin_routes(harness.app)

    assert len(routes) >= 15
    assert ("/admin/tenants/{slug}/config/apply", "POST") in routes


@pytest.mark.parametrize(
    ("path", "method"),
    admin_routes(Harness().app),
    ids=lambda value: str(value),
)
def test_every_admin_route_except_login_redirects_when_signed_out(
    path: str, method: str, harness: Harness
) -> None:
    """Enumerated from the app, so a route added to the wrong router fails here.

    Every rejection is the identical redirect. Telling a stranger that their cookie was
    well-formed but expired tells them the signing secret has not changed.
    """
    if path == LOGIN_PATH:
        return
    client = harness.client(signed_in=False)

    response = client.request(method, fill(path), follow_redirects=False)

    assert response.status_code == 303, f"{method} {path} did not redirect"
    assert response.headers["location"] == LOGIN_PATH


def test_a_forged_cookie_is_refused_like_no_cookie_at_all(harness: Harness) -> None:
    client = harness.client(signed_in=False, token="forged.token")

    response = client.get("/admin/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH


def test_an_expired_session_is_refused(harness: Harness) -> None:
    stale = mint_session(secret=SECRET, subject=STAFF, now=NOW - SESSION_TTL - dt.timedelta(1))

    response = harness.client(token=stale).get("/admin/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH


def test_the_login_page_is_reachable_without_a_session(harness: Harness) -> None:
    response = harness.client(signed_in=False).get(LOGIN_PATH)

    assert response.status_code == 200
    assert "Sign in" in response.text


# ------------------------------------------------------------------------------ login


def test_signing_in_sets_a_hardened_cookie(harness: Harness) -> None:
    response = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": STAFF, "password": PASSWORD}, follow_redirects=False
    )

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie


def test_a_failed_login_says_only_that_it_failed(harness: Harness) -> None:
    """No "no such user", no "wrong password", no "too many attempts"."""
    unknown = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": "eve", "password": "x"}
    )
    wrong = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": STAFF, "password": "x"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert "Login failed." in unknown.text
    assert unknown.text == wrong.text


def test_logging_out_expires_the_cookie_with_every_flag_it_was_set_with(
    harness: Harness,
) -> None:
    """``Secure`` above all: RFC 6265bis §4.1.3 says a browser MUST ignore a ``__Host-``
    cookie that arrives without it, so a deletion missing it is dropped on the floor by
    exactly the browsers the prefix was chosen for — and the session survives the logout.

    Starlette's ``delete_cookie`` defaults to ``secure=False``, which is how that shipped.
    """
    response = harness.client().post("/admin/logout", data=harness.form(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "Secure" in cookie, "a __Host- cookie without Secure is ignored, so this deletes nothing"
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=0" in cookie


def test_the_logout_cookie_matches_the_login_cookie_attribute_for_attribute(
    harness: Harness,
) -> None:
    """A cookie is only replaced by one whose name, path and domain match."""

    def attributes(header: str) -> set[str]:
        return {part.strip().split("=")[0].lower() for part in header.split(";")[1:]}

    login = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": STAFF, "password": PASSWORD}, follow_redirects=False
    )
    logout = harness.client().post("/admin/logout", data=harness.form(), follow_redirects=False)

    shared = {"path", "secure", "httponly", "samesite"}
    assert shared <= attributes(login.headers["set-cookie"])
    assert shared <= attributes(logout.headers["set-cookie"])


def test_every_guarded_page_offers_a_way_to_sign_out(harness: Harness) -> None:
    """The route existed and nothing reached it: ``grep logout templates/`` was empty."""
    for path in ("/admin/", f"/admin/leads?tenant={SLUG}", f"/admin/tenants/{SLUG}"):
        body = harness.client().get(path).text
        assert f'action="{ADMIN_PREFIX}/logout"' in body, f"{path} has no sign-out"
        assert "Sign out" in body


def test_the_sign_out_form_carries_a_usable_token(harness: Harness) -> None:
    """Rendering a button that posts a token the server then refuses is worse than none."""
    body = harness.client().get("/admin/").text
    token = body.split(f'name="{CSRF_FIELD}" value="')[1].split('"')[0]

    response = harness.client().post(
        "/admin/logout", data={CSRF_FIELD: token}, follow_redirects=False
    )

    assert response.status_code == 303


def test_the_login_page_offers_no_sign_out(harness: Harness) -> None:
    body = harness.client(signed_in=False).get(LOGIN_PATH).text

    assert "Sign out" not in body


def test_a_failed_login_sets_no_cookie(harness: Harness) -> None:
    response = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": STAFF, "password": "x"}
    )

    assert "set-cookie" not in response.headers


# ------------------------------------------------------------------------------- CSRF


def state_changing_admin_routes(app: Any) -> list[tuple[str, str]]:
    """Every admin route whose method is not a safe read, **enumerated from the app**.

    Hand-maintained before, and that was the defect: a `POST .../config/quickset` added to
    the guarded router rewrote a tenant's rubric with no token at all while the whole suite
    stayed green, because the list did not name it and the *authentication* enumeration was
    satisfied — the new route was on the right router. Deriving the list is what makes a
    route added tomorrow appear in this test whether or not anybody remembers.

    The login POST is excluded: it is on the public router and there is no session yet to
    bind a token to. `docs/admin.md` covers the residual login-CSRF risk.
    """
    return sorted(
        (path, method)
        for path, method in admin_routes(app)
        if method not in {"GET", "HEAD", "OPTIONS"} and path != LOGIN_PATH
    )


def test_there_are_state_changing_routes_to_check(harness: Harness) -> None:
    """An empty enumeration would make every test below vacuously true."""
    found = state_changing_admin_routes(harness.app)

    assert len(found) >= 5
    assert ("/admin/tenants/{slug}/config/apply", "POST") in found


@pytest.mark.parametrize(
    ("path", "method"),
    state_changing_admin_routes(Harness().app),
    ids=lambda value: str(value),
)
def test_every_state_changing_route_refuses_a_post_without_a_csrf_token(
    path: str, method: str, harness: Harness
) -> None:
    """Enumerated, so a new write route is covered by existing here rather than by listing.

    The body is deliberately empty. The check runs on the router before any handler body,
    so what a given route would have done with the fields is beside the point — it never
    gets them.
    """
    response = harness.client().request(method, fill(path), data={}, follow_redirects=False)

    assert response.status_code == 403, f"{method} {path} accepted a request with no token"


def test_the_guarded_router_carries_both_checks_as_dependencies(harness: Harness) -> None:
    """Structural, beside the enumeration: CSRF is declared where authentication is.

    The enumeration above catches a route that skips the check. This catches the other
    direction — somebody removing the dependency and re-adding per-handler calls, which
    would leave the enumeration passing for exactly as long as nobody adds a route.
    """
    routers = [
        getattr(route, "original_router")  # noqa: B009 - FastAPI's own inclusion internals
        for route in harness.app.routes
        if type(route).__name__ == "_IncludedRouter"
    ]
    guarded = [
        router
        for router in routers
        if any(getattr(entry, "path", "") == "/admin/leads" for entry in router.routes)
    ]

    assert len(guarded) == 1, "the guarded router is not where it was expected"
    names = {dependency.dependency.__name__ for dependency in guarded[0].dependencies}
    assert names == {"session_of", "csrf_of"}


def test_a_csrf_token_from_another_session_is_refused(harness: Harness) -> None:
    other = csrf_token(
        secret=SECRET, session_token=mint_session(secret=SECRET, subject="eve", now=NOW)
    )

    response = harness.client().post(
        "/admin/tenants/acme/config/apply",
        data={CSRF_FIELD: other, "config": json.dumps(config_document(min_confidence=0.75))},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_a_refused_csrf_post_writes_nothing(harness: Harness) -> None:
    """The status code is not the assertion; the store is."""
    before = dict(harness.tenant_store.tenants[SLUG].config)

    harness.client().post(
        "/admin/tenants/acme/config/apply",
        data={"config": json.dumps(config_document(min_confidence=0.75))},
        follow_redirects=False,
    )

    assert dict(harness.tenant_store.tenants[SLUG].config) == before
    assert len(harness.versions.versions(SLUG)) == 1


# ---------------------------------------------------------------------- the config editor


def test_the_editor_renders_the_stored_config(harness: Harness) -> None:
    response = harness.client().get(f"/admin/tenants/{SLUG}/config")

    assert response.status_code == 200
    assert "icp_description" in response.text


def test_previewing_shows_a_field_by_field_diff_and_writes_nothing(harness: Harness) -> None:
    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/preview",
        data=harness.form(config=json.dumps(config_document(min_confidence=0.75))),
    )

    assert response.status_code == 200
    assert "min_confidence" in response.text
    assert harness.tenant_store.tenants[SLUG].config.get("min_confidence") is None
    assert len(harness.versions.versions(SLUG)) == 1


def test_an_invalid_config_re_renders_the_form_with_the_operators_text_intact(
    harness: Harness,
) -> None:
    """Losing somebody's edit because they mistyped a threshold is how people stop using
    the tool and go back to psql."""
    typed = json.dumps(config_document(min_confidence=7.0))

    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/preview", data=harness.form(config=typed, note="my note")
    )

    assert response.status_code == 400
    assert "min_confidence" in response.text
    assert "my note" in response.text
    assert "7.0" in response.text, "the operator's document was not returned to them"
    assert len(harness.versions.versions(SLUG)) == 1


def test_a_document_that_is_not_json_is_a_form_error_and_not_a_crash(harness: Harness) -> None:
    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/preview", data=harness.form(config="{not json")
    )

    assert response.status_code == 400
    assert "not valid JSON" in response.text


def test_applying_writes_one_config_and_one_version(harness: Harness) -> None:
    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/apply",
        data=harness.form(config=json.dumps(config_document(min_confidence=0.75)), note="tighter"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert harness.tenant_store.tenants[SLUG].config["min_confidence"] == 0.75
    versions = harness.versions.versions(SLUG)
    assert [version.version for version in versions] == [1, 2]
    assert versions[-1].changed_by == STAFF


def test_the_audit_row_is_attributed_to_the_session_and_not_to_the_form(
    harness: Harness,
) -> None:
    """A form field naming the author would be an attribution anybody could forge."""
    harness.client().post(
        f"/admin/tenants/{SLUG}/config/apply",
        data=harness.form(
            config=json.dumps(config_document(min_confidence=0.75)), changed_by="somebody-else"
        ),
        follow_redirects=False,
    )

    assert harness.versions.versions(SLUG)[-1].changed_by == STAFF


def test_reverting_restores_the_earlier_config_and_appends(harness: Harness) -> None:
    harness.client().post(
        f"/admin/tenants/{SLUG}/config/apply",
        data=harness.form(config=json.dumps(config_document(min_confidence=0.75))),
        follow_redirects=False,
    )

    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/revert",
        data=harness.form(version="1"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert harness.tenant_store.tenants[SLUG].config.get("min_confidence") is None
    assert [version.version for version in harness.versions.versions(SLUG)] == [1, 2, 3]


def test_a_lost_save_race_is_a_conflict_and_not_a_missing_tenant(harness: Harness) -> None:
    """Two people editing during an incident. The loser must be told what happened.

    The store reported the race by raising ``UnknownTenantError`` — the same type a
    genuinely missing tenant raises — so the handler missed it and the page said *there is
    no such tenant*, 404. The reasonable next move from there is psql, which is the thing
    this screen exists to prevent.
    """

    class Conflicting(InMemoryConfigVersionStore):
        def append(self, **kwargs: Any) -> Any:
            raise ConfigVersionConflictError(
                "another change to tenant 'acme' took this version number; nothing was "
                "written — reload the config and try again"
            )

    harness.deps.config_editor._versions = Conflicting()

    response = harness.client().post(
        f"/admin/tenants/{SLUG}/config/apply",
        data=harness.form(config=json.dumps(config_document(min_confidence=0.75)), note="mine"),
        follow_redirects=False,
    )

    assert response.status_code == 409, "a lost race is not a 404 and not a 500"
    assert "no such tenant" not in response.text
    assert "reload the config" in response.text
    # And the operator's edit is handed back rather than lost to a retype.
    assert "0.75" in response.text
    assert "mine" in response.text


def test_a_genuinely_missing_tenant_is_still_a_404(harness: Harness) -> None:
    """The other half: the two must not have collapsed into one answer the other way."""
    response = harness.client().get("/admin/tenants/nobody/config", follow_redirects=False)

    assert response.status_code == 404
    assert "no such tenant" in response.text


def test_the_history_page_shows_who_changed_what(harness: Harness) -> None:
    harness.client().post(
        f"/admin/tenants/{SLUG}/config/apply",
        data=harness.form(config=json.dumps(config_document(min_confidence=0.75)), note="why"),
        follow_redirects=False,
    )

    response = harness.client().get(f"/admin/tenants/{SLUG}/config/history")

    assert STAFF in response.text
    assert "why" in response.text


# --------------------------------------------------------------------------- the views


def test_the_lead_browser_lists_and_pages(harness: Harness) -> None:
    response = harness.client().get(f"/admin/leads?tenant={SLUG}&limit=2")

    assert response.status_code == 200
    assert "Next page" in response.text


def test_the_browser_refuses_an_inverted_date_range_with_a_message(harness: Harness) -> None:
    response = harness.client().get(f"/admin/leads?tenant={SLUG}&from=2026-09-30&to=2026-09-01")

    assert response.status_code == 400
    assert "ends before it starts" in response.text


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1E+10000", "not a number"])
def test_a_confidence_that_is_not_a_finite_number_is_ignored(value: str, harness: Harness) -> None:
    """``Decimal`` accepts all of these from a query string.

    ``NaN`` is the interesting one: it compares false against everything, so the page would
    come back empty and correct-looking rather than erroring — an operator filtering leads
    would conclude there were none. The others become a bind parameter the driver renders.
    """
    response = harness.client().get(f"/admin/leads?tenant={SLUG}&min_confidence={value}")

    assert response.status_code == 200
    assert "lead-0001" in response.text, "the filter was applied instead of ignored"


def test_a_cursor_whose_row_id_is_not_a_uuid_is_ignored_rather_than_a_500(
    harness: Harness,
) -> None:
    """Well-formed enough to decode, and then a ``ValueError`` one layer down."""
    forged = base64.urlsafe_b64encode(b"2026-09-16T09:00:00+00:00|not-a-uuid").decode().rstrip("=")

    response = harness.client().get(f"/admin/leads?tenant={SLUG}&cursor={forged}")

    assert response.status_code == 200


def test_a_nonsense_cursor_is_ignored_rather_than_a_500(harness: Harness) -> None:
    """The cursor arrives from a URL; a stack trace here is a 500 anybody can request."""
    response = harness.client().get(f"/admin/leads?tenant={SLUG}&cursor=%21%21not-a-cursor")

    assert response.status_code == 200


def test_the_detail_page_renders_the_payload_to_a_human(harness: Harness) -> None:
    """Allowed, and the point of the page. Invariant 5 is about logs, not about screens."""
    response = harness.client().get(f"/admin/leads/lead-0001?tenant={SLUG}")

    assert response.status_code == 200
    assert "Priya Raghunathan" in response.text


def test_a_lead_belonging_to_another_tenant_is_not_found(harness: Harness) -> None:
    response = harness.client().get("/admin/leads/lead-0001?tenant=someone-else")

    assert response.status_code == 404
    assert "Priya" not in response.text


def test_the_review_answers_the_question_the_schema_was_shaped_for(harness: Harness) -> None:
    response = harness.client().get(f"/admin/review?tenant={SLUG}")

    assert response.status_code == 200
    assert "logistics" in response.text


def test_the_dashboard_reads_rollups_and_renders(harness: Harness) -> None:
    response = harness.client().get(f"/admin/tenants/{SLUG}?window=7d")

    assert response.status_code == 200
    assert "Billable leads" in response.text
    assert "Feedback agreement" in response.text


# ----------------------------------------------------------------------------- promote


def test_promoting_a_lead_records_it_once(harness: Harness) -> None:
    body = harness.form(tenant=SLUG, lead_id="lead-0001", expected_tier="warm", note=RATIONALE)

    first = harness.client().post("/admin/review/promote", data=body, follow_redirects=False)
    second = harness.client().post("/admin/review/promote", data=body, follow_redirects=False)

    assert first.status_code == second.status_code == 303
    assert len(harness.promotion_store.list_promotions(tenant_slug=SLUG)) == 1


def test_a_promotion_with_too_short_a_rationale_is_refused(harness: Harness) -> None:
    response = harness.client().post(
        "/admin/review/promote",
        data=harness.form(
            tenant=SLUG, lead_id="lead-0001", expected_tier="warm", note="looks warm"
        ),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert harness.promotion_store.list_promotions(tenant_slug=SLUG) == []


def test_the_promotions_page_renders_the_jsonl_with_no_real_contact_details(
    harness: Harness,
) -> None:
    harness.client().post(
        "/admin/review/promote",
        data=harness.form(tenant=SLUG, lead_id="lead-0001", expected_tier="warm", note=RATIONALE),
        follow_redirects=False,
    )

    response = harness.client().get(f"/admin/promotions?tenant={SLUG}")

    assert response.status_code == 200
    assert "real_acme_" in response.text
    assert "Priya" not in response.text
    assert "northstar-logistics" not in response.text


# ------------------------------------------------------------------------------ re-run


def test_the_rerun_page_shows_the_estimate_before_anything_is_spent(harness: Harness) -> None:
    response = harness.client().get(f"/admin/tenants/{SLUG}/rerun")

    assert response.status_code == 200
    assert "estimated cost" in response.text
    assert harness.assessor.calls == 0


def test_a_rerun_without_the_confirmation_box_spends_nothing(harness: Harness) -> None:
    response = harness.client().post(
        f"/admin/tenants/{SLUG}/rerun",
        data=harness.form(config=json.dumps(config_document())),
    )

    assert response.status_code == 400
    assert harness.assessor.calls == 0


def test_a_confirmed_rerun_compares_tiers_and_writes_nothing(harness: Harness) -> None:
    response = harness.client().post(
        f"/admin/tenants/{SLUG}/rerun",
        data=harness.form(
            config=json.dumps(
                config_document(thresholds={"hot": 95.0, "warm": 55.0, "cold": 30.0})
            ),
            confirm="yes",
        ),
    )

    assert response.status_code == 200
    assert "would change tier" in response.text
    assert harness.assessor.calls == 3
    assert harness.tenant_store.tenants[SLUG].config["thresholds"]["hot"] == 80.0


def test_a_tenant_with_no_leads_gets_a_page_that_says_so(harness: Harness) -> None:
    """Not an empty table, which reads as a bug."""
    harness.queries.leads = []

    response = harness.client().get(f"/admin/tenants/{SLUG}/rerun")

    assert "no assessed leads yet" in response.text


# ----------------------------------------------------------------------------- headers


def test_admin_pages_are_never_cached_or_indexed(harness: Harness) -> None:
    """Several of them render a lead's contact details; a copy in a proxy is uncontrolled."""
    response = harness.client().get("/admin/")

    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_content_security_policy_forbids_scripts_entirely(harness: Harness) -> None:
    """ "No JavaScript build step" as something the browser enforces, not a habit."""
    policy = harness.client().get("/admin/").headers["content-security-policy"]

    assert policy.startswith("default-src 'none'")
    assert "script-src" not in policy


# ---------------------------------------------------------- what the admin must not do


def test_no_admin_route_deletes_anything(harness: Harness) -> None:
    """Deleting a tenant or a lead is #37's, with its own audit. Not a button here."""
    methods = {method for _, method in admin_routes(harness.app)}

    assert "DELETE" not in methods


def test_no_page_renders_a_key_or_a_secret(harness: Harness) -> None:
    """A key's prefix, label and lifecycle dates are allowed; the key never is.

    Checked as a property of the wiring rather than of the templates: the admin's deps
    carry no credential source at all, so there is nothing for a page to render even by
    accident.
    """
    fields = set(AdminDeps.__dataclass_fields__)

    assert "credentials" not in fields
    assert not any("secret" in name for name in fields if name != "session_secret")
    for path in ("/admin/", f"/admin/tenants/{SLUG}", f"/admin/tenants/{SLUG}/config"):
        assert "hmac_secret_ref" not in harness.client().get(path).text


def test_a_page_that_blows_up_while_rendering_a_lead_leaks_nothing(harness: Harness) -> None:
    """The scenario invariant 5 is actually exposed to on this surface.

    An exception raised while the admin is working on a lead can be *holding* one — in its
    message, in a repr, in a traceback. A default error page that echoed what it was doing
    would put the submitter's contact details on a screen and into whatever collects
    unhandled exceptions. Driven by making the query raise with the payload in the message,
    because that is exactly how a driver or a serialiser fails.
    """

    def explode(*, tenant_slug: str, lead_id: str) -> Any:
        del tenant_slug, lead_id
        raise RuntimeError(f"could not decode row: {A_PAYLOAD}")

    harness.queries.lead_detail = explode  # type: ignore[method-assign]

    with capture_json_logs() as logs:
        response = harness.client().get(f"/admin/leads/lead-0001?tenant={SLUG}")

    assert response.status_code == 500
    for value in A_PAYLOAD.values():
        assert value not in response.text, "the error page echoed the lead"
        assert value not in logs.text, "the failure was logged with the lead in it"
    # The class name is all that is kept, which is what an operator needs to act on.
    assert "RuntimeError" in logs.text


def test_a_failing_page_says_nothing_about_the_request_either(harness: Harness) -> None:
    """Not the URL, not the query string, not the exception's own words."""

    def explode(*, tenant_slug: str, lead_id: str) -> Any:
        del tenant_slug, lead_id
        raise RuntimeError("a secret internal detail")

    harness.queries.lead_detail = explode  # type: ignore[method-assign]

    body = harness.client().get(f"/admin/leads/lead-0001?tenant={SLUG}").text

    assert "a secret internal detail" not in body
    assert "lead-0001" not in body


# ------------------------------------------------------- invariant 5, over a whole path


def test_no_admin_request_on_the_happy_path_logs_a_lead(harness: Harness) -> None:
    """Invariant 5 proved per *path*, not per logging helper.

    The helpers in ``observability/events.py`` are each tested to carry no payload, and
    that is necessary and nowhere near sufficient: adding
    ``LOGGER.info("promoted %s", detail.raw_payload)`` to the promote handler left all 2184
    tests passing, because nothing exercised a whole request with a real payload behind it
    and looked at what came out.

    So this walks the path an operator actually walks — sign in, browse, open a lead,
    promote it, preview a rubric and save it — with one request's worth of logging captured
    around all of it, and asserts that no value from the payload appears anywhere in the
    output. Not in a message, not in a field, not inside an escaped traceback.
    """
    with capture_json_logs() as logs:
        client = harness.client(signed_in=False)
        client.post(
            LOGIN_PATH, data={"username": STAFF, "password": PASSWORD}, follow_redirects=False
        )
        signed_in = harness.client()
        signed_in.get("/admin/")
        signed_in.get(f"/admin/leads?tenant={SLUG}")
        detail = signed_in.get(f"/admin/leads/lead-0001?tenant={SLUG}")
        signed_in.get(f"/admin/review?tenant={SLUG}")
        signed_in.post(
            "/admin/review/promote",
            data=harness.form(
                tenant=SLUG, lead_id="lead-0001", expected_tier="warm", note=RATIONALE
            ),
            follow_redirects=False,
        )
        signed_in.get(f"/admin/promotions?tenant={SLUG}")
        signed_in.post(
            f"/admin/tenants/{SLUG}/config/preview",
            data=harness.form(config=json.dumps(config_document(min_confidence=0.75))),
        )
        signed_in.post(
            f"/admin/tenants/{SLUG}/config/apply",
            data=harness.form(config=json.dumps(config_document(min_confidence=0.75))),
            follow_redirects=False,
        )
        signed_in.get(f"/admin/tenants/{SLUG}")
        signed_in.post("/admin/logout", data=harness.form(), follow_redirects=False)
        captured = logs.text
        records = logs.records()

    # The page really did render the lead, so "it is not in the logs" is a statement about
    # a path that carried it rather than one that never had it.
    assert "Priya Raghunathan" in detail.text
    assert captured, "nothing was logged at all, so this test proves nothing"
    assert records, "the capture produced no parseable records"

    for field, value in A_PAYLOAD.items():
        assert value not in captured, f"the payload's {field} reached the logs"
    assert not re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", captured), (
        "something address-shaped reached the logs"
    )


def test_a_failed_login_logs_only_a_bounded_prefix_of_the_username(
    harness: Harness,
) -> None:
    """The realistic incident: a staff member types their password into the username box.

    One line out of place and a live credential is in CloudWatch for the retention period.
    The field is bounded below any plausible password length, and a truncated value says so.
    """
    a_password = "correct horse battery staple"

    with capture_json_logs() as logs:
        harness.client(signed_in=False).post(
            LOGIN_PATH, data={"username": a_password, "password": "x"}
        )

    assert a_password not in logs.text
    record = logs.one(EVENT_ADMIN_LOGIN_FAILED)
    assert len(record["username"]) <= MAX_LOGGED_USERNAME_CHARS
    assert record["username_truncated"] is True
    assert record["username"] == a_password[:MAX_LOGGED_USERNAME_CHARS]


def test_an_ordinary_username_is_logged_whole_so_a_human_recognises_it(
    harness: Harness,
) -> None:
    """The bound must not cost the field its purpose."""
    with capture_json_logs() as logs:
        harness.client(signed_in=False).post(
            LOGIN_PATH, data={"username": STAFF, "password": "wrong"}
        )

    record = logs.one(EVENT_ADMIN_LOGIN_FAILED)
    assert record["username"] == STAFF
    assert record["username_truncated"] is False


def test_the_username_bound_is_below_any_password_this_system_will_mint() -> None:
    """Pinned, because the bound is only a defence while that stays true."""
    from leadquali.adminctl import MIN_PASSWORD_CHARS

    assert MAX_LOGGED_USERNAME_CHARS < MIN_PASSWORD_CHARS
