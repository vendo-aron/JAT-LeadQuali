"""Usage metering: what a tenant used, what it cost us, and what we may bill for.

Token counts and ``cost_usd`` have been written on every ``assessments`` row since the
first migration, so metering is a ``SUM`` rather than a schema change. What this module
adds is the part a ``SUM`` cannot decide on its own: **which rows are billable**, what a
"day" is, when a day is final, and what to do when the number we computed disagrees with
the invoice Anthropic sends.

Three counts, and they are deliberately three different numbers
---------------------------------------------------------------

``leads_ingested``
    Rows in ``leads``. Everything a tenant's form sent us, spam included.

``leads_assessed``
    Rows in ``assessments``, whatever their status. A failed model call is an assessment
    *attempt*: it burned input tokens, it cost money, and invariant 3 means the lead still
    reached a human.

``leads_billable``
    Rows in ``assessments`` with ``input_tokens > 0`` — the calls we actually paid
    Anthropic for. This is the number a customer is charged on, and the number a plan's
    quota is measured against.

The gap between the first and the third is the deterministic spam pre-filter, and it is
not billable. The reasoning is written down here because it is a commercial decision, not
an implementation detail: a tenant cannot control who posts to their own public form,
charging them for our filter working would make them pay for our efficiency, and — worse —
it would give us a standing incentive not to improve the filter. A failed *model* call is
the other way round: we were billed for it by Anthropic, so the customer is billed for it
too. ``docs/metering-and-billing.md`` says the same thing in the customer's language.

The day boundary is UTC
-----------------------

A tenant in Sydney and a tenant in California must not each get their own definition of
"yesterday" inside one nightly billing job, so a usage day is a UTC calendar day for
everyone. The rollup is a full recomputation of a (tenant, day) from ``leads`` and
``assessments``, written with one upsert, so re-running it is a no-op apart from
``computed_at`` — see :meth:`MeteringService.rollup_day`.

A day is not final until it is over. Rolling up today's partial day is legitimate (an
admin view wants it) and billing from it is not, so every read that a billing job might
touch takes an explicit ``closed_days_only`` rather than relying on the caller to
subtract a day.

Nothing here can stop a lead being qualified
--------------------------------------------

A quota is a commercial matter and invariant 3 is a technical one. :class:`QuotaStatus`
reports, logs and emits a metric; there is no code path in this module — or reachable
from it — that refuses an ingest or skips an assessment. A customer who goes over their
plan gets an invoice and a conversation, not silently unqualified leads.
"""

from __future__ import annotations

import calendar
import csv
import io
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from leadquali.app.ports import ClockPort
from leadquali.observability.events import log_quota_crossed

__all__ = [
    "DEFAULT_QUOTA_ALERT_FRACTION",
    "MONTHLY_INFRASTRUCTURE_USD",
    "RECONCILIATION_TOLERANCE",
    "BillingPeriod",
    "DailySpend",
    "DayVariance",
    "InvoiceFormatError",
    "MarginReport",
    "MeteringError",
    "MeteringService",
    "MeteringStorePort",
    "QuotaLevel",
    "QuotaStatus",
    "ReconciliationReport",
    "RevenuePort",
    "TenantQuota",
    "UsageTotals",
    "infrastructure_usd_for",
    "is_billable",
    "parse_invoice_csv",
]

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_QUOTA_ALERT_FRACTION: Final[Decimal] = Decimal("0.80")
"""How much of a plan may be used before somebody is told. Mirrors the server default on
``tenants.quota_alert_fraction``; four fifths of a month's allowance leaves enough of the
month for an upgrade conversation to happen before the overage does."""

MONTHLY_INFRASTRUCTURE_USD: Final[Decimal] = Decimal("139.60")
"""The monthly infrastructure bill from ``docs/infrastructure-cost.md``, at the defaults in
``infra/network.yaml``.

It is an estimate from published rates — no AWS account exists in this repository — and it
is **mostly fixed**: the RDS Proxy alone is $87.60/month and is spent whether there is one
tenant or fifty. See :func:`infrastructure_usd_for` for what that does to a per-tenant
margin number."""

RECONCILIATION_TOLERANCE: Final[Decimal] = Decimal("0.02")
"""How far our computed spend may sit from Anthropic's invoice before somebody looks.

Two percent, because several things legitimately move the number and none of them is a
bug: our rate card (:data:`~leadquali.adapters.llm_anthropic.CLAUDE_OPUS_5_PRICES`) is a
documented snapshot and Anthropic's published prices change; the console rounds where we
keep six decimal places; a call our client gave up on may still have been served and
billed; and cache-write pricing depends on the TTL requested, so a change of cache
strategy shows up here before it shows up anywhere else. Anything past two percent is a
number nobody should bill a customer from until it is explained."""


