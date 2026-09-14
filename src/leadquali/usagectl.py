"""``python -m leadquali.usagectl`` — metering, quotas, margin and reconciliation.

Six commands over :class:`~leadquali.app.metering.MeteringService`, and between them they
are the whole of #33's operable surface:

    python -m leadquali.usagectl rollup acme-demo --month 2026-09
    python -m leadquali.usagectl usage acme-demo --month 2026-09 --json
    python -m leadquali.usagectl quota acme-demo
    python -m leadquali.usagectl set-quota acme-demo --quota 2000
    python -m leadquali.usagectl margin acme-demo --month 2026-09
    python -m leadquali.usagectl reconcile anthropic-september.csv --month 2026-09

``docs/metering-and-billing.md`` is the runbook these belong to.

Four behaviours are load-bearing rather than cosmetic.

* **A day that is not over is never billed by accident.** Every read defaults to closed
  days only and says ``(partial)`` in as many words when it is asked for today's figures
  with ``--include-today``. The billing job does not have to remember to subtract a day.
* **Money is rendered as a decimal string, in JSON too.** ``"0.213450"``, not ``0.21345``.
  A JSON number is a double by the time anything else has parsed it, and a billing figure
  that has been through binary floating point cannot be reconciled with one that has not.
* **``reconcile`` exits 1 when the variance is outside tolerance**, so it can be run from
  a cron and noticed. It is the one command whose exit code is a *finding* rather than a
  failure, and that is said on the line it prints.
* **Every dependency is injected.** ``main`` takes a factory, so the tests drive the real
  argument parsing, the real service and the real output formatting with no Postgres.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, TextIO

from leadquali.app.metering import (
    DEFAULT_QUOTA_ALERT_FRACTION,
    RECONCILIATION_TOLERANCE,
    BillingPeriod,
    DayVariance,
    MarginReport,
    MeteringError,
    MeteringService,
    QuotaLevel,
    QuotaStatus,
    ReconciliationReport,
    UsageTotals,
    parse_invoice_csv,
)
from leadquali.config import Settings, get_settings
from leadquali.observability import configure_logging

__all__ = [
    "EXIT_FAILED",
    "EXIT_INPUT_ERROR",
    "EXIT_OUT_OF_TOLERANCE",
    "ServiceFactory",
    "build_parser",
    "main",
]

#: A command that was understood and could not be carried out: no such tenant, a period
#: that does not parse, an unreadable export.
EXIT_FAILED: Final[int] = 1

#: A usage or input problem. The same code ``cli.py`` and ``tenantctl.py`` reserve for it.
EXIT_INPUT_ERROR: Final[int] = 2

#: ``reconcile`` ran fine and found a variance outside tolerance. Deliberately the same
#: code as :data:`EXIT_FAILED`: to a cron there is no useful difference between "the
#: reconciliation could not be done" and "the reconciliation says the numbers disagree" —
#: both mean a person has to look before anybody bills from them.
EXIT_OUT_OF_TOLERANCE: Final[int] = EXIT_FAILED

ServiceFactory = Callable[[Settings], MeteringService]
"""How ``main`` gets a service. Injected so the tests never touch Postgres."""

#: Commands that are not about a period at all. A plan is a property of the tenant, not of
#: a month, so ``set-quota`` takes no ``--month`` and is not given one.
PERIODLESS_COMMANDS: Final[frozenset[str]] = frozenset({"set-quota"})


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m leadquali.usagectl",
        description="Meter tenant usage, check quotas and margin, reconcile Anthropic spend.",
    )
    commands = parser.add_subparsers(dest="command")

    rollup = commands.add_parser(
        "rollup",
        help="Recompute usage_daily for a tenant over a period. Idempotent; safe to re-run.",
    )
    rollup.add_argument("slug", help="The tenant.")
    _add_period(rollup)
    _add_include_today(rollup)

    usage = commands.add_parser("usage", help="Billable usage for a tenant and period.")
    usage.add_argument("slug")
    _add_period(usage)
    _add_include_today(usage)
    usage.add_argument(
        "--daily",
        action="store_true",
        help="Break the period down by day instead of totalling it.",
    )
    _add_json(usage)

    quota = commands.add_parser("quota", help="A tenant's usage against its plan.")
    quota.add_argument("slug")
    _add_period(quota)
    _add_json(quota)

    set_quota = commands.add_parser(
        "set-quota",
        help="Put a tenant on a plan, or take them off one. Never blocks anything.",
    )
    set_quota.add_argument("slug")
    set_quota.add_argument(
        "--quota",
        type=int,
        default=None,
        help="Billable leads included per month. Omit (or pass --unlimited) for no limit.",
    )
    set_quota.add_argument(
        "--unlimited",
        action="store_true",
        help="Remove the tenant's allowance. The default state for every tenant.",
    )
    set_quota.add_argument(
        "--alert-fraction",
        type=Decimal,
        default=None,
        help=(
            "How much of the allowance may be used before the warning fires, in (0, 1] "
            f"(default: {DEFAULT_QUOTA_ALERT_FRACTION})."
        ),
    )

    margin = commands.add_parser(
        "margin", help="Revenue minus inference and allocated infrastructure cost."
    )
    margin.add_argument("slug")
    _add_period(margin)
    _add_include_today(margin)
    _add_json(margin)

    reconcile = commands.add_parser(
        "reconcile",
        help="Compare our computed spend against an Anthropic console export (all tenants).",
    )
    reconcile.add_argument(
        "invoice",
        type=Path,
        help="CSV exported from the Anthropic console: date, tokens and cost per day.",
    )
    _add_period(reconcile)
    reconcile.add_argument(
        "--tolerance",
        type=Decimal,
        default=RECONCILIATION_TOLERANCE,
        help=(
            "Fractional variance to accept "
            f"(default: {RECONCILIATION_TOLERANCE}, i.e. {RECONCILIATION_TOLERANCE:.0%})."
        ),
    )
    _add_json(reconcile)

    return parser


def _add_period(parser: argparse.ArgumentParser) -> None:
    """``--month``, or ``--from``/``--to``; the current UTC month by default."""
    parser.add_argument("--month", default=None, help="A whole calendar month, as YYYY-MM.")
    parser.add_argument(
        "--from",
        dest="since",
        default=None,
        help="First day of the period, as YYYY-MM-DD. Requires --to.",
    )
    parser.add_argument(
        "--to",
        dest="until",
        default=None,
        help="Last day of the period, inclusive, as YYYY-MM-DD. Requires --from.",
    )


def _add_include_today(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-today",
        action="store_true",
        help=(
            "Include the day that is still running. Its figures can still grow, so they "
            "are marked partial and must not be invoiced from."
        ),
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON document instead of a human-readable report.",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: ServiceFactory | None = None,
    settings: Settings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one command. Returns the process exit code.

    Args:
        argv: Arguments, without the program name. ``None`` reads ``sys.argv``.
        service_factory: Builds the :class:`~leadquali.app.metering.MeteringService`.
            ``None`` wires the real Postgres store, the system clock and the
            "revenue is unknown" adapter.
        settings: Configuration to build from. ``None`` reads the process settings.
        stdout: Where reports and JSON go. ``None`` is the process's stdout, forced to
            UTF-8 so a tenant name with an umlaut cannot crash the command.
        stderr: Where warnings and errors go.

    Returns:
        ``0``, :data:`EXIT_FAILED` (which ``reconcile`` also uses for a variance outside
        tolerance) or :data:`EXIT_INPUT_ERROR`.
    """
    # Logs go to stderr, never stdout: `--json` is parsed by something, and one
    # interleaved log line would break it.
    configure_logging(stream=sys.stderr)
    out = stdout if stdout is not None else _utf8(sys.stdout)
    err = stderr if stderr is not None else _utf8(sys.stderr)

    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command is None:
        parser.print_help(out)
        return EXIT_INPUT_ERROR

    try:
        service = (service_factory or _default_service_factory)(
            settings if settings is not None else get_settings()
        )
    except RuntimeError as error:  # a missing DATABASE_URL, most likely
        print(f"usagectl: {error}", file=err)
        return EXIT_INPUT_ERROR

    period: BillingPeriod | None = None
    if args.command not in PERIODLESS_COMMANDS:
        try:
            period = _period(args, today=service.today())
        except ValueError as error:
            print(f"usagectl: {error}", file=err)
            return EXIT_INPUT_ERROR

    try:
        return _dispatch(args, service, period, out, err)
    except MeteringError as error:
        print(f"usagectl: {error}", file=err)
        return EXIT_FAILED
    except OSError as error:
        print(f"usagectl: {error}", file=err)
        return EXIT_INPUT_ERROR


