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
3. **Rate limit**, per authenticated tenant. A hook; #26's usage plans do the real work.
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
from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.adapters.store_postgres import PostgresLeadStore, contact_email_hash
from leadquali.api.feedback import FeedbackDeps, register_feedback_routes
from leadquali.api.ratelimit import NoRateLimit, RateLimiterPort
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
    load_credentials,
    verify,
)
from leadquali.app.ingest import IngestRequest, IngestService
from leadquali.app.ports import ClockPort
from leadquali.config import Settings, get_settings
from leadquali.domain.spam import DEFAULT_SPAM_POLICY, SpamPolicy

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

    The queue is ``InProcessLeadQueue`` because there is no SQS yet — #26 owns the producer
    and swaps it in here, behind :class:`~leadquali.app.ingest.LeadQueuePort`, with no
    change to the route. Until then a lead accepted on one process is qualified by that
    same process (or, in collecting mode, by whatever drains it), which is enough to run
    the whole pipeline on a laptop and not enough to run it in production.

    Raises:
        RuntimeError: ``DATABASE_URL`` or ``INGEST_CREDENTIALS`` is not configured.
        IngestCredentialsError: the credentials are configured but unreadable. Failing at
            startup is the point: a process that started with no credentials would reject
            every real customer, and one that treated "none configured" as "no auth
            needed" would accept every stranger.
    """
    resolved = settings if settings is not None else get_settings()
    clock = SystemClock()
    return IngestDeps(
        service=IngestService(
            store=PostgresLeadStore.from_env(resolved),
            queue=InProcessLeadQueue(),
            clock=clock,
            spam_policy=spam_policy,
        ),
        credentials=load_credentials(resolved.require_ingest_credentials()),
        clock=clock,
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
    deps: IngestDeps | None = None, feedback_deps: FeedbackDeps | None = None
) -> FastAPI:
    """Build the ASGI application.

    Two public surfaces share it and share nothing else: ``POST /leads``, which a customer's
    website calls with a signed request, and ``GET``/``POST /feedback/{token}``, which a
    sales rep opens from an email. One app because it is one deployment — the feedback link
    has to resolve on a host the rep can reach, and standing up a second service for two
    routes would mean a second domain, a second certificate and a second thing to page
    someone about. Their dependencies stay separate objects (see
    :class:`~leadquali.api.feedback.FeedbackDeps`) so that neither endpoint can reach the
    other's collaborators.

    Args:
        deps: the ingest wiring. ``None`` — the default, and what uvicorn and Mangum get —
            resolves the production dependencies lazily on the first request.
        feedback_deps: the feedback wiring, resolved the same way.

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
            413: {"model": ErrorResponse, "description": "Body larger than the limit."},
            422: {"model": ValidationErrorResponse, "description": "Schema validation failed."},
            429: {"model": ErrorResponse, "description": "Rate limited."},
        },
    )
    async def ingest(request: Request) -> Response:
        """Authenticate, validate, record and enqueue one lead. See the module docstring."""
        return await _handle_ingest(request, provide())

    return app


async def _handle_ingest(request: Request, deps: IngestDeps) -> Response:
    """The ingest handler, outside the closure so it can be read and tested on its own."""
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
        _log_rejection(auth.failure, request)
        return _error(401, _UNAUTHORISED_DETAIL)

    limit = deps.rate_limiter.check(tenant_id=auth.tenant_id, now=deps.clock.now())
    if not limit.allowed:
        LOGGER.warning("ingest rate limited tenant=%s", auth.tenant_id)
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
        )
    )

    # Invariant 5: identifiers, a hash and a disposition. Never the address, never the
    # lead's own words, never the raw payload.
    LOGGER.info(
        "lead accepted tenant=%s submission=%s lead=%s disposition=%s reason=%s "
        "contact=%s latency_ms=%d",
        receipt.tenant_id,
        receipt.submission_id,
        receipt.lead_id,
        receipt.disposition.value,
        receipt.spam_reason.value if receipt.spam_reason is not None else "-",
        contact_email_hash(payload.form.email) or "-",
        deps.clock.monotonic_ms() - started_ms,
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


def _log_rejection(failure: AuthFailure, request: Request) -> None:
    """Record *why* a request was refused, where only we can see it.

    The tenant header is logged as claimed — it is an assertion by a stranger, not a fact,
    and it is the only handle an operator has on "a customer's form has the wrong key"
    versus "someone is probing us".
    """
    LOGGER.warning(
        "ingest rejected reason=%s claimed_tenant=%s client=%s",
        failure.value,
        request.headers.get("x-leadquali-tenant", "-")[:64],
        request.client.host if request.client is not None else "-",
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


#: The ASGI application. ``uvicorn leadquali.api.main:app`` and ``run_local.py`` serve this
#: one; ``api/handlers.py`` wraps the same object for Lambda.
app = create_app()


__all__ = [
    "HEALTH_PATH",
    "INGEST_PATH",
    "IngestDeps",
    "app",
    "build_deps",
    "create_app",
]
