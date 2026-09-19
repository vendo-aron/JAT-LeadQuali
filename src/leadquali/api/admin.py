"""The staff admin: server-rendered HTML, its own deps, everything behind one session.

Shape, and why it is this one
-----------------------------

FastAPI plus Jinja2, server-rendered forms, no JavaScript framework, no bundler, no npm.
Three reasons, all of them about what this thing actually is:

* **It ships inside the same Lambda as everything else.** A separate front end is a second
  artefact, a second deployment, and a second thing that can be stale relative to the API.
* **The audience is a handful of staff.** Nobody is waiting on a 40ms interaction budget on
  a page that lists rows.
* **A build step is a second deployment pipeline to keep alive.** Node versions, a lockfile,
  a CI job and a cache — permanently, for a page that lists rows.

The router is registered on the existing app with its **own deps object**, exactly as
:func:`~leadquali.api.feedback.register_feedback_routes` does, so an admin request cannot
reach the ingest handler's collaborators and an ingest request cannot reach a
:class:`~leadquali.app.tenants.TenantService`.

Access control is structural
----------------------------

There are two routers. One carries the login page and nothing else; the other carries every
other admin route and is constructed with a **router-level dependency** on
:func:`_require_session`. A new route added to the guarded router is protected because of
where it was added, not because its author remembered a decorator —
``tests/unit/test_api_admin.py`` enumerates every registered ``/admin`` route from the app
and asserts each one redirects when unauthenticated, so a route added to the wrong router
fails the suite rather than shipping open.

Every state-changing form carries a CSRF token bound to the session
(:func:`~leadquali.app.admin_auth.csrf_token`), verified before the handler does anything.
A config editor with no CSRF protection is a one-click rubric rewrite from any page a
logged-in operator happens to open.

PII
---

The lead detail page renders the submitter's own words, contact details included. That is
the point of it and invariant 5 allows it: the rule is about *logs*, not about a screen a
person is looking at. What follows is that nothing in this module may log a payload, a
form body or a row — so the handlers log event names and identifiers, and
:func:`_failure_page` renders a fixed page carrying only an error class, never the request
and never the row that broke.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from leadquali.app.admin_auth import (
    ADMIN_PREFIX,
    CSRF_FIELD,
    LOGIN_FAILED_MESSAGE,
    SESSION_COOKIE,
    SESSION_TTL,
    SessionRejected,
    StaffAuthenticator,
    StaffSession,
    csrf_token,
    csrf_token_matches,
    mint_session,
    verify_session,
)
from leadquali.app.admin_views import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AdminQueryPort,
    DashboardWindow,
    LeadFilter,
    PageCursor,
    group_by_industry,
)
from leadquali.app.config_versions import (
    MAX_NOTE_CHARS,
    ConfigEditor,
    ConfigVersionConflictError,
    UnknownConfigVersionError,
)
from leadquali.app.feedback import Verdict
from leadquali.app.golden_promotion import GoldenPromotionError, GoldenPromotionService
from leadquali.app.metering import BillingPeriod, MeteringService
from leadquali.app.ports import ClockPort
from leadquali.app.rerun import RERUN_BATCH_CAP, RerunNotConfirmedError, RerunService
from leadquali.app.tenants import TenantAdminError, TenantService, UnknownTenantError
from leadquali.config import Settings
from leadquali.domain.models import Tier
from leadquali.domain.tenant_config import TenantConfigError
from leadquali.observability import (
    EVENT_ADMIN_CSRF_REJECTED,
    EVENT_ADMIN_PAGE_FAILED,
    EVENT_ADMIN_SESSION_REJECTED,
    log_admin_config_changed,
    log_admin_lead_promoted,
    log_admin_login_failed,
    log_event,
)

LOGGER: Final = logging.getLogger(__name__)

__all__ = [
    "ADMIN_PREFIX",
    "LOGIN_PATH",
    "MAX_CONFIG_BYTES",
    "AdminDeps",
    "build_admin_deps",
    "register_admin_routes",
]

#: Where an unauthenticated request lands.
LOGIN_PATH: Final[str] = f"{ADMIN_PREFIX}/login"

#: Largest config document the editor accepts. A rubric is a few kilobytes of prose and
#: numbers; 256 KiB is two orders of magnitude of headroom and still bounds the work a
#: single form post can ask for.
MAX_CONFIG_BYTES: Final[int] = 256 * 1024

#: Never cached, never indexed. Every page here is behind a session and several of them
#: render a lead's contact details; a copy in a corporate proxy is a copy nobody controls.
_PAGE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # No script, no style, no image from anywhere. The pages are server-rendered HTML with
    # one inline stylesheet, so this is the whole surface they need — and it is what makes
    # "no CDN, no bundler" a property the browser enforces rather than a habit.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}

#: Methods that read and do not change anything, and are therefore exempt from the CSRF
#: check. Exactly HTTP's own safe methods: anything else — POST, PUT, PATCH, DELETE — must
#: carry the token, so a verb added later is covered by default rather than by amendment.
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

#: Rows the feedback review will render. The view exists to find a pattern, and a pattern
#: that needs more than 500 rows to see is one a chart should be showing instead.
_REVIEW_ROW_CAP: Final[int] = 500

#: How far back the feedback review looks by default: "last month", which is the question
#: plan §4 actually asks.
_REVIEW_DEFAULT_DAYS: Final[int] = 30


@lru_cache(maxsize=1)
def _templates() -> Environment:
    """The Jinja environment, built once per process.

    ``PackageLoader`` reads the templates out of the installed package rather than off a
    path relative to the source tree, which is what makes them work inside a Lambda zip —
    provided they are declared in ``[tool.setuptools.package-data]``. A template that is
    not packaged is a 500 in Lambda and a pass in the tests, which is the worst combination
    available, so ``tests/unit/test_admin_templates.py`` asserts every file here is covered
    by those globs.

    Autoescaping is on. Several of these pages render a lead's own words, which is
    attacker-controlled text from a public form.
    """
    environment = Environment(
        loader=PackageLoader("leadquali.api", "templates"),
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["money"] = _money
    environment.filters["percent"] = _percent
    environment.filters["pretty_json"] = _pretty_json
    return environment


@dataclass(frozen=True, slots=True)
class AdminDeps:
    """Everything the admin routes need, injected rather than imported.

    Deliberately not :class:`~leadquali.api.main.IngestDeps` and not
    :class:`~leadquali.api.feedback.FeedbackDeps`. The three surfaces share an ASGI app and
    nothing else: ingest must not be able to reach a
    :class:`~leadquali.app.tenants.TenantService`, and the admin has no business holding
    the lead queue.
    """

    tenants: TenantService
    config_editor: ConfigEditor
    queries: AdminQueryPort
    promotions: GoldenPromotionService
    metering: MeteringService
    rerun: RerunService
    authenticator: StaffAuthenticator
    session_secret: bytes
    clock: ClockPort

    def __post_init__(self) -> None:
        """Refuse a signing secret that is not one, at wiring time rather than at login."""
        mint_session(
            secret=self.session_secret, subject="startup-check", now=dt.datetime.now(dt.UTC)
        )


def build_admin_deps(settings: Settings | None = None) -> AdminDeps:
    """Wire the production admin: Postgres, Secrets Manager, argon2, the real model.

    Imported lazily inside the function body, for the same reason
    :func:`leadquali.api.main._default_deps` does it: importing this module at a Lambda
    cold start must not open a database connection or demand a secret that only the admin
    Lambda has.

    Raises:
        RuntimeError: a required setting is missing. The message names the variable.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.enrich_null import NullEnricher
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
    from leadquali.adapters.llm_anthropic import AnthropicLeadAssessor, build_anthropic_client
    from leadquali.adapters.metering_postgres import PostgresMeteringStore
    from leadquali.adapters.revenue_none import UnknownRevenue
    from leadquali.adapters.store_admin import (
        PostgresAdminQueryStore,
        PostgresConfigVersionStore,
        PostgresGoldenPromotionStore,
    )
    from leadquali.adapters.store_tenants import PostgresTenantAdminStore
    from leadquali.adapters.unit_of_work import PostgresUnitOfWork
    from leadquali.app.admin_auth import load_staff_credentials
    from leadquali.config import get_settings

    resolved = settings if settings is not None else get_settings()
    credentials = load_staff_credentials(resolved.require_admin_credentials())
    session_secret = resolved.require_admin_session_secret().encode("utf-8")
    clock = SystemClock()
    hasher = Argon2KeyHasher()
    url = resolved.require_database_url()
    tenants = TenantService(
        store=PostgresTenantAdminStore.from_url(url),
        hasher=hasher,
        secrets=_NoNewSecrets(),
        clock=clock,
    )
    return AdminDeps(
        tenants=tenants,
        config_editor=ConfigEditor(
            tenants=tenants,
            versions=PostgresConfigVersionStore.from_url(url),
            unit_of_work=PostgresUnitOfWork.from_url(url),
            clock=clock,
        ),
        queries=PostgresAdminQueryStore.from_url(url),
        promotions=GoldenPromotionService(store=PostgresGoldenPromotionStore.from_url(url)),
        metering=MeteringService(
            store=PostgresMeteringStore.from_url(url), clock=clock, revenue=UnknownRevenue()
        ),
        rerun=RerunService(
            assessor=AnthropicLeadAssessor(
                build_anthropic_client(resolved.require_anthropic_api_key())
            ),
            # The re-run enriches with nothing rather than with #18's DNS lookups: a
            # historical lead's domain may have changed hands since it arrived, and a
            # preview that silently enriched differently from the original run would be
            # comparing two things at once.
            enricher=NullEnricher(),
            clock=clock,
        ),
        authenticator=StaffAuthenticator(credentials=credentials, verifier=hasher),
        session_secret=session_secret,
        clock=clock,
    )