def _dispatch(
    args: argparse.Namespace,
    service: MeteringService,
    period: BillingPeriod | None,
    out: TextIO,
    err: TextIO,
) -> int:
    """Run the parsed command. Errors propagate to :func:`main`, which renders them.

    ``period`` is ``None`` only for the commands in :data:`PERIODLESS_COMMANDS`; every
    branch that needs one asks :func:`_over` for it, which is the compiler's assurance
    rather than a runtime branch anybody can reach.
    """
    match args.command:
        case "rollup":
            days = service.rollup_range(
                tenant_id=args.slug,
                start=_over(period).start,
                end=_over(period).end,
                closed_days_only=not args.include_today,
            )
            if not days:
                print(
                    f"nothing to roll up for {args.slug} in {_over(period)}: no day in it "
                    "is over yet. Pass --include-today to meter the day in progress.",
                    file=err,
                )
                return 0
            for totals in days:
                print(_rollup_line(totals), file=out)
            print(f"rolled up {len(days)} day(s) for {args.slug}", file=err)
            return 0

        case "usage":
            if args.daily:
                rows = service.daily_usage(tenant_id=args.slug, period=_over(period))
                if args.as_json:
                    print(json.dumps([_usage_json(row) for row in rows], indent=2), file=out)
                else:
                    _print_daily(rows, _over(period), out)
                return 0
            totals = service.usage_for_period(
                tenant_id=args.slug,
                period=_over(period),
                closed_days_only=not args.include_today,
            )
            if args.as_json:
                print(json.dumps(_usage_json(totals), indent=2), file=out)
            else:
                _print_usage(totals, out)
            return 0

        case "quota":
            status = service.quota_status(tenant_id=args.slug, period=_over(period))
            if args.as_json:
                print(json.dumps(_quota_json(status), indent=2), file=out)
            else:
                _print_quota(status, out)
            if status.level is not QuotaLevel.OK:
                print(
                    "nothing has been blocked and nothing will be: a quota is a billing "
                    "conversation, not a switch. Every lead is still qualified.",
                    file=err,
                )
            return 0

        case "set-quota":
            if args.quota is not None and args.unlimited:
                print("usagectl: pass either --quota or --unlimited, not both", file=err)
                return EXIT_INPUT_ERROR
            fraction = (
                args.alert_fraction
                if args.alert_fraction is not None
                else DEFAULT_QUOTA_ALERT_FRACTION
            )
            written = service.set_quota(
                tenant_id=args.slug,
                monthly_lead_quota=None if args.unlimited else args.quota,
                alert_fraction=fraction,
            )
            allowance = (
                "unlimited"
                if written.monthly_lead_quota is None
                else f"{written.monthly_lead_quota} billable leads/month"
            )
            print(f"{args.slug}: {allowance}, alert at {written.alert_fraction:.0%}", file=out)
            print(
                "this is a reporting threshold only: no lead is ever refused, skipped or "
                "downgraded because of it.",
                file=err,
            )
            return 0

        case "margin":
            report = service.margin(
                tenant_id=args.slug,
                period=_over(period),
                closed_days_only=not args.include_today,
            )
            if args.as_json:
                print(json.dumps(_margin_json(report), indent=2), file=out)
            else:
                _print_margin(report, out)
            print(report.allocation_caveat, file=err)
            return 0

        case "reconcile":
            invoice = parse_invoice_csv(_read_text(args.invoice))
            computed = service.reconcile(invoice=invoice, period=_over(period))
            # The service applies the standing tolerance; --tolerance replaces it for this
            # run only, which is what makes a deliberate one-off ("we changed the rate card
            # mid-month, accept 5% this once") an argument rather than an edit.
            variance = ReconciliationReport(
                period=computed.period, days=computed.days, tolerance=args.tolerance
            )
            if args.as_json:
                print(json.dumps(_reconcile_json(variance), indent=2), file=out)
            else:
                _print_reconciliation(variance, out)
            if variance.within_tolerance:
                return 0
            print(
                "variance is outside tolerance. Do not bill from these figures until the "
                "difference is explained; see docs/metering-and-billing.md.",
                file=err,
            )
            return EXIT_OUT_OF_TOLERANCE

    # argparse only produces the commands above; this is the compiler's assurance, not a
    # runtime branch anyone can reach.
    raise AssertionError(f"unhandled command {args.command!r}")


