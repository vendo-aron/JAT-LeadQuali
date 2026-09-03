"""The feedback capability: what a verdict is, and the token that authorises one.

This module is the reason the product can ever be evaluated. `docs/IMPLEMENTATION_PLAN.md`
§7 says the golden set grows from the ``feedback`` table, and the only thing that writes to
that table is a sales rep clicking a link in a routing email. So the link has to work from
a phone, in a mail client, with no login — and it has to be impossible to forge, because a
public URL that writes to the training set is a public URL that poisons it.

Why the token is a *bearer capability* and not a session
--------------------------------------------------------

There is no login and there will not be one: a rep who has to authenticate does not click,
and a feedback loop nobody uses is worth exactly nothing. What the URL carries instead is
one capability — "record verdict V for lead L, on behalf of rater R, until time T" — signed
with a key only this deployment holds. Everything that could be tampered with is inside the
signed material, so there is nothing an attacker can vary:

* **The lead id is signed.** It is never a bare path parameter, so a leaked link cannot be
  edited into a verdict on somebody else's lead — which, since ``lead_id`` is a UUID that
  appears in no other public surface, would otherwise be the one way to write arbitrary
  feedback.
* **The verdict is signed.** One token means one verdict. A "good lead" link cannot be
  turned into a "bad lead" one, so a leaked link cannot flip the label it was minted for.
* **The tenant is signed, and keys are derived per tenant** (:func:`tenant_key`). A token
  minted for tenant A does not verify under tenant B even if the process secret leaks into
  one tenant's logs.
* **An expiry is signed.** A link found in a forwarded mailbox two years later is inert.

Why this is a second HMAC scheme and not :mod:`leadquali.api.signing`
---------------------------------------------------------------------

The construction is deliberately the same shape as ``api.signing.signing_string`` — a
newline-separated canonical string, an algorithm label and a version as its first two
lines, HMAC-SHA256, :func:`hmac.compare_digest` — so there is one thing to learn and one
thing to review. It is a separate module for two reasons that are not stylistic:

* **Layering.** ``adapters/notify_ses.py`` mints these tokens and ``api/main.py`` verifies
  them. ``app`` is the only layer both may import (``domain ← app ← adapters/api``); an
  adapter importing the API package would invert that for the sake of a hash function.
* **Different threat, different key.** ``api.signing`` authenticates a *request* from a
  customer's website using a secret that customer holds. This authorises a *link* handed to
  that customer's staff. Signing feedback with the ingest secret would mean anyone holding
  a website's ingest key could mint labels straight into the training data.

Nothing here does I/O and nothing here imports a third-party package, so a token can be
minted and verified in a unit test, in the SES adapter and in the request handler with the
same code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

#: Names the scheme inside the signed material, so a future v2 with different fields cannot
#: be confused for a v1 token by either end.
TOKEN_ALGORITHM: Final[str] = "LEADQUALI-FEEDBACK-HMAC-SHA256"  # noqa: S105 — a label, not a secret

#: The scheme version, and the token's first dot-separated part.
TOKEN_VERSION: Final[str] = "fb1"  # noqa: S105 — a version marker, not a secret

#: Shortest process secret a deployment may configure, in characters. The same bar as
#: :data:`leadquali.api.signing.MIN_SIGNING_SECRET_CHARS`, for the same reason: the check
#: exists to catch a placeholder in a config file, not to make an HMAC stronger.
MIN_TOKEN_SECRET_CHARS: Final[int] = 32

#: How long a feedback link stays usable by default. Long enough that a rep who reads the
#: email on Monday and acts on Friday still counts, short enough that a link sitting in an
#: archived mailbox is not a standing write capability. Configurable per deployment.
DEFAULT_TOKEN_TTL_DAYS: Final[int] = 30

#: Longest token this module will even look at. A token is ~230 characters; the cap stops a
#: multi-megabyte path from being base64-decoded before it is rejected.
MAX_TOKEN_CHARS: Final[int] = 1024

#: Longest free-text note kept from the thank-you page. A rep writes a sentence; anything
#: past this is truncated rather than refused, because losing a rep's words to a validation
#: error is a worse outcome than storing 2 kB.
MAX_NOTES_CHARS: Final[int] = 2000

#: The public path a feedback link points at, with the token as its last segment. Defined
#: here, in the one module both ends import, so the adapter that *mints* the link and the
#: route that *serves* it cannot disagree about where it goes — a mismatch would be a dead
#: link in every email, discovered by a rep rather than by a test.
FEEDBACK_PATH_PREFIX: Final[str] = "/feedback/"

_KEY_DERIVATION_LABEL: Final[bytes] = b"LEADQUALI-FEEDBACK-KEY-v1"


class Verdict(StrEnum):
    """What a human thought of a routed lead.

    The three values mirror ``feedback.verdict``'s CHECK constraint in #15's schema
    (``verdict_known``). :attr:`UNSURE` exists so a rep who genuinely cannot tell has
    somewhere to put that: a forced good/bad choice on an ambiguous lead is noise in the
    golden set, and noise is worse than a smaller set.
    """

    GOOD = "good"
    BAD = "bad"
    UNSURE = "unsure"

    @property
    def label(self) -> str:
        """How this verdict is written to a human, on a button or a page."""
        return _VERDICT_LABELS[self]

    @property
    def opposite(self) -> Verdict:
        """The verdict a rep changing their mind would want.

        :attr:`UNSURE` is its own opposite — there is no "the other one" for it — which is
        why the thank-you page offers a change link only for the two decisive verdicts.
        """
        return _VERDICT_OPPOSITES[self]


_VERDICT_LABELS: Final[dict[Verdict, str]] = {
    Verdict.GOOD: "good lead",
    Verdict.BAD: "bad lead",
    Verdict.UNSURE: "not sure",
}

_VERDICT_OPPOSITES: Final[dict[Verdict, Verdict]] = {
    Verdict.GOOD: Verdict.BAD,
    Verdict.BAD: Verdict.GOOD,
    Verdict.UNSURE: Verdict.UNSURE,
}


class UnknownLeadError(LookupError):
    """The token names a lead this tenant does not have.

    Raised by a :class:`~leadquali.app.ports.FeedbackStorePort` implementation rather than
    guessed at by the caller. It is a real, expected condition and not a bug: #37's
    retention job deletes leads, and the link in a rep's mailbox outlives the row. The
    endpoint turns it into a page that says so instead of a stack trace.
    """


# --------------------------------------------------------------------------- rater ids


def rater_id(destination: str) -> str:
    """The opaque subject id recorded in ``feedback.rater`` for a routing destination.

    #16 flags ``rater`` as an **opaque subject id, never an email address**, and #15's
    schema comment says why: feedback is retained for as long as it is useful to the
    rubric, which is longer than ``leads.raw_payload`` is kept, so an address here would
    put personal data outside the one place invariant 5 allows it to live.

    A v1 routing email goes to a shared sales destination and the person who clicks is not
    identified — so the honest subject is *the destination*, hashed. It is stable (the same
    inbox produces the same id every time, which is what makes a second click an update
    rather than a new row), it is not reversible from the database alone, and it carries no
    address. When #29 adds per-rep identities, this is the one function that changes.

    Raises:
        ValueError: the destination is blank. A rater id derived from nothing would collide
            across every tenant, and a feedback row is worthless without knowing whose it is.
    """
    normalised = destination.strip().lower()
    if not normalised:
        raise ValueError("destination must not be blank; it is what the rater id is derived from")
    digest = hashlib.sha256(f"destination:{normalised}".encode()).hexdigest()
    return f"dest:{digest[:32]}"


# ------------------------------------------------------------------------ the token


@dataclass(frozen=True, slots=True)
class FeedbackClaim:
    """What one feedback link authorises. Every field of it is signed.

    Immutable, because it is what the signature attests to: a claim that could be edited
    after verification would make the verification meaningless.
    """

    tenant_id: str
    lead_id: str
    verdict: Verdict
    rater: str
    expires_at: datetime

    def signing_string(self) -> str:
        """The canonical string the MAC is computed over.

        Seven newline-separated lines::

            LEADQUALI-FEEDBACK-HMAC-SHA256
            fb1
            <tenant id>
            <lead id>
            <verdict>
            <rater subject id>
            <expiry, unix seconds>

        Field-per-line with no separator that can appear in a field, so no combination of
        values can be re-cut into a different claim with the same string.
        """
        return "\n".join(
            (
                TOKEN_ALGORITHM,
                TOKEN_VERSION,
                self.tenant_id,
                self.lead_id,
                self.verdict.value,
                self.rater,
                str(int(self.expires_at.timestamp())),
            )
        )


class TokenFailure(StrEnum):
    """Why a token was refused.

    For the log and for choosing the page's wording — an expired link deserves "ask for a
    fresh email", a forged one deserves nothing but a flat refusal.
    """

    MALFORMED = "malformed"
    """Not a token at all: wrong shape, wrong version, undecodable, unknown verdict."""

    BAD_SIGNATURE = "bad_signature"
    """Well-formed and not signed by us. Tampered, forged, or minted with another key."""

    EXPIRED = "expired"
    """Signed by us, and past its expiry."""


@dataclass(frozen=True, slots=True)
class TokenAccepted:
    """The token verifies, and this is what it authorises."""

    claim: FeedbackClaim


@dataclass(frozen=True, slots=True)
class TokenRejected:
    """The token does not verify, and the visitor is told very little about why."""

    failure: TokenFailure


TokenResult = TokenAccepted | TokenRejected


def tenant_key(secret: bytes, tenant_id: str) -> bytes:
    """Derive one tenant's signing key from the process secret.

    A single configured secret with a per-tenant derived key, rather than a secret per
    tenant: one value for #26/#28 to provision and rotate, and still no way to move a token
    from one tenant to another. Standard HMAC-as-KDF, with a label so this key cannot
    collide with anything else derived from the same secret later.
    """
    return hmac.new(
        secret, _KEY_DERIVATION_LABEL + b"|" + tenant_id.encode("utf-8"), hashlib.sha256
    ).digest()


def mint_token(
    *,
    secret: bytes,
    tenant_id: str,
    lead_id: str,
    verdict: Verdict,
    rater: str,
    expires_at: datetime,
) -> str:
    """Build the token for one feedback link: ``fb1.<claim>.<mac>``.

    The claim travels base64url-encoded rather than hashed because the server has no
    database of outstanding tokens and must not need one: everything it needs to write the
    row is in the URL, which is what makes the endpoint a single statement and the link
    survive a deploy. The MAC is computed over the *encoded* claim exactly as it will be
    presented, so verification never has to re-canonicalise attacker-supplied text.

    Args:
        secret: the process feedback secret. Per-tenant derivation happens here.
        tenant_id: whose lead this is.
        lead_id: the lead the verdict is about.
        verdict: the one verdict this link can record. Mint one link per verdict.
        rater: the opaque subject id to file the feedback under — see :func:`rater_id`.
        expires_at: when the link stops working. Timezone-aware.

    Raises:
        ValueError: the secret is too short, or a field is blank, or ``expires_at`` is
            naive. Every one of these is a wiring mistake that would otherwise become a
            link nobody can use or a token signed with a placeholder.
    """
    check_token_secret(secret)
    if not tenant_id or not lead_id or not rater:
        raise ValueError("tenant_id, lead_id and rater must all be non-empty")
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware; a naive expiry is a guess")

    claim = FeedbackClaim(
        tenant_id=tenant_id,
        lead_id=lead_id,
        verdict=verdict,
        rater=rater,
        expires_at=expires_at,
    )
    encoded = _b64encode(claim.signing_string().encode("utf-8"))
    mac = _mac(secret=secret, tenant_id=tenant_id, encoded_claim=encoded)
    return f"{TOKEN_VERSION}.{encoded}.{mac}"


def verify_token(*, secret: bytes, token: str, now: datetime) -> TokenResult:
    """Check one token and return what it authorises, or why it was refused.

    Order matters: shape, then signature, then expiry. The signature is checked before the
    expiry so that an unsigned claim can never be *read* — an attacker who could learn
    "this lead id is real, the link just expired" from a forged token would have an oracle
    over lead ids.

    Args:
        secret: the process feedback secret.
        token: the path segment as it arrived. Attacker-controlled in full.
        now: current time, injected so the expiry window is testable without sleeping.
    """
    if not token or len(token) > MAX_TOKEN_CHARS:
        return TokenRejected(TokenFailure.MALFORMED)

    parts = token.split(".")
    if len(parts) != 3:
        return TokenRejected(TokenFailure.MALFORMED)
    version, encoded, presented = parts
    if version != TOKEN_VERSION or not encoded or not presented:
        return TokenRejected(TokenFailure.MALFORMED)

    try:
        raw = _b64decode(encoded).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return TokenRejected(TokenFailure.MALFORMED)

    lines = raw.split("\n")
    if len(lines) != 7:
        return TokenRejected(TokenFailure.MALFORMED)
    algorithm, claimed_version, tenant_id, lead_id, verdict_value, rater, expiry = lines
    if algorithm != TOKEN_ALGORITHM or claimed_version != TOKEN_VERSION:
        return TokenRejected(TokenFailure.MALFORMED)
    if not tenant_id or not lead_id or not rater or not expiry.isdigit():
        return TokenRejected(TokenFailure.MALFORMED)
    try:
        verdict = Verdict(verdict_value)
    except ValueError:
        return TokenRejected(TokenFailure.MALFORMED)

    # The claim is still unauthenticated here; the only thing taken from it so far is which
    # tenant's key to try, and a wrong guess simply fails the comparison below.
    expected = _mac(secret=secret, tenant_id=tenant_id, encoded_claim=encoded)
    if not hmac.compare_digest(expected, presented):
        return TokenRejected(TokenFailure.BAD_SIGNATURE)

    expires_at = datetime.fromtimestamp(int(expiry), tz=UTC)
    if now >= expires_at:
        return TokenRejected(TokenFailure.EXPIRED)

    return TokenAccepted(
        FeedbackClaim(
            tenant_id=tenant_id,
            lead_id=lead_id,
            verdict=verdict,
            rater=rater,
            expires_at=expires_at,
        )
    )


def feedback_url(*, base_url: str, token: str) -> str:
    """The absolute URL for one feedback link.

    ``base_url`` is configuration (``FEEDBACK_BASE_URL``) rather than something derived from
    an inbound request: the email is composed by a worker that has no request to derive a
    host from, and a link built from a ``Host`` header is a link an attacker can point at
    their own server. Any trailing slash on the configured value is normalised away so that
    ``https://x.example`` and ``https://x.example/`` produce the same link.

    Raises:
        ValueError: the base URL is blank or is not absolute. A relative link in an email
            is a link that does nothing.
    """
    trimmed = base_url.strip().rstrip("/")
    if not trimmed.startswith(("http://", "https://")):
        raise ValueError(
            f"the feedback base URL must be absolute, got {base_url!r}; "
            "an email link has no page to be relative to"
        )
    return f"{trimmed}{FEEDBACK_PATH_PREFIX}{token}"


def load_token_secret(raw: str) -> bytes:
    """Validate and encode the configured feedback secret.

    Raises:
        ValueError: shorter than :data:`MIN_TOKEN_SECRET_CHARS`. Checked at wiring time so
            a placeholder is found by a failed start-up rather than by a rep whose link
            stopped working after a rotation.
    """
    secret = raw.encode("utf-8")
    check_token_secret(secret)
    return secret


def check_token_secret(secret: bytes) -> None:
    """Raise unless ``secret`` is long enough to be a real one.

    Public so that anything holding an already-encoded secret — the SES adapter, the API's
    wiring — can fail at construction rather than at the first lead or the first click.

    Raises:
        ValueError: shorter than :data:`MIN_TOKEN_SECRET_CHARS`.
    """
    if len(secret) < MIN_TOKEN_SECRET_CHARS:
        raise ValueError(
            f"the feedback token secret must be at least {MIN_TOKEN_SECRET_CHARS} characters"
        )


def _mac(*, secret: bytes, tenant_id: str, encoded_claim: str) -> str:
    material = f"{TOKEN_VERSION}.{encoded_claim}".encode()
    digest = hmac.new(tenant_key(secret, tenant_id), material, hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(raw: bytes) -> str:
    """Unpadded base64url, so the token is one path segment with nothing to percent-encode."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


__all__ = [
    "DEFAULT_TOKEN_TTL_DAYS",
    "FEEDBACK_PATH_PREFIX",
    "MAX_NOTES_CHARS",
    "MAX_TOKEN_CHARS",
    "MIN_TOKEN_SECRET_CHARS",
    "TOKEN_ALGORITHM",
    "TOKEN_VERSION",
    "FeedbackClaim",
    "TokenAccepted",
    "TokenFailure",
    "TokenRejected",
    "TokenResult",
    "UnknownLeadError",
    "Verdict",
    "check_token_secret",
    "feedback_url",
    "load_token_secret",
    "mint_token",
    "rater_id",
    "tenant_key",
    "verify_token",
]