class _NoNewSecrets:
    """A :class:`~leadquali.app.tenants.TenantSecretsPort` that refuses to provision.

    The admin edits configuration; it does not onboard customers, and it must not be able
    to mint a tenant's HMAC signing secret. ``tenantctl`` is where that happens, with an
    operator's own AWS credentials. Wiring the real provisioner here would give a web page
    the ability to write to Secrets Manager, which is a capability no page needs.
    """

    def create_tenant_hmac_secret(self, slug: str) -> str:
        """Always refuse."""
        raise TenantAdminError(
            f"the admin cannot provision secrets (asked for '{slug}'); onboard a tenant "
            "with `python -m leadquali.tenantctl create`"
        )


@lru_cache(maxsize=1)
def _default_admin_deps() -> AdminDeps:
    """The production wiring, built on first request rather than at import."""
    return build_admin_deps()


def register_admin_routes(app: FastAPI, deps: AdminDeps | None = None) -> None:
    """Mount the staff admin at ``/admin`` on an existing application.

    Two routers. ``public`` holds the login page. ``guarded`` is constructed with a
    router-level dependency on the session, so **every** route added to it is protected by
    virtue of being on it — there is no per-handler check to forget.

    ``deps`` of ``None`` resolves the production wiring lazily on the first request.
    """
    provide: Callable[[], AdminDeps] = (lambda: deps) if deps is not None else _default_admin_deps

    def session_of(request: Request) -> StaffSession:
        """The verified session, or a redirect to the login page.

        Raised as an :class:`HTTPException` rather than returned, so that it short-circuits
        before a handler body runs. Every rejection — no cookie, a forged one, an expired
        one — produces the identical redirect: telling a stranger that their cookie was
        well-formed but expired tells them the signing secret has not changed.
        """
        current = provide()
        verified = verify_session(
            secret=current.session_secret,
            token=request.cookies.get(SESSION_COOKIE, ""),
            now=current.clock.now(),
        )
        if isinstance(verified, SessionRejected):
            log_event(
                LOGGER,
                EVENT_ADMIN_SESSION_REJECTED,
                tenant_id=None,
                reason=verified.failure.value,
            )
            raise HTTPException(status_code=303, detail="sign in", headers={"Location": LOGIN_PATH})
        # The layout's sign-out form needs a token on every guarded page, including the
        # ones with no form of their own. Derived once here rather than in each handler.
        request.state.nav_csrf = csrf_token(
            secret=current.session_secret, session_token=request.cookies.get(SESSION_COOKIE, "")
        )
        return verified

    async def csrf_of(request: Request) -> None:
        """Verify the CSRF token on any request that is not a safe read.

        On the router beside :func:`session_of`, and for the same reason: a new POST is
        protected because of where it lives, not because its author remembered to call a
        helper. The per-handler version of this check shipped first and was wrong in the
        way per-handler checks are always wrong — a route added later to the same router
        satisfied every test in the suite, including the one that enumerates routes for
        *authentication*, and rewrote a tenant's rubric with no token at all.

        The form is read here and cached on ``request.state``, because a request body can
        only be consumed once: the handlers that need the fields take them from
        :func:`_form`, which returns the cached copy rather than reading the stream again.
        """
        if request.method in _SAFE_METHODS:
            return
        current = provide()
        form = dict(await request.form())
        request.state.admin_form = form
        if not csrf_token_matches(
            str(form.get(CSRF_FIELD, "")),
            secret=current.session_secret,
            session_token=request.cookies.get(SESSION_COOKIE, ""),
        ):
            log_event(
                LOGGER,
                EVENT_ADMIN_CSRF_REJECTED,
                level=logging.WARNING,
                method=request.method,
            )
            raise HTTPException(status_code=403, detail="this form has expired; reload the page")

    public = APIRouter(prefix=ADMIN_PREFIX)
    # Order matters: the session is verified before the CSRF token, so an expired session
    # gets the login redirect rather than a "this form has expired" page it cannot act on.
    guarded = APIRouter(prefix=ADMIN_PREFIX, dependencies=[Depends(session_of), Depends(csrf_of)])

    _register_login(public, provide)
    _register_pages(guarded, provide, session_of)

    app.include_router(public)
    app.include_router(guarded)


