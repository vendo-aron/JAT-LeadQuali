"""The feedback endpoint: where a click in a sales rep's inbox becomes training data.

This is the other half of :mod:`leadquali.adapters.notify_ses`, and between them they are
the only mechanism by which this product ever learns anything. The decision record is
explicit that there is no historical labelled data, so the golden set (#22) can only grow
from the ``feedback`` table, and #23's eval harness measures nothing until it does. Every
decision in this file is therefore made in favour of *the click actually happening and
actually meaning something*.

Why a GET does not write
------------------------

The obvious design — one link, ``GET`` it, row written — is broken by the environment it
ships into. Outlook Safe Links, Proofpoint, Mimecast, Gmail's image and link prefetchers and
half the corporate mail gateways in existence **fetch every URL in a message before a human
sees it**, some of them from a data centre on another continent, some of them twice. A
mutating GET means every routed lead silently acquires a verdict nobody gave, and the golden
set fills with noise that looks exactly like signal. That is worse than having no feedback
at all: an eval harness calibrated on scanner clicks would confidently point the rubric in a
random direction.

So the endpoint is split along the lines HTTP already draws:

* ``GET /feedback/{token}`` is **safe**. It verifies the token and renders a page with one
  large button and an optional notes box. It writes nothing, so a scanner that fetches it
  ten times changes nothing.
* ``POST /feedback/{token}`` writes. The form carries a ``confirm`` field whose value is
  derived from the token with the same secret (:func:`confirmation_code`), so the write
  requires having *rendered the page*, not merely having seen the URL. A scanner that
  blindly POSTs the URL with no body — rare, but they exist — gets the confirmation page
  back instead of a write.

The cost is one extra tap. The alternative considered was an idempotent write on GET plus a
visible undo, which keeps the single tap; it was rejected because the undo only helps a
*human* who is looking at the page, and the population being defended against is machines
that never render it. A tap that a rep expects (the page says "one more tap") is a far
cheaper price than a dataset we cannot trust. The confirmation page also has somewhere
honest to put the notes box, which #19 asks for and which a bare redirect has no room for.

Everything else the rep sees
----------------------------

The response is always a page, never JSON: this URL is opened by a human in a mobile
browser, and a bare ``{"ok": true}`` is a feature that looks broken. A repeat click updates
rather than duplicates (the store's job), and the thank-you page offers the opposite verdict
as a one-tap change of mind — minted here, server-side, bound to the same lead, rater and
expiry as the token that got them here, so changing your mind cannot become a way to write
feedback about anything else.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from fastapi import FastAPI, Request, Response

from leadquali.adapters.clock_system import SystemClock
from leadquali.adapters.store_postgres import PostgresFeedbackStore
from leadquali.app.feedback import (
    FEEDBACK_PATH_PREFIX,
    MAX_NOTES_CHARS,
    FeedbackClaim,
    TokenFailure,
    TokenRejected,
    UnknownLeadError,
    Verdict,
    check_token_secret,
    load_token_secret,
    mint_token,
    verify_token,
)
from leadquali.app.ports import ClockPort, FeedbackStorePort, RecordedFeedback
from leadquali.config import Settings, get_settings

LOGGER = logging.getLogger(__name__)

#: The route, built from the prefix the link minter uses, so the two cannot drift apart.
FEEDBACK_ROUTE: Final[str] = f"{FEEDBACK_PATH_PREFIX}{{token}}"

#: The form field that proves the page was rendered before the verdict was posted.
CONFIRM_FIELD: Final[str] = "confirm"
NOTES_FIELD: Final[str] = "notes"

#: Longest form body accepted. A verdict plus a sentence; anything larger is not a rep.
MAX_FORM_BYTES: Final[int] = 8 * 1024

#: Never cached, never indexed. These URLs are single-purpose capabilities, and a copy in a
#: corporate proxy or a search index is a copy nobody controls.
_PAGE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
}

_GOOD_COLOUR: Final[str] = "#067647"
_BAD_COLOUR: Final[str] = "#b42318"


@dataclass(frozen=True, slots=True)
class FeedbackDeps:
    """Everything the feedback routes need, injected rather than imported.

    Deliberately not :class:`~leadquali.api.main.IngestDeps`: the two endpoints share an
    ASGI app and nothing else. Ingest must not be able to reach a feedback writer, and the
    feedback endpoint has no business holding ingest credentials or the lead queue.
    """

    store: FeedbackStorePort
    token_secret: bytes
    clock: ClockPort

    def __post_init__(self) -> None:
        check_token_secret(self.token_secret)


def build_feedback_deps(settings: Settings | None = None) -> FeedbackDeps:
    """Wire the production feedback dependencies: Postgres, the real clock, the real secret.

    Raises:
        RuntimeError: ``DATABASE_URL`` or ``FEEDBACK_TOKEN_SECRET`` is not configured.
        ValueError: the secret is configured but too short to be one.
    """
    resolved = settings if settings is not None else get_settings()
    return FeedbackDeps(
        store=PostgresFeedbackStore.from_env(resolved),
        token_secret=load_token_secret(resolved.require_feedback_token_secret()),
        clock=SystemClock(),
    )


@lru_cache(maxsize=1)
def _default_feedback_deps() -> FeedbackDeps:
    """The production wiring, built on first request rather than at import.

    Same reasoning as :func:`leadquali.api.main._default_deps`: importing the module at a
    Lambda cold start must not open a database connection or demand a secret.
    """
    return build_feedback_deps()


def register_feedback_routes(app: FastAPI, deps: FeedbackDeps | None = None) -> None:
    """Add ``GET`` and ``POST /feedback/{token}`` to an existing application.

    Called by :func:`leadquali.api.main.create_app`, so there is one app and one deployment
    rather than a second service to route to. ``deps`` of ``None`` resolves the production
    wiring lazily on the first request.
    """
    provide: Callable[[], FeedbackDeps] = (
        (lambda: deps) if deps is not None else _default_feedback_deps
    )

    @app.get(
        FEEDBACK_ROUTE,
        response_class=Response,
        summary="Show the confirmation page for a feedback link. Writes nothing.",
        responses={
            200: {"description": "The confirmation page, or an explanation of a bad link."},
            400: {"description": "The link is not one of ours."},
            410: {"description": "The link has expired."},
        },
    )
    async def show_feedback(token: str) -> Response:
        """Render the confirmation page. Deliberately safe: see the module docstring."""
        return _show(token, provide())

    @app.post(
        FEEDBACK_ROUTE,
        response_class=Response,
        summary="Record the verdict this link carries.",
        responses={
            200: {"description": "Recorded, updated, or asked to confirm."},
            400: {"description": "The link is not one of ours."},
            404: {"description": "The lead this link names no longer exists."},
            410: {"description": "The link has expired."},
            503: {"description": "The verdict could not be stored; try again."},
        },
    )
    async def record_feedback(token: str, request: Request) -> Response:
        """Verify, confirm, write, and say thank you."""
        form = _parse_form(await _read_bounded(request))
        return _record(token, form, provide())


# ------------------------------------------------------------------------- the handlers


def _show(token: str, deps: FeedbackDeps) -> Response:
    result = verify_token(secret=deps.token_secret, token=token, now=deps.clock.now())
    if isinstance(result, TokenRejected):
        return _rejection_page(result)
    return _page(
        title="Confirm your feedback",
        status=200,
        body=_confirm_form(claim=result.claim, token=token, deps=deps, notes=""),
    )


def _record(token: str, form: Mapping[str, str], deps: FeedbackDeps) -> Response:
    result = verify_token(secret=deps.token_secret, token=token, now=deps.clock.now())
    if isinstance(result, TokenRejected):
        return _rejection_page(result)

    claim = result.claim
    notes = _clean_notes(form.get(NOTES_FIELD, ""))
    if not _confirmation_matches(form.get(CONFIRM_FIELD, ""), token=token, deps=deps):
        # Not an error page: an unconfirmed POST is almost always a machine, and the one
        # human who could reach this (a resubmitted form after a rotation) deserves the
        # button rather than a scolding.
        LOGGER.info(
            "feedback post without a valid confirmation tenant=%s lead=%s",
            claim.tenant_id,
            claim.lead_id,
        )
        return _page(
            title="Confirm your feedback",
            status=200,
            body=_confirm_form(claim=claim, token=token, deps=deps, notes=notes),
        )

    now = deps.clock.now()
    try:
        recorded = deps.store.record_feedback(
            tenant_id=claim.tenant_id,
            lead_id=claim.lead_id,
            rater=claim.rater,
            verdict=claim.verdict,
            notes=notes or None,
            recorded_at=now,
        )
    except UnknownLeadError:
        LOGGER.warning(
            "feedback for a lead that no longer exists tenant=%s lead=%s",
            claim.tenant_id,
            claim.lead_id,
        )
        return _page(
            title="That lead is gone",
            status=404,
            body=_message_block(
                heading="We could not find that lead",
                detail=(
                    "The lead this link points at is no longer in the system — it may have "
                    "been deleted under the data retention policy. Nothing was recorded."
                ),
            ),
        )
    except Exception:
        # The rep must never be told "thank you" for a verdict that was not stored: this is
        # the one signal the product learns from, and a silent loss is unrecoverable.
        LOGGER.exception("feedback write failed tenant=%s lead=%s", claim.tenant_id, claim.lead_id)
        return _page(
            title="We could not save that",
            status=503,
            body=_message_block(
                heading="Something went wrong on our side",
                detail="Your verdict was not saved. Please tap the link again in a minute.",
            ),
        )

    # Identifiers and an opaque rater id only. Never the notes — a rep writes about a
    # person, in free text — and never an address (invariant 5).
    LOGGER.info(
        "feedback recorded tenant=%s lead=%s rater=%s verdict=%s created=%s changed=%s notes=%s",
        claim.tenant_id,
        claim.lead_id,
        claim.rater,
        recorded.verdict.value,
        recorded.created,
        recorded.changed,
        "yes" if notes else "no",
    )
    return _page(
        title="Thank you",
        status=200,
        body=_thank_you(claim=claim, recorded=recorded, deps=deps, notes=notes),
    )


# -------------------------------------------------------------------- the confirmation


def confirmation_code(*, secret: bytes, token: str) -> str:
    """A short value derived from the token, proving the confirmation page was rendered.

    Not a CSRF defence in the session sense — there is no session and nothing to steal —
    but the thing that keeps a *blind* POST from writing. Deriving it from the token with
    the same secret means the server stores nothing, the code cannot be guessed, and it is
    valid for exactly as long as the token it belongs to.
    """
    digest = hmac.new(secret, f"confirm|{token}".encode(), hashlib.sha256).hexdigest()
    return digest[:32]


def _confirmation_matches(presented: str, *, token: str, deps: FeedbackDeps) -> bool:
    return hmac.compare_digest(presented, confirmation_code(secret=deps.token_secret, token=token))


# --------------------------------------------------------------------------- the pages


def _confirm_form(*, claim: FeedbackClaim, token: str, deps: FeedbackDeps, notes: str) -> str:
    colour = _GOOD_COLOUR if claim.verdict is Verdict.GOOD else _BAD_COLOUR
    code = confirmation_code(secret=deps.token_secret, token=token)
    return (
        f'<p class="lede">Marking this lead as a '
        f'<strong style="color:{colour}">{html.escape(claim.verdict.label)}</strong>.</p>'
        f'<form method="post" action="{html.escape(_path_for(token), quote=True)}">'
        f'<input type="hidden" name="{CONFIRM_FIELD}" value="{html.escape(code, quote=True)}" />'
        f'<label for="{NOTES_FIELD}">Anything worth adding? (optional)</label>'
        f'<textarea id="{NOTES_FIELD}" name="{NOTES_FIELD}" rows="3" '
        f'maxlength="{MAX_NOTES_CHARS}" placeholder="e.g. wrong industry, but a good fit '
        f'for the other product">{html.escape(notes)}</textarea>'
        f'<button type="submit" style="background:{colour}">'
        f"Confirm: {html.escape(claim.verdict.label)}</button>"
        "</form>"
        '<p class="note">One more tap, and only because mail scanners follow links before '
        "you do — this keeps their clicks out of the data.</p>"
    )


def _thank_you(
    *, claim: FeedbackClaim, recorded: RecordedFeedback, deps: FeedbackDeps, notes: str
) -> str:
    colour = _GOOD_COLOUR if recorded.verdict is Verdict.GOOD else _BAD_COLOUR
    verdict = f'<strong style="color:{colour}">{html.escape(recorded.verdict.label)}</strong>'
    if recorded.changed and recorded.previous_verdict is not None:
        headline = f"Changed from {html.escape(recorded.previous_verdict.label)} to {verdict}."
    elif recorded.created:
        headline = f"Recorded as a {verdict}."
    else:
        headline = f"Still recorded as a {verdict}."

    blocks = [
        f'<p class="lede">{headline}</p>',
        '<p class="note">This is what tunes the scoring — thank you. You can close this tab.</p>',
    ]
    if notes:
        blocks.append('<p class="note">Your note was saved.</p>')

    opposite = claim.verdict.opposite
    if opposite is not claim.verdict:
        # Minted here, with the same lead, rater and expiry: a change of mind must not be a
        # way to write feedback about anything else, or to extend the capability's life.
        other_token = mint_token(
            secret=deps.token_secret,
            tenant_id=claim.tenant_id,
            lead_id=claim.lead_id,
            verdict=opposite,
            rater=claim.rater,
            expires_at=claim.expires_at,
        )
        other_code = confirmation_code(secret=deps.token_secret, token=other_token)
        blocks.append(
            f'<form method="post" action="{html.escape(_path_for(other_token), quote=True)}" '
            'class="secondary">'
            f'<input type="hidden" name="{CONFIRM_FIELD}" '
            f'value="{html.escape(other_code, quote=True)}" />'
            f'<button type="submit" class="link">Actually, this was a '
            f"{html.escape(opposite.label)}</button></form>"
        )
    return "".join(blocks)


def _rejection_page(rejected: TokenRejected) -> Response:
    """One page per refusal reason, and never a hint about what a good link looks like."""
    if rejected.failure is TokenFailure.EXPIRED:
        return _page(
            title="This link has expired",
            status=410,
            body=_message_block(
                heading="This link has expired",
                detail=(
                    "Feedback links stop working after a while so that an old email cannot "
                    "keep changing the record. Nothing was recorded."
                ),
            ),
        )
    return _page(
        title="This link is not valid",
        status=400,
        body=_message_block(
            heading="This link is not valid",
            detail=(
                "It may have been altered or truncated by a mail client. Open the original "
                "link from the routing email. Nothing was recorded."
            ),
        ),
    )


def _message_block(*, heading: str, detail: str) -> str:
    return f'<p class="lede">{html.escape(heading)}</p><p class="note">{html.escape(detail)}</p>'


def _page(*, title: str, status: int, body: str) -> Response:
    """One small self-contained page. No external assets, no scripts, no tracking.

    A rep opens this on a phone, on a train, on a corporate network that may block
    everything but the origin they were sent to, so the whole page is one document with its
    styles inline. There is no JavaScript at all: nothing here needs it, and a page that
    writes to a database is a page that should work with scripting disabled.
    """
    document = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8' />"
        "<meta name='viewport' content='width=device-width, initial-scale=1' />"
        "<meta name='robots' content='noindex, nofollow' />"
        f"<title>{html.escape(title)} · LeadQuali</title>"
        "<style>"
        ":root{color-scheme:light dark}"
        "*{box-sizing:border-box}"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;padding:24px;background:#f2f4f7;color:#101828;"
        "font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
        "Arial,sans-serif}"
        "main{background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:28px;"
        "max-width:32rem;width:100%}"
        "h1{font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:#475467;"
        "margin:0 0 16px}"
        ".lede{font-size:20px;margin:0 0 12px}"
        ".note{color:#475467;font-size:14px;margin:12px 0 0}"
        "label{display:block;font-size:14px;color:#475467;margin:20px 0 6px}"
        "textarea{width:100%;padding:10px;font:inherit;border:1px solid #d0d5dd;"
        "border-radius:8px;resize:vertical;background:#fff;color:#101828}"
        "button{margin-top:18px;width:100%;padding:16px;font:inherit;font-weight:700;"
        "color:#fff;border:0;border-radius:8px;cursor:pointer}"
        "form.secondary{margin-top:20px;border-top:1px solid #e4e7ec;padding-top:8px}"
        "button.link{background:none;color:#475467;font-weight:500;text-decoration:underline;"
        "padding:8px;margin-top:8px}"
        "@media (prefers-color-scheme:dark){body{background:#101828;color:#f9fafb}"
        "main{background:#1d2939;border-color:#344054}h1,.note,label{color:#98a2b3}"
        "textarea{background:#101828;color:#f9fafb;border-color:#475467}"
        "button.link{color:#98a2b3}}"
        "</style></head><body><main><h1>LeadQuali feedback</h1>"
        f"{body}</main></body></html>"
    )
    return Response(
        content=document,
        status_code=status,
        media_type="text/html; charset=utf-8",
        headers=dict(_PAGE_HEADERS),
    )


def _path_for(token: str) -> str:
    """The form's action: a relative path, so it works behind any host or stage prefix."""
    return f"{FEEDBACK_PATH_PREFIX}{urllib.parse.quote(token, safe='')}"