class MeteringError(Exception):
    """A metering operation cannot be carried out, with a reason an operator can act on."""


class InvoiceFormatError(MeteringError):
    """An invoice export is not in a shape this tool can read."""


# ------------------------------------------------------------------------ the billing rule


def is_billable(*, input_tokens: int) -> bool:
    """Whether one assessment attempt is charged to the customer.

    The rule in one place, so the SQL in
    :mod:`leadquali.adapters.metering_postgres` and the documentation cannot drift apart:
    an attempt that consumed input tokens reached Anthropic and appeared on our invoice,
    so it appears on the customer's. An attempt that consumed none never reached the
    model — the deterministic pre-filter stopped it — and is not billable.

    Note that this is a question about *tokens*, not about success. A refusal, a timeout
    after the request was accepted and a ``max_tokens`` truncation all burned input tokens
    and are all billable.
    """
    return input_tokens > 0


# ----------------------------------------------------------------------------- periods


@dataclass(frozen=True, slots=True, order=True)
class BillingPeriod:
    """A closed range of UTC calendar days, ``start`` and ``end`` both included.

    Inclusive at both ends because that is how an invoice line reads ("1-30 September"),
    and a half-open period rendered to a customer loses a day the first time somebody
    prints it.
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        """Refuse an inverted period at construction rather than returning zero usage."""
        if self.end < self.start:
            raise ValueError(f"billing period ends ({self.end}) before it starts ({self.start})")

    @classmethod
    def of_day(cls, day: date) -> BillingPeriod:
        """The one-day period covering ``day``."""
        return cls(start=day, end=day)

    @classmethod
    def of_month(cls, year: int, month: int) -> BillingPeriod:
        """The whole calendar month, first to last day inclusive."""
        last = calendar.monthrange(year, month)[1]
        return cls(start=date(year, month, 1), end=date(year, month, last))

    @classmethod
    def parse_month(cls, text: str) -> BillingPeriod:
        """Parse ``YYYY-MM`` into the whole of that month.

        An unpadded month (``2026-9``) is accepted: it can only mean September. The
        reversed spelling (``09-2026``) is refused, because a parser that guessed at it
        would be guessing at which half is the year.

        Raises:
            ValueError: not a month.
        """
        try:
            parsed = datetime.strptime(text, "%Y-%m")
        except ValueError:
            raise ValueError(f"'{text}' is not a month; expected YYYY-MM, e.g. 2026-09") from None
        return cls.of_month(parsed.year, parsed.month)

    @property
    def days(self) -> int:
        """How many calendar days the period covers, both ends included."""
        return (self.end - self.start).days + 1

    def dates(self) -> Iterator[date]:
        """Every day in the period, oldest first."""
        for offset in range(self.days):
            yield self.start + timedelta(days=offset)

    def contains(self, day: date) -> bool:
        """Whether ``day`` falls inside the period."""
        return self.start <= day <= self.end

    def includes_open_day(self, *, today: date) -> bool:
        """Whether the period reaches today or later, and so can still grow."""
        return self.end >= today

    def closed_as_of(self, *, today: date) -> BillingPeriod | None:
        """The part of this period that is over, or ``None`` if none of it is.

        A day is closed once it is entirely in the past in UTC, so the last closed day is
        always yesterday. ``None`` — rather than an empty period — because "there is
        nothing to bill yet" is a different answer from "the bill is zero", and a caller
        that ignores the distinction should not be able to do so silently.
        """
        last_closed = today - timedelta(days=1)
        if last_closed < self.start:
            return None
        return BillingPeriod(start=self.start, end=min(self.end, last_closed))

    def __str__(self) -> str:
        """``2026-09-01..2026-09-30``, or a single date for a one-day period."""
        return str(self.start) if self.start == self.end else f"{self.start}..{self.end}"


# ------------------------------------------------------------------------------ totals


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """What one tenant used over one period.

    The same type is returned for a single rolled-up day and for a whole billing period:
    the numbers mean the same thing at both scales, and having two types would mean two
    renderers, two JSON shapes and two chances to sum the wrong column.
    """

    tenant_id: str
    period: BillingPeriod
    leads_ingested: int
    """Submissions stored, spam included. Not billable on its own — see :func:`is_billable`."""

    leads_assessed: int
    """Assessment attempts, successful or not."""

    leads_billable: int
    """Attempts that cost us tokens. **This is the number a customer is charged on.**"""

    assessments_failed: int
    """Attempts that produced no usable judgement. A subset of ``leads_assessed``, and
    mostly a subset of ``leads_billable`` too: a refusal is an HTTP 200 we paid for."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: Decimal
    """Our inference cost, summed from ``assessments.cost_usd``. An estimate of the
    Anthropic invoice, reconciled against the real one by
    :meth:`MeteringService.reconcile`."""

    computed_at: datetime | None = None
    """When the underlying rollup rows were last recomputed; the newest one wins. ``None``
    when the period has no rollup rows at all."""

    partial: bool = False
    """``True`` when the period includes a day that is not yet over, so these numbers can
    still grow. A billing job must not invoice from a partial total."""

    @classmethod
    def zero(cls, *, tenant_id: str, period: BillingPeriod) -> UsageTotals:
        """A period in which nothing happened."""
        return cls(
            tenant_id=tenant_id,
            period=period,
            leads_ingested=0,
            leads_assessed=0,
            leads_billable=0,
            assessments_failed=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=Decimal(0),
        )

    @property
    def total_tokens(self) -> int:
        """Every token, however it was billed. The four counters are disjoint."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def leads_filtered(self) -> int:
        """Submissions that never reached the model: ingested minus assessed.

        Deterministic spam, mostly. Negative is impossible in a consistent rollup but is
        representable across a midnight boundary — a lead received at 23:59:59 and
        assessed at 00:00:01 lands in two different days — so it is clamped rather than
        rendered as a negative count nobody can explain.
        """
        return max(self.leads_ingested - self.leads_assessed, 0)

    @property
    def cost_per_billable_lead_usd(self) -> Decimal | None:
        """Inference cost per charged lead, or ``None`` when nothing was charged."""
        if self.leads_billable == 0:
            return None
        return self.cost_usd / Decimal(self.leads_billable)


# ------------------------------------------------------------------------------- quota


class QuotaLevel(StrEnum):
    """Where a tenant stands against its plan. Never an instruction to stop anything."""

    OK = "ok"
    WARNING = "warning"
    """Past the alert fraction and not yet past the plan. The point of the whole feature:
    somebody has a conversation with the customer while there is still month left."""

    EXCEEDED = "exceeded"
    """Past the plan. Every lead is still ingested, assessed and routed — this produces an
    invoice line and a phone call, not a refusal."""


@dataclass(frozen=True, slots=True)
class TenantQuota:
    """A tenant's plan allowance, as configured on its ``tenants`` row."""

    tenant_id: str
    monthly_lead_quota: int | None
    """Billable leads included in the plan per month. ``None`` means unlimited, which is
    the default and the right default: a quota that appeared by accident would start
    paging somebody about a customer who never agreed to one."""

    alert_fraction: Decimal = DEFAULT_QUOTA_ALERT_FRACTION


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """A tenant's usage against its plan for one period.

    **Nothing acts on this.** It is rendered by ``usagectl``, logged, and published as a
    CloudWatch metric. Invariant 3 is not negotiable, and a quota is a commercial matter:
    the system does not have the authority to decide that a customer's next lead is worth
    less than their invoice.
    """

    tenant_id: str
    period: BillingPeriod
    used: int
    """Billable leads in the period. Pre-filtered spam is not in here — it does not count
    against a plan, for the reasons in this module's docstring."""

    quota: int | None
    alert_fraction: Decimal
    level: QuotaLevel
    partial: bool = False
    """``True`` when the period is not over, which is the normal case for a live check."""

    @property
    def fraction(self) -> Decimal | None:
        """How much of the plan is spent, or ``None`` when the plan is unlimited."""
        if self.quota is None or self.quota == 0:
            return None
        return Decimal(self.used) / Decimal(self.quota)

    @property
    def remaining(self) -> int | None:
        """Leads left in the plan, floored at zero; ``None`` when unlimited."""
        if self.quota is None:
            return None
        return max(self.quota - self.used, 0)

    @classmethod
    def evaluate(
        cls,
        *,
        tenant_id: str,
        period: BillingPeriod,
        used: int,
        quota: TenantQuota,
        partial: bool = False,
    ) -> QuotaStatus:
        """Classify ``used`` against ``quota``.

        The two boundaries are decided here rather than at a call site, because they are
        the kind of thing two call sites would answer differently:

        * **Exactly at the alert fraction is a warning.** "80% of your plan" is the
          promise the number makes; firing at 80.0001% would make the alert depend on
          rounding.
        * **Exactly at the quota is not exceeded.** The thousandth lead of a
          thousand-lead plan is included in the plan. The thousand-and-first is overage.
        """
        if quota.monthly_lead_quota is None:
            return cls(
                tenant_id=tenant_id,
                period=period,
                used=used,
                quota=None,
                alert_fraction=quota.alert_fraction,
                level=QuotaLevel.OK,
                partial=partial,
            )
        allowance = quota.monthly_lead_quota
        if used > allowance:
            level = QuotaLevel.EXCEEDED
        elif allowance > 0 and Decimal(used) / Decimal(allowance) >= quota.alert_fraction:
            level = QuotaLevel.WARNING
        else:
            level = QuotaLevel.OK
        return cls(
            tenant_id=tenant_id,
            period=period,
            used=used,
            quota=allowance,
            alert_fraction=quota.alert_fraction,
            level=level,
            partial=partial,
        )


