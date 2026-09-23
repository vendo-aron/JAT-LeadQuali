"""Billing state: what Stripe told us, what we told Stripe, and what it does to a tenant.

This module is the whole of billing that is not Stripe. It holds the vocabulary
(:class:`SubscriptionState`, :class:`EventStatus`), the two ports
(:class:`BillingPort` for the payment processor, :class:`BillingStorePort` for our own
tables) and :class:`BillingService`, which is the only thing that decides that a tenant
becomes suspended or that a day's usage is sent. It imports no SDK and writes no SQL; the
Stripe client lives behind :class:`BillingPort` in
:mod:`leadquali.adapters.billing_stripe` and is the only place ``stripe`` is imported.

Three properties are load-bearing, and each one is a way this could cost somebody money.

**200 fast, process later.** The webhook route verifies a signature, inserts a row and
returns 200. It does not update a tenant. Stripe treats a slow or failing endpoint as a
delivery failure and retries it, so doing real work inline means a transient database
blip turns into duplicate deliveries of an event we half-applied. :meth:`
BillingService.receive_event` is therefore only an insert, and
:meth:`BillingService.process_pending` — driven by a one-minute schedule — is where the
handlers run. The cost is up to a minute of latency before a failed payment starts its
grace period, measured against a dunning window of **days**. In exchange there is no
second queue, no fan-out and no ordering problem.

**Idempotency on Stripe's event id.** ``stripe_events.event_id`` is the primary key and
the insert is ``ON CONFLICT DO NOTHING``, so a retried delivery is a no-op *even while the
first copy is still pending* — which is exactly when a retry is most likely, because a
still-pending row means we were slow. A handler therefore runs at most once per event.

**Idempotency on usage, twice over.** Double-reporting usage overbills a customer, and the
issue is explicit that this is worse than under-reporting. Two independent mechanisms:
``usage_reports`` is unique on ``(tenant_id, usage_date)`` so a day already sent is
skipped, and every meter event carries a deterministic identifier derived from the tenant
and the day (:func:`usage_external_id`) so that Stripe deduplicates it even if our table
were lost. See :data:`STRIPE_IDENTIFIER_DEDUPE_NOTE` for the limit of the second one.

**The one exception to invariant 4.** ``stripe_events`` is not tenant-scoped on insert. It
cannot be: the webhook arrives before we know who it is about, and refusing to store an
event we cannot attribute would mean throwing away the only record that it arrived.
``tenant_id`` is therefore nullable, it is filled in by the handler once a customer is
resolved, and **every read that is about a tenant filters on it**. That exception is
written here, in the docstring of the table's model, and in the migration, because an
unexplained exception to an invariant is how the invariant dies.

**Suspension never drops a lead.** Nothing in this module refuses, discards or defers a
lead. Suspending a tenant sets ``tenants.status``, which makes the *ingest* endpoint answer
403 — a clear error a customer's form can show its operator — and which the qualification
worker does not consult at all, so a lead already on the queue when a subscription is
cancelled is still assessed and still delivered. ``tests/unit/test_billing_suspension.py``
pins both halves.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.metering import BillingPeriod, MeteringService
from leadquali.app.ports import ClockPort
from leadquali.app.tenants import TenantStatus
from leadquali.observability import log_event

LOGGER: Final = logging.getLogger(__name__)

#: How long a tenant keeps serving after a failed payment before the sweep suspends it.
#:
#: **This number is a commercial decision, not an engineering one**, and seven days is the
#: orchestrator's default rather than the owner's. It is long enough for a real dunning
#: cycle — Stripe's default smart retries run over about a week — and short enough that a
#: customer who has genuinely stopped paying is not served for a month for free. The owner
#: should confirm it; ``docs/billing-integration.md`` says so in as many words.
DUNNING_GRACE_DAYS: Final[int] = 7

#: :data:`DUNNING_GRACE_DAYS` as a duration, for arithmetic on ``dunning_until``.
DUNNING_GRACE: Final[timedelta] = timedelta(days=DUNNING_GRACE_DAYS)

#: How many times a handler may fail before the event is parked as ``failed``.
#:
#: Five one-minute attempts is five minutes of transient trouble absorbed silently. Past
#: that the failure is not transient, and continuing to retry would hide it: a ``failed``
#: row is a metric, a log line and something an operator has to look at.
MAX_EVENT_ATTEMPTS: Final[int] = 5

#: How many pending events one drain takes. Bounded so that a backlog is worked through
#: over several runs rather than in one invocation that times out and retries everything.
DEFAULT_DRAIN_LIMIT: Final[int] = 100

#: How far back Stripe accepts a meter event's timestamp, in days.
#:
#: Read off the installed SDK's ``MeterEventCreateParams.timestamp``: "Must be within the
#: past 35 calendar days or up to 5 minutes in the future". A backfill older than this is
#: refused here rather than sent and recorded as reported.
METER_EVENT_MAX_AGE_DAYS: Final[int] = 35

#: The namespace the deterministic usage identifier is derived in. A fixed UUID rather
#: than a hash of a string, so that the value cannot change if somebody reformats the
#: input — a changed identifier is a day billed twice.
USAGE_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f0f9d5e-3d4b-5a2f-9a27-9f0a3f9b5c11")

#: What Stripe's own deduplication is worth, so that nobody mistakes it for the guarantee.
STRIPE_IDENTIFIER_DEDUPE_NOTE: Final[str] = (
    "Stripe enforces meter-event identifier uniqueness 'within a rolling period of at "
    "least 24 hours' (SDK 15.6.1, MeterEventCreateParams.identifier). It is a backstop "
    "against a retry minutes apart, not a durable ledger: the usage_reports table is what "
    "makes a day reported once, and re-reporting a day a week later would bill it twice."
)

#: Longest ``last_error`` we will store. It holds an exception class and one short line —
#: never a traceback and never a payload, which would put a customer's billing details in
#: a table we query and a log we ship.
MAX_ERROR_CHARS: Final[int] = 200

__all__ = [
    "DEFAULT_DRAIN_LIMIT",
    "DUNNING_GRACE",
    "DUNNING_GRACE_DAYS",
    "HANDLED_EVENT_TYPES",
    "MAX_ERROR_CHARS",
    "MAX_EVENT_ATTEMPTS",
    "METER_EVENT_MAX_AGE_DAYS",
    "STRIPE_IDENTIFIER_DEDUPE_NOTE",
    "USAGE_ID_NAMESPACE",
    "BillingCustomer",
    "BillingError",
    "BillingPort",
    "BillingService",
    "BillingStorePort",
    "BillingSubscription",
    "BillingTenant",
    "DunningState",
    "EventReceipt",
    "EventStatus",
    "ProcessingSummary",
    "ReportedUsage",
    "StripeEvent",
    "SubscriptionState",
    "UnknownBillingTenantError",
    "UsageReport",
    "UsageReportOutcome",
    "usage_external_id",
]


class BillingError(Exception):
    """Billing could not be carried out. The message is for an operator, not a customer."""


class UnknownBillingTenantError(BillingError):
    """No such tenant, or a tenant with no Stripe customer behind it."""


# ------------------------------------------------------------------------ vocabularies


class SubscriptionState(StrEnum):
    """Stripe's ``subscription.status``, as the installed SDK 15.6.1 declares it.

    Taken from ``stripe.Subscription.__annotations__["status"]`` rather than from
    documentation, so that this list is the one the library will actually hand us. Note
    that the SDK types the field as ``Literal[...] | str``: Stripe reserves the right to
    add a status, which is why :meth:`parse` answers ``None`` for anything unknown instead
    of raising or guessing.
    """

    ACTIVE = "active"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAST_DUE = "past_due"
    PAUSED = "paused"
    TRIALING = "trialing"
    UNPAID = "unpaid"

    @classmethod
    def parse(cls, raw: object) -> SubscriptionState | None:
        """Read a status off an event payload, or ``None`` if it is not one we know.

        ``None`` rather than an exception, and rather than a default: an unrecognised
        status must not activate anybody and must not suspend anybody either. It is logged
        and the tenant is left exactly as it was, which is the only safe answer to "Stripe
        has told us something this code has never heard of".
        """
        if not isinstance(raw, str):
            return None
        try:
            return cls(raw)
        except ValueError:
            return None

    @property
    def keeps_service(self) -> bool:
        """Whether a tenant on a subscription in this state should be serving.

        ``past_due`` is in here on purpose and is the whole point of the grace period: an
        invoice that failed this morning is a card to fix, not a customer to cut off.
        ``unpaid`` is not — by the time Stripe says ``unpaid`` its own retries are over.
        """
        return self in {
            SubscriptionState.ACTIVE,
            SubscriptionState.TRIALING,
            SubscriptionState.PAST_DUE,
        }


class EventStatus(StrEnum):
    """Where a stored webhook event is in its life. Mirrors the CHECK on ``stripe_events``."""

    PENDING = "pending"
    """Stored and not yet applied, or applied and failed fewer than
    :data:`MAX_EVENT_ATTEMPTS` times."""

    PROCESSED = "processed"
    """Applied, or deliberately not applicable — an event type we do not handle, or one
    about a Stripe customer that is not one of our tenants."""

    FAILED = "failed"
    """Out of attempts. Never retried automatically; an operator has to look. This is a
    metric and a log line, not a row that quietly disappears."""


#: The event types this service acts on. Anything else is stored, marked processed and
#: logged — Stripe sends what the endpoint subscribes to plus whatever it adds next year,
#: and leaving those pending forever would fill the drain with work nothing will ever do.
HANDLED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
        "invoice.payment_succeeded",
        "invoice.paid",
    }
)


# ------------------------------------------------------------------------- value types


@dataclass(frozen=True, slots=True)
class BillingTenant:
    """A tenant, reduced to the columns billing cares about.

    ``tenant_id`` is the slug every other port speaks in; the Stripe identifiers are
    ``NULL`` until the tenant has been linked to a customer, which is what distinguishes
    "not on a plan" from "on a plan that is not paying".
    """

    tenant_id: str
    status: TenantStatus
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    dunning_until: datetime | None = None

    @property
    def is_billable(self) -> bool:
        """Whether there is a Stripe customer to send this tenant's usage to."""
        return bool(self.stripe_customer_id)

    def dunning(self, *, now: datetime) -> DunningState:
        """This tenant's grace period as of ``now``."""
        return DunningState(tenant_id=self.tenant_id, until=self.dunning_until, now=now)