# ------------------------------------------------------------------------- the login


def _register_login(router: APIRouter, provide: Callable[[], AdminDeps]) -> None:
    """The one pair of routes that does not require a session."""

    @router.get("/login", response_class=HTMLResponse, summary="Staff sign-in page.")
    async def show_login() -> Response:
        """Render the sign-in form. Writes nothing and reveals nothing."""
        return _render("admin/login.html", {"message": None})

    @router.post("/login", response_class=HTMLResponse, summary="Sign in.")
    async def sign_in(request: Request) -> Response:
        """Check the credentials and, on success, set the session cookie.

        A failure re-renders the same page with :data:`LOGIN_FAILED_MESSAGE` and no other
        information, and costs the same KDF call a success does — see
        :class:`~leadquali.app.admin_auth.StaffAuthenticator`.
        """
        deps = provide()
        form = await request.form()
        username = str(form.get("username", ""))
        outcome = deps.authenticator.authenticate(
            username=username,
            password=str(form.get("password", "")),
            now=deps.clock.now(),
        )
        if not outcome.authenticated:
            log_admin_login_failed(LOGGER, username=username.strip(), gated=outcome.gated)
            return _render("admin/login.html", {"message": LOGIN_FAILED_MESSAGE}, status=401)
        assert outcome.subject is not None  # narrowed by `authenticated`
        token = mint_session(
            secret=deps.session_secret, subject=outcome.subject, now=deps.clock.now()
        )
        response = RedirectResponse(
            url=f"{ADMIN_PREFIX}/", status_code=303, headers=dict(_PAGE_HEADERS)
        )
        _set_session_cookie(response, token)
        return response


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie with every flag that makes it one.

    ``HttpOnly`` so no script can read it; ``Secure`` so it never crosses plain HTTP;
    ``SameSite=Lax`` so a cross-site form post cannot carry it (the CSRF token is the belt
    to this brace); ``Path=/`` and no ``Domain``, which together with the ``__Host-`` prefix
    stop a sibling subdomain from setting or overwriting it. ``max_age`` matches the token's
    own absolute expiry, so the browser forgets it at the same moment the server stops
    honouring it.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """Expire the session cookie, with the same flags it was set with.

    Every flag is repeated, and ``Secure`` is the one that matters: Starlette's
    ``delete_cookie`` defaults to ``secure=False``, and RFC 6265bis §4.1.3 says a browser
    MUST ignore a ``__Host-``-prefixed cookie that arrives without it. The deletion was
    therefore dropped on the floor by exactly the conforming browsers the prefix was chosen
    for, and the session survived a logout. A cookie is only replaced by one whose name,
    path and domain match, so this has to mirror :func:`_set_session_cookie` attribute for
    attribute rather than merely name the cookie.
    """
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        expires=0,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# -------------------------------------------------------------------- the guarded pages