# ------------------------------------------------------------------------------ margin


@dataclass(frozen=True, slots=True)
class MarginReport:
    """Revenue minus cost for one tenant over one period, with the unknowns kept unknown.

    ``revenue_usd`` is ``None`` until #35 exists. That is rendered as ``unknown`` rather
    than filled in with a plausible figure: a margin report with a fabricated revenue in
    it is worse than one that admits it does not know, because somebody will act on it.

    ``infrastructure_usd`` is an **allocation, not a measurement** — see
    :func:`infrastructure_usd_for` and :attr:`allocation_caveat`.
    """

    usage: UsageTotals
    revenue_usd: Decimal | None
    inference_usd: Decimal
    infrastructure_usd: Decimal
    fleet_billable_leads: int
    """Billable leads across every tenant in the period — the denominator of the
    allocation, carried so a reader can see what the share was computed from."""

    @property
    def tenant_id(self) -> str:
        return self.usage.tenant_id

    @property
    def period(self) -> BillingPeriod:
        return self.usage.period

    @property
    def cost_usd(self) -> Decimal:
        """Inference plus the tenant's share of infrastructure."""
        return self.inference_usd + self.infrastructure_usd

    @property
    def margin_usd(self) -> Decimal | None:
        """Revenue minus cost, or ``None`` while revenue is unknown."""
        if self.revenue_usd is None:
            return None
        return self.revenue_usd - self.cost_usd

    @property
    def margin_fraction(self) -> Decimal | None:
        """Margin as a share of revenue, or ``None`` when revenue is unknown or zero."""
        margin = self.margin_usd
        if margin is None or self.revenue_usd is None or self.revenue_usd == 0:
            return None
        return margin / self.revenue_usd

    @property
    def allocation_caveat(self) -> str:
        """The sentence that must accompany this number wherever it is shown."""
        return (
            "infrastructure is allocated pro rata by billable leads, not measured: most of "
            f"${MONTHLY_INFRASTRUCTURE_USD}/month is fixed (the RDS Proxy alone is $87.60) "
            "and would be spent for one tenant or fifty, so a single tenant's share falls "
            "as customers are added"
        )


