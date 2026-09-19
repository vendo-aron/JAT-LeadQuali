"""Verifying that a webhook really came from Stripe — standard library only.

``POST /webhooks/stripe`` is the second public, internet-facing surface in this system
(``POST /leads`` is the first), and it is the one that changes *billing state*. Everything
that arrives on it is attacker-controlled until an HMAC says otherwise, so the rules are
the same as :mod:`leadquali.api.signing`'s and so is the shape of the code.

Three reasons this is written here rather than delegated to ``stripe.Webhook``:

1. **The signature covers the raw request bytes, and must be checked before parsing.**
   The SDK's ``construct_event`` verifies *and parses* in one call and hands back a parsed
   ``Event``. Used carelessly that invites exactly the trap ``api/signing.py`` documents —
   verifying one representation of a body and then acting on another. Here the bytes that
   were verified are the bytes that get stored, and parsing happens afterwards, once, on
   material that has already proved it came from Stripe.
2. **Nothing third-party is on the rejection path.** A malformed request from a stranger
   costs a length check, a header split and at most a few HMACs. The webhook Lambda
   imports no SDK to say "no", which bounds both the cost and the attack surface of the
   cheapest thing anyone can do to this endpoint.
3. **It is testable with fixtures and no network** — see ``tests/unit/test_stripe_signing.py``,
   which builds every header it asserts on by signing a known body with a known secret.

The construction, read out of the installed SDK (``stripe/_webhook.py``, 15.6.1) rather
than from documentation:

* ``Stripe-Signature`` is a comma-separated list of ``k=v`` pairs. One pair is ``t``, a
  unix timestamp in seconds. One or more are ``v1``, hex HMAC-SHA256 signatures.
* The signed payload is ``f"{t}.{body}"``.
* The key is the endpoint's ``whsec_`` secret, used as raw UTF-8 bytes.
* A request is valid if **any** ``v1`` value matches, compared with
  :func:`hmac.compare_digest`.
* A timestamp outside the tolerance is refused. Stripe's default is 300 seconds.

Two deliberate differences from the SDK, both of which make this stricter:

* **Bytes, never text.** The SDK decodes the body to ``str`` and re-encodes it to compute
  the MAC. That round-trip is lossless for valid UTF-8 and raises ``UnicodeDecodeError``
  for anything else — on the rejection path of a public endpoint. Here the body stays
  ``bytes`` from the socket to :func:`hmac.compare_digest`, so a body that is not valid
  UTF-8 is simply a signature that does not match.
* **The tolerance is two-sided.** The SDK only rejects timestamps in the past. A request
  dated a year ahead would pass its check forever. We reject both directions, exactly as
  :data:`leadquali.api.signing.MAX_CLOCK_SKEW_SECONDS` does.

**Multiple ``v1`` values exist so that a signing secret can be rotated without downtime.**
While two endpoint secrets are live Stripe signs with both and sends both, so an endpoint
that only inspected the first value would reject every webhook for the length of the
overlap — which is a billing outage that looks like Stripe being broken. Every value is
tried; the first match wins.

**Order of checks.** Shape, then the HMAC, then the clock. The clock check comes *last* on
purpose: "your timestamp is stale" is information only someone who holds the secret has
earned. Checking it first would let a stranger tell a well-formed forgery from a
badly-formed one, and the caller is told none of this anyway — every failure is the same
400 (see :mod:`leadquali.api.webhooks`).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

#: The header Stripe sends. Matched case-insensitively by the route, like every header.
STRIPE_SIGNATURE_HEADER: Final[str] = "Stripe-Signature"

#: The signature scheme we accept. ``v0`` exists and is not a signature over the body —
#: the Stripe CLI emits it — so it is ignored rather than trusted.
SIGNATURE_SCHEME: Final[str] = "v1"

#: The key carrying the unix timestamp inside the header.
TIMESTAMP_KEY: Final[str] = "t"

#: How far a webhook's timestamp may sit from ours, in either direction. Stripe's own
#: default, so an endpoint that is stricter would reject deliveries Stripe considers fine.
DEFAULT_TOLERANCE_SECONDS: Final[int] = 300

#: Longest header we will look at. A rotation sends two signatures and a very cautious
#: future might send a handful; a thousand is somebody making us compute HMACs for free.
#: The limit is on the header rather than on the number of pairs so the cost of *finding*
#: the pairs is bounded too.
MAX_SIGNATURE_HEADER_CHARS: Final[int] = 4096

#: Endpoint signing secrets start with this. Checked at load because the failure it
#: catches — the *API key* pasted into the webhook secret setting — produces a deployment
#: that rejects every real webhook while Stripe retries for three days.
WEBHOOK_SECRET_PREFIX: Final[str] = "whsec_"  # noqa: S105  # a prefix, not a secret

#: Shortest secret a deployment may configure, prefix included. Stripe's are much longer;
#: this is a placeholder check, not a strength estimate.
MIN_WEBHOOK_SECRET_CHARS: Final[int] = len(WEBHOOK_SECRET_PREFIX) + 24

_HEX_64_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")

#: A unix timestamp in seconds, bounded so that a header cannot ask us to build an
#: arbitrarily large integer before we have authenticated anything.
_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9]{1,12}\Z")

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "MAX_SIGNATURE_HEADER_CHARS",
    "MIN_WEBHOOK_SECRET_CHARS",
    "SIGNATURE_SCHEME",
    "STRIPE_SIGNATURE_HEADER",
    "TIMESTAMP_KEY",
    "WEBHOOK_SECRET_PREFIX",
    "SignatureFailure",
    "SignatureRejected",
    "SignatureResult",
    "StripeWebhookSecretError",
    "VerifiedSignature",
    "compute_signature",
    "load_webhook_secret",
    "signature_header",
    "signed_payload",
    "verify_signature",
]


class StripeWebhookSecretError(ValueError):
    """The configured webhook signing secret is unusable.

    Raised at load time. A process that cannot authenticate webhooks must fail to start
    rather than start and refuse every delivery — and, far worse than either, must never
    have a code path where "no secret configured" means "accept anything".
    """


class SignatureFailure(StrEnum):
    """Why a webhook was refused. For the log and the metric; never for the caller.

    Every value produces the identical 400 with the identical body. The distinction
    matters to an operator ("Stripe is signing with a secret we do not hold" is a very
    different morning from "somebody is posting junk at us") and to nobody else.
    """

    MISSING_HEADER = "missing_header"
    """No ``Stripe-Signature`` at all. Costs nothing."""

    MALFORMED_HEADER = "malformed_header"
    """The header is present and is not the shape Stripe sends — no timestamp, more than
    one timestamp, a non-numeric one, or more of it than we are willing to parse."""

    NO_SIGNATURES = "no_signatures"
    """Well-formed, with a timestamp, and carrying no ``v1`` value we could check."""

    BAD_SIGNATURE = "bad_signature"
    """No ``v1`` value matched. Either a forgery or a secret mismatch."""

    STALE = "stale"
    """The signature is genuine and the timestamp is outside the tolerance in either
    direction. Only ever reported for a request that verified."""


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    """The body really was signed with our endpoint secret, at ``timestamp``.

    Carries no parsed event: parsing is the caller's job and happens after this, on the
    same bytes that were verified.
    """

    timestamp: int


@dataclass(frozen=True, slots=True)
class SignatureRejected:
    """The body was not accepted, and why — for us, not for the sender."""

    failure: SignatureFailure


SignatureResult = VerifiedSignature | SignatureRejected
"""What :func:`verify_signature` answers with. Never a bare ``bool``: the reason is the
only thing that distinguishes a rotation we botched from a stranger probing the endpoint,
and collapsing it loses that before it reaches a log line."""


def load_webhook_secret(raw: str) -> str:
    """Validate a configured ``whsec_`` endpoint secret and return it stripped.

    Args:
        raw: the secret as configured, possibly with surrounding whitespace (a value
            pasted out of the Stripe dashboard usually has some).

    Returns:
        The secret, whitespace removed.

    Raises:
        StripeWebhookSecretError: it is empty, too short, or not a webhook signing secret.
    """
    secret = raw.strip()
    if not secret:
        raise StripeWebhookSecretError(
            "the Stripe webhook signing secret is empty; set STRIPE_WEBHOOK_SECRET (or "
            "STRIPE_WEBHOOK_SECRET_SECRET_ARN) to the endpoint's whsec_ value"
        )
    if not secret.startswith(WEBHOOK_SECRET_PREFIX):
        raise StripeWebhookSecretError(
            f"the Stripe webhook signing secret must start with '{WEBHOOK_SECRET_PREFIX}'; "
            "the value configured is something else — most likely an API key"
        )
    if len(secret) < MIN_WEBHOOK_SECRET_CHARS:
        raise StripeWebhookSecretError(
            f"the Stripe webhook signing secret is only {len(secret)} characters; a real "
            f"one is at least {MIN_WEBHOOK_SECRET_CHARS}"
        )
    return secret


def signed_payload(*, timestamp: int, body: bytes) -> bytes:
    """The exact bytes a Stripe webhook signature is computed over: ``t`` + ``.`` + body.

    Kept as its own function so the construction is written down once and can be read —
    and reimplemented — without reading the verifier around it. Bytes in, bytes out: the
    body is never decoded.
    """
    return f"{timestamp}.".encode("ascii") + body


def compute_signature(*, secret: str, timestamp: int, body: bytes) -> str:
    """The lowercase hex HMAC-SHA256 Stripe would send for this body at this time."""
    return hmac.new(
        secret.encode("utf-8"), signed_payload(timestamp=timestamp, body=body), hashlib.sha256
    ).hexdigest()


def signature_header(*, secret: str, body: bytes, timestamp: int) -> str:
    """Build a ``Stripe-Signature`` header for these inputs.

    The inverse of :func:`verify_signature`, and it exists in the shipping module rather
    than in a test helper for two reasons. It documents the header's shape next to the
    code that parses it, so the two cannot drift; and it lets the test suite build every
    fixture it needs offline, which is what makes this whole path verifiable in an
    environment with no Stripe key and no Stripe CLI.
    """
    signature = compute_signature(secret=secret, timestamp=timestamp, body=body)
    return f"{TIMESTAMP_KEY}={timestamp},{SIGNATURE_SCHEME}={signature}"


def verify_signature(
    *,
    body: bytes,
    header: str | None,
    secret: str,
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> SignatureResult:
    """Decide whether ``body`` was signed by Stripe with our endpoint secret.

    Args:
        body: the **raw** request bytes, exactly as they arrived and before any parsing.
            Re-serialising parsed JSON here would break every signature the first time a
            key order or a number's repr differed.
        header: the ``Stripe-Signature`` value, or ``None`` if the request had none.
        secret: the endpoint's ``whsec_`` secret, already validated by
            :func:`load_webhook_secret`.
        now: current time, injected so the tolerance is testable without sleeping.
        tolerance_seconds: how far the timestamp may sit from ``now``, either way.

    Returns:
        :class:`VerifiedSignature` with the signed timestamp, or :class:`SignatureRejected`
        with the reason — which belongs in a log line and never in a response.
    """
    if not header:
        return SignatureRejected(SignatureFailure.MISSING_HEADER)
    if len(header) > MAX_SIGNATURE_HEADER_CHARS:
        return SignatureRejected(SignatureFailure.MALFORMED_HEADER)

    parsed = _parse_header(header)
    if isinstance(parsed, SignatureFailure):
        return SignatureRejected(parsed)
    timestamp, signatures = parsed

    expected = compute_signature(secret=secret, timestamp=timestamp, body=body)
    # Every candidate is tried, not just the first: during a secret rotation Stripe signs
    # with both the old and the new endpoint secret and sends both values, and an endpoint
    # that read only one of them would reject every webhook until the rotation finished.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        return SignatureRejected(SignatureFailure.BAD_SIGNATURE)

    # Deliberately after the HMAC: see the module docstring. A stranger learns nothing
    # about our clock, and only a genuine signature can ever be called stale.
    if abs(now.timestamp() - timestamp) > tolerance_seconds:
        return SignatureRejected(SignatureFailure.STALE)

    return VerifiedSignature(timestamp=timestamp)


def _parse_header(header: str) -> tuple[int, tuple[str, ...]] | SignatureFailure:
    """Split a ``Stripe-Signature`` into its timestamp and its candidate signatures.

    Unknown keys are skipped rather than refused — ``v0`` is already one of them and the
    scheme list is Stripe's to extend — but the parts we *do* read are checked strictly:
    exactly one timestamp, numeric and bounded, and signatures that are 64 hex characters
    before an HMAC is computed over anything.
    """
    timestamp_text: str | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, separator, value = item.strip().partition("=")
        if not separator:
            continue
        if key == TIMESTAMP_KEY:
            if timestamp_text is not None:
                # Two timestamps is not a header Stripe sends. Picking one would be
                # picking which of two claims to authenticate against.
                return SignatureFailure.MALFORMED_HEADER
            timestamp_text = value
        elif key == SIGNATURE_SCHEME:
            lowered = value.lower()
            if _HEX_64_RE.match(lowered):
                signatures.append(lowered)

    if timestamp_text is None or not _TIMESTAMP_RE.match(timestamp_text):
        return SignatureFailure.MALFORMED_HEADER
    if not signatures:
        return SignatureFailure.NO_SIGNATURES
    return int(timestamp_text), tuple(signatures)