def _register_pages(
    router: APIRouter,
    provide: Callable[[], AdminDeps],
    session_of: Callable[[Request], StaffSession],
) -> None:
    """Every route that requires a session.

    They are added to a router that already carries the session dependency, so none of
    them re-checks it. ``session_of`` is called again inside the handlers that need the
    *subject* — for attribution and for the CSRF token — which is a second verification of
    a cookie this request has already proved, and costs one HMAC.
    """

    @router.post("/logout", summary="End this session.")
    async def sign_out(request: Request) -> Response:
        """Clear the browser's copy of the session cookie.

        A POST, because a link that logs you out is a nuisance any prefetcher can trigger
        — and, being on the guarded router, it carries the CSRF token like every other
        write.

        **This clears the browser's copy and nothing else.** The session token is a signed
        bearer credential with an absolute expiry and no server-side record, so a copy
        taken off the machine keeps working until it expires. That is the trade the design
        made (``app/admin_auth.py``) and it is what makes rotating ``ADMIN_SESSION_SECRET``
        the answer to a suspected theft rather than this button; ``docs/admin.md`` §3 says
        so where an operator will read it.
        """
        del request
        response = RedirectResponse(url=LOGIN_PATH, status_code=303, headers=dict(_PAGE_HEADERS))
        _clear_session_cookie(response)
        return response

    @router.get("/", response_class=HTMLResponse, summary="Every tenant.")
    async def home(request: Request) -> Response:
        """The tenant list: the way in to every other screen."""
        deps = provide()
        return _guarded(
            lambda: _render(
                "admin/tenants.html",
                {"tenants": deps.tenants.list_tenants()},
                request=request,
            )
        )

    # ------------------------------------------------------------------- the dashboard

    @router.get("/tenants/{slug}", response_class=HTMLResponse, summary="One tenant's dashboard.")
    async def dashboard(slug: str, request: Request) -> Response:
        """Volume, tier mix, cost and feedback agreement, over a chosen window.

        Volume and cost come from #33's ``usage_daily`` rollups and never from
        ``assessments``; the tier mix and the agreement rate are two direct queries, both
        bounded by this tenant and this window.
        """
        window = _window(request.query_params.get("window"))
        deps = provide()

        def page() -> Response:
            tenant = deps.tenants.get_tenant(slug=slug)
            today = deps.clock.now().date()
            period = BillingPeriod(start=today - dt.timedelta(days=window.days - 1), end=today)
            # closed_days_only=False on both: an operator looking at a dashboard wants
            # today's partial figures, and #33 marks them `partial` so the page can say so
            # rather than presenting a half-finished day as a settled number.
            totals = deps.metering.usage_for_period(
                tenant_id=slug, period=period, closed_days_only=False
            )
            daily = deps.metering.daily_usage(tenant_id=slug, period=period, closed_days_only=False)
            return _render(
                "admin/dashboard.html",
                {
                    "tenant": tenant,
                    "window": window,
                    "windows": list(DashboardWindow),
                    "period": period,
                    "totals": totals,
                    "daily": daily,
                    "tier_mix": deps.queries.tier_mix(
                        tenant_slug=slug, start=period.start, end=period.end
                    ),
                    "agreement": deps.queries.feedback_agreement(
                        tenant_slug=slug, start=period.start, end=period.end
                    ),
                },
                request=request,
            )

        return _guarded(page)

    # ---------------------------------------------------------------- the config editor

    @router.get(
        "/tenants/{slug}/config",
        response_class=HTMLResponse,
        summary="Edit a tenant's rubric.",
    )
    async def edit_config(slug: str, request: Request) -> Response:
        """Render the editor, seeded with what is stored."""
        deps = provide()

        def page() -> Response:
            tenant = deps.config_editor.current(slug=slug)
            return _config_form(
                request=request,
                deps=deps,
                slug=slug,
                document=json.dumps(dict(tenant.config), indent=2, sort_keys=True),
                note="",
                errors=(),
            )

        return _guarded(page)

    @router.post(
        "/tenants/{slug}/config/preview",
        response_class=HTMLResponse,
        summary="Validate a candidate rubric and diff it. Writes nothing.",
    )
    async def preview_config(slug: str, request: Request) -> Response:
        """Step two of edit → preview → confirm.

        On a validation failure this re-renders the editor **with the operator's text
        intact**. Losing somebody's edit because they mistyped a threshold is how people
        stop using the tool and go back to psql, which is the outcome this whole screen
        exists to prevent.
        """
        deps = provide()
        form = _form(request)
        raw = str(form.get("config", ""))
        note = str(form.get("note", ""))[:MAX_NOTE_CHARS]

        def page() -> Response:
            parsed, errors = _parse_config(raw)
            if parsed is None:
                return _config_form(
                    request=request, deps=deps, slug=slug, document=raw, note=note, errors=errors
                )
            try:
                preview = deps.config_editor.preview(slug=slug, document=parsed)
            except TenantConfigError as error:
                return _config_form(
                    request=request,
                    deps=deps,
                    slug=slug,
                    document=raw,
                    note=note,
                    errors=(str(error),),
                )
            return _render(
                "admin/config_preview.html",
                {
                    "slug": slug,
                    "preview": preview,
                    "document": raw,
                    "note": note,
                    "csrf": _csrf_for(request, deps),
                    "rerun_cap": RERUN_BATCH_CAP,
                },
                request=request,
            )

        return _guarded(page)

    @router.post(
        "/tenants/{slug}/config/apply",
        summary="Write the previewed rubric and its audit row.",
    )
    async def apply_config(slug: str, request: Request) -> Response:
        """Step three. The only route that writes a rubric.

        Re-validated here rather than trusted from the preview: the confirm is a separate
        request and the document travelled through the browser in between.
        """
        deps = provide()
        form = _form(request)
        subject = session_of(request).subject
        raw = str(form.get("config", ""))
        note = str(form.get("note", ""))[:MAX_NOTE_CHARS] or None

        def page() -> Response:
            parsed, errors = _parse_config(raw)
            if parsed is None:
                return _config_form(
                    request=request,
                    deps=deps,
                    slug=slug,
                    document=raw,
                    note=note or "",
                    errors=errors,
                )
            try:
                version = deps.config_editor.apply(
                    slug=slug, document=parsed, changed_by=subject, note=note
                )
            except ConfigVersionConflictError as error:
                # Somebody else saved while this form was open. 409, and the operator's
                # text is handed back so the edit is not lost — a lost race must cost a
                # reload, not a retype.
                return _config_form(
                    request=request,
                    deps=deps,
                    slug=slug,
                    document=raw,
                    note=note or "",
                    errors=(str(error),),
                    status=409,
                )
            except (TenantConfigError, ValueError) as error:
                return _config_form(
                    request=request,
                    deps=deps,
                    slug=slug,
                    document=raw,
                    note=note or "",
                    errors=(str(error),),
                )
            log_admin_config_changed(
                LOGGER,
                tenant_id=slug,
                changed_by=subject,
                version=version.version,
                fields_changed=len(deps.config_editor.preview(slug=slug, document=parsed).changes),
            )
            return RedirectResponse(
                url=f"{ADMIN_PREFIX}/tenants/{slug}/config/history",
                status_code=303,
                headers=dict(_PAGE_HEADERS),
            )

        return _guarded(page)

    @router.get(
        "/tenants/{slug}/config/history",
        response_class=HTMLResponse,
        summary="Who changed this tenant's rubric, when, and to what.",
    )
    async def config_history(slug: str, request: Request) -> Response:
        """The audit trail, newest first, with a revert button on every earlier version."""
        deps = provide()

        def page() -> Response:
            versions = deps.config_editor.history(slug=slug)
            return _render(
                "admin/config_history.html",
                {
                    "slug": slug,
                    "versions": versions,
                    "current_version": versions[0].version if versions else None,
                    "csrf": _csrf_for(request, deps),
                },
                request=request,
            )

        return _guarded(page)

    @router.post(
        "/tenants/{slug}/config/revert",
        summary="Restore an earlier rubric as a new version.",
    )
    async def revert_config(slug: str, request: Request) -> Response:
        """A revert appends; it never deletes. See :meth:`ConfigEditor.revert`."""
        deps = provide()
        form = _form(request)
        subject = session_of(request).subject

        def page() -> Response:
            try:
                target = int(str(form.get("version", "")))
            except ValueError:
                return _failure_page("that is not a version number", status=400)
            try:
                version = deps.config_editor.revert(
                    slug=slug, to_version=target, changed_by=subject, note=None
                )
            except UnknownConfigVersionError:
                return _failure_page("there is no such version of this tenant's rubric", status=404)
            except (TenantConfigError, ValueError) as error:
                return _failure_page(str(error), status=400)
            log_admin_config_changed(
                LOGGER,
                tenant_id=slug,
                changed_by=subject,
                version=version.version,
                fields_changed=0,
                reverted_from=target,
            )
            return RedirectResponse(
                url=f"{ADMIN_PREFIX}/tenants/{slug}/config/history",
                status_code=303,
                headers=dict(_PAGE_HEADERS),
            )

        return _guarded(page)

    # ------------------------------------------------------------------------ the re-run

    @router.get(
        "/tenants/{slug}/rerun",
        response_class=HTMLResponse,
        summary="Plan a re-run of historical leads against a candidate rubric.",
    )
    async def plan_rerun(slug: str, request: Request) -> Response:
        """Show what a re-run would touch and what it would cost. Spends nothing."""
        deps = provide()

        def page() -> Response:
            return _render(
                "admin/rerun.html",
                {
                    "slug": slug,
                    "plan": _rerun_plan(deps, slug),
                    "document": json.dumps(
                        dict(deps.config_editor.current(slug=slug).config), indent=2, sort_keys=True
                    ),
                    "csrf": _csrf_for(request, deps),
                    "report": None,
                },
                request=request,
            )

        return _guarded(page)

    @router.post(
        "/tenants/{slug}/rerun",
        response_class=HTMLResponse,
        summary="Re-run historical leads against a candidate rubric. Costs money.",
    )
    async def run_rerun(slug: str, request: Request) -> Response:
        """Re-assess against the candidate rubric. Writes nothing and sends nothing.

        Refuses without an explicit confirmation, because a request that spends money
        must not be one a stray click or a prefetcher can make.
        """
        deps = provide()
        form = _form(request)
        raw = str(form.get("config", ""))
        confirmed = str(form.get("confirm", "")) == "yes"

        def page() -> Response:
            parsed, errors = _parse_config(raw)
            if parsed is None:
                return _failure_page(errors[0] if errors else "that is not a rubric", status=400)
            try:
                candidate = deps.config_editor.preview(slug=slug, document=parsed).validated
            except TenantConfigError as error:
                return _failure_page(str(error), status=400)
            plan = _rerun_plan(deps, slug)
            try:
                report = deps.rerun.run(plan=plan, config=candidate, confirmed=confirmed)
            except RerunNotConfirmedError as error:
                return _render(
                    "admin/rerun.html",
                    {
                        "slug": slug,
                        "plan": plan,
                        "document": raw,
                        "csrf": _csrf_for(request, deps),
                        "report": None,
                        "message": str(error),
                    },
                    status=400,
                    request=request,
                )
            return _render(
                "admin/rerun.html",
                {
                    "slug": slug,
                    "plan": plan,
                    "document": raw,
                    "csrf": _csrf_for(request, deps),
                    "report": report,
                },
                request=request,
            )

        return _guarded(page)

    # ----------------------------------------------------------------- the lead browser

    @router.get("/leads", response_class=HTMLResponse, summary="Browse assessed leads.")
    async def browse(request: Request) -> Response:
        """Filter by tenant, tier, date range and confidence; page with a keyset cursor."""
        deps = provide()
        params = request.query_params

        def page() -> Response:
            slug = params.get("tenant", "")
            if not slug:
                return _render(
                    "admin/leads.html",
                    {
                        "tenants": deps.tenants.list_tenants(),
                        "slug": slug or None,
                        "criteria": None,
                        "page": None,
                        "params": dict(params),
                        "errors": (),
                    },
                    request=request,
                )
            try:
                criteria = LeadFilter(
                    tier=Tier(params["tier"]) if params.get("tier") else None,
                    start=_date(params.get("from")),
                    end=_date(params.get("to")),
                    min_confidence=_decimal(params.get("min_confidence")),
                    max_confidence=_decimal(params.get("max_confidence")),
                )
            except ValueError as error:
                return _render(
                    "admin/leads.html",
                    {
                        "tenants": deps.tenants.list_tenants(),
                        "slug": slug or None,
                        "criteria": None,
                        "page": None,
                        "params": dict(params),
                        "errors": (str(error),),
                    },
                    status=400,
                    request=request,
                )
            found = deps.queries.browse_leads(
                tenant_slug=slug,
                criteria=criteria,
                cursor=PageCursor.decode(params.get("cursor", "")),
                limit=_page_size(params.get("limit")),
            )
            return _render(
                "admin/leads.html",
                {
                    "tenants": deps.tenants.list_tenants(),
                    "slug": slug,
                    "criteria": criteria,
                    "page": found,
                    "params": dict(params),
                    "errors": (),
                },
                request=request,
            )

        return _guarded(page)

    @router.get("/leads/{lead_id}", response_class=HTMLResponse, summary="One lead, in full.")
    async def lead_detail(lead_id: str, request: Request) -> Response:
        """The payload, the assessment, the reasoning, the routing and the feedback.

        This renders a lead's contact details to a human by design. What must never happen
        is the same data reaching a log line or an error page, which is why nothing here
        logs the row and why :func:`_failure_page` carries no request and no record.
        """
        deps = provide()
        slug = request.query_params.get("tenant", "")

        def page() -> Response:
            detail = deps.queries.lead_detail(tenant_slug=slug, lead_id=lead_id) if slug else None
            if detail is None:
                return _render(
                    "admin/lead_detail.html", {"detail": None}, status=404, request=request
                )
            return _render("admin/lead_detail.html", {"detail": detail}, request=request)

        return _guarded(page)

    # -------------------------------------------------------------- the feedback review

    @router.get(
        "/review",
        response_class=HTMLResponse,
        summary="Leads the system and the rep disagreed about, grouped by industry.",
    )
    async def review(request: Request) -> Response:
        """*Every lead scored hot last month that the rep marked bad, grouped by industry.*

        The query plan §4 was designed around, as a first-class view with its own URL. The
        tier and verdict default to ``hot`` and ``bad`` because that is the question; both
        are selectable because "cold leads the rep loved" is the other half of it.
        """
        deps = provide()
        params = request.query_params

        def page() -> Response:
            slug = params.get("tenant", "")
            if not slug:
                return _render(
                    "admin/review.html",
                    {
                        "tenants": deps.tenants.list_tenants(),
                        "slug": None,
                        "groups": (),
                        "tier": Tier.HOT,
                        "verdict": Verdict.BAD,
                        "params": dict(params),
                        "csrf": _csrf_for(request, deps),
                        "promoted": frozenset(),
                    },
                    request=request,
                )
            tier = Tier(params["tier"]) if params.get("tier") else Tier.HOT
            verdict = Verdict(params["verdict"]) if params.get("verdict") else Verdict.BAD
            today = deps.clock.now().date()
            rows = deps.queries.feedback_review(
                tenant_slug=slug,
                tier=tier,
                verdict=verdict,
                start=_date(params.get("from")) or today - dt.timedelta(days=_REVIEW_DEFAULT_DAYS),
                end=_date(params.get("to")) or today,
                limit=_REVIEW_ROW_CAP,
            )
            return _render(
                "admin/review.html",
                {
                    "tenants": deps.tenants.list_tenants(),
                    "slug": slug,
                    "groups": group_by_industry(rows),
                    "tier": tier,
                    "verdict": verdict,
                    "params": dict(params),
                    "csrf": _csrf_for(request, deps),
                    "promoted": deps.promotions.already_promoted(
                        tenant_slug=slug, lead_ids=[row.lead_id for row in rows]
                    ),
                },
                request=request,
            )

        return _guarded(page)

    @router.post("/review/promote", summary="Promote one lead into the eval golden set.")
    async def promote(request: Request) -> Response:
        """One click from a disagreement to a golden case. Idempotent by construction."""
        deps = provide()
        form = _form(request)
        subject = session_of(request).subject

        def page() -> Response:
            slug = str(form.get("tenant", ""))
            lead_id = str(form.get("lead_id", ""))
            try:
                promotion, created = deps.promotions.promote(
                    tenant_slug=slug,
                    lead_id=lead_id,
                    expected_tier=Tier(str(form.get("expected_tier", ""))),
                    promoted_by=subject,
                    note=str(form.get("note", "")),
                    now=deps.clock.now(),
                )
            except (GoldenPromotionError, ValueError) as error:
                return _failure_page(str(error), status=400)
            if created:
                log_admin_lead_promoted(
                    LOGGER,
                    tenant_id=slug,
                    lead_id=lead_id,
                    case_id=promotion.case_id,
                    promoted_by=subject,
                )
            return RedirectResponse(url=f"{ADMIN_PREFIX}/promotions?tenant={slug}", status_code=303)

        return _guarded(page)

    @router.get(
        "/promotions",
        response_class=HTMLResponse,
        summary="Promoted leads, and the JSONL to append to the golden set.",
    )
    async def promotions(request: Request) -> Response:
        """The lines to commit.

        Rendered rather than written: ``golden_leads.jsonl`` lives in a git repository,
        the labels in it are human judgements, and a Lambda's filesystem is read-only. The
        operator copies these into a reviewed commit, which is also the human in the loop
        that :func:`~leadquali.app.golden_promotion.strip_pii` cannot be.
        """
        deps = provide()
        slug = request.query_params.get("tenant", "")

        def page() -> Response:
            if not slug:
                return _render(
                    "admin/promotions.html",
                    {"tenants": deps.tenants.list_tenants(), "slug": None, "rows": (), "jsonl": ""},
                    request=request,
                )
            rows = deps.promotions.promotions_for(tenant_slug=slug)
            payloads: dict[str, Mapping[str, Any]] = {}
            for row in rows:
                detail = deps.queries.lead_detail(tenant_slug=slug, lead_id=row.lead_id)
                if detail is not None:
                    payloads[row.lead_id] = detail.raw_payload
            return _render(
                "admin/promotions.html",
                {
                    "tenants": deps.tenants.list_tenants(),
                    "slug": slug,
                    "rows": rows,
                    "jsonl": deps.promotions.export_jsonl(promotions=rows, payloads=payloads),
                },
                request=request,
            )

        return _guarded(page)