# ------------------------------------------------------------------------------ periods


def _over(period: BillingPeriod | None) -> BillingPeriod:
    """The period a command was given.

    Every command but the ones in :data:`PERIODLESS_COMMANDS` is handed one by ``main``,
    so ``None`` here is a command that grew a period argument without telling ``main``
    about it — a programming error, and one worth failing loudly rather than defaulting to
    a month somebody did not ask for and might invoice from.
    """
    if period is None:
        raise AssertionError("this command needs a period; see PERIODLESS_COMMANDS")
    return period


def _period(args: argparse.Namespace, *, today: date) -> BillingPeriod:
    """The period a command was asked for.

    Defaults to the current UTC month, which is what makes ``usagectl rollup acme`` a
    sensible thing to run from a daily cron: it recomputes every closed day of the month
    so far, so a day that was missed while the job was broken is repaired by the next run
    rather than needing a bespoke backfill.

    Raises:
        ValueError: the arguments do not describe a period.
    """
    if args.month is not None and (args.since is not None or args.until is not None):
        raise ValueError("pass either --month or --from/--to, not both")
    if args.month is not None:
        return BillingPeriod.parse_month(args.month)
    if args.since is None and args.until is None:
        return BillingPeriod.of_month(today.year, today.month)
    if args.since is None or args.until is None:
        raise ValueError("--from and --to go together; pass both or pass --month")
    start, end = _day(args.since), _day(args.until)
    if end < start:
        raise ValueError(f"--from {start} is after --to {end}")
    return BillingPeriod(start=start, end=end)