@dataclass(frozen=True, slots=True)
class DunningState:
    """How far into a grace period a tenant is.

    A value type rather than two loose datetimes because "is this tenant in dunning?" and
    "has the grace period run out?" are asked from three places — the sweep, the CLI and
    the log line — and three spellings of a ``>`` against ``None`` is one spelling too
    many.
    """

    tenant_id: str
    until: datetime | None
    now: datetime

    @property
    def active(self) -> bool:
        """The tenant owes us money and the grace period has not run out."""
        return self.until is not None and self.until > self.now

    @property
    def expired(self) -> bool:
        """The grace period has run out and the tenant should be suspended."""
        return self.until is not None and self.until <= self.now

    @property
    def seconds_remaining(self) -> int | None:
        """How much grace is left, or ``None`` when there is no grace period at all."""
        if self.until is None:
            return None
        return max(int((self.until - self.now).total_seconds()), 0)


def usage_external_id(*, tenant_id: str, usage_date: date) -> str:
    """The deterministic handle one tenant-day of usage is reported under.

    A UUID5 over ``"<tenant>:<date>"`` in a fixed namespace, which gives the "UUID-like
    identifier" Stripe's own documentation asks for while being a pure function of the two
    facts that identify the day. Determinism is the point: if our ``usage_reports`` table
    were restored from a backup and a day were reported a second time within Stripe's
    deduplication window, Stripe would drop the duplicate. See
    :data:`STRIPE_IDENTIFIER_DEDUPE_NOTE` for how far that goes and where it stops.
    """
    return str(uuid.uuid5(USAGE_ID_NAMESPACE, f"{tenant_id}:{usage_date.isoformat()}"))