def infrastructure_usd_for(period: BillingPeriod) -> Decimal:
    """The infrastructure bill attributable to ``period``, from the monthly figure.

    A whole calendar month costs exactly :data:`MONTHLY_INFRASTRUCTURE_USD`; any other
    period gets the share of each month it touches, by days. The bill is a monthly
    subscription to fixed capacity, so "half a month costs half" is the only defensible
    reading of it — and it keeps a 28-day February from looking cheaper per day than a
    31-day January for no reason a customer could act on.
    """
    total = Decimal(0)
    cursor = period.start
    while cursor <= period.end:
        days_in_month = calendar.monthrange(cursor.year, cursor.month)[1]
        month_end = date(cursor.year, cursor.month, days_in_month)
        counted = (min(month_end, period.end) - cursor).days + 1
        total += MONTHLY_INFRASTRUCTURE_USD * Decimal(counted) / Decimal(days_in_month)
        cursor = month_end + timedelta(days=1)
    return total


# ---------------------------------------------------------------------- reconciliation


@dataclass(frozen=True, slots=True)
class DailySpend:
    """One day of token spend, from either side of the reconciliation.

    Deliberately one type for both sides: the comparison is only meaningful if our
    rollup and Anthropic's export are reduced to the same six numbers first.
    """

    usage_date: date
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Decimal = Decimal(0)


