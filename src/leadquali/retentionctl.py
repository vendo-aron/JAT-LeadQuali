"""``python -m leadquali.retentionctl`` — the retention job and deletion requests by hand.

Four commands over :class:`~leadquali.app.retention.RetentionService`:

    python -m leadquali.retentionctl purge --apply
    python -m leadquali.retentionctl purge acme-demo --batch-size 100 --apply
    python -m leadquali.retentionctl policy acme-demo --raw-days 30 --apply
    python -m leadquali.retentionctl find acme-demo --email someone@example.com
    python -m leadquali.retentionctl erase acme-demo --email someone@example.com \\
        --requested-by SUP-4471 --apply --receipt receipt.txt

``docs/data-retention-policy.md`` and ``docs/deletion-requests.md`` are the runbooks these
belong to. The scheduled half of the same work is the Lambda in
:mod:`leadquali.api.retention`; this exists because the moment you actually need it is an
incident, and "wait for tonight's schedule" is not an answer to a regulator's clock.

Four behaviours are load-bearing rather than cosmetic.

* **Dry run is the default.** Neither ``--apply`` nor ``--dry-run`` means dry, for every
  command that writes. This is the one tool in the system whose entire purpose is
  destroying customer data, and the behaviour you want when the flag is missing is the one
  that changes nothing. ``--dry-run`` exists anyway, so a runbook can say it out loud.
* **The address is an input and never an output.** ``erase`` and ``find`` take
  ``--email``; nothing they print, log or write contains it. What comes back is the
  SHA-256, which is what the audit row holds and what a controller can recompute.
* **A receipt can be written to a file.** ``--receipt`` writes the rendered receipt, which
  is the artifact the requester is shown. It is written *after* the erasure has committed,
  so a file that exists describes something that happened.
* **Every dependency is injected.** ``main`` takes a factory, so the tests drive the real
  argument parsing, the real service and the real output formatting with no Postgres.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

from leadquali.app.retention import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    PurgedRecords,
    PurgeReport,
    RetentionError,
    RetentionService,
    SubjectFootprint,
)
from leadquali.config import Settings, get_settings
from leadquali.observability import configure_logging

__all__ = [
    "EXIT_FAILED",
    "EXIT_INPUT_ERROR",
    "ServiceFactory",
    "build_parser",
    "main",
]

#: A command that was understood and could not be carried out: no such tenant, a window the
#: database refused, an erasure that was rolled back.
EXIT_FAILED: Final[int] = 1

#: A usage or input problem. The same code ``cli.py``, ``tenantctl.py`` and ``usagectl.py``
#: reserve for it.
EXIT_INPUT_ERROR: Final[int] = 2

ServiceFactory = Callable[[Settings], RetentionService]


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m leadquali.retentionctl",
        description="Run the retention purge, and carry out deletion requests.",
    )
    commands = parser.add_subparsers(dest="command")

    purge = commands.add_parser(
        "purge",
        help="Redact expired payloads and delete leads past the assessment window.",
        description=(
            "Runs both retention tiers. With no tenant it runs for every tenant, which is "
            "what the nightly schedule does."
        ),
    )
    purge.add_argument("slug", nargs="?", default=None, help="One tenant, or all of them.")
    purge.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"Rows per transaction (default: {DEFAULT_BATCH_SIZE}, maximum "
            f"{MAX_BATCH_SIZE}). The job loops until it drains; this bounds one statement."
        ),
    )
    _add_apply(purge)
    _add_json(purge)

    policy = commands.add_parser(
        "policy", help="Show, or change, a tenant's two retention windows."
    )
    policy.add_argument("slug", help="The tenant.")
    policy.add_argument(
        "--raw-days",
        type=int,
        default=None,
        help="New tier-1 window: how long the lead payload is kept.",
    )
    policy.add_argument(
        "--assessment-days",
        type=int,
        default=None,
        help="New tier-2 window: how long the lead row and its children are kept.",
    )
    _add_apply(policy)
    _add_json(policy)

    find = commands.add_parser(
        "find",
        help="What is held about one person, without deleting any of it.",
        description=(
            "Always read-only. Run this first: it is what you answer a controller with, "
            "and when it finds nothing that is itself the answer."
        ),
    )
    find.add_argument("slug", help="The tenant whose data to search.")
    find.add_argument("--email", required=True, help="The subject's address. Never printed.")
    _add_json(find)

    erase = commands.add_parser(
        "erase", help="Carry out a deletion request for one person, for one tenant."
    )
    erase.add_argument("slug", help="The tenant whose copy of the data to erase.")
    erase.add_argument("--email", required=True, help="The subject's address. Never printed.")
    erase.add_argument(
        "--requested-by",
        required=True,
        help="Who asked and how it was verified — a ticket reference, not the subject.",
    )
    erase.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Write the rendered receipt here, after the erasure has committed.",
    )
    _add_apply(erase)
    _add_json(erase)
    return parser


def _add_apply(parser: argparse.ArgumentParser) -> None:
    """``--apply`` / ``--dry-run``, with dry as the answer when neither is given."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it this command reports and changes nothing.",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Report without writing. The default, stated explicitly for a runbook.",
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON object instead of a human report.",
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
        service_factory: Builds the :class:`~leadquali.app.retention.RetentionService`.
            ``None`` wires the real Postgres store and the system clock.
        settings: Configuration to build from. ``None`` reads the process settings.
        stdout: Where reports and JSON go. ``None`` is the process's stdout, forced to
            UTF-8 so a tenant name with an umlaut cannot crash the command.
        stderr: Where warnings and errors go.

    Returns:
        ``0``, :data:`EXIT_FAILED` or :data:`EXIT_INPUT_ERROR`.
    """
    # Logs go to stderr, never stdout: `--json` is parsed by something, and one interleaved
    # log line would break it. It matters more here than elsewhere, because the log line
    # this command produces is half the audit trail.
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
        print(f"retentionctl: {error}", file=err)
        return EXIT_INPUT_ERROR

    try:
        return _dispatch(args, service, out, err)
    except RetentionError as error:
        print(f"retentionctl: {error}", file=err)
        return EXIT_FAILED
    except ValueError as error:
        print(f"retentionctl: {error}", file=err)
        return EXIT_INPUT_ERROR
    except OSError as error:
        print(f"retentionctl: {error}", file=err)
        return EXIT_INPUT_ERROR


def _dispatch(args: argparse.Namespace, service: RetentionService, out: TextIO, err: TextIO) -> int:
    """Run the parsed command. Errors propagate to :func:`main`, which renders them."""
    match args.command:
        case "purge":
            return _purge(args, service, out)
        case "policy":
            return _policy(args, service, out)
        case "find":
            return _find(args, service, out)
        case "erase":
            return _erase(args, service, out, err)
        case _:  # pragma: no cover - argparse rejects an unknown subcommand first
            raise ValueError(f"unknown command {args.command!r}")


def _purge(args: argparse.Namespace, service: RetentionService, out: TextIO) -> int:
    """Run both tiers, for one tenant or for all of them."""
    dry_run = not args.apply
    if args.slug is None:
        reports = list(service.purge_all(batch_size=args.batch_size, dry_run=dry_run))
    else:
        reports = [
            service.purge_tenant(tenant_id=args.slug, batch_size=args.batch_size, dry_run=dry_run)
        ]

    if args.as_json:
        print(json.dumps([_purge_json(report) for report in reports], indent=2), file=out)
        return 0

    if not reports:
        print("no tenants", file=out)
        return 0
    for report in reports:
        print(report.render(), file=out)
    if dry_run:
        print("", file=out)
        print("Dry run: nothing was written. Re-run with --apply.", file=out)
        # Said explicitly because the number it cannot produce is the one somebody will
        # otherwise read as zero — see PurgeReport's dry-run branch.
        print(
            f"({PurgedRecords.ASSESSMENT_REASONING.value} is not counted on a dry run: "
            "finding out means redacting.)",
            file=out,
        )
    return 0


def _policy(args: argparse.Namespace, service: RetentionService, out: TextIO) -> int:
    """Show one tenant's windows, or change them."""
    current = service.policy_for(args.slug)
    wanted_raw = args.raw_days if args.raw_days is not None else current.raw_retention_days
    wanted_assessment = (
        args.assessment_days
        if args.assessment_days is not None
        else current.assessment_retention_days
    )
    changing = (wanted_raw, wanted_assessment) != (
        current.raw_retention_days,
        current.assessment_retention_days,
    )

    if changing and args.apply:
        current = service.set_policy(
            tenant_id=args.slug,
            raw_retention_days=wanted_raw,
            assessment_retention_days=wanted_assessment,
        )

    if args.as_json:
        document = {
            "tenant_id": current.tenant_id,
            "raw_retention_days": current.raw_retention_days,
            "assessment_retention_days": current.assessment_retention_days,
            "applied": bool(changing and args.apply),
        }
        print(json.dumps(document, indent=2), file=out)
        return 0

    print(f"{current.tenant_id}:", file=out)
    print(f"  raw payload (tier 1):   {current.raw_retention_days} days", file=out)
    print(f"  assessment  (tier 2):   {current.assessment_retention_days} days", file=out)
    if changing and not args.apply:
        print("", file=out)
        print(
            f"Would set tier 1 to {wanted_raw} days and tier 2 to {wanted_assessment} days. "
            "Re-run with --apply.",
            file=out,
        )
    return 0