def _day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"'{text}' is not a date; expected YYYY-MM-DD") from None


# ------------------------------------------------------------------------------- output


def _money(value: Decimal | None, *, places: str = "0.000001") -> str:
    """Render an amount at the scale the database stores, or ``unknown``."""
    return "unknown" if value is None else str(value.quantize(Decimal(places)))


def _percent(value: Decimal | None) -> str:
    return "unknown" if value is None else f"{value:.2%}"


def _rollup_line(totals: UsageTotals) -> str:
    partial = "  (partial)" if totals.partial else ""
    return (
        f"{totals.period.start}  ingested {totals.leads_ingested:>6}  "
        f"assessed {totals.leads_assessed:>6}  billable {totals.leads_billable:>6}  "
        f"cost ${_money(totals.cost_usd, places='0.0001')}{partial}"
    )


def _print_usage(totals: UsageTotals, out: TextIO) -> None:
    print(f"tenant:            {totals.tenant_id}", file=out)
    print(f"period:            {totals.period}{'  (partial)' if totals.partial else ''}", file=out)
    print(f"leads ingested:    {totals.leads_ingested}", file=out)
    print(f"  pre-filtered:    {totals.leads_filtered}  (not billable)", file=out)
    print(f"leads assessed:    {totals.leads_assessed}", file=out)
    print(f"  of which failed: {totals.assessments_failed}", file=out)
    print(f"leads billable:    {totals.leads_billable}", file=out)
    print(f"tokens in/out:     {totals.input_tokens} / {totals.output_tokens}", file=out)
    print(
        f"tokens cached:     {totals.cache_read_tokens} read / "
        f"{totals.cache_creation_tokens} written",
        file=out,
    )
    print(f"inference cost:    ${_money(totals.cost_usd)}", file=out)
    print(f"  per billable:    ${_money(totals.cost_per_billable_lead_usd)}", file=out)
    if totals.computed_at is not None:
        print(f"rolled up at:      {totals.computed_at.isoformat()}", file=out)