#: Column names accepted for each field of an Anthropic usage export, lowercased and
#: stripped. The console has shipped more than one spelling of these, and a CSV that fails
#: to load at 9am on invoice day because a header gained the word ``input`` is a bad
#: trade against a dictionary of aliases.
_INVOICE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "usage_date": ("date", "usage_date", "day", "usage_date_utc"),
    "input_tokens": ("input_tokens", "uncached_input_tokens", "input"),
    "output_tokens": ("output_tokens", "output"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens", "cache_read"),
    "cache_creation_tokens": (
        "cache_creation_tokens",
        "cache_creation_input_tokens",
        "cache_write_tokens",
        "cache_creation",
    ),
    "cost_usd": ("cost_usd", "cost", "amount_usd", "amount", "total_cost_usd"),
}


def parse_invoice_csv(text: str) -> Sequence[DailySpend]:
    """Read an Anthropic console usage export into :class:`DailySpend` rows, by day.

    The export is one row per day *per model* (and per workspace, and per API key,
    depending on how it was requested), so rows sharing a date are summed. That is what
    makes the comparison against our fleet-wide rollup an apples-to-apples one: both sides
    end up as one row per UTC day.

    Args:
        text: the CSV file's contents.

    Returns:
        One row per date, oldest first.

    Raises:
        InvoiceFormatError: the file has no header, is missing a column this tool needs,
            or holds a value that is not a date or a number. The message names the columns
            that *were* found, because the usual cause is an export of the wrong report
            and the fastest fix is seeing what it actually contained.
    """
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames
    if not header:
        raise InvoiceFormatError("the invoice export is empty: no header row")

    found = [name.strip().lower() for name in header if name is not None]
    resolved: dict[str, str] = {}
    for field_name, aliases in _INVOICE_COLUMNS.items():
        match = next((alias for alias in aliases if alias in found), None)
        if match is None:
            raise InvoiceFormatError(
                f"the invoice export has no column for {field_name} (accepted: "
                f"{', '.join(aliases)}); its columns are: {', '.join(found) or '(none)'}"
            )
        resolved[field_name] = match

    by_day: dict[date, DailySpend] = {}
    for number, raw in enumerate(reader, start=2):
        row = {
            (name.strip().lower() if name is not None else ""): (value or "")
            for name, value in raw.items()
        }
        day = _invoice_date(row[resolved["usage_date"]], line=number)
        spend = DailySpend(
            usage_date=day,
            input_tokens=_invoice_int(row[resolved["input_tokens"]], line=number),
            output_tokens=_invoice_int(row[resolved["output_tokens"]], line=number),
            cache_read_tokens=_invoice_int(row[resolved["cache_read_tokens"]], line=number),
            cache_creation_tokens=_invoice_int(row[resolved["cache_creation_tokens"]], line=number),
            cost_usd=_invoice_decimal(row[resolved["cost_usd"]], line=number),
        )
        existing = by_day.get(day)
        by_day[day] = spend if existing is None else _add_spend(existing, spend)
    return [by_day[day] for day in sorted(by_day)]


def _add_spend(left: DailySpend, right: DailySpend) -> DailySpend:
    return DailySpend(
        usage_date=left.usage_date,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        cache_creation_tokens=left.cache_creation_tokens + right.cache_creation_tokens,
        cost_usd=left.cost_usd + right.cost_usd,
    )


def _invoice_date(value: str, *, line: int) -> date:
    """Read one date cell.

    The console exports a plain ``YYYY-MM-DD``; an export that carries a timestamp is
    truncated to its leading date rather than refused, because the day is the only part
    of it this comparison uses and refusing would send an operator to a spreadsheet.
    """
    text = value.strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        raise InvoiceFormatError(
            f"line {line}: '{value}' is not a date (expected YYYY-MM-DD)"
        ) from None


def _invoice_int(value: str, *, line: int) -> int:
    text = value.strip().replace(",", "")
    if not text:
        return 0
    try:
        # Token counts come back as "1234" or, from a spreadsheet round trip, "1234.0".
        return int(Decimal(text))
    except (ArithmeticError, ValueError):
        raise InvoiceFormatError(f"line {line}: '{value}' is not a whole number") from None


def _invoice_decimal(value: str, *, line: int) -> Decimal:
    text = value.strip().replace(",", "").removeprefix("$")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except ArithmeticError:
        raise InvoiceFormatError(f"line {line}: '{value}' is not an amount") from None


