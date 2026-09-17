"""The public ingest edge: ``POST /leads`` and ``GET /health``.

The ingest route is written for a stranger, and the order of operations is the order of
cost, cheapest and most sceptical first:

1. **Size.** ``Content-Length`` is checked before a byte is read, and the stream is counted
   as it arrives so a request that lies about its length — or declares nothing at all — is
   cut off at the limit. An oversized body is refused *before* parsing, because parsing is
   the expensive part and doing it on unauthenticated input is the whole vulnerability.
2. **Authentication.** The API key and the HMAC signature over the raw bytes, compared with
   :func:`hmac.compare_digest`. Every rejection — unknown tenant, wrong key, forged
   signature, stale timestamp, replayed nonce — is the same 401 with the same body. A
   different status, a different message or a measurably different response time would
   turn the endpoint into an oracle for which tenants exist.
3. **Rate limit**, per authenticated tenant, from the allowance on that tenant's row. The
   stage-level throttle in ``infra/template.yaml`` is the account-wide backstop behind it;
   see ``docs/tenant-onboarding.md`` for why these are not API Gateway usage plans.
4. **Schema validation.** Only now is the body parsed.
5. **Persist, screen, enqueue** — :class:`~leadquali.app.ingest.IngestService`.
6. **202**, with the submission id echoed back.

The body is read **once**, into bytes, and those same bytes are what the signature covers
and what the parser sees. Reading a request stream twice is the classic bug in this shape
of code, and re-serialising parsed JSON to check a signature is the subtler version of it:
a key order or a float repr that differs by one character breaks every signature, and it
breaks them for the customer, in production, on a Friday.

**Nothing slow happens here.** No model call, no enrichment, no DNS, no email — the handler
does two or three short SQL statements and a queue write. That is not an optimisation, it
is the architecture (plan §3): a Claude call with adaptive thinking takes seconds, and a
form post that waits on one produces browser timeouts, duplicate submissions and a lead
lost every time the model is slow. The app is constructed without a
:class:`~leadquali.app.ports.LeadAssessorPort` at all, so the fast path cannot regress into
a slow one by accident; ``tests/unit/test_api_ingest.py`` asserts that structurally.

The other public surface, ``/feedback/{token}``, is registered here by
:func:`~leadquali.api.feedback.register_feedback_routes` and implemented in
:mod:`leadquali.api.feedback`. It shares this app because it is one deployment, and it
shares nothing else: its dependencies are a separate object, so the ingest handler cannot
reach a feedback writer and the feedback handler holds no ingest credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from leadquali.adapters.clock_system import SystemClock
from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.adapters.store_postgres import PostgresLeadStore, session_factory_from_env
from leadquali.adapters.store_tenants import PostgresIngestCredentials, PostgresTenantAdminStore
from leadquali.api.admin import AdminDeps, register_admin_routes
from leadquali.api.feedback import FeedbackDeps, register_feedback_routes
from leadquali.api.ratelimit import NoRateLimit, RateLimiterPort, TenantRateLimiter
from leadquali.api.schemas import (
    MAX_BODY_BYTES,
    ErrorResponse,
    FieldError,
    IngestAccepted,
    LeadIngestRequest,
    ValidationErrorResponse,
)
from leadquali.api.signing import (
    AuthFailure,
    AuthRejected,
    IngestCredentialSource,
    ReplayGuard,
    verify,
)
from leadquali.app.ingest import IngestRequest, IngestService
from leadquali.app.ports import ClockPort
from leadquali.config import Settings, get_settings
from leadquali.domain.spam import DEFAULT_SPAM_POLICY, SpamPolicy
from leadquali.observability import configure_logging, log_context, log_event, new_trace_id

LOGGER = logging.getLogger(__name__)

#: The ingest route, and the path the signature is computed over.
#:
#: Signing a *constant* rather than ``request.url.path`` is deliberate: behind API Gateway
#: the deployed path carries a stage prefix (``/prod/leads``) that the form neither knows
#: nor should have to know. The logical path is the contract; the deployment's URL is not.
INGEST_PATH: Final[str] = "/leads"

#: The load balancer's probe. Deliberately free of dependencies — see :func:`create_app`.
HEALTH_PATH: Final[str] = "/health"

#: The one thing a rejected caller is told. No variation, ever: "unknown tenant" and
#: "wrong key" must be indistinguishable, or the endpoint enumerates its own customers.
_UNAUTHORISED_DETAIL: Final[str] = "authentication failed"

#: The one exception, and it is not an oracle. A caller that reaches this has already
#: presented a valid, unrevoked key *and passed the argon2 check* for the tenant it named,
#: so there is nothing left to enumerate — and the integrator on the customer's side needs
#: to know that the account, not their integration, is what stopped working. See
#: :mod:`leadquali.app.credentials` for why the status is checked after the KDF.
_SUSPENDED_DETAIL: Final[str] = "tenant is not active"

#: A dependency of ours is down, not a problem with the request. Answered 503 rather than
#: 401 or 500: a 401 would tell a good customer their key is bad, and a browser form that
#: receives a 500 does not retry — the lead would simply be gone, which is invariant 3
#: broken by an outage in something else.
_UNAVAILABLE_DETAIL: Final[str] = "temporarily unable to accept submissions; retry shortly"

#: How long a 503'd sender is asked to wait. Comfortably longer than a Secrets Manager
#: throttle takes to clear and shorter than a visitor will keep a tab open.
_UNAVAILABLE_RETRY_AFTER: Final[int] = 5

_NO_STORE: Final[dict[str, str]] = {"Cache-Control": "no-store"}


@dataclass(frozen=True, slots=True)
class IngestDeps:
    """Everything the ingest route needs, injected rather than imported.

    Constructed once per process by :func:`build_deps` in production and by the tests with
    in-memory doubles. There is deliberately no assessor, notifier or enricher in here:
    the endpoint cannot call what it has not been given.
    """

    service: IngestService
    credentials: IngestCredentialSource
    clock: ClockPort
    replay_guard: ReplayGuard = field(default_factory=ReplayGuard)
    rate_limiter: RateLimiterPort = field(default_factory=NoRateLimit)
    max_body_bytes: int = MAX_BODY_BYTES


def build_deps(
    settings: Settings | None = None, *, spam_policy: SpamPolicy = DEFAULT_SPAM_POLICY
) -> IngestDeps:
    """Wire the production dependencies: Postgres, the in-process queue, the real clock.

    Credentials come from the database (``tenants`` + ``tenant_api_keys``), not from the
    ``INGEST_CREDENTIALS`` environment secret. That is the whole point of #31: a revocation
    is then one ``UPDATE`` that takes effect on the next request, where editing a JSON
    secret would have to propagate to every warm container and would leave the old value
    live for the length of a cache TTL. ``StaticCredentials`` is still there for the tests
    and for a laptop, but nothing assembles it here.

    The queue is ``InProcessLeadQueue`` because there is no SQS yet — #26 owns the producer
    and swaps it in here, behind :class:`~leadquali.app.ingest.LeadQueuePort`, with no
    change to the route. Until then a lead accepted on one process is qualified by that
    same process (or, in collecting mode, by whatever drains it), which is enough to run
    the whole pipeline on a laptop and not enough to run it in production.

    Raises:
        RuntimeError: ``DATABASE_URL`` is not configured. Loud at wiring time rather than
            at the first lead: a process that started without a credential store would
            reject every real customer, and one that treated "no store" as "no auth
            needed" would accept every stranger.
    """
    resolved = settings if settings is not None else get_settings()
    clock = SystemClock()
    sessions = session_factory_from_env(resolved)
    # One hasher per process, because its memo and its per-key failure gate are what keep
    # argon2 off the hot path; a fresh one per request would defeat both.
    verifier = Argon2KeyHasher()
    return IngestDeps(
        service=IngestService(
            store=PostgresLeadStore(sessions),
            queue=InProcessLeadQueue(),
            clock=clock,
            spam_policy=spam_policy,
        ),
        credentials=PostgresIngestCredentials(
            sessions, verifier=verifier, resolver=resolved.secret_resolver()
        ),
        clock=clock,
        rate_limiter=TenantRateLimiter(PostgresTenantAdminStore(sessions)),
    )


@lru_cache(maxsize=1)
def _default_deps() -> IngestDeps:
    """The production dependencies, built on first request rather than at import.

    Importing this module must not open a database connection or demand a secret: the same
    module is imported by the tests, by ``--reload``'s child process and by Mangum at cold
    start. Deferring the wiring to the first request keeps ``import leadquali.api.main``
    free of side effects while still failing loudly the moment a request needs something
    that was never configured.
    """
    return build_deps()


def create_app(
    deps: IngestDeps | None = None,
    feedback_deps: FeedbackDeps | None = None,
    admin_deps: AdminDeps | None = None,
) -> FastAPI:
    """Build the ASGI application.

    Three surfaces share it and share nothing else: ``POST /leads``, which a customer's
    website calls with a signed request; ``GET``/``POST /feedback/{token}``, which a sales
    rep opens from an email; and ``/admin``, which a member of staff signs in to. One app
    because it is one deployment — the feedback link has to resolve on a host the rep can
    reach, and standing up a second service would mean a second domain, a second
    certificate and a second thing to page someone about. Their dependencies stay separate
    objects (:class:`~leadquali.api.feedback.FeedbackDeps`,
    :class:`~leadquali.api.admin.AdminDeps`) so that no endpoint can reach another's
    collaborators — ingest must not be able to reach a
    :class:`~leadquali.app.tenants.TenantService`, and the admin has no business holding
    the lead queue.

    **The admin routes are mounted behind their own session dependency**, declared on the
    router rather than on each handler, so a route added later is protected by where it
    lives. ``tests/unit/test_api_admin.py`` enumerates them from this app and checks it.

    Args:
        deps: the ingest wiring. ``None`` — the default, and what uvicorn and Mangum get —
            resolves the production dependencies lazily on the first request.
        feedback_deps: the feedback wiring, resolved the same way.
        admin_deps: the admin wiring, resolved the same way.

    Returns:
        A deployment-agnostic ASGI app. It is served by uvicorn locally (``run_local.py``)
        and by Mangum in Lambda (``api/handlers.py``); neither is mentioned here.
    """
    provide: Callable[[], IngestDeps] = (lambda: deps) if deps is not None else _default_deps

    app = FastAPI(
        title="LeadQuali",
        version="1",
        summary=(
            "Accepts inbound web-form leads before the model is ever called, and records "
            "the one-click verdicts that grow the golden set."
        ),
        docs_url="/docs",
        redoc_url=None,
    )
    register_feedback_routes(app, feedback_deps)
    register_admin_routes(app, admin_deps)

    @app.get(
        HEALTH_PATH,
        summary="Liveness probe for the load balancer.",
        response_model=dict[str, str],
    )
    async def health() -> dict[str, str]:
        """Say that the process is up.

        Deliberately checks nothing else. A probe that touched the database would take the
        whole service out of the load balancer during a failover that ingest could have
        ridden out — and it would do so on an unauthenticated endpoint, which is a free
        way to make someone else's database do work.
        """
        return {"status": "ok"}

    @app.post(
        INGEST_PATH,
        status_code=202,
        summary="Accept one signed web-form lead.",
        response_model=None,
        responses={
            202: {"model": IngestAccepted, "description": "Recorded. The verdict is not public."},
            401: {"model": ErrorResponse, "description": "Key or signature rejected."},
            403: {"model": ErrorResponse, "description": "The tenant is suspended."},
            413: {"model": ErrorResponse, "description": "Body larger than the limit."},
            422: {"model": ValidationErrorResponse, "description": "Schema validation failed."},
            429: {"model": ErrorResponse, "description": "Rate limited."},
            503: {
                "model": ErrorResponse,
                "description": "A dependency is unavailable; retry after the given delay.",
            },
        },
    )
    async def ingest(request: Request) -> Response:
        """Authenticate, validate, record and enqueue one lead. See the module docstring."""
        return await _handle_ingest(request, provide())

    return app


async def _handle_ingest(request: Request, deps: IngestDeps) -> Response:
    """The ingest handler, outside the closure so it can be read and tested on its own."""
    trace_id = new_trace_id()
    with log_context(trace_id=trace_id):
        return await _handle_ingest_traced(request, deps, trace_id)


async def _handle_ingest_traced(request: Request, deps: IngestDeps, trace_id: str) -> Response:
    """The handler proper, under a bound trace id.

    The id is minted *here* rather than in
    :meth:`~leadquali.app.ingest.IngestService.accept` so that a request refused before the
    service is ever reached — 413, 401, 429, 422 — still logs under an id. A stranger's
    rejected probe and a customer's accepted lead are then the same kind of thing in the
    log, which is what makes "what happened to this request" answerable at all.
    """
    started_ms = deps.clock.monotonic_ms()

    body = await _read_bounded_body(request, deps.max_body_bytes)
    if body is None:
        return _error(413, "request body is larger than the limit")

    auth = verify(
        method=request.method,
        path=INGEST_PATH,
        headers=request.headers,
        body=body,
        credentials=deps.credentials,
        replay_guard=deps.replay_guard,
        now=deps.clock.now(),
    )
    if isinstance(auth, AuthRejected):
        return _refuse(auth.failure, request)

    limit = deps.rate_limiter.check(tenant_id=auth.tenant_id, now=deps.clock.now())
    if not limit.allowed:
        log_event(
            LOGGER,
            "ingest.rate_limited",
            level=logging.WARNING,
            tenant_id=auth.tenant_id,
            retry_after_seconds=limit.retry_after_seconds,
        )
        return _error(
            429,
            "too many submissions; retry later",
            headers={"Retry-After": str(limit.retry_after_seconds)},
        )

    try:
        payload = LeadIngestRequest.model_validate_json(body)
    except ValidationError as error:
        return _validation_error(error)

    receipt = deps.service.accept(
        IngestRequest(
            tenant_id=auth.tenant_id,
            submission_id=payload.submission_id,
            submission=payload.form.to_submission(),
            source=payload.source,
            honeypot=payload.honeypot,
            elapsed_ms=payload.elapsed_ms,
            trace_id=trace_id,
        )
    )

    # The lead's own event — identifiers, a hash and a disposition — is emitted by the
    # ingest service, so that #26's producer and a replay script emit it too. This line is
    # about the *request*: what the endpoint answered and how long it took, which is the
    # 200 ms budget in plan section 3 made observable.
    log_event(
        LOGGER,
        "http.ingest",
        tenant_id=receipt.tenant_id,
        submission_id=receipt.submission_id,
        lead_id=receipt.lead_id,
        disposition=receipt.disposition.value,
        status=202,
        latency_ms=deps.clock.monotonic_ms() - started_ms,
    )

    accepted = IngestAccepted(submission_id=receipt.submission_id, received_at=receipt.received_at)
    return JSONResponse(
        status_code=202, content=accepted.model_dump(mode="json"), headers=_NO_STORE
    )


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read the whole body, once, refusing anything over ``limit``.

    Returns ``None`` when the request is too large — checked against ``Content-Length``
    first, so an oversized post costs nothing, and then counted across the stream, because
    a declared length is a claim by the sender and a chunked request makes no claim at all.

    The bytes are returned rather than stashed on the request because they are what the
    signature covers: one read, one buffer, and the parser gets exactly what was signed.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _refuse(failure: AuthFailure, request: Request) -> JSONResponse:
    """Turn a rejection into the one answer the caller is allowed to see.

    Three answers, and the split matters more than it looks. Almost everything is an
    identical 401, because a different status or a different message for "no such tenant"
    than for "wrong key" is a free enumeration oracle. A suspended tenant is a 403 —
    reachable only *after* the argon2 check, so only by someone who has proved they hold
    the secret. And a dependency we could not read is a 503 with a ``Retry-After``: the
    sender did nothing wrong and is the one party who can still save the lead by coming
    back.
    """
    match failure:
        case AuthFailure.TENANT_SUSPENDED:
            status, detail, headers = 403, _SUSPENDED_DETAIL, None
        case AuthFailure.UNAVAILABLE:
            status, detail, headers = (
                503,
                _UNAVAILABLE_DETAIL,
                {"Retry-After": str(_UNAVAILABLE_RETRY_AFTER)},
            )
        case _:
            status, detail, headers = 401, _UNAUTHORISED_DETAIL, None
    _log_rejection(failure, request, status)
    return _error(status, detail, headers=headers)


def _log_rejection(failure: AuthFailure, request: Request, status: int) -> None:
    """Record *why* a request was refused, where only we can see it.

    The tenant header is logged as claimed — it is an assertion by a stranger, not a fact,
    and it is the only handle an operator has on "a customer's form has the wrong key"
    versus "someone is probing us". The one case where it is a *fact* is a suspended
    tenant, which is also the one case the caller is told anything about.
    """
    log_event(
        LOGGER,
        "ingest.rejected",
        level=logging.WARNING,
        reason=failure.value,
        claimed_tenant=request.headers.get("x-leadquali-tenant", "-")[:64],
        client=request.client.host if request.client is not None else "-",
        status=status,
    )


def _error(status: int, detail: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    """A body-shaped error: one sentence, nothing about what exists."""
    merged = dict(_NO_STORE)
    if headers:
        merged.update(headers)
    return JSONResponse(
        status_code=status, content=ErrorResponse(detail=detail).model_dump(), headers=merged
    )


def _validation_error(error: ValidationError) -> JSONResponse:
    """A 422 that helps the integrator without echoing the submission back.

    Pydantic's own error dicts carry the offending ``input`` — which is the lead's data,
    and would put a stranger's email address in an error body and, from there, into
    whatever logs it (invariant 5). Only the field path, the message and the type survive.
    """
    errors = [
        FieldError(
            field=".".join(str(part) for part in item["loc"]) or "body",
            message=item["msg"],
        )
        for item in error.errors(include_url=False)[:20]
    ]
    body: dict[str, Any] = ValidationErrorResponse(
        detail="the submission did not validate", errors=errors
    ).model_dump()
    return JSONResponse(status_code=422, content=body, headers=_NO_STORE)


# Logging is configured at import, before the app object exists, because uvicorn, Mangum
# and `run_local.py` all import this module and none of them gives us a startup hook we can
# rely on — and a request served before logging is configured is a request with no record.
# `configure_logging` converges rather than accumulating, so the reload child process, the
# Lambda cold start and a test that reconfigures afterwards are all safe.
configure_logging()

#: The ASGI application. ``uvicorn leadquali.api.main:app`` and ``run_local.py`` serve this
#: one; ``api/handlers.py`` wraps the same object for Lambda.
app = create_app()


__all__ = [
    "HEALTH_PATH",
    "INGEST_PATH",
    "AdminDeps",
    "IngestDeps",
    "app",
    "build_deps",
    "create_app",
]