# ----------------------------------------------------------------------------- helpers


def _rerun_plan(deps: AdminDeps, slug: str) -> Any:
    """Build the re-run plan for one tenant, with the cost estimate from #33's rollups."""
    today = deps.clock.now().date()
    recent = deps.metering.usage_for_period(
        tenant_id=slug,
        period=BillingPeriod(start=today - dt.timedelta(days=29), end=today),
        closed_days_only=False,
    )
    return deps.rerun.plan(
        tenant_slug=slug,
        candidates=deps.queries.rerun_candidates(tenant_slug=slug, limit=RERUN_BATCH_CAP),
        cost_per_lead_usd=recent.cost_per_billable_lead_usd,
    )


def _config_form(
    *,
    request: Request,
    deps: AdminDeps,
    slug: str,
    document: str,
    note: str,
    errors: Sequence[str],
    status: int | None = None,
) -> Response:
    """Re-render the editor, keeping whatever the operator typed.

    ``status`` defaults to 400 when there are errors, which is right for a document the
    operator can fix by editing it. A lost save race passes 409 instead: nothing about the
    document is wrong, somebody else just got there first.
    """
    return _render(
        "admin/config_edit.html",
        {
            "slug": slug,
            "document": document,
            "note": note,
            "errors": tuple(errors),
            "csrf": _csrf_for(request, deps),
            "max_note": MAX_NOTE_CHARS,
        },
        status=status if status is not None else (400 if errors else 200),
        request=request,
    )


