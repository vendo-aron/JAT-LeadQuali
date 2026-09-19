"""Our idea of Stripe's wire, checked against the installed SDK. No key, no network.

``tests/unit/test_billing_stripe.py`` proves the adapter's logic against a fake client.
What it cannot prove is that the fake resembles the real thing: a fake is a statement about
the SDK made by the person who wrote the fake, and a wrong one is a green suite and a
``TypeError`` on the first real call.

So this file asks the installed ``stripe`` 15.6.1 directly — for the services we call, for
the parameter names we send, for the subscription status vocabulary we branch on, and for
the webhook signature construction we reimplemented in the standard library. Every one of
these is an assertion that would fail on an SDK upgrade that moved something, which is
exactly when somebody should be made to look.

It lives in ``tests/contract`` for the same reason the Anthropic one does: it is not a unit
test of our code, it is a test of the boundary. It needs no credentials and runs in the
default suite.
"""

from __future__ import annotations

import inspect
import sys
import typing
from datetime import UTC, datetime

import pytest
import stripe
from stripe.params.billing._meter_event_create_params import MeterEventCreateParams

from leadquali.api.stripe_signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_SCHEME,
    compute_signature,
    signature_header,
    verify_signature,
)
from leadquali.app.billing import (
    METER_EVENT_MAX_AGE_DAYS,
    STRIPE_IDENTIFIER_DEDUPE_NOTE,
    SubscriptionState,
)

SECRET = "whsec_" + "q" * 40
BODY = b'{"id":"evt_1","type":"invoice.paid","data":{"object":{"customer":"cus_1"}}}'


@pytest.fixture(scope="module")
def client() -> stripe.StripeClient:
    """A client with a syntactically valid test key. Nothing here makes a request."""
    return stripe.StripeClient("sk_test_contract")


# ---------------------------------------------------------- the services we call


def test_the_v1_namespace_carries_every_service_the_adapter_uses(
    client: stripe.StripeClient,
) -> None:
    """``StripeClient.customers`` still resolves in 15.6.1 and emits a DeprecationWarning
    on every access, which would put a deprecation notice in the log of every billing run.
    ``StripeClient.v1.customers`` is what the SDK tells you to use, and it is what
    ``StripeClientPort`` describes."""
    assert callable(client.v1.customers.create)
    assert callable(client.v1.subscriptions.create)
    assert callable(client.v1.subscriptions.cancel)
    assert callable(client.v1.subscriptions.update)
    assert callable(client.v1.billing.meter_events.create)
    assert callable(client.v1.billing_portal.sessions.create)


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("customers.create", ["params", "options"]),
        ("subscriptions.create", ["params", "options"]),
        ("subscriptions.cancel", ["subscription_exposed_id", "params", "options"]),
        ("subscriptions.update", ["subscription_exposed_id", "params", "options"]),
        ("billing.meter_events.create", ["params", "options"]),
        ("billing_portal.sessions.create", ["params", "options"]),
    ],
)
def test_each_service_takes_the_arguments_our_protocol_declares(
    client: stripe.StripeClient, method: str, expected: list[str]
) -> None:
    """The adapter passes the subscription id positionally to ``cancel`` and ``update``
    and the params first everywhere else. If Stripe reorders these, the fake in the unit
    tests would keep passing while the real call raised."""
    target: object = client.v1
    for part in method.split("."):
        target = getattr(target, part)
    parameters = list(inspect.signature(target).parameters)  # type: ignore[arg-type]  # a bound method
    assert parameters == expected


# -------------------------------------------------------- meter events, not usage records


def test_meter_events_are_the_metered_billing_path_in_this_sdk() -> None:
    """The decision #48 left open, settled by reading the library rather than the docs."""
    assert hasattr(stripe.billing, "MeterEvent")
    assert stripe.billing.MeterEvent.OBJECT_NAME == "billing.meter_event"


def test_the_older_subscription_item_usage_record_api_is_gone() -> None:
    """The reason this is a one-file decision, stated as a test.

    In 15.6.1 there is no ``stripe.UsageRecord`` and no
    ``SubscriptionItem.create_usage_record``: Stripe removed the older metered-billing
    path, so meter events are not a preference, they are the only option. The day a
    successor appears this test is where somebody finds out.
    """
    assert not hasattr(stripe, "UsageRecord")
    assert not hasattr(stripe.SubscriptionItem, "create_usage_record")
    assert not hasattr(stripe.SubscriptionItem, "create_usage_record_async")


def test_the_meter_event_parameters_are_the_ones_the_adapter_sends() -> None:
    # ``__annotations__`` rather than ``get_type_hints``: the TypedDict inherits from
    # ``RequestOptions``, whose forward references do not resolve outside the SDK's own
    # module namespace. The raw annotations are what we need anyway — the names, and the
    # spelling of the payload's value type.
    fields = set(MeterEventCreateParams.__annotations__)
    assert {"event_name", "identifier", "payload", "timestamp"} <= fields