# ------------------------------------------------------------------------ the form body


async def _read_bounded(request: Request) -> bytes:
    """Read the form body, refusing anything past :data:`MAX_FORM_BYTES`.

    Counted across the stream rather than trusted from ``Content-Length``: this endpoint is
    as public as the ingest one, and a declared length is a claim by the sender.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FORM_BYTES:
            return b""
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_form(body: bytes) -> dict[str, str]:
    """Parse ``application/x-www-form-urlencoded`` without a multipart dependency.

    The form has two short fields and is submitted by a page this module rendered, so the
    stdlib parser is the whole requirement — pulling in ``python-multipart`` to read two
    strings would add an attack surface to the only unauthenticated write in the system.
    """
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(decoded, keep_blank_values=True).items()
    }


def _clean_notes(raw: str) -> str:
    """Bound and de-fang the rep's free text.

    Truncated rather than refused — losing a sentence a rep took the trouble to type is
    worse than storing 2 kB — and stripped of control characters, which a browser will not
    send but a hand-rolled POST will.
    """
    text = "".join(character for character in raw if character >= " " or character in "\n\t")
    text = text.strip()
    return text[:MAX_NOTES_CHARS]


__all__ = [
    "CONFIRM_FIELD",
    "FEEDBACK_ROUTE",
    "MAX_FORM_BYTES",
    "NOTES_FIELD",
    "FeedbackDeps",
    "build_feedback_deps",
    "confirmation_code",
    "register_feedback_routes",
]