def _parse_config(raw: str) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    """Parse the textarea into a config document, or say why it is not one.

    Bounded before parsing, because parsing is the expensive part — the same order of
    operations the ingest endpoint uses, for the same reason.
    """
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        return None, (f"that document is larger than {MAX_CONFIG_BYTES // 1024} KiB",)
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        return None, (f"that is not valid JSON: {error}",)
    if not isinstance(parsed, dict):
        return None, ("a rubric is a JSON object",)
    return parsed, ()


def _csrf_for(request: Request, deps: AdminDeps) -> str:
    """This session's CSRF token, derived from the cookie the request arrived with."""
    return csrf_token(
        secret=deps.session_secret, session_token=request.cookies.get(SESSION_COOKIE, "")
    )


def _form(request: Request) -> Mapping[str, Any]:
    """The posted form, as the router's CSRF dependency already read it.

    Not re-read from the stream: an ASGI request body is consumed once, and the dependency
    that verified the token had to read it to find the token. Taking the cached copy is
    also what makes the ordering safe — by the time a handler body runs, the form it is
    about to act on is the same bytes the token was checked against.
    """
    cached: Mapping[str, Any] | None = getattr(request.state, "admin_form", None)
    if cached is None:  # pragma: no cover - unreachable behind the router dependency
        raise HTTPException(status_code=400, detail="that request carried no form")
    return cached