@dataclass(frozen=True, slots=True)
class DayVariance:
    """One day, as we computed it and as Anthropic billed it."""

    usage_date: date
    ours_usd: Decimal
    invoice_usd: Decimal

    @property
    def variance_usd(self) -> Decimal:
        """Ours minus theirs. Positive means we over-estimated our own cost."""
        return self.ours_usd - self.invoice_usd

    @property
    def variance_fraction(self) -> Decimal | None:
        """Variance as a share of the invoice, or ``None`` when the invoice is zero.

        ``None`` rather than infinity or 100%: a day Anthropic did not bill at all and we
        think cost money is not "a large percentage wrong", it is a different question —
        usually a day the export did not cover.
        """
        if self.invoice_usd == 0:
            return None
        return self.variance_usd / self.invoice_usd


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Our computed spend against Anthropic's, day by day and in total."""

    period: BillingPeriod
    days: Sequence[DayVariance]
    tolerance: Decimal = RECONCILIATION_TOLERANCE

    @property
    def ours_usd(self) -> Decimal:
        return sum((day.ours_usd for day in self.days), Decimal(0))

    @property
    def invoice_usd(self) -> Decimal:
        return sum((day.invoice_usd for day in self.days), Decimal(0))

    @property
    def variance_usd(self) -> Decimal:
        return self.ours_usd - self.invoice_usd

    @property
    def variance_fraction(self) -> Decimal | None:
        """Total variance as a share of the invoice, or ``None`` when it billed nothing."""
        if self.invoice_usd == 0:
            return None
        return self.variance_usd / self.invoice_usd

    @property
    def within_tolerance(self) -> bool:
        """Whether the totals agree closely enough to bill from.

        Measured on the *total*, not day by day: a single day can swing on one expensive
        call landing either side of midnight, and a reconciliation that fails for that
        would be ignored within a week. An invoice of zero is only reconciled if we also
        computed zero — otherwise there is nothing to take a percentage of and the answer
        is "look at it".
        """
        fraction = self.variance_fraction
        if fraction is None:
            return self.ours_usd == 0
        return abs(fraction) <= self.tolerance


# ------------------------------------------------------------------------------- ports


@runtime_checkable
class MeteringStorePort(Protocol):
    """Where usage is recomputed from and read back.

    Every tenant-scoped method takes ``tenant_id`` and filters on it (invariant 4). The
    two fleet-wide methods are named for what they are and return their results **keyed by
    tenant or by day**, never as an unattributed total: reconciliation and cost allocation
    genuinely need every tenant at once, and the way to keep that from turning into "a
    metering query with no tenant filter" is for the fleet-wide ones to be impossible to
    mistake for a tenant's own usage at the call site.
    """

    def rollup_day(self, *, tenant_id: str, day: date) -> UsageTotals:
        """Recompute one tenant-day from ``leads`` and ``assessments`` and store it.

        Must be a full replacement of the rollup row, never an increment, so that running
        it twice leaves the same numbers behind.
        """
        ...

    def usage_for_period(self, *, tenant_id: str, period: BillingPeriod) -> UsageTotals:
        """Read the stored rollup for a period. Must not scan ``assessments``."""
        ...

    def daily_usage(self, *, tenant_id: str, period: BillingPeriod) -> Sequence[UsageTotals]:
        """The stored rollup rows for a period, one per day that has one, oldest first."""
        ...

    def quota_for(self, *, tenant_id: str) -> TenantQuota:
        """The tenant's plan allowance.

        Raises:
            MeteringError: no such tenant.
        """
        ...

    def set_quota(
        self, *, tenant_id: str, monthly_lead_quota: int | None, alert_fraction: Decimal
    ) -> TenantQuota:
        """Write a tenant's plan allowance, and return it as stored.

        Raises:
            MeteringError: no such tenant.
        """
        ...

    def fleet_billable_leads(self, *, period: BillingPeriod) -> Mapping[str, int]:
        """Billable leads per tenant across every tenant, for cost allocation."""
        ...

    def fleet_daily_spend(self, *, period: BillingPeriod) -> Sequence[DailySpend]:
        """Token spend per day across every tenant, for invoice reconciliation."""
        ...


@runtime_checkable
class RevenuePort(Protocol):
    """What a tenant was charged for a period. Implemented by #35's Stripe adapter.

    Until that exists, :class:`~leadquali.adapters.revenue_none.UnknownRevenue` answers
    ``None`` to everything, which is the honest answer and the one the margin report
    renders as ``unknown``.
    """

    def revenue_usd(self, *, tenant_id: str, period: BillingPeriod) -> Decimal | None:
        """Recognised revenue in US dollars, or ``None`` if it is not known."""
        ...


# ----------------------------------------------------------------------------- service