@dataclass(frozen=True, slots=True)
class UsageReport:
    """One tenant-day of billable usage, ready to send.

    ``quantity`` is :attr:`~leadquali.app.metering.UsageTotals.leads_billable` from #33's
    rollup — **distinct leads with at least one attempt that cost input tokens**, never a
    count of assessment rows. A lead redelivered three times after a dispatch failure is
    one billable lead; billing the attempts would charge a customer for our own outage.
    """

    tenant_id: str
    usage_date: date
    quantity: int
    external_id: str = ""

    def __post_init__(self) -> None:
        """Fill the deterministic identifier, and refuse a negative quantity.

        The identifier is derived rather than passed so that no call site can invent one:
        two spellings of "which day is this" is how a day gets billed twice.
        """
        if self.quantity < 0:
            raise ValueError(f"usage quantity for {self.tenant_id} {self.usage_date} is negative")
        object.__setattr__(
            self,
            "external_id",
            usage_external_id(tenant_id=self.tenant_id, usage_date=self.usage_date),
        )

    @property
    def period(self) -> BillingPeriod:
        """The one-day billing period this report covers."""
        return BillingPeriod.of_day(self.usage_date)


@dataclass(frozen=True, slots=True)
class ReportedUsage:
    """What the payment processor said when it took a usage report."""

    tenant_id: str
    usage_date: date
    external_id: str
    quantity: int
    accepted_at: datetime | None = None


class UsageReportOutcome(StrEnum):
    """What happened when a tenant-day was put up for reporting.

    An enum rather than a bool because four of these five answers are *correct* and only
    one of them sent anything: a caller that could only see "did it report?" would treat
    "already reported" and "this day is not over" as the same kind of nothing, and the
    first is routine while the second is a scheduling bug.
    """

    REPORTED = "reported"
    ALREADY_REPORTED = "already_reported"
    ZERO_USAGE = "zero_usage"
    """Nothing billable happened. Recorded in ``usage_reports`` so tomorrow's run knows the
    day was dealt with, and not sent: a meter event of zero is noise on an invoice."""

    DAY_NOT_CLOSED = "day_not_closed"
    """The day is today or later. Never billed — a partial day under-reports now and gets
    re-reported tomorrow, which over-reports."""

    NOT_BILLABLE = "not_billable"
    """The tenant has no Stripe customer, so there is nowhere to send it."""

    TOO_OLD = "too_old"
    """Older than :data:`METER_EVENT_MAX_AGE_DAYS`; Stripe would refuse the timestamp."""

    @property
    def reported(self) -> bool:
        """Whether a usage report actually reached the payment processor."""
        return self is UsageReportOutcome.REPORTED

    @property
    def already_reported(self) -> bool:
        """Whether the day was skipped because it had already been sent."""
        return self is UsageReportOutcome.ALREADY_REPORTED


@dataclass(frozen=True, slots=True)
class BillingCustomer:
    """A Stripe customer, as our own types see it. No Stripe object leaves the adapter."""

    tenant_id: str
    customer_id: str


@dataclass(frozen=True, slots=True)
class BillingSubscription:
    """A Stripe subscription, reduced to what this system acts on."""

    tenant_id: str
    subscription_id: str
    customer_id: str
    state: SubscriptionState
    cancel_at_period_end: bool = False


@dataclass(frozen=True, slots=True)
class StripeEvent:
    """One row of ``stripe_events``.

    ``payload`` is the verified body, stored verbatim: it is the evidence of what Stripe
    actually said, and re-deriving it from our own columns would lose whatever we did not
    think to model.
    """

    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    received_at: datetime
    status: EventStatus = EventStatus.PENDING
    attempts: int = 0
    tenant_id: str | None = None
    processed_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class EventReceipt:
    """What receiving a webhook did. Both fields, because they are different facts."""

    event_id: str
    stored: bool
    """``True`` when this call inserted the row."""

    duplicate: bool
    """``True`` when we already held this event id — a Stripe retry, and a no-op."""


