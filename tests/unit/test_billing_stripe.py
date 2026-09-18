"""The Stripe adapter, driven against a fake client with the SDK's own call surface.

There is no Stripe API key in this environment and no Stripe CLI, so what can be asserted
here is exactly what matters anyway: **the parameters we put on the wire**. A wrong
``event_name`` is a meter event Stripe silently drops; a missing ``identifier`` is a
retried billing run charged twice; a ``value`` sent as an integer where Stripe's payload is
``Dict[str, str]`` is a 400 discovered on the first day of a month.

The fake's method signatures were copied from the installed SDK 15.6.1 — ``StripeClient.v1``
services taking ``(params, options=None)``, ``subscriptions.cancel`` taking the id first.
``tests/unit/test_billing_stripe_contract.py`` checks that claim against the real SDK so
this file cannot drift into testing a surface Stripe does not have.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import pytest

from leadquali.adapters.billing_stripe import (
    METER_CUSTOMER_KEY,
    METER_VALUE_KEY,
    StripeBilling,
    StripeBillingError,
)
from leadquali.app.billing import SubscriptionState, UsageReport

TENANT = "acme-demo"
CUSTOMER = "cus_acme"
PRICE = "price_leads_monthly"
METER = "leadquali_billable_leads"


class FakeResource:
    """A Stripe resource: attribute access over a dict, which is what the SDK returns."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class FakeService:
    """One Stripe service — ``customers``, ``subscriptions``, ``meter_events``, ``sessions``.

    Records every call and answers with a :class:`FakeResource`. ``raises`` makes the call
    fail the way a ``StripeError`` would.
    """

    def __init__(self, response: Any = None, *, raises: Exception | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[tuple[tuple[Any, ...], Mapping[str, Any] | None]] = []

    def _record(self, args: tuple[Any, ...], options: Mapping[str, Any] | None) -> Any:
        self.calls.append((args, options))
        if self.raises is not None:
            raise self.raises
        return self.response

    def create(self, params: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> Any:
        return self._record((params,), options)

    def cancel(
        self,
        subscription_exposed_id: str,
        params: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._record((subscription_exposed_id, params), options)

    def update(
        self,
        subscription_exposed_id: str,
        params: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._record((subscription_exposed_id, params), options)

    @property
    def params(self) -> Mapping[str, Any]:
        """The params of the single call made, for the common one-call assertion."""
        assert len(self.calls) == 1, f"expected one call, saw {len(self.calls)}"
        args, _ = self.calls[0]
        last = args[-1]
        assert isinstance(last, Mapping)
        return last

    @property
    def options(self) -> Mapping[str, Any] | None:
        assert len(self.calls) == 1
        return self.calls[0][1]


class FakeClient:
    """``stripe.StripeClient``, reduced to the four services this adapter touches."""

    def __init__(self, **services: FakeService) -> None:
        self.customers = services.get("customers", FakeService())
        self.subscriptions = services.get("subscriptions", FakeService())
        self.meter_events = services.get("meter_events", FakeService())
        self.sessions = services.get("sessions", FakeService())
        self.billing = FakeResource(meter_events=self.meter_events)
        self.billing_portal = FakeResource(sessions=self.sessions)
        # The SDK's non-deprecated namespace: StripeClient.customers still works in 15.6.1
        # but warns, and StripeClient.v1.customers is what it tells you to use instead.
        self.v1 = self

    @property
    def calls(self) -> int:
        return sum(
            len(service.calls)
            for service in (self.customers, self.subscriptions, self.meter_events, self.sessions)
        )


def adapter(client: FakeClient) -> StripeBilling:
    return StripeBilling(client=client, price_id=PRICE, meter_event_name=METER)  # type: ignore[arg-type]  # the fake mirrors the SDK's call surface; see the contract test


# ------------------------------------------------------------------------- customers


def test_creating_a_customer_sends_the_tenant_as_metadata_and_an_idempotency_key() -> None:
    """The metadata is how an operator in the Stripe dashboard knows which account a
    customer is; the idempotency key is what stops a retried onboarding from creating two
    customers, which would be two invoices for the same tenant."""
    customers = FakeService(FakeResource(id=CUSTOMER))
    client = FakeClient(customers=customers)

    result = adapter(client).create_customer(
        tenant_id=TENANT, name="Acme Ltd", email="ap@acme.example"
    )

    assert result.customer_id == CUSTOMER
    assert result.tenant_id == TENANT
    assert customers.params["name"] == "Acme Ltd"
    assert customers.params["email"] == "ap@acme.example"
    assert customers.params["metadata"] == {"tenant_id": TENANT}
    options = customers.options
    assert options is not None
    assert options["idempotency_key"] == f"leadquali-customer-{TENANT}"


def test_an_email_is_omitted_rather_than_sent_as_none() -> None:
    """Stripe reads ``email: null`` as "clear it". Omitting the key is the only way to say
    "we do not have one"."""
    customers = FakeService(FakeResource(id=CUSTOMER))
    adapter(FakeClient(customers=customers)).create_customer(tenant_id=TENANT, name="Acme")
    assert "email" not in customers.params


def test_a_response_with_no_id_is_an_error_not_a_silently_empty_customer() -> None:
    customers = FakeService(FakeResource())
    with pytest.raises(StripeBillingError):
        adapter(FakeClient(customers=customers)).create_customer(tenant_id=TENANT, name="Acme")


# ---------------------------------------------------------------------- subscriptions


def test_creating_a_subscription_sends_one_item_at_the_configured_price() -> None:
    subscriptions = FakeService(
        FakeResource(id="sub_1", status="active", customer=CUSTOMER, cancel_at_period_end=False)
    )
    result = adapter(FakeClient(subscriptions=subscriptions)).create_subscription(
        tenant_id=TENANT, customer_id=CUSTOMER, price_id=PRICE
    )

    assert result.subscription_id == "sub_1"
    assert result.state is SubscriptionState.ACTIVE
    assert subscriptions.params["customer"] == CUSTOMER
    assert subscriptions.params["items"] == [{"price": PRICE}]
    assert subscriptions.params["metadata"] == {"tenant_id": TENANT}


def test_a_subscription_status_we_do_not_know_is_an_error_not_a_guess() -> None:
    """Better to fail loudly at the one call site than to return a state the caller will
    act on. Stripe reserves the right to add statuses; acting on an unknown one is how a
    customer gets suspended by a vocabulary change."""
    subscriptions = FakeService(FakeResource(id="sub_1", status="brand_new", customer=CUSTOMER))
    with pytest.raises(StripeBillingError):
        adapter(FakeClient(subscriptions=subscriptions)).create_subscription(
            tenant_id=TENANT, customer_id=CUSTOMER, price_id=PRICE
        )


def test_cancelling_immediately_calls_cancel_with_the_id_first() -> None:
    subscriptions = FakeService(
        FakeResource(id="sub_1", status="canceled", customer=CUSTOMER, cancel_at_period_end=False)
    )
    result = adapter(FakeClient(subscriptions=subscriptions)).cancel_subscription(
        tenant_id=TENANT, subscription_id="sub_1"
    )
    args, _ = subscriptions.calls[0]
    assert args[0] == "sub_1"
    assert result.state is SubscriptionState.CANCELED


def test_cancelling_at_period_end_is_an_update_not_a_cancel() -> None:
    """A customer who has paid for the rest of the month keeps the rest of the month.
    ``cancel`` would end it today and Stripe would not send
    ``customer.subscription.deleted`` weeks later, which is when the tenant should stop."""
    subscriptions = FakeService(
        FakeResource(id="sub_1", status="active", customer=CUSTOMER, cancel_at_period_end=True)
    )
    client = FakeClient(subscriptions=subscriptions)
    result = adapter(client).cancel_subscription(
        tenant_id=TENANT, subscription_id="sub_1", at_period_end=True
    )
    args, _ = subscriptions.calls[0]
    assert args[0] == "sub_1"
    assert args[1] == {"cancel_at_period_end": True}
    assert result.cancel_at_period_end is True
    assert result.state is SubscriptionState.ACTIVE


# ----------------------------------------------------------------------- meter events


def test_usage_is_reported_as_a_meter_event_with_the_deterministic_identifier() -> None:
    """Every field here is load-bearing. ``event_name`` must match the meter configured in
    #34's runbook; the payload keys are the meter's ``customer_mapping.event_payload_key``
    and ``value_settings.event_payload_key`` defaults; ``identifier`` is what makes a
    retried billing run within Stripe's deduplication window harmless."""
    meter_events = FakeService(FakeResource(identifier="x"))
    client = FakeClient(meter_events=meter_events)
    report = UsageReport(tenant_id=TENANT, usage_date=date(2026, 9, 7), quantity=42)

    result = adapter(client).report_usage(tenant_id=TENANT, customer_id=CUSTOMER, report=report)

    params = meter_events.params
    assert params["event_name"] == METER
    assert params["identifier"] == report.external_id
    assert params["payload"] == {METER_CUSTOMER_KEY: CUSTOMER, METER_VALUE_KEY: "42"}
    assert result.quantity == 42
    assert result.external_id == report.external_id


def test_the_meter_payload_values_are_strings_because_stripe_types_them_that_way() -> None:
    """``MeterEventCreateParams.payload`` is ``Dict[str, str]`` in the installed SDK. An
    integer would be a 400 on the first day of a month, from a scheduled job nobody is
    watching."""
    meter_events = FakeService(FakeResource(identifier="x"))
    adapter(FakeClient(meter_events=meter_events)).report_usage(
        tenant_id=TENANT,
        customer_id=CUSTOMER,
        report=UsageReport(tenant_id=TENANT, usage_date=date(2026, 9, 7), quantity=7),
    )
    payload = meter_events.params["payload"]
    assert all(isinstance(value, str) for value in payload.values())


def test_the_meter_event_is_timestamped_at_the_end_of_the_day_it_bills() -> None:
    """Stripe aggregates meter events by their timestamp, so a day's usage has to land
    inside that day. The last second of the UTC day is used rather than midnight, because
    midnight is the *next* day's first instant and would move every day's usage forward
    one billing period at a month boundary."""
    meter_events = FakeService(FakeResource(identifier="x"))
    adapter(FakeClient(meter_events=meter_events)).report_usage(
        tenant_id=TENANT,
        customer_id=CUSTOMER,
        report=UsageReport(tenant_id=TENANT, usage_date=date(2026, 9, 30), quantity=1),
    )
    stamped = datetime.fromtimestamp(meter_events.params["timestamp"], tz=UTC)
    assert stamped.date() == date(2026, 9, 30)
    assert stamped.hour == 23 and stamped.minute == 59


def test_a_failed_meter_event_raises_rather_than_reporting_success() -> None:
    """The service records the day as reported only if this returns. A swallowed error
    would mark a day done that Stripe never received, and under-billing recorded as done
    is not recoverable."""
    meter_events = FakeService(raises=RuntimeError("stripe said no"))
    with pytest.raises(StripeBillingError):
        adapter(FakeClient(meter_events=meter_events)).report_usage(
            tenant_id=TENANT,
            customer_id=CUSTOMER,
            report=UsageReport(tenant_id=TENANT, usage_date=date(2026, 9, 7), quantity=1),
        )


# ------------------------------------------------------------------------ the portal


def test_a_portal_session_returns_the_url_and_nothing_else() -> None:
    sessions = FakeService(FakeResource(id="bps_1", url="https://billing.stripe.com/p/session/x"))
    url = adapter(FakeClient(sessions=sessions)).portal_session_url(
        tenant_id=TENANT, customer_id=CUSTOMER, return_url="https://acme.example/billing"
    )
    assert url == "https://billing.stripe.com/p/session/x"
    assert sessions.params == {
        "customer": CUSTOMER,
        "return_url": "https://acme.example/billing",
    }


def test_a_portal_session_with_no_url_is_an_error() -> None:
    sessions = FakeService(FakeResource(id="bps_1"))
    with pytest.raises(StripeBillingError):
        adapter(FakeClient(sessions=sessions)).portal_session_url(
            tenant_id=TENANT, customer_id=CUSTOMER, return_url="https://acme.example/billing"
        )


# --------------------------------------------------------------------------- hygiene


def test_no_stripe_identifier_is_logged_in_an_error_message() -> None:
    """An adapter error goes into ``stripe_events.last_error`` and into CloudWatch. A
    customer id in there is a billing identifier in a log, which is the same class of
    mistake as an email address (invariant 5)."""
    meter_events = FakeService(raises=RuntimeError("boom"))
    with pytest.raises(StripeBillingError) as caught:
        adapter(FakeClient(meter_events=meter_events)).report_usage(
            tenant_id=TENANT,
            customer_id=CUSTOMER,
            report=UsageReport(tenant_id=TENANT, usage_date=date(2026, 9, 7), quantity=1),
        )
    assert CUSTOMER not in str(caught.value)
    assert TENANT in str(caught.value)


def test_the_adapter_satisfies_the_port() -> None:
    from leadquali.app.billing import BillingPort

    assert isinstance(adapter(FakeClient()), BillingPort)