class MeteringService:
    """Rollups, usage reads, quota status, margin and reconciliation.

    Args:
        store: where rollups are computed and read.
        clock: injected so "is this day closed?" is testable without waiting for midnight.
        revenue: where revenue comes from; :class:`RevenuePort`.
        logger: where the quota crossing event goes. Defaults to this module's logger.
    """

    def __init__(
        self,
        *,
        store: MeteringStorePort,
        clock: ClockPort,
        revenue: RevenuePort,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._revenue = revenue
        self._logger = logger if logger is not None else LOGGER

    # ------------------------------------------------------------------------ rollups

    def rollup_day(self, *, tenant_id: str, day: date) -> UsageTotals:
        """Recompute one UTC day for one tenant and replace its rollup row.

        Idempotent by construction: the store recomputes the day from ``leads`` and
        ``assessments`` and writes the result as a whole row, so a second run produces the
        same numbers. Re-running yesterday after a late SQS redelivery is therefore the
        normal way to correct a day, not an exceptional one.

        Rolling up *today* is allowed — an admin view wants the partial day — and the
        result says so in :attr:`UsageTotals.partial`.
        """
        totals = self._store.rollup_day(tenant_id=tenant_id, day=day)
        return replace(totals, partial=day >= self.today())

    def rollup_range(
        self,
        *,
        tenant_id: str,
        start: date,
        end: date,
        closed_days_only: bool = True,
    ) -> Sequence[UsageTotals]:
        """Recompute every day in ``start..end`` inclusive, oldest first.

        One statement per day rather than one clever statement over the range. It is the
        same number of index scans, it keeps :meth:`rollup_day` as the only place the
        counting rule is written, and a failure half way leaves the days it did finish
        correctly rolled up instead of rolling back a fortnight of work.

        Args:
            closed_days_only: skip days that are not over yet. Defaults to ``True``,
                because the caller that gets this wrong is a billing job and the failure
                is an invoice from a partial day.
        """
        period = BillingPeriod(start=start, end=end)
        if closed_days_only:
            closed = period.closed_as_of(today=self.today())
            if closed is None:
                return []
            period = closed
        return [self.rollup_day(tenant_id=tenant_id, day=day) for day in period.dates()]

    # --------------------------------------------------------------------------- reads

    def usage_for_period(
        self, *, tenant_id: str, period: BillingPeriod, closed_days_only: bool = True
    ) -> UsageTotals:
        """Billable usage for one tenant and period, read from the rollup table.

        This is the acceptance criterion "a single query returns billable usage for any
        tenant and period", and it is a single query *against the rollup*: it does not
        touch ``assessments``, so it costs the same in month one and in year three.

        Args:
            closed_days_only: exclude a day that is still running. Defaults to ``True``;
                an admin screen that wants today's partial figures passes ``False``.
        """
        today = self.today()
        effective = period
        if closed_days_only:
            closed = period.closed_as_of(today=today)
            if closed is None:
                return UsageTotals.zero(tenant_id=tenant_id, period=period)
            effective = closed
        totals = self._store.usage_for_period(tenant_id=tenant_id, period=effective)
        return replace(totals, partial=effective.includes_open_day(today=today))

    def daily_usage(self, *, tenant_id: str, period: BillingPeriod) -> Sequence[UsageTotals]:
        """The stored rollup rows for a period, for a day-by-day view. Never recomputes."""
        return self._store.daily_usage(tenant_id=tenant_id, period=period)

    def quota_status(
        self, *, tenant_id: str, period: BillingPeriod, closed_days_only: bool = False
    ) -> QuotaStatus:
        """How much of its plan a tenant has used, and whether anyone should be told.

        **This never stops anything.** Crossing a quota emits a log event and a CloudWatch
        metric so that a person can have a commercial conversation; it does not refuse an
        ingest, skip an assessment or downgrade a lead. Invariant 3 says a lead is never
        silently dropped, and "the customer was over their plan" is not an exception to it.

        Args:
            closed_days_only: defaults to ``False`` here, unlike the billing reads — an
                alert is about what is happening now, and excluding today would make a
                tenant that blew through its plan this morning look fine until midnight.
        """
        usage = self.usage_for_period(
            tenant_id=tenant_id, period=period, closed_days_only=closed_days_only
        )
        quota = self._store.quota_for(tenant_id=tenant_id)
        status = QuotaStatus.evaluate(
            tenant_id=tenant_id,
            period=period,
            used=usage.leads_billable,
            quota=quota,
            partial=usage.partial,
        )
        if status.level is not QuotaLevel.OK and status.quota is not None:
            fraction = status.fraction
            log_quota_crossed(
                self._logger,
                tenant_id=tenant_id,
                period=str(period),
                used=status.used,
                quota=status.quota,
                fraction=fraction if fraction is not None else Decimal(0),
                level=status.level.value,
            )
        return status

    def set_quota(
        self,
        *,
        tenant_id: str,
        monthly_lead_quota: int | None,
        alert_fraction: Decimal = DEFAULT_QUOTA_ALERT_FRACTION,
    ) -> TenantQuota:
        """Put a tenant on a plan, or take them off one.

        ``monthly_lead_quota=None`` means unlimited, which is where every tenant starts.
        The two bounds the database enforces are checked here as well, so an operator gets
        a sentence rather than a constraint violation: a quota of zero is a suspension and
        there is a status column for that, and an alert fraction outside (0, 1] either
        fires on the first lead of every month or can never fire at all.

        Raises:
            MeteringError: the numbers are not a usable plan, or there is no such tenant.
        """
        if monthly_lead_quota is not None and monthly_lead_quota <= 0:
            raise MeteringError(
                f"a monthly quota of {monthly_lead_quota} is not a plan; pass a positive "
                "number, or omit it for unlimited. To stop a tenant ingesting, suspend "
                "them with tenantctl instead."
            )
        if not Decimal(0) < alert_fraction <= Decimal(1):
            raise MeteringError(
                f"an alert fraction of {alert_fraction} is outside (0, 1]: at 0 it would "
                "fire on the first lead of every month, and above 1 it could never fire"
            )
        return self._store.set_quota(
            tenant_id=tenant_id,
            monthly_lead_quota=monthly_lead_quota,
            alert_fraction=alert_fraction,
        )

    # -------------------------------------------------------------------------- margin

    def margin(
        self, *, tenant_id: str, period: BillingPeriod, closed_days_only: bool = True
    ) -> MarginReport:
        """Revenue minus inference and allocated infrastructure cost, for one tenant.

        Infrastructure is allocated pro rata by billable leads across every tenant in the
        period. That is a convention, not a measurement — see :func:`infrastructure_usd_for`
        and :attr:`MarginReport.allocation_caveat`, both of which say so, and both of which
        have to keep saying so wherever this number is rendered.
        """
        usage = self.usage_for_period(
            tenant_id=tenant_id, period=period, closed_days_only=closed_days_only
        )
        fleet = self._store.fleet_billable_leads(period=usage.period)
        fleet_total = sum(fleet.values())
        share = (
            Decimal(usage.leads_billable) / Decimal(fleet_total) if fleet_total > 0 else Decimal(0)
        )
        return MarginReport(
            usage=usage,
            revenue_usd=self._revenue.revenue_usd(tenant_id=tenant_id, period=usage.period),
            inference_usd=usage.cost_usd,
            infrastructure_usd=infrastructure_usd_for(usage.period) * share,
            fleet_billable_leads=fleet_total,
        )

    # ------------------------------------------------------------------- reconciliation

    def reconcile(
        self, *, invoice: Sequence[DailySpend], period: BillingPeriod
    ) -> ReconciliationReport:
        """Compare our fleet-wide computed spend against an Anthropic export.

        Across all tenants, because that is what the invoice covers: Anthropic bills the
        workspace, not the customer. Days present on one side only appear with the other
        side at zero rather than being dropped — a day the export skipped and a day we have
        no rollup for are both things somebody needs to see.

        Args:
            invoice: the parsed export; see :func:`parse_invoice_csv`.
            period: the range to compare. Invoice rows outside it are ignored, so a
                whole-year export can be reconciled one month at a time.
        """
        ours = {
            day.usage_date: day.cost_usd
            for day in self._store.fleet_daily_spend(period=period)
            if period.contains(day.usage_date)
        }
        theirs = {
            day.usage_date: day.cost_usd for day in invoice if period.contains(day.usage_date)
        }
        days = [
            DayVariance(
                usage_date=day,
                ours_usd=ours.get(day, Decimal(0)),
                invoice_usd=theirs.get(day, Decimal(0)),
            )
            for day in sorted(set(ours) | set(theirs))
        ]
        return ReconciliationReport(period=period, days=days)

    # ----------------------------------------------------------------------- internals

    def today(self) -> date:
        """Today in UTC — the only timezone a usage day is ever expressed in.

        Public because the CLI needs the same answer the service uses when it decides
        which days are closed: a command that defaulted its period from the *host's* local
        date would ask for a month the service then clipped differently.
        """
        return self._clock.now().date()
