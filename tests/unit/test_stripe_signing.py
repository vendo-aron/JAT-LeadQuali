"""Stripe's webhook signature, verified without Stripe's SDK and without a network.

Every property asserted here is one a mutation could break silently and expensively: a
verifier that accepts an unsigned request, one that stops checking the timestamp, one
that compares with ``==``, or one that only looks at the first ``v1`` value and so breaks
every secret rotation. None of them needs a database, so none of them is allowed to live
in a Docker-gated test — see ``docs/billing-integration.md``.

The fixtures are built here, in the test, by signing a known body with a known secret.
That is the only honest way to do it offline: a captured real header would be a signature
over a body we would then have to store byte-exactly forever, and the secret behind it
would have to be in the repository.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from leadquali.api.stripe_signing import (
    DEFAULT_TOLERANCE_SECONDS,
    MAX_SIGNATURE_HEADER_CHARS,
    SIGNATURE_SCHEME,
    STRIPE_SIGNATURE_HEADER,
    WEBHOOK_SECRET_PREFIX,
    SignatureFailure,
    SignatureRejected,
    StripeWebhookSecretError,
    VerifiedSignature,
    compute_signature,
    load_webhook_secret,
    signature_header,
    signed_payload,
    verify_signature,
)

SECRET = "whsec_" + "k" * 40
OTHER_SECRET = "whsec_" + "z" * 40
BODY = b'{"id":"evt_1","type":"invoice.payment_failed","data":{"object":{"id":"in_1"}}}'
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
TIMESTAMP = int(NOW.timestamp())


def header_for(*, secret: str = SECRET, body: bytes = BODY, timestamp: int = TIMESTAMP) -> str:
    """A well-formed ``Stripe-Signature`` for these inputs."""
    return signature_header(secret=secret, body=body, timestamp=timestamp)


def verify(header: str | None, *, body: bytes = BODY, now: datetime = NOW) -> object:
    return verify_signature(body=body, header=header, secret=SECRET, now=now)


# ------------------------------------------------------------------ the construction


def test_the_signed_payload_is_the_timestamp_a_dot_and_the_raw_bytes() -> None:
    """Stripe's construction, restated so a change to it fails here and not in production.

    Bytes throughout: the body is never decoded, so a webhook whose body is not valid
    UTF-8 is a signature check that fails, not a ``UnicodeDecodeError`` on the rejection
    path of a public endpoint.
    """
    assert signed_payload(timestamp=1757332800, body=b'{"a":1}') == b'1757332800.{"a":1}'
    assert signed_payload(timestamp=1757332800, body=b"\xff") == b"1757332800.\xff"


def test_the_signature_is_hex_hmac_sha256_over_that_payload() -> None:
    """Computed independently here, so the module cannot define its own truth."""
    expected = hmac.new(
        SECRET.encode("utf-8"), f"{TIMESTAMP}.".encode() + BODY, hashlib.sha256
    ).hexdigest()
    assert compute_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY) == expected
    assert len(expected) == 64


def test_the_header_we_build_is_the_shape_stripe_sends() -> None:
    header = header_for()
    assert header.startswith(f"t={TIMESTAMP},")
    assert f"{SIGNATURE_SCHEME}=" in header


# ----------------------------------------------------------------------- acceptance


def test_a_correctly_signed_request_verifies() -> None:
    result = verify(header_for())
    assert isinstance(result, VerifiedSignature)
    assert result.timestamp == TIMESTAMP


def test_any_of_several_v1_values_may_match_so_a_secret_can_rotate() -> None:
    """The whole reason Stripe sends more than one: during a rotation the endpoint holds
    two secrets and signs with both, and a verifier that only read the first would reject
    every webhook for the length of the overlap."""
    old = compute_signature(secret=OTHER_SECRET, timestamp=TIMESTAMP, body=BODY)
    new = compute_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY)
    assert isinstance(verify(f"t={TIMESTAMP},v1={old},v1={new}"), VerifiedSignature)
    assert isinstance(verify(f"t={TIMESTAMP},v1={new},v1={old}"), VerifiedSignature)


def test_unknown_pairs_are_ignored_rather_than_fatal() -> None:
    """Stripe's CLI adds ``v0`` and the scheme list is theirs to extend. A verifier that
    refused anything it did not recognise would break on their next addition."""
    signature = compute_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY)
    header = f"t={TIMESTAMP},v0=deadbeef,v1={signature},somethingnew=1"
    assert isinstance(verify(header), VerifiedSignature)


def test_the_signature_comparison_is_case_insensitive_on_the_hex() -> None:
    signature = compute_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY).upper()
    assert isinstance(verify(f"t={TIMESTAMP},v1={signature}"), VerifiedSignature)


# ------------------------------------------------------------------------ rejection


def test_a_missing_header_is_rejected() -> None:
    result = verify(None)
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.MISSING_HEADER


def test_an_empty_header_is_rejected() -> None:
    assert isinstance(verify(""), SignatureRejected)


@pytest.mark.parametrize(
    "header",
    [
        "junk",
        "t=,v1=abc",
        "t=notanumber,v1=abc",
        "v1=abc",
        f"t={TIMESTAMP}",
        f"t={TIMESTAMP},v1=",
        f"t={TIMESTAMP},v1=nothex",
        f"t={TIMESTAMP},t={TIMESTAMP},v1=abc",
        "=,=,=",
    ],
    ids=[
        "no pairs at all",
        "empty timestamp",
        "non-numeric timestamp",
        "no timestamp",
        "no signature",
        "empty signature",
        "signature is not hex",
        "two timestamps",
        "junk pairs",
    ],
)
def test_a_malformed_header_is_rejected_without_computing_anything(header: str) -> None:
    result = verify(header)
    assert isinstance(result, SignatureRejected)
    assert result.failure in {
        SignatureFailure.MALFORMED_HEADER,
        SignatureFailure.NO_SIGNATURES,
    }


def test_an_absurdly_long_header_is_refused_before_it_is_parsed() -> None:
    """A header is attacker-controlled and free to send. Ten thousand ``v1`` values would
    otherwise be ten thousand HMACs per request, on an unauthenticated endpoint."""
    signature = compute_signature(secret=SECRET, timestamp=TIMESTAMP, body=BODY)
    flood = ",".join([f"v1={signature}"] * 500)
    header = f"t={TIMESTAMP},{flood}"
    assert len(header) > MAX_SIGNATURE_HEADER_CHARS
    result = verify(header)
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.MALFORMED_HEADER


def test_a_body_that_differs_by_one_byte_is_rejected() -> None:
    header = header_for()
    tampered = BODY.replace(b'"in_1"', b'"in_2"')
    assert tampered != BODY and len(tampered) == len(BODY)
    result = verify(header, body=tampered)
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_a_signature_made_with_another_secret_is_rejected() -> None:
    result = verify(header_for(secret=OTHER_SECRET))
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_the_timestamp_is_part_of_the_signature_so_it_cannot_be_moved() -> None:
    """Swapping a fresh ``t`` onto an old signature must not turn a stale capture into a
    live request."""
    old = TIMESTAMP - 10_000
    signature = compute_signature(secret=SECRET, timestamp=old, body=BODY)
    result = verify(f"t={TIMESTAMP},v1={signature}")
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_a_stale_timestamp_is_rejected_even_with_a_valid_signature() -> None:
    stale = NOW - timedelta(seconds=DEFAULT_TOLERANCE_SECONDS + 1)
    result = verify(header_for(timestamp=int(stale.timestamp())))
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.STALE


def test_a_timestamp_far_in_the_future_is_rejected_too() -> None:
    """Stripe's own SDK only checks the past. Ours checks both directions, for the same
    reason ``api/signing.py`` does: a clock that is wrong in one direction is as much a
    bug as one that is wrong in the other, and a request dated next year would otherwise
    stay replayable forever."""
    ahead = NOW + timedelta(seconds=DEFAULT_TOLERANCE_SECONDS + 1)
    result = verify(header_for(timestamp=int(ahead.timestamp())))
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.STALE


def test_a_timestamp_at_the_edge_of_the_window_is_accepted() -> None:
    edge = NOW - timedelta(seconds=DEFAULT_TOLERANCE_SECONDS)
    assert isinstance(verify(header_for(timestamp=int(edge.timestamp()))), VerifiedSignature)


def test_the_stale_check_happens_after_the_signature_check() -> None:
    """An unsigned request costs one HMAC and no clock read; a *stale* answer is only
    given to someone who could sign. Otherwise the response distinguishes "you hold the
    secret" from "you do not", which is the one thing the endpoint must not say."""
    stale = int((NOW - timedelta(days=2)).timestamp())
    result = verify(f"t={stale},v1={'a' * 64}")
    assert isinstance(result, SignatureRejected)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


# --------------------------------------------------------------------- the secret


def test_a_usable_webhook_secret_round_trips() -> None:
    assert load_webhook_secret(f"  {SECRET}  ") == SECRET


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "sk_test_abc123", WEBHOOK_SECRET_PREFIX, "whsec_short"],
    ids=["empty", "blank", "an api key by mistake", "prefix only", "too short"],
)
def test_an_unusable_webhook_secret_is_refused_at_load(raw: str) -> None:
    """At load, not at request time: a deployment whose webhook secret is the *API key*
    pasted into the wrong box must fail to start, not reject every real webhook while
    Stripe retries for three days."""
    with pytest.raises(StripeWebhookSecretError):
        load_webhook_secret(raw)


def test_the_header_name_is_the_one_stripe_sends() -> None:
    assert STRIPE_SIGNATURE_HEADER.lower() == "stripe-signature"
