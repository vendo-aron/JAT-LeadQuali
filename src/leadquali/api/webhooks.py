"""The billing HTTP surface: Stripe's webhook in, and a tenant's billing portal out.

Two routes, registered onto the existing application the way
:func:`~leadquali.api.feedback.register_feedback_routes` is, and sharing nothing with
ingest but the deployment. :class:`BillingDeps` is its own object on purpose: the webhook
handler holds no lead store and no queue, so it cannot reach them however the app is
assembled, and the ingest handler holds no billing service.

``POST /webhooks/stripe``
-------------------------

Public, internet-facing, unauthenticated until an HMAC says otherwise, and it is the door
to *billing state* — which is why it does as little as it possibly can:

1. read the body once, bounded, into bytes;
2. verify the signature over **those bytes**, before parsing anything
   (:mod:`leadquali.api.stripe_signing`, standard library only);
3. parse, check it is an event with an id and a type;
4. ``INSERT ... ON CONFLICT (event_id) DO NOTHING``;
5. 200.

No tenant is updated here. The handlers run a minute later in a scheduled drain
(:meth:`~leadquali.app.billing.BillingService.process_pending`), and the reason is Stripe's
own retry behaviour: an endpoint that is slow or 500s is one Stripe re-delivers, so doing
real work inline turns a database blip into duplicate deliveries of a half-applied event.
Up to a minute of latency before a failed payment starts its grace period is nothing
against a dunning window measured in days.

**Every rejection is the same 400 with the same body.** Missing header, malformed header,
wrong signature, stale timestamp, body that is not a Stripe event — one status, one
sentence. A different message per failure would tell whoever is probing which part of their
forgery was wrong, and would tell someone holding a stale capture that their signature was
otherwise fine. 400 rather than 401 because that is what the issue asks for and what
Stripe's own convention expects: an unverifiable delivery is a bad request, and Stripe
stops retrying a 4xx rather than hammering us for three days.

The one thing that is *not* caught is a failure to store the event. That is a 500 on
purpose. A 200 we could not honour would make Stripe consider the event delivered and never
send it again, and an event Stripe will never resend is a subscription change we will never
know about.

``POST /billing/portal``
------------------------

A tenant's own backend asks for a URL where its people manage their payment method and
plan. Authenticated with **the same signed-request scheme as ingest**
(:mod:`leadquali.api.signing`) — the tenant already holds an API key and a signing secret,
and inventing a second credential for one endpoint would be a second thing to rotate and a
second thing to get wrong. The path is inside the signed string, so an ingest signature
cannot be replayed here.

Two deliberate restrictions:

* **The return url is configuration, not request input.** Taking it off the body would make
  an authenticated endpoint into a redirector to any page the caller names, wearing
  Stripe's domain on the way. ``STRIPE_PORTAL_RETURN_URL`` is the only source.
* **A suspended tenant gets a 403**, exactly as it does at ingest, because this endpoint
  reuses ingest's credential semantics rather than weakening them for a billing
  convenience. That is a real limitation with a real mitigation: a tenant stays *active*
  for the whole seven-day grace period, which is when self-service recovery matters, and
  Stripe's own dunning email carries a hosted invoice link that works regardless.
  ``docs/billing-integration.md`` says so where an operator will find it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from leadquali.api.signing import (
    AuthFailure,
    AuthRejected,
    IngestCredentialSource,
    ReplayGuard,
    verify,
)
from leadquali.api.stripe_signing import (
    STRIPE_SIGNATURE_HEADER,
    SignatureRejected,
    verify_signature,
)
from leadquali.app.billing import BillingService, UnknownBillingTenantError
from leadquali.app.ports import ClockPort
from leadquali.config import Settings, get_settings
from leadquali.observability import log_event

LOGGER: Final = logging.getLogger(__name__)

#: The webhook route. This exact string is what the endpoint in the Stripe dashboard is
#: configured with (#34's runbook); changing it means changing that too.
WEBHOOK_PATH: Final[str] = "/webhooks/stripe"

#: The portal route, and the path its signature is computed over. A constant rather than
#: ``request.url.path`` for the same reason ingest's is: behind API Gateway the deployed
#: path carries a stage prefix the caller neither knows nor should have to know.
PORTAL_PATH: Final[str] = "/billing/portal"

#: Largest webhook body accepted. Stripe events are a few kilobytes; an expanded invoice
#: with many lines is larger. Half a megabyte is comfortably above anything real and far
#: below what a stranger could make us HMAC for free.
MAX_WEBHOOK_BODY_BYTES: Final[int] = 512 * 1024

#: The only thing a rejected webhook is told. No variation, ever — see the module docstring.
_REJECTED_DETAIL: Final[str] = "signature verification failed"

_UNAUTHORISED_DETAIL: Final[str] = "authentication failed"
_SUSPENDED_DETAIL: Final[str] = "tenant is not active"

#: A dependency on the auth path was unreachable. The caller did nothing wrong and is the
#: one party who can still succeed by coming back, so it gets a 503 with a ``Retry-After``
#: rather than a 401 — the same answer, for the same reason, that ingest gives (#31's
#: review fix). Distinguishing it costs nothing here: a caller that reaches this has not
#: been told anything about whether their key is valid.
_UNAVAILABLE_DETAIL: Final[str] = "billing is temporarily unavailable; retry shortly"
_UNAVAILABLE_RETRY_AFTER: Final[int] = 5

_NO_STORE: Final[dict[str, str]] = {"Cache-Control": "no-store"}

__all__ = [
    "MAX_WEBHOOK_BODY_BYTES",
    "PORTAL_PATH",
    "WEBHOOK_PATH",
    "BillingDeps",
    "build_billing_deps",
    "register_billing_routes",
]


@dataclass(frozen=True, slots=True)
class BillingDeps:
    """Everything the billing routes need, injected rather than imported.

    Deliberately not :class:`~leadquali.api.main.IngestDeps`. The two surfaces share an
    ASGI app and nothing else: the webhook endpoint has no business holding a lead store or
    the queue, and ingest has no business holding a Stripe webhook secret.
    """

    service: BillingService
    webhook_secret: str
    """The endpoint's ``whsec_``, already validated by ``load_webhook_secret``."""

    clock: ClockPort
    credentials: IngestCredentialSource
    """The portal route's authentication. The same per-tenant keys ingest uses; see the
    module docstring for why there is not a second credential."""

    portal_return_url: str = ""
    replay_guard: ReplayGuard = field(default_factory=ReplayGuard)
    """The portal route's own nonce memory. Its own, not ingest's: the two endpoints share
    no collaborator, and a nonce burned at one must not refuse a request at the other."""

    max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES


def build_billing_deps(settings: Settings | None = None) -> BillingDeps:
    """Wire the production billing dependencies.

    Raises:
        RuntimeError: ``DATABASE_URL``, the Stripe API key, the price id, the webhook
            secret or the portal return url is not configured. Loud at wiring time rather
            than at the first webhook: a process that started without a webhook secret
            would reject every delivery Stripe made for three days.
        StripeWebhookSecretError: the webhook secret is configured and is not one.
    """
    from leadquali.adapters.billing_stripe import StripeBilling
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
    from leadquali.adapters.metering_postgres import PostgresMeteringStore
    from leadquali.adapters.revenue_none import UnknownRevenue
    from leadquali.adapters.store_billing import PostgresBillingStore
    from leadquali.adapters.store_postgres import session_factory_from_env
    from leadquali.adapters.store_tenants import PostgresIngestCredentials
    from leadquali.app.metering import MeteringService

    resolved = settings if settings is not None else get_settings()
    clock = SystemClock()
    sessions = session_factory_from_env(resolved)
    return BillingDeps(
        service=BillingService(
            store=PostgresBillingStore(sessions),
            billing=StripeBilling.from_env(resolved),
            metering=MeteringService(
                store=PostgresMeteringStore(sessions), clock=clock, revenue=UnknownRevenue()
            ),
            clock=clock,
        ),
        webhook_secret=resolved.require_stripe_webhook_secret(),
        clock=clock,
        credentials=PostgresIngestCredentials(
            sessions, verifier=Argon2KeyHasher(), resolver=resolved.secret_resolver()
        ),
        portal_return_url=resolved.require_stripe_portal_return_url(),
    )


