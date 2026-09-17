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

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leadquali.api.admin import LOGIN_PATH, AdminDeps
from leadquali.api.main import create_app
from leadquali.app.admin_auth import (
    CSRF_FIELD,
    SESSION_COOKIE,
    SESSION_TTL,
    StaffAuthenticator,
    csrf_token,
    mint_session,
)
from leadquali.app.config_versions import ConfigEditor
from leadquali.app.feedback import Verdict
from leadquali.app.golden_promotion import GoldenPromotionService
from leadquali.app.metering import MeteringService
from leadquali.app.rerun import RerunService
from leadquali.app.tenants import TenantService
from leadquali.domain.models import Tier
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


def test_a_failed_login_sets_no_cookie(harness: Harness) -> None:
    response = harness.client(signed_in=False).post(
        LOGIN_PATH, data={"username": STAFF, "password": "x"}
    )

    assert "set-cookie" not in response.headers


# ------------------------------------------------------------------------------- CSRF


POSTS_THAT_WRITE = [
    ("/admin/tenants/acme/config/apply", {"config": "{}"}),
    ("/admin/tenants/acme/config/revert", {"version": "1"}),
    ("/admin/review/promote", {"tenant": SLUG, "lead_id": "lead-0001"}),
    ("/admin/tenants/acme/rerun", {"config": "{}", "confirm": "yes"}),
    ("/admin/logout", {}),
]


@pytest.mark.parametrize(("path", "fields"), POSTS_THAT_WRITE, ids=lambda value: str(value))
def test_a_state_changing_post_without_a_csrf_token_is_refused(
    path: str, fields: dict[str, str], harness: Harness
) -> None:
    response = harness.client().post(path, data=fields, follow_redirects=False)

    assert response.status_code == 403


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