def _guarded(page: Callable[[], Response]) -> Response:
    """Render a page, turning any unexpected failure into a page that says nothing.

    The admin renders payloads, so an exception here can be holding one — in a message, in
    a repr, in a traceback. A default error page that echoed the request or the record
    would put a lead's contact details on a screen and, worse, in whatever collects
    unhandled exceptions. So the class name is logged and a fixed page is returned.
    """
    try:
        return page()
    except HTTPException:
        raise
    except UnknownTenantError:
        return _failure_page("there is no such tenant", status=404)
    except Exception as error:
        # The class name and nothing else. Deliberately no `exc_info`: a traceback's frames
        # hold the row being rendered, and the exception's own message is written by
        # whatever raised — a driver quoting the bytes it could not decode, for instance.
        log_event(
            LOGGER,
            EVENT_ADMIN_PAGE_FAILED,
            level=logging.ERROR,
            error=type(error).__name__,
        )
        return _failure_page("something went wrong rendering that page")


def _failure_page(message: str, *, status: int = 500) -> Response:
    """A fixed page. ``message`` is one of this module's own strings, never a record.

    The status is part of the honesty: an operator who typed a rationale that is too short
    has not caused a server error, and a monitor that alarmed on it would be alarming on
    somebody using the tool correctly. 500 is the default because the path that has no
    better answer is the unexpected one.
    """
    return _render("admin/error.html", {"message": message}, status=status)