def _print_daily(rows: Sequence[UsageTotals], period: BillingPeriod, out: TextIO) -> None:
    if not rows:
        print(f"(no rollup rows for {period}; run `usagectl rollup` first)", file=out)
        return
    for totals in rows:
        print(_rollup_line(totals), file=out)


def _print_quota(status: QuotaStatus, out: TextIO) -> None:
    print(f"tenant:   {status.tenant_id}", file=out)
    print(f"period:   {status.period}{'  (in progress)' if status.partial else ''}", file=out)
    print(f"used:     {status.used} billable leads", file=out)
    if status.quota is None:
        print("quota:    unlimited", file=out)
        print("status:   ok", file=out)
        return
    print(f"quota:    {status.quota}  (alert at {_percent(status.alert_fraction)})", file=out)
    print(f"used:     {_percent(status.fraction)} of plan", file=out)
    print(f"left:     {status.remaining}", file=out)
    print(f"status:   {status.level.value}", file=out)


def _print_margin(report: MarginReport, out: TextIO) -> None:
    usage = report.usage
    print(f"tenant:            {report.tenant_id}", file=out)
    print(f"period:            {report.period}{'  (partial)' if usage.partial else ''}", file=out)
    print(
        f"billable leads:    {usage.leads_billable} of {report.fleet_billable_leads} fleet-wide",
        file=out,
    )
    print(f"revenue:           {_money_or_unknown(report.revenue_usd)}", file=out)
    print(f"inference cost:    ${_money(report.inference_usd)}", file=out)
    print(
        f"infrastructure:    ${_money(report.infrastructure_usd)}  (allocated, not measured)",
        file=out,
    )
    print(f"total cost:        ${_money(report.cost_usd)}", file=out)
    print(f"margin:            {_money_or_unknown(report.margin_usd)}", file=out)
    print(f"margin %:          {_percent(report.margin_fraction)}", file=out)


def _money_or_unknown(value: Decimal | None) -> str:
    return "unknown" if value is None else f"${_money(value)}"


def _print_reconciliation(report: ReconciliationReport, out: TextIO) -> None:
    print(f"period:   {report.period}  (all tenants, as Anthropic bills it)", file=out)
    print(f"{'date':<12}{'ours':>14}{'invoice':>14}{'variance':>14}{'':>4}%", file=out)
    for day in report.days:
        print(
            f"{day.usage_date!s:<12}{_money(day.ours_usd, places='0.0001'):>14}"
            f"{_money(day.invoice_usd, places='0.0001'):>14}"
            f"{_money(day.variance_usd, places='0.0001'):>14}"
            f"{_percent(day.variance_fraction):>9}",
            file=out,
        )
    print(
        f"{'total':<12}{_money(report.ours_usd, places='0.0001'):>14}"
        f"{_money(report.invoice_usd, places='0.0001'):>14}"
        f"{_money(report.variance_usd, places='0.0001'):>14}"
        f"{_percent(report.variance_fraction):>9}",
        file=out,
    )
    verdict = "within" if report.within_tolerance else "OUTSIDE"
    print(f"{verdict} tolerance of {_percent(report.tolerance)}", file=out)


# --------------------------------------------------------------------------------- JSON


def _period_json(period: BillingPeriod) -> dict[str, Any]:
    return {"start": str(period.start), "end": str(period.end), "days": period.days}