@dataclass(frozen=True, slots=True)
class ProcessingSummary:
    """The result of one drain, for the log line and the metric."""

    attempted: int = 0
    processed: int = 0
    retrying: int = 0
    failed: int = 0

    def with_(self, **changes: int) -> ProcessingSummary:
        """Return a copy with counters incremented by ``changes``."""
        return replace(
            self, **{name: getattr(self, name) + value for name, value in changes.items()}
        )


# ------------------------------------------------------------------------------- ports


@runtime_checkable
class BillingPort(Protocol):
    """The payment processor, as this application needs it.

    Implemented by :class:`~leadquali.adapters.billing_stripe.StripeBilling`, which is the
    only module in the package that imports ``stripe``. Every method is keyword-only and
    every method takes ``tenant_id`` — not because the processor needs it (it does not;
    the customer id is enough) but because every log line, every metric and every error
    raised out of an adapter has to be attributable to a tenant, and a signature that
    makes that optional is one where it goes missing.
    """

    def create_customer(
        self, *, tenant_id: str, name: str, email: str | None = None
    ) -> BillingCustomer:
        """Create the Stripe customer a tenant is billed as.

        Must be idempotent on ``tenant_id``: a retried onboarding must not leave a tenant
        with two customers, because two customers is two invoices.
        """
        ...

    def create_subscription(
        self, *, tenant_id: str, customer_id: str, price_id: str
    ) -> BillingSubscription:
        """Subscribe a customer to a price."""
        ...

    def cancel_subscription(
        self, *, tenant_id: str, subscription_id: str, at_period_end: bool = False
    ) -> BillingSubscription:
        """Cancel a subscription now, or at the end of the period the customer paid for."""
        ...

    def report_usage(
        self, *, tenant_id: str, customer_id: str, report: UsageReport
    ) -> ReportedUsage:
        """Send one tenant-day of billable usage.

        Must carry ``report.external_id`` to the processor as its idempotency handle, and
        must raise rather than return on a rejection: a caller that recorded a day as
        reported when it was not would under-bill silently and for ever.
        """
        ...

    def portal_session_url(self, *, tenant_id: str, customer_id: str, return_url: str) -> str:
        """A single-use URL where this tenant manages its own payment method and plan."""
        ...


@runtime_checkable
class BillingStorePort(Protocol):
    """Our own billing tables: ``stripe_events``, ``usage_reports`` and the tenant columns.

    Everything about a *tenant* takes ``tenant_id`` and filters on it (invariant 4). The
    event methods do not, and that is the deliberate exception documented in the module
    docstring: a webhook arrives before we know whose it is.
    """

    def insert_event(self, *, event: StripeEvent) -> bool:
        """Store a verified event, and say whether it was new.

        Must be an ``INSERT ... ON CONFLICT (event_id) DO NOTHING``. ``False`` means we
        already hold this event id — a Stripe retry — and the caller must do nothing else,
        including when the copy we hold is still ``pending``.
        """
        ...

    def pending_events(self, *, limit: int) -> Sequence[StripeEvent]:
        """Events still to apply, oldest first. Never returns a ``failed`` row."""
        ...

    def mark_event_processed(
        self, *, event_id: str, processed_at: datetime, tenant_id: str | None
    ) -> None:
        """Record that an event has been applied, and who it turned out to be about."""
        ...

    def mark_event_attempt_failed(
        self, *, event_id: str, error: str, attempted_at: datetime, max_attempts: int
    ) -> EventStatus:
        """Count a failed attempt and return the status the row now has.

        ``pending`` while attempts remain, ``failed`` once ``max_attempts`` is reached.
        The decision is the store's so that it is one atomic ``UPDATE`` rather than a read
        and a write two schedulers can interleave.
        """
        ...

    def tenant_for_customer(self, *, stripe_customer_id: str) -> str | None:
        """Which of our tenants is this Stripe customer, if any."""
        ...

    def billing_tenant(self, *, tenant_id: str) -> BillingTenant | None:
        """One tenant's billing columns."""
        ...

    def fleet_billable_tenants(self) -> Sequence[BillingTenant]:
        """Every tenant that has a Stripe customer, oldest first.

        ``fleet_`` by #33's convention, and for #33's reason: this is a query with no tenant
        predicate, and the way that stops being a hole is that the name says so at every
        call site. It is the daily billing run's worklist — a list of *who to iterate over*
        rather than any customer's data — and it is on no request path.

        Deliberately *not* filtered by status: a suspended tenant's usage from before it
        was suspended is still owed, and a job that skipped them would write off exactly
        the customers who are not paying.
        """
        ...

    def link_customer(self, *, tenant_id: str, stripe_customer_id: str) -> None:
        """Record which Stripe customer a tenant is billed as."""
        ...

    def set_subscription(self, *, tenant_id: str, stripe_subscription_id: str | None) -> None:
        """Record (or clear) the tenant's current subscription."""
        ...

    def set_status(self, *, tenant_id: str, status: TenantStatus) -> None:
        """Set ``tenants.status``. The only thing in billing that can stop new leads."""
        ...

    def set_dunning_until(self, *, tenant_id: str, until: datetime | None) -> None:
        """Start, extend or clear a tenant's grace period."""
        ...

    def fleet_tenants_in_expired_dunning(self, *, now: datetime) -> Sequence[BillingTenant]:
        """Active tenants whose grace period has run out, across every tenant.

        ``fleet_`` for the same reason as :meth:`fleet_billable_tenants`: the sweep's
        worklist genuinely has no tenant to be scoped to, so it says so in its name rather
        than looking like a query somebody forgot to filter.
        """
        ...

    def record_usage_report(
        self,
        *,
        tenant_id: str,
        usage_date: date,
        quantity: int,
        external_id: str,
        reported_at: datetime,
    ) -> bool:
        """Record that a tenant-day has been reported, and say whether it was new.

        Takes the row's columns rather than a :class:`UsageReport`, so that ``tenant_id``
        is a **named parameter of the method** and not a field of a value object. Invariant
        4 asks every repository method to name its tenant, and a method whose tenant
        arrives inside an argument does not — which also makes it invisible to the
        isolation sweep, since the sweep injects the tenant by parameter name. The report
        stays the unit the *processor* is handed (:meth:`BillingPort.report_usage`); a
        store is handed a row.

        Must be unique on ``(tenant_id, usage_date)``. ``False`` means the day was already
        recorded and nothing further must be sent.
        """
        ...

    def usage_reported(self, *, tenant_id: str, usage_date: date) -> bool:
        """Whether this tenant-day has already been reported."""
        ...