def test_the_meter_event_payload_is_typed_as_strings() -> None:
    """``Dict[str, str]``. Sending the quantity as an ``int`` would be a 400 from a
    scheduled job on the first day of a month, which is the worst possible place to
    discover it."""
    payload = str(MeterEventCreateParams.__annotations__["payload"])
    assert "Dict[str, str]" in payload
    assert "int" not in payload


def test_the_documented_backfill_window_is_the_constant_we_enforce() -> None:
    """``timestamp``: "Must be within the past 35 calendar days or up to 5 minutes in the
    future." The service refuses an older day rather than sending one Stripe will reject.

    Read out of the SDK's own source, because Python keeps a TypedDict field's docstring
    nowhere else — and a number this important should be traceable to where it came from
    rather than to somebody's memory of a documentation page.
    """
    source = inspect.getsource(sys.modules[MeterEventCreateParams.__module__])
    assert f"within the past {METER_EVENT_MAX_AGE_DAYS} calendar days" in source


def test_stripes_own_identifier_deduplication_is_only_a_rolling_window() -> None:
    """The reason ``usage_reports`` exists as well. Stripe's guarantee is "within a rolling
    period of at least 24 hours", aimed at "accidental retries" — it is a backstop, not a
    ledger, and a day re-reported a week later would be billed twice."""
    source = inspect.getsource(sys.modules[MeterEventCreateParams.__module__])
    assert "rolling period of at least 24 hours" in source
    assert "24 hours" in STRIPE_IDENTIFIER_DEDUPE_NOTE


# ----------------------------------------------------------------- the vocabulary


def test_our_subscription_states_are_exactly_the_sdks() -> None:
    """Branching on a status Stripe does not have would be dead code; missing one it does
    have would be a customer left in a state nothing handles."""
    annotation = stripe.Subscription.__annotations__["status"]
    literals: set[str] = set()
    for argument in typing.get_args(annotation):
        literals.update(str(value) for value in typing.get_args(argument))
    assert literals == {state.value for state in SubscriptionState}


def test_a_subscription_no_longer_carries_current_period_end() -> None:
    """Worth pinning because it is a trap: older Stripe integrations read
    ``subscription.current_period_end`` to decide when service should stop. In this API
    version the field has moved onto the subscription's items, so anything that reached for
    it would silently get ``None``. Nothing in this system uses it — the grace period is
    ours and is measured from the failed invoice — and this test says that on purpose.
    """
    assert "current_period_end" not in stripe.Subscription.__annotations__


# ------------------------------------------------------- the signature construction


def test_our_signature_is_byte_for_byte_the_sdks() -> None:
    """The load-bearing one. ``api/stripe_signing.py`` reimplements Stripe's construction
    in the standard library so that no SDK code is on the rejection path of a public
    endpoint. This is what proves the reimplementation is the same function."""
    timestamp = 1_788_000_000
    theirs = stripe.WebhookSignature._compute_signature(f"{timestamp}.{BODY.decode()}", SECRET)
    assert compute_signature(secret=SECRET, timestamp=timestamp, body=BODY) == theirs


def test_a_header_the_sdk_generates_verifies_here() -> None:
    """From their end to ours: the SDK's own test-helper builds the header, we accept it."""
    now = datetime.now(UTC)
    header = stripe.WebhookSignature.generate_signature_header(
        BODY.decode(), SECRET, timestamp=int(now.timestamp())
    )
    result = verify_signature(body=BODY, header=header, secret=SECRET, now=now)
    assert result.__class__.__name__ == "VerifiedSignature"


def test_a_header_we_generate_verifies_in_the_sdk() -> None:
    """And from our end to theirs, which is what makes the test fixtures honest."""
    timestamp = int(datetime.now(UTC).timestamp())
    header = signature_header(secret=SECRET, body=BODY, timestamp=timestamp)
    assert stripe.WebhookSignature.verify_header(BODY, header, SECRET, 300) is True


def test_we_use_the_sdks_scheme_and_tolerance() -> None:
    assert SIGNATURE_SCHEME == stripe.WebhookSignature.EXPECTED_SCHEME
    assert DEFAULT_TOLERANCE_SECONDS == stripe.Webhook.DEFAULT_TOLERANCE


def test_the_sdk_only_checks_the_past_which_is_why_ours_checks_both() -> None:
    """Documents the one place we are deliberately stricter than Stripe. Their check is
    ``timestamp < time.time() - tolerance``; a timestamp a year in the future passes it."""
    future = int(datetime.now(UTC).timestamp()) + 86_400
    header = signature_header(secret=SECRET, body=BODY, timestamp=future)
    assert stripe.WebhookSignature.verify_header(BODY, header, SECRET, 300) is True
    ours = verify_signature(body=BODY, header=header, secret=SECRET, now=datetime.now(UTC))
    assert ours.__class__.__name__ == "SignatureRejected"