def _find(args: argparse.Namespace, service: RetentionService, out: TextIO) -> int:
    """Report what is held about one person. Always read-only."""
    footprint = service.find_subject(tenant_id=args.slug, email=args.email)
    if args.as_json:
        print(json.dumps(_footprint_json(footprint), indent=2), file=out)
        return 0
    print(_render_footprint(footprint), file=out)
    return 0


def _erase(args: argparse.Namespace, service: RetentionService, out: TextIO, err: TextIO) -> int:
    """Carry out a deletion request, or report what one would do."""
    if not args.apply:
        footprint = service.find_subject(tenant_id=args.slug, email=args.email)
        if args.as_json:
            print(json.dumps(_footprint_json(footprint), indent=2), file=out)
            return 0
        print(_render_footprint(footprint), file=out)
        print("", file=out)
        print("Dry run: nothing was deleted. Re-run with --apply.", file=out)
        return 0

    receipt = service.erase_subject(
        tenant_id=args.slug, email=args.email, requested_by=args.requested_by
    )
    if args.receipt is not None:
        # After the erasure has committed, so a receipt file that exists is always about
        # something that happened. A failure to write it is reported and does not change
        # the exit code: the deletion is done and the audit row is in the database, which
        # is the durable half.
        try:
            args.receipt.write_text(receipt.render() + "\n", encoding="utf-8")
        except OSError as error:
            print(
                f"retentionctl: the erasure succeeded; the receipt file did not: {error}",
                file=err,
            )

    if args.as_json:
        print(json.dumps(receipt.as_dict(), indent=2), file=out)
        return 0
    print(receipt.render(), file=out)
    return 0