# ----------------------------------------------------------------------------- service


class BillingService:
    """Everything billing decides, with no SDK and no SQL in sight.

    Args:
        store: our own billing tables.
        billing: the payment processor, behind :class:`BillingPort`.
        metering: #33's service. The **only** source of a billable quantity: reading
            ``usage_daily`` from here directly would be a second place the billing rule is
            written, and the two would diverge on the day somebody changed one of them.
        clock: injected, so a seven-day grace period is testable in microseconds.
        logger: where billing events go. Defaults to this module's logger.
    """

    def __init__(
        self,
        *,
        store: BillingStorePort,
        billing: BillingPort,
        metering: MeteringService,
        clock: ClockPort,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._billing = billing
        self._metering = metering
        self._clock = clock
        self._logger = logger if logger is not None else LOGGER

    # ------------------------------------------------------------------- the webhook

    def receive_event(
        self, *, event_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> EventReceipt:
        """Store one verified webhook event. **Applies nothing.**

        This is the entire body of the route once the signature has passed, and it is
        deliberately this small: Stripe reads a slow or 500-ing endpoint as a delivery
        failure and retries, so the way to make retries harmless is to make the endpoint do
        one insert.

        Args:
            event_id: Stripe's ``evt_...``. The idempotency key and the primary key.
            event_type: the ``type`` field, stored so the drain can dispatch without
                parsing the payload again.
            payload: the verified body, stored verbatim.

        Returns:
            A receipt saying whether this call stored the event or found it already held.
        """
        event = StripeEvent(
            event_id=event_id,
            event_type=event_type,
            payload=dict(payload),
            received_at=self._clock.now(),
        )
        stored = self._store.insert_event(event=event)
        log_event(
            self._logger,
            "billing.webhook_received",
            event_id=event_id,
            event_type=event_type,
            stored=stored,
        )
        return EventReceipt(event_id=event_id, stored=stored, duplicate=not stored)

    # -------------------------------------------------------------------- processing

    def process_pending(self, *, limit: int = DEFAULT_DRAIN_LIMIT) -> ProcessingSummary:
        """Apply up to ``limit`` stored events, oldest first.

        Driven by a one-minute schedule. Each event is applied in its own try/except so
        that one poisonous event does not stop the queue behind it — the same
        per-item-failure reasoning as the SQS worker's partial batch response.

        Returns:
            Counts for the log line: attempted, processed, still retrying, given up on.
        """
        summary = ProcessingSummary()
        for event in self._store.pending_events(limit=limit):
            summary = summary.with_(attempted=1)
            try:
                tenant_id = self._apply(event)
            except Exception as error:
                status = self._record_failure(event, error)
                summary = summary.with_(
                    failed=1 if status is EventStatus.FAILED else 0,
                    retrying=1 if status is EventStatus.PENDING else 0,
                )
                continue
            self._store.mark_event_processed(
                event_id=event.event_id, processed_at=self._clock.now(), tenant_id=tenant_id
            )
            summary = summary.with_(processed=1)
        return summary

    def _record_failure(self, event: StripeEvent, error: Exception) -> EventStatus:
        """Count a failed attempt, and say so loudly once the attempts run out."""
        description = f"{type(error).__name__}: {error}"[:MAX_ERROR_CHARS]
        status = self._store.mark_event_attempt_failed(
            event_id=event.event_id,
            error=description,
            attempted_at=self._clock.now(),
            max_attempts=MAX_EVENT_ATTEMPTS,
        )
        log_event(
            self._logger,
            "billing.event_failed",
            level=logging.ERROR if status is EventStatus.FAILED else logging.WARNING,
            event_id=event.event_id,
            event_type=event.event_type,
            status=status.value,
            attempts=event.attempts + 1,
            error=type(error).__name__,
        )
        return status

    def _apply(self, event: StripeEvent) -> str | None:
        """Dispatch one event, returning the tenant it turned out to be about."""
        if event.event_type not in HANDLED_EVENT_TYPES:
            # Not an error and not something to retry: we simply do not act on it. Stored,
            # because the row is the record that Stripe sent it.
            log_event(
                self._logger,
                "billing.event_ignored",
                event_id=event.event_id,
                event_type=event.event_type,
            )
            return None

        obj = _event_object(event.payload)
        customer_id = _customer_id(obj)
        if not customer_id:
            log_event(
                self._logger,
                "billing.event_unattributed",
                level=logging.WARNING,
                event_id=event.event_id,
                event_type=event.event_type,
                reason="no customer on the event object",
            )
            return None
        tenant_id = self._store.tenant_for_customer(stripe_customer_id=customer_id)
        if tenant_id is None:
            # A Stripe account can hold customers that are not our tenants. Nothing to do,
            # and nothing wrong: processed, unattributed, logged.
            log_event(
                self._logger,
                "billing.event_unattributed",
                level=logging.WARNING,
                event_id=event.event_id,
                event_type=event.event_type,
                reason="no tenant for this stripe customer",
            )
            return None

        if event.event_type.startswith("customer.subscription."):
            self._apply_subscription(event, tenant_id=tenant_id, obj=obj)
        elif event.event_type == "invoice.payment_failed":
            self._begin_dunning(tenant_id=tenant_id, event=event)
        else:
            self._settle(tenant_id=tenant_id, event=event)
        return tenant_id

    def _apply_subscription(
        self, event: StripeEvent, *, tenant_id: str, obj: Mapping[str, Any]
    ) -> None:
        """Subscription created, updated or deleted."""
        subscription_id = obj.get("id")
        state = SubscriptionState.parse(obj.get("status"))

        if event.event_type == "customer.subscription.deleted":
            self._store.set_subscription(tenant_id=tenant_id, stripe_subscription_id=None)
            self._suspend(tenant_id=tenant_id, reason="subscription_deleted")
            return

        if isinstance(subscription_id, str) and subscription_id:
            self._store.set_subscription(
                tenant_id=tenant_id, stripe_subscription_id=subscription_id
            )
        if state is None:
            log_event(
                self._logger,
                "billing.subscription_status_unknown",
                level=logging.WARNING,
                event_id=event.event_id,
                tenant_id=tenant_id,
                status=str(obj.get("status")),
            )
            return
        if state.keeps_service:
            self._activate(tenant_id=tenant_id, reason=f"subscription_{state.value}")
        elif state is not SubscriptionState.PAST_DUE:
            # incomplete, incomplete_expired, canceled, unpaid, paused: not serving, and
            # not a grace period either — Stripe's own retries are over by then.
            self._suspend(tenant_id=tenant_id, reason=f"subscription_{state.value}")

    def _begin_dunning(self, *, tenant_id: str, event: StripeEvent) -> None:
        """A failed payment. Start a grace period; **do not** suspend anybody.

        The tenant keeps serving. Stripe retries an invoice several times over its dunning
        cycle and each retry sends another ``invoice.payment_failed``, so an existing grace
        period is left alone rather than restarted — restarting it on every retry would
        make it unbounded, which is the same as never suspending at all.
        """
        tenant = self._require_tenant(tenant_id)
        now = self._clock.now()
        if tenant.dunning(now=now).active:
            log_event(
                self._logger,
                "billing.dunning_continues",
                tenant_id=tenant_id,
                event_id=event.event_id,
                dunning_until=tenant.dunning_until.isoformat() if tenant.dunning_until else None,
            )
            return
        until = now + DUNNING_GRACE
        self._store.set_dunning_until(tenant_id=tenant_id, until=until)
        log_event(
            self._logger,
            "billing.dunning_started",
            level=logging.WARNING,
            tenant_id=tenant_id,
            event_id=event.event_id,
            dunning_until=until.isoformat(),
            grace_days=DUNNING_GRACE_DAYS,
        )

    def _settle(self, *, tenant_id: str, event: StripeEvent) -> None:
        """A paid invoice: clear the grace period, and bring a suspended tenant back.

        Re-activation is conditional on the tenant still having a subscription, and the
        condition is load-bearing rather than defensive. Cancelling a subscription clears
        ``stripe_subscription_id`` and suspends the tenant — and Stripe then issues a
        **final invoice** for the usage up to the cancellation, which the customer pays.
        Without this check that last payment would silently resurrect an account that had
        been cancelled, which is the opposite of what both parties agreed. A tenant
        suspended because its grace period ran out still *has* a subscription, so the case
        the acceptance criterion cares about — suspend, pay, active — is unaffected.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.dunning_until is not None:
            self._store.set_dunning_until(tenant_id=tenant_id, until=None)
        if tenant.status is not TenantStatus.ACTIVE:
            if tenant.stripe_subscription_id is None:
                log_event(
                    self._logger,
                    "billing.invoice_settled_without_subscription",
                    level=logging.WARNING,
                    tenant_id=tenant_id,
                    event_id=event.event_id,
                )
            else:
                self._activate(tenant_id=tenant_id, reason="invoice_paid")
        log_event(
            self._logger, "billing.invoice_settled", tenant_id=tenant_id, event_id=event.event_id
        )

    # ---------------------------------------------------------------- status changes

    def _activate(self, *, tenant_id: str, reason: str) -> None:
        """Make a tenant active and clear any grace period. Idempotent."""
        tenant = self._require_tenant(tenant_id)
        if tenant.status is not TenantStatus.ACTIVE:
            self._store.set_status(tenant_id=tenant_id, status=TenantStatus.ACTIVE)
        if tenant.dunning_until is not None:
            self._store.set_dunning_until(tenant_id=tenant_id, until=None)
        log_event(self._logger, "billing.tenant_activated", tenant_id=tenant_id, reason=reason)

    def _suspend(self, *, tenant_id: str, reason: str) -> None:
        """Suspend a tenant.

        **This stops new leads and nothing else.** A suspended tenant's ingest answers 403
        — a clear error a form can show its operator, never a silent 202 — and the
        qualification worker does not read ``tenants.status`` at all, so a lead already on
        the queue is still assessed, still routed and still delivered.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.status is TenantStatus.SUSPENDED:
            return
        self._store.set_status(tenant_id=tenant_id, status=TenantStatus.SUSPENDED)
        log_event(
            self._logger,
            "billing.tenant_suspended",
            level=logging.WARNING,
            tenant_id=tenant_id,
            reason=reason,
        )

    def sweep_dunning(self) -> Sequence[BillingTenant]:
        """Suspend every tenant whose grace period has run out.

        Scheduled, not event-driven: the thing that has to happen is the *absence* of a
        payment for seven days, and an absence does not arrive as a webhook. A tenant whose
        invoice was paid has had ``dunning_until`` cleared by
        :meth:`_settle` and is therefore not in this list at all.

        Returns:
            The tenants suspended by this sweep, for the log line and the operator's mail.
        """
        now = self._clock.now()
        suspended: list[BillingTenant] = []
        for tenant in self._store.fleet_tenants_in_expired_dunning(now=now):
            if not tenant.dunning(now=now).expired:
                # Defensive: the store's filter is the authority, and re-checking it here
                # means a bug in a WHERE clause cannot suspend a paying customer.
                continue
            self._suspend(tenant_id=tenant.tenant_id, reason="dunning_expired")
            suspended.append(tenant)
        log_event(
            self._logger,
            "billing.dunning_sweep",
            level=logging.WARNING if suspended else logging.INFO,
            suspended=len(suspended),
        )
        return suspended

    # -------------------------------------------------------------- usage reporting

    def report_usage_for_day(self, *, tenant_id: str, usage_date: date) -> UsageReportOutcome:
        """Report one tenant's billable usage for one closed day.

        The quantity is :attr:`~leadquali.app.metering.UsageTotals.leads_billable` read
        back from #33's rollup for exactly this day — not ``leads_ingested``, which
        includes spam we filtered and do not charge for, and not ``leads_assessed``, which
        counts a redelivered lead once per attempt. ``usage_for_period`` is called with its
        default ``closed_days_only``, deliberately rather than explicitly, so that the
        default cannot be weakened here without being weakened everywhere.

        The order of operations is what keeps it idempotent: check our table, send, then
        record. Recording first would leave a day marked reported that Stripe never
        received, and under-billing a day for ever is not recoverable from a table that
        says it was done.

        Raises:
            Exception: whatever the processor raised. The day is **not** recorded, so the
                next run tries again.
        """
        if usage_date >= self._metering.today():
            return UsageReportOutcome.DAY_NOT_CLOSED
        if (self._metering.today() - usage_date).days > METER_EVENT_MAX_AGE_DAYS:
            log_event(
                self._logger,
                "billing.usage_too_old",
                level=logging.WARNING,
                tenant_id=tenant_id,
                usage_date=usage_date.isoformat(),
                max_age_days=METER_EVENT_MAX_AGE_DAYS,
            )
            return UsageReportOutcome.TOO_OLD

        tenant = self._require_tenant(tenant_id)
        if not tenant.is_billable or tenant.stripe_customer_id is None:
            return UsageReportOutcome.NOT_BILLABLE
        if self._store.usage_reported(tenant_id=tenant_id, usage_date=usage_date):
            return UsageReportOutcome.ALREADY_REPORTED

        totals = self._metering.usage_for_period(
            tenant_id=tenant_id, period=BillingPeriod.of_day(usage_date)
        )
        report = UsageReport(
            tenant_id=tenant_id, usage_date=usage_date, quantity=totals.leads_billable
        )

        if report.quantity == 0:
            # Recorded, not sent. A zero meter event is a line on an invoice that says
            # nothing, and Stripe aggregates server-side so there is nothing to establish.
            self._record(report)
            return UsageReportOutcome.ZERO_USAGE

        self._billing.report_usage(
            tenant_id=tenant_id, customer_id=tenant.stripe_customer_id, report=report
        )
        fresh = self._record(report)
        log_event(
            self._logger,
            "billing.usage_reported",
            tenant_id=tenant_id,
            usage_date=usage_date.isoformat(),
            quantity=report.quantity,
            external_id=report.external_id,
        )
        return UsageReportOutcome.REPORTED if fresh else UsageReportOutcome.ALREADY_REPORTED

    def _record(self, report: UsageReport) -> bool:
        """Write one report to ``usage_reports``, unpacked into the row's own columns.

        The unpacking happens here, once, so that the store's signature can name its tenant
        (invariant 4) without the service having to hold the four fields apart.
        """
        return self._store.record_usage_report(
            tenant_id=report.tenant_id,
            usage_date=report.usage_date,
            quantity=report.quantity,
            external_id=report.external_id,
            reported_at=self._clock.now(),
        )

    def report_usage_for_all(self, *, usage_date: date) -> Mapping[str, UsageReportOutcome]:
        """Report one day for every tenant that has a Stripe customer.

        One tenant's failure does not stop the rest: the exception is logged against that
        tenant and its day stays unrecorded, so tomorrow's run picks it up again. A job
        that aborted on the first failure would silently stop billing every customer whose
        slug sorts after a broken one.
        """
        outcomes: dict[str, UsageReportOutcome] = {}
        for tenant in self._store.fleet_billable_tenants():
            try:
                outcomes[tenant.tenant_id] = self.report_usage_for_day(
                    tenant_id=tenant.tenant_id, usage_date=usage_date
                )
            except Exception as error:
                log_event(
                    self._logger,
                    "billing.usage_report_failed",
                    level=logging.ERROR,
                    tenant_id=tenant.tenant_id,
                    usage_date=usage_date.isoformat(),
                    error=type(error).__name__,
                )
        return outcomes

    # ------------------------------------------------------------------ the customer

    def portal_url(self, *, tenant_id: str, return_url: str) -> str:
        """A URL where this tenant manages its own payment method and plan.

        Raises:
            UnknownBillingTenantError: no such tenant, or it has no Stripe customer. A
                tenant that is not on a plan has no portal to open, and inventing one
                would mean creating a customer as a side effect of somebody clicking a
                link.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.stripe_customer_id is None:
            raise UnknownBillingTenantError(
                f"tenant '{tenant_id}' has no Stripe customer; link one before opening a portal"
            )
        return self._billing.portal_session_url(
            tenant_id=tenant_id,
            customer_id=tenant.stripe_customer_id,
            return_url=return_url,
        )

    def link_customer(
        self, *, tenant_id: str, name: str, email: str | None = None
    ) -> BillingCustomer:
        """Create this tenant's Stripe customer and record it, or return the existing one.

        Idempotent on our side as well as the adapter's: a tenant that already has a
        customer id is returned as it is rather than given a second one, because two Stripe
        customers for one tenant is two invoices for the same account.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.stripe_customer_id is not None:
            return BillingCustomer(tenant_id=tenant_id, customer_id=tenant.stripe_customer_id)
        customer = self._billing.create_customer(tenant_id=tenant_id, name=name, email=email)
        self._store.link_customer(tenant_id=tenant_id, stripe_customer_id=customer.customer_id)
        log_event(
            self._logger,
            "billing.customer_linked",
            tenant_id=tenant_id,
            stripe_customer_id=customer.customer_id,
        )
        return customer

    def subscribe(self, *, tenant_id: str, price_id: str) -> BillingSubscription:
        """Put a linked tenant on a price, and record the subscription.

        The tenant's status is *not* changed here. It becomes active when Stripe says the
        subscription is active, which arrives as ``customer.subscription.created`` — so
        there is one place that decides, and it is the same place whether the subscription
        was created by this method or by a customer clicking through checkout.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.stripe_customer_id is None:
            raise UnknownBillingTenantError(
                f"tenant '{tenant_id}' has no Stripe customer; link one before subscribing"
            )
        subscription = self._billing.create_subscription(
            tenant_id=tenant_id, customer_id=tenant.stripe_customer_id, price_id=price_id
        )
        self._store.set_subscription(
            tenant_id=tenant_id, stripe_subscription_id=subscription.subscription_id
        )
        return subscription

    def cancel(self, *, tenant_id: str, at_period_end: bool = False) -> BillingSubscription:
        """Cancel a tenant's subscription.

        Suspension is again left to the webhook: Stripe sends
        ``customer.subscription.deleted`` when the cancellation takes effect, which for
        ``at_period_end`` is weeks later, and suspending the tenant here would cut off a
        customer who has paid for the rest of the month.
        """
        tenant = self._require_tenant(tenant_id)
        if tenant.stripe_subscription_id is None:
            raise UnknownBillingTenantError(f"tenant '{tenant_id}' has no subscription to cancel")
        return self._billing.cancel_subscription(
            tenant_id=tenant_id,
            subscription_id=tenant.stripe_subscription_id,
            at_period_end=at_period_end,
        )

    def today(self) -> date:
        """Today in UTC — the only timezone a usage day is ever expressed in.

        Delegated to :class:`~leadquali.app.metering.MeteringService` rather than read from
        a clock here, so that "which day is it?" has one answer across the rollup, the
        quota check and the billing run. A job that computed yesterday from a host's local
        date would bill a different day than the rollup counted.
        """
        return self._metering.today()

    def tenant(self, *, tenant_id: str) -> BillingTenant:
        """One tenant's billing state.

        Raises:
            UnknownBillingTenantError: no such tenant.
        """
        return self._require_tenant(tenant_id)

    def _require_tenant(self, tenant_id: str) -> BillingTenant:
        tenant = self._store.billing_tenant(tenant_id=tenant_id)
        if tenant is None:
            raise UnknownBillingTenantError(f"no tenant '{tenant_id}'")
        return tenant


# --------------------------------------------------------------------- payload reading


def _event_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``data.object`` of a Stripe event, or an empty mapping.

    Read defensively rather than with a schema: the payload has been *authenticated* — it
    really came from Stripe — but that is not the same as being the shape this version of
    the code expects, and a ``KeyError`` here would turn a harmless unfamiliar event into
    five retries and a ``failed`` row.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    obj = data.get("object")
    return obj if isinstance(obj, Mapping) else {}


def _customer_id(obj: Mapping[str, Any]) -> str | None:
    """The Stripe customer an event object is about.

    ``customer`` is a string id on an unexpanded object and a nested object when the
    endpoint has been configured to expand it, so both are read. Anything else is ``None``
    and the event is left unattributed rather than guessed at.
    """
    customer = obj.get("customer")
    if isinstance(customer, str) and customer:
        return customer
    if isinstance(customer, Mapping):
        nested = customer.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None
