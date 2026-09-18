"""Stripe, behind :class:`~leadquali.app.billing.BillingPort`. The only file importing it.

``CLAUDE.md``: one file per external system, and the SDK lives in that file. Here the rule
earns its keep twice over. Stripe has already changed its metered-billing API once — usage
records on a subscription item, then meter events — and it will change something else;
keeping every Stripe type inside this module means that change is one file's diff rather
than a refactor of the billing service, the scheduler and the tests. And **no Stripe object
crosses the boundary**: every method here returns one of our own frozen dataclasses, so
nothing upstream can start depending on an attribute the SDK renames.

Meter events, not usage records
-------------------------------

The design brief left the choice open and said to read the installed SDK. In
``stripe`` 15.6.1 there is no choice to make: ``stripe.billing.MeterEvent`` and
``client.v1.billing.meter_events`` exist, and the older path is **gone** — there is no
``stripe.UsageRecord`` and no ``SubscriptionItem.create_usage_record``. Metered usage is
reported as a meter event, aggregated server-side by a meter configured in the dashboard
(#34's runbook), and this adapter is the one place that knows it.

Idempotency, and exactly how far Stripe's half of it goes
---------------------------------------------------------

``MeterEventCreateParams.identifier`` is the handle, and the SDK documents its own limit:
uniqueness is enforced "within a rolling period of at least 24 hours", to address "issues
arising from accidental retries". That makes it a backstop against a job that runs twice in
an hour — not a ledger. The durable guarantee is ``usage_reports`` in our own database,
unique on ``(tenant_id, usage_date)``. Both are used; see
:data:`~leadquali.app.billing.STRIPE_IDENTIFIER_DEDUPE_NOTE`.

Customer and subscription creation carry an ``idempotency_key`` in the request options,
derived from the tenant, so that a retried onboarding cannot produce two customers — which
would be two invoices for one account and is not something a unique constraint on our side
can prevent.

The client is a Protocol
------------------------

:class:`StripeClientPort` describes the handful of services this module calls, so the unit
tests can substitute a fake with the same surface and so mypy checks the call sites. The
real ``stripe.StripeClient`` is cast to it once, in :meth:`StripeBilling.from_env`: the SDK
types its parameters as ``TypedDict``\\ s, which are not supertypes of ``Mapping[str, Any]``,
so a structural match is impossible to state without either the cast or a dependency on
Stripe's generated parameter types throughout. ``tests/unit/test_billing_stripe_contract.py``
checks the Protocol against the installed SDK so the cast cannot be quietly wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import Any, Final, Protocol, cast

import stripe

from leadquali.app.billing import (
    BillingCustomer,
    BillingSubscription,
    ReportedUsage,
    SubscriptionState,
    UsageReport,
)
from leadquali.config import Settings, get_settings

LOGGER: Final = logging.getLogger(__name__)

#: The payload key a meter reads the customer from. Stripe's default for
#: ``customer_mapping.event_payload_key``; #34's runbook must create the meter with it.
METER_CUSTOMER_KEY: Final[str] = "stripe_customer_id"

#: The payload key a meter reads the quantity from. Stripe's default for
#: ``value_settings.event_payload_key``.
METER_VALUE_KEY: Final[str] = "value"

#: Prefix for the request-level idempotency keys this adapter sends. Namespaced so that a
#: key of ours can never collide with one from another integration on the same account.
IDEMPOTENCY_PREFIX: Final[str] = "leadquali"

__all__ = [
    "IDEMPOTENCY_PREFIX",
    "METER_CUSTOMER_KEY",
    "METER_VALUE_KEY",
    "StripeBilling",
    "StripeBillingError",
    "StripeClientPort",
]


class StripeBillingError(RuntimeError):
    """A Stripe call failed, or answered with something we cannot use.

    Raised instead of letting a ``stripe.StripeError`` escape, for the same reason no
    Stripe *object* escapes: the layer above must not have to import the SDK to catch an
    error from it. The message names the **tenant** and never a Stripe customer, invoice
    or subscription id — this string is stored in ``stripe_events.last_error`` and shipped
    to CloudWatch, and a billing identifier in a log is the same class of mistake as an
    email address (invariant 5).
    """


# ------------------------------------------------------------------- the client surface


class _CreateService(Protocol):
    """A Stripe service whose only method we call is ``create(params, options)``."""

    def create(self, params: Mapping[str, Any], options: Mapping[str, Any] | None = ...) -> Any: ...


class _SubscriptionService(Protocol):
    """``client.v1.subscriptions``. The id is positional on ``cancel`` and ``update``."""

    def create(self, params: Mapping[str, Any], options: Mapping[str, Any] | None = ...) -> Any: ...

    def cancel(
        self,
        subscription_exposed_id: str,
        params: Mapping[str, Any] | None = ...,
        options: Mapping[str, Any] | None = ...,
    ) -> Any: ...

    def update(
        self,
        subscription_exposed_id: str,
        params: Mapping[str, Any] | None = ...,
        options: Mapping[str, Any] | None = ...,
    ) -> Any: ...


class _BillingNamespace(Protocol):
    @property
    def meter_events(self) -> _CreateService: ...


class _PortalNamespace(Protocol):
    @property
    def sessions(self) -> _CreateService: ...


class _V1Namespace(Protocol):
    """``StripeClient.v1``, the namespace 15.6.1 tells you to use.

    The un-namespaced ``StripeClient.customers`` still works and emits a
    ``DeprecationWarning`` on every attribute access, which would put a deprecation notice
    in the logs of every billing run.
    """

    @property
    def customers(self) -> _CreateService: ...

    @property
    def subscriptions(self) -> _SubscriptionService: ...

    @property
    def billing(self) -> _BillingNamespace: ...

    @property
    def billing_portal(self) -> _PortalNamespace: ...


class StripeClientPort(Protocol):
    """The part of ``stripe.StripeClient`` this adapter uses, and nothing more."""

    @property
    def v1(self) -> _V1Namespace: ...


# ------------------------------------------------------------------------- the adapter


class StripeBilling:
    """Stripe as a :class:`~leadquali.app.billing.BillingPort`.

    Args:
        client: the Stripe client, or anything with the same surface.
        price_id: the recurring price new subscriptions are created against. From
            configuration, never a literal — the id differs between test mode and live
            mode and between one plan and the next, and a deploy is not how a price should
            change.
        meter_event_name: the ``event_name`` of the Stripe meter that aggregates billable
            leads. Must match the meter created by #34's runbook; a mismatch is not an
            error from Stripe's side, it is usage that quietly aggregates into nothing.
        logger: where billing calls are logged. Defaults to this module's logger.
    """

    def __init__(
        self,
        *,
        client: StripeClientPort,
        price_id: str,
        meter_event_name: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._price_id = price_id
        self._meter_event_name = meter_event_name
        self._logger = logger if logger is not None else LOGGER

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> StripeBilling:
        """Build the adapter from configuration, resolving the API key from #28's secrets.

        Raises:
            RuntimeError: the API key or the price id is not configured.
            SecretResolutionError: the key's ARN is set and could not be read.
        """
        resolved = settings if settings is not None else get_settings()
        client = stripe.StripeClient(
            api_key=resolved.require_stripe_api_key(),
            # Pinned rather than floating. An API version that changes underneath a running
            # deployment changes the shape of the webhooks we parse and the fields we read,
            # which is a billing outage discovered by a customer.
            stripe_version=resolved.stripe_api_version,
        )
        return cls(
            # The SDK types its parameters as TypedDicts, which are not supertypes of
            # Mapping[str, Any], so the real client cannot structurally satisfy a Protocol
            # written in terms of plain mappings. The cast is the one place that is
            # acknowledged; tests/unit/test_billing_stripe_contract.py checks it holds.
            client=cast(StripeClientPort, client),
            price_id=resolved.require_stripe_price_id(),
            meter_event_name=resolved.stripe_meter_event_name,
        )

    # ------------------------------------------------------------------- the customer

    def create_customer(
        self, *, tenant_id: str, name: str, email: str | None = None
    ) -> BillingCustomer:
        """Create the Stripe customer a tenant is billed as.

        The tenant slug goes into ``metadata`` so that an operator looking at a customer in
        the Stripe dashboard can tell which account it is without a lookup in our database,
        and into the idempotency key so that a retried onboarding returns the customer it
        created the first time instead of making a second one.
        """
        params: dict[str, Any] = {"name": name, "metadata": {"tenant_id": tenant_id}}
        if email is not None:
            # Omitted rather than sent as null: Stripe reads an explicit null as "clear the
            # email", which is a different instruction from "we do not have one".
            params["email"] = email
        customer = self._call(
            tenant_id,
            "create_customer",
            lambda: self._client.v1.customers.create(
                params, {"idempotency_key": f"{IDEMPOTENCY_PREFIX}-customer-{tenant_id}"}
            ),
        )
        return BillingCustomer(
            tenant_id=tenant_id,
            customer_id=self._require_str(customer, "id", tenant_id, "customer"),
        )

    # --------------------------------------------------------------- the subscription

    def create_subscription(
        self, *, tenant_id: str, customer_id: str, price_id: str
    ) -> BillingSubscription:
        """Subscribe a customer to a price."""
        subscription = self._call(
            tenant_id,
            "create_subscription",
            lambda: self._client.v1.subscriptions.create(
                {
                    "customer": customer_id,
                    "items": [{"price": price_id}],
                    "metadata": {"tenant_id": tenant_id},
                },
                {"idempotency_key": f"{IDEMPOTENCY_PREFIX}-subscription-{tenant_id}-{price_id}"},
            ),
        )
        return self._subscription(subscription, tenant_id=tenant_id)

    def cancel_subscription(
        self, *, tenant_id: str, subscription_id: str, at_period_end: bool = False
    ) -> BillingSubscription:
        """Cancel a subscription now, or at the end of the paid period.

        The two are different API calls, not one call with a flag. ``at_period_end`` is an
        *update* that sets ``cancel_at_period_end``; the subscription stays live and Stripe
        sends ``customer.subscription.deleted`` when the period actually ends, which is
        when the tenant should be suspended. Calling ``cancel`` for it would end service
        today for a customer who has paid for the rest of the month.
        """
        if at_period_end:
            subscription = self._call(
                tenant_id,
                "cancel_subscription_at_period_end",
                lambda: self._client.v1.subscriptions.update(
                    subscription_id, {"cancel_at_period_end": True}
                ),
            )
        else:
            subscription = self._call(
                tenant_id,
                "cancel_subscription",
                lambda: self._client.v1.subscriptions.cancel(subscription_id),
            )
        return self._subscription(subscription, tenant_id=tenant_id)

    # ---------------------------------------------------------------------- the usage

    def report_usage(
        self, *, tenant_id: str, customer_id: str, report: UsageReport
    ) -> ReportedUsage:
        """Report one tenant-day of billable usage as a Stripe meter event.

        The payload keys are a meter's defaults (:data:`METER_CUSTOMER_KEY`,
        :data:`METER_VALUE_KEY`) and both values are **strings**, because
        ``MeterEventCreateParams.payload`` is typed ``Dict[str, str]`` in the installed
        SDK; an integer here is a 400 from a scheduled job on the first day of a month.

        The timestamp is the last second of the UTC day being billed, not midnight.
        Midnight is the *next* day's first instant, and a meter aggregates by timestamp —
        so midnight would move every day's usage into the following billing period and get
        a month boundary wrong by one day's revenue in both directions.

        Raises:
            StripeBillingError: Stripe refused the event. Deliberately not swallowed: the
                caller records the day as reported only if this returns.
        """
        params: dict[str, Any] = {
            "event_name": self._meter_event_name,
            "identifier": report.external_id,
            "payload": {METER_CUSTOMER_KEY: customer_id, METER_VALUE_KEY: str(report.quantity)},
            "timestamp": _end_of_day(report.usage_date),
        }
        self._call(
            tenant_id,
            "report_usage",
            lambda: self._client.v1.billing.meter_events.create(params),
        )
        return ReportedUsage(
            tenant_id=tenant_id,
            usage_date=report.usage_date,
            external_id=report.external_id,
            quantity=report.quantity,
            accepted_at=datetime.now(UTC),
        )

    # --------------------------------------------------------------------- the portal

    def portal_session_url(self, *, tenant_id: str, customer_id: str, return_url: str) -> str:
        """Open a billing portal session and return its URL.

        Only the URL leaves this method. The session object carries a customer id, a
        configuration id and a return url, none of which the caller needs and all of which
        would end up in a response body or a log if they were handed over.
        """
        session = self._call(
            tenant_id,
            "portal_session",
            lambda: self._client.v1.billing_portal.sessions.create(
                {"customer": customer_id, "return_url": return_url}
            ),
        )
        return self._require_str(session, "url", tenant_id, "portal session")

    # ----------------------------------------------------------------------- plumbing

    def _call(self, tenant_id: str, operation: str, call: Any) -> Any:
        """Run one Stripe call, turning any SDK failure into :class:`StripeBillingError`.

        ``Exception`` rather than ``stripe.StripeError``: a network stack under the SDK can
        raise things the SDK does not wrap, and every one of them means the same thing to
        the caller — the call did not happen, do not record it as done.
        """
        try:
            return call()
        except Exception as error:
            raise StripeBillingError(
                f"stripe {operation} failed for tenant '{tenant_id}': {type(error).__name__}"
            ) from error

    def _subscription(self, raw: Any, *, tenant_id: str) -> BillingSubscription:
        """Map a Stripe subscription onto ours, refusing a status we do not know."""
        status = SubscriptionState.parse(getattr(raw, "status", None))
        if status is None:
            raise StripeBillingError(
                f"stripe returned a subscription status this build does not know for tenant "
                f"'{tenant_id}': {getattr(raw, 'status', None)!r}"
            )
        return BillingSubscription(
            tenant_id=tenant_id,
            subscription_id=self._require_str(raw, "id", tenant_id, "subscription"),
            customer_id=str(getattr(raw, "customer", "") or ""),
            state=status,
            cancel_at_period_end=bool(getattr(raw, "cancel_at_period_end", False)),
        )

    def _require_str(self, raw: Any, attribute: str, tenant_id: str, what: str) -> str:
        """Read a required string off a Stripe object, or say which one was missing.

        A missing id is not a thing that should ever happen, which is exactly why it is
        checked: the alternative is an empty ``stripe_customer_id`` written to a tenant row
        and a billing run that silently sends every event to nobody.
        """
        value = getattr(raw, attribute, None)
        if not isinstance(value, str) or not value:
            raise StripeBillingError(
                f"stripe returned a {what} with no '{attribute}' for tenant '{tenant_id}'"
            )
        return value

    def __repr__(self) -> str:
        """Name the configuration, never the key — a repr ends up in tracebacks."""
        return (
            f"StripeBilling(price_id={self._price_id!r}, "
            f"meter_event_name={self._meter_event_name!r})"
        )


def _end_of_day(day: date) -> int:
    """The unix timestamp of the last second of ``day`` in UTC. See :meth:`report_usage`."""
    return int(datetime.combine(day, time(23, 59, 59), tzinfo=UTC).timestamp())