def _render(
    template: str,
    context: Mapping[str, Any],
    *,
    status: int = 200,
    request: Request | None = None,
) -> Response:
    """Render one template with the admin's standard headers.

    ``request`` is passed by the guarded pages and carries the token the layout's sign-out
    form needs — separate from the ``csrf`` a page puts in its own forms, because the
    layout needs one on *every* guarded page including those with no form of their own.
    Omitting it is how the login page and the error page say "draw no sign-out button",
    which is right for both.
    """
    nav_csrf: str | None = getattr(request.state, "nav_csrf", None) if request is not None else None
    body = (
        _templates()
        .get_template(template)
        .render(
            **context,
            admin_prefix=ADMIN_PREFIX,
            csrf_field=CSRF_FIELD,
            nav_csrf=nav_csrf,
        )
    )
    return HTMLResponse(content=body, status_code=status, headers=dict(_PAGE_HEADERS))


def _window(raw: str | None) -> DashboardWindow:
    """The chosen dashboard window, defaulting to a month."""
    try:
        return DashboardWindow(raw) if raw else DashboardWindow.MONTH
    except ValueError:
        return DashboardWindow.MONTH


def _page_size(raw: str | None) -> int:
    """A page size from a query string, bounded. ``?limit=1000000`` is not a preference."""
    try:
        asked = int(raw) if raw else DEFAULT_PAGE_SIZE
    except ValueError:
        return DEFAULT_PAGE_SIZE
    return max(1, min(asked, MAX_PAGE_SIZE))


def _date(raw: str | None) -> dt.date | None:
    """An ISO date from a query string, or ``None`` if it is not one."""
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _decimal(raw: str | None) -> Decimal | None:
    """A number from a query string, or ``None`` if it is not a usable one.

    ``Decimal`` accepts ``NaN``, ``Infinity`` and ``1E+10000`` — all of which arrive from a
    query string as easily as ``0.5``. ``NaN`` compares false against everything, so it
    would silently return an empty page rather than an error; the others become a bind
    parameter the driver has to render. A confidence is a probability, so anything that is
    not a finite number is not one.
    """
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _money(value: Decimal | None) -> str:
    """Money, or an em dash. Never a bare ``None`` on a page about spending."""
    return "—" if value is None else f"${value:,.4f}"


def _percent(value: Decimal | None) -> str:
    """A rate as a percentage, or an em dash for "no basis to say"."""
    return "—" if value is None else f"{value * 100:.0f}%"


def _pretty_json(value: Any) -> str:
    """A payload or a config, indented. Autoescaped by the template like any other text."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