@lru_cache(maxsize=1)
def _default_billing_deps() -> BillingDeps:
    """The production wiring, built on first request rather than at import.

    Same reasoning as :func:`leadquali.api.main._default_deps`: importing this module must
    not open a database connection or demand a Stripe key, because the tests, ``--reload``
    and Mangum's cold start all import it.
    """
    return build_billing_deps()


def register_billing_routes(app: FastAPI, deps: BillingDeps | None = None) -> None:
    """Add ``POST /webhooks/stripe`` and ``POST /billing/portal`` to an existing app.

    Args:
        app: the application to register on.
        deps: the billing wiring. ``None`` resolves the production dependencies lazily on
            the first request.
    """
    provide: Callable[[], BillingDeps] = (
        (lambda: deps) if deps is not None else _default_billing_deps
    )

    @app.post(
        WEBHOOK_PATH,
        response_model=None,
        summary="Receive one Stripe webhook. Verifies, stores and returns; applies later.",
        responses={
            200: {"description": "Verified and stored, or already held (a Stripe retry)."},
            400: {"description": "Unsigned, wrongly signed, stale, or not a Stripe event."},
            413: {"description": "Body larger than the limit."},
        },
    )
    async def stripe_webhook(request: Request) -> Response:
        """Verify, store, answer. See the module docstring."""
        return await _handle_webhook(request, provide())

    @app.post(
        PORTAL_PATH,
        response_model=None,
        summary="Open a Stripe billing portal session for the calling tenant.",
        responses={
            200: {"description": "A single-use portal URL."},
            401: {"description": "Key or signature rejected."},
            403: {"description": "The tenant is suspended."},
            409: {"description": "The tenant has no Stripe customer to manage."},
            413: {"description": "Body larger than the limit."},
        },
    )
    async def billing_portal(request: Request) -> Response:
        """Authenticate the tenant and hand back a portal URL."""
        return await _handle_portal(request, provide())


# ------------------------------------------------------------------------- the webhook


async def _handle_webhook(request: Request, deps: BillingDeps) -> Response:
    """The webhook handler, outside the closure so it can be read and tested on its own."""
    body = await _read_bounded_body(request, deps.max_body_bytes)
    if body is None:
        # Refused on size before a single HMAC, so a stranger cannot make us authenticate
        # an arbitrary number of megabytes.
        return _error(413, "request body is larger than the limit")

    outcome = verify_signature(
        body=body,
        header=request.headers.get(STRIPE_SIGNATURE_HEADER),
        secret=deps.webhook_secret,
        now=deps.clock.now(),
    )
    if isinstance(outcome, SignatureRejected):
        log_event(
            LOGGER,
            "billing.webhook_rejected",
            level=logging.WARNING,
            reason=outcome.failure.value,
            client=request.client.host if request.client is not None else "-",
            status=400,
        )
        return _error(400, _REJECTED_DETAIL)

    event = _parse_event(body)
    if event is None:
        # Authenticated but not the shape we can store. Logged at WARNING because a body
        # that Stripe signed and we cannot read means our idea of an event is out of date.
        log_event(
            LOGGER,
            "billing.webhook_rejected",
            level=logging.WARNING,
            reason="not_a_stripe_event",
            status=400,
        )
        return _error(400, _REJECTED_DETAIL)

    event_id, event_type, payload = event
    receipt = deps.service.receive_event(event_id=event_id, event_type=event_type, payload=payload)
    # Deliberately the same 200 whether we stored it or already held it: a Stripe retry is
    # a success from Stripe's point of view and must not look like anything else.
    return JSONResponse(
        status_code=200,
        content={"received": True},
        headers=_NO_STORE | {"X-LeadQuali-Event-Stored": "1" if receipt.stored else "0"},
    )