def _usage_json(totals: UsageTotals) -> dict[str, Any]:
    """One usage total as JSON.

    Every money figure is a **string**, not a JSON number. Whatever reads this will parse a
    number as a double, and a billing figure that has been through binary floating point
    can no longer be reconciled against one that has not — which is the entire point of
    ``cost_usd`` being ``numeric`` in the database and ``Decimal`` in the code.
    """
    return {
        "tenant_id": totals.tenant_id,
        "period": _period_json(totals.period),
        "partial": totals.partial,
        "leads_ingested": totals.leads_ingested,
        "leads_filtered": totals.leads_filtered,
        "leads_assessed": totals.leads_assessed,
        "leads_billable": totals.leads_billable,
        "assessments_failed": totals.assessments_failed,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_tokens": totals.cache_read_tokens,
        "cache_creation_tokens": totals.cache_creation_tokens,
        "cost_usd": str(totals.cost_usd),
        "cost_per_billable_lead_usd": (
            None
            if totals.cost_per_billable_lead_usd is None
            else str(totals.cost_per_billable_lead_usd)
        ),
        "computed_at": None if totals.computed_at is None else totals.computed_at.isoformat(),
    }


def _quota_json(status: QuotaStatus) -> dict[str, Any]:
    return {
        "tenant_id": status.tenant_id,
        "period": _period_json(status.period),
        "partial": status.partial,
        "used": status.used,
        "quota": status.quota,
        "remaining": status.remaining,
        "fraction": None if status.fraction is None else str(status.fraction),
        "alert_fraction": str(status.alert_fraction),
        "level": status.level.value,
        "enforced": False,
    }


def _margin_json(report: MarginReport) -> dict[str, Any]:
    return {
        "tenant_id": report.tenant_id,
        "period": _period_json(report.period),
        "revenue_usd": None if report.revenue_usd is None else str(report.revenue_usd),
        "inference_usd": str(report.inference_usd),
        "infrastructure_usd": str(report.infrastructure_usd),
        "cost_usd": str(report.cost_usd),
        "margin_usd": None if report.margin_usd is None else str(report.margin_usd),
        "margin_fraction": (
            None if report.margin_fraction is None else str(report.margin_fraction)
        ),
        "fleet_billable_leads": report.fleet_billable_leads,
        "infrastructure_allocation": report.allocation_caveat,
        "usage": _usage_json(report.usage),
    }


def _day_variance_json(day: DayVariance) -> dict[str, Any]:
    return {
        "date": str(day.usage_date),
        "ours_usd": str(day.ours_usd),
        "invoice_usd": str(day.invoice_usd),
        "variance_usd": str(day.variance_usd),
        "variance_fraction": (
            None if day.variance_fraction is None else str(day.variance_fraction)
        ),
    }


def _reconcile_json(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "period": _period_json(report.period),
        "tolerance": str(report.tolerance),
        "ours_usd": str(report.ours_usd),
        "invoice_usd": str(report.invoice_usd),
        "variance_usd": str(report.variance_usd),
        "variance_fraction": (
            None if report.variance_fraction is None else str(report.variance_fraction)
        ),
        "within_tolerance": report.within_tolerance,
        "days": [_day_variance_json(day) for day in report.days],
    }


# ---------------------------------------------------------------------------------- IO


def _read_text(path: Path) -> str:
    """Read a CSV export.

    Raises:
        OSError: the file is missing or unreadable, with the path in the message rather
            than a bare errno.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise OSError(f"no such file: {path}") from None


def _utf8(stream: TextIO) -> TextIO:
    """Force UTF-8 on a standard stream where the platform allows it.

    Same reason as ``tenantctl``: a Windows console's default code page would otherwise
    raise ``UnicodeEncodeError`` on a report containing a tenant name with an umlaut.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")
    return stream


def _default_service_factory(settings: Settings) -> MeteringService:
    """Wire the real store, the system clock and the "revenue is unknown" adapter.

    Imported inside the function, not at module scope, so that ``import
    leadquali.usagectl`` — which the tests do — pulls in neither SQLAlchemy's engine
    machinery nor a database connection.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.metering_postgres import PostgresMeteringStore
    from leadquali.adapters.revenue_none import UnknownRevenue

    return MeteringService(
        store=PostgresMeteringStore.from_env(settings),
        clock=SystemClock(),
        revenue=UnknownRevenue(),
    )


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m leadquali.usagectl`
    raise SystemExit(main())