def _render_footprint(footprint: SubjectFootprint) -> str:
    """What is held about one person, as text. Carries the hash, never the address."""
    if footprint.empty:
        return "\n".join(
            [
                f"tenant:              {footprint.tenant_id}",
                f"subject (SHA-256):   {footprint.subject_hash}",
                "",
                "No data held for this subject.",
            ]
        )
    children = footprint.children
    return "\n".join(
        [
            f"tenant:              {footprint.tenant_id}",
            f"subject (SHA-256):   {footprint.subject_hash}",
            "",
            f"  leads:             {footprint.leads}",
            f"    found by hash:   {footprint.matched_by_hash}",
            f"    found by scan:   {footprint.matched_by_payload_scan}",
            f"  assessments:       {children.assessments}",
            f"  routing events:    {children.routing_events}",
            f"  feedback:          {children.feedback}",
            f"  golden promotions: {children.golden_promotions}",
        ]
    )


def _footprint_json(footprint: SubjectFootprint) -> dict[str, Any]:
    """The footprint as JSON. Lead ids are included; the address is not."""
    return {
        "tenant_id": footprint.tenant_id,
        "subject_hash": footprint.subject_hash,
        "leads": footprint.leads,
        "lead_ids": list(footprint.lead_ids),
        "matched_by_hash": footprint.matched_by_hash,
        "matched_by_payload_scan": footprint.matched_by_payload_scan,
        "children": {
            "assessments": footprint.children.assessments,
            "routing_events": footprint.children.routing_events,
            "feedback": footprint.children.feedback,
            "golden_promotions": footprint.children.golden_promotions,
        },
        "empty": footprint.empty,
    }


def _purge_json(report: PurgeReport) -> dict[str, Any]:
    """One purge report as JSON, keyed by the class of record."""
    return {
        "tenant_id": report.tenant_id,
        "ran_at": report.ran_at.isoformat(),
        "dry_run": report.dry_run,
        "counts": {kind.value: report.count(kind) for kind in PurgedRecords},
    }


def _utf8(stream: TextIO) -> TextIO:
    """Force UTF-8 on a standard stream where the platform allows it.

    Same reason as ``usagectl``: a Windows console's default code page would otherwise
    raise ``UnicodeEncodeError`` on a report containing a tenant name with an umlaut.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")
    return stream


def _default_service_factory(settings: Settings) -> RetentionService:
    """Wire the real store and the system clock.

    Imported inside the function, not at module scope, so that ``import
    leadquali.retentionctl`` — which the tests do — pulls in neither SQLAlchemy's engine
    machinery nor a database connection.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.retention_postgres import PostgresRetentionStore

    return RetentionService(store=PostgresRetentionStore.from_env(settings), clock=SystemClock())


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m leadquali.retentionctl`
    raise SystemExit(main())