def _parse_event(body: bytes) -> tuple[str, str, dict[str, Any]] | None:
    """Read a verified body as a Stripe event, or ``None`` if it is not one.

    Only ``id`` and ``type`` are required, and both must be non-empty strings: ``id`` is
    the idempotency key and the primary key, so an event without one has nowhere to be
    stored, and ``type`` is what the drain dispatches on. Everything else is kept verbatim
    and interpreted later, which is what lets this endpoint survive Stripe adding fields.
    """
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    event_id = parsed.get("id")
    event_type = parsed.get("type")
    if not isinstance(event_id, str) or not event_id:
        return None
    if not isinstance(event_type, str) or not event_type:
        return None
    return event_id, event_type, parsed


# -------------------------------------------------------------------------- the portal


async def _handle_portal(request: Request, deps: BillingDeps) -> Response:
    """Authenticate a tenant with ingest's signing scheme and return a portal URL."""
    body = await _read_bounded_body(request, deps.max_body_bytes)
    if body is None:
        return _error(413, "request body is larger than the limit")

    auth = verify(
        method=request.method,
        path=PORTAL_PATH,
        headers=request.headers,
        body=body,
        credentials=deps.credentials,
        replay_guard=deps.replay_guard,
        now=deps.clock.now(),
    )
    if isinstance(auth, AuthRejected):
        return _refuse_portal(auth.failure)

    try:
        url = deps.service.portal_url(tenant_id=auth.tenant_id, return_url=deps.portal_return_url)
    except UnknownBillingTenantError:
        # 409 rather than 404: the tenant exists and is authenticated, it simply has no
        # Stripe customer yet. Creating one here as a side effect of somebody opening a
        # link is exactly the kind of implicit write that produces duplicate customers.
        log_event(
            LOGGER,
            "billing.portal_unavailable",
            level=logging.WARNING,
            tenant_id=auth.tenant_id,
        )
        return _error(409, "this tenant has no billing account yet")

    log_event(LOGGER, "billing.portal_opened", tenant_id=auth.tenant_id)
    return JSONResponse(status_code=200, content={"url": url}, headers=_NO_STORE)


def _refuse_portal(failure: AuthFailure) -> JSONResponse:
    """Turn a rejection into the one answer the caller is allowed to see.

    The same three-way split ``api/main.py`` makes, and deliberately the same code: an
    endpoint that answered differently would be a second place the enumeration rules are
    written. Almost everything is an identical 401; a suspended tenant is a 403, reachable
    only after the argon2 check and so only by someone who has proved they hold the secret
    (#31's review fix moved that check after the KDF, which is what makes the 403 safe to
    give); and an unreadable dependency is a 503 with a ``Retry-After``.
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
    log_event(
        LOGGER,
        "billing.portal_rejected",
        level=logging.WARNING,
        reason=failure.value,
        status=status,
    )
    return _error(status, detail, headers=headers)


# ------------------------------------------------------------------------------ shared


async def _read_bounded_body(request: Request, limit: int) -> bytes | None:
    """Read the whole body once, refusing anything over ``limit``.

    ``Content-Length`` first, so an oversized post costs nothing, and then counted across
    the stream, because a declared length is a claim by the sender and a chunked request
    makes no claim at all. The bytes are returned rather than re-read because they are what
    the signature covers — the same one-read rule as ingest, and for the same reason.
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


def _error(status: int, detail: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    """One sentence, nothing about what exists or which check failed."""
    merged = dict(_NO_STORE)
    if headers:
        merged.update(headers)
    return JSONResponse(status_code=status, content={"detail": detail}, headers=merged)
