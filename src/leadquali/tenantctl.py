"""``python -m leadquali.tenantctl`` — onboarding and credentials, from a terminal.

This is the whole of "onboarding a customer is a config write, not a deploy" (invariant 1),
made operable. Creating a customer, changing their rubric, suspending them and issuing,
rotating or revoking their keys are all commands here, and none of them is a code change,
a redeploy or a hand-written ``UPDATE``.

    python -m leadquali.tenantctl create acme-demo tenants/acme-demo.json
    python -m leadquali.tenantctl issue-key acme-demo --label "acme website"
    python -m leadquali.tenantctl rotate-key acme-demo 3f1c9a02b7d45e68
    python -m leadquali.tenantctl revoke-key acme-demo 3f1c9a02b7d45e68

``docs/tenant-onboarding.md`` is the runbook these commands belong to.

Three behaviours are load-bearing rather than cosmetic.

* **A new key is printed once, alone on a line, on stdout.** It exists nowhere else — not
  in the database, not in a log, not in the record the command prints beside it. The line
  is bare so that ``tenantctl issue-key acme | tail -1`` is a sane thing to do; the warning
  that it will not be shown again goes to **stderr**, where it cannot contaminate that pipe.
* **Human output is stdout and diagnostics are stderr**, and ``--json`` on the read commands
  emits one object with nothing else mixed in — the same split ``cli.py`` makes, for the
  same reason: these commands get piped.
* **Every dependency is injected.** ``main`` takes a factory, so the tests drive the real
  argument parsing, the real service and the real output formatting without Postgres, AWS
  or a KDF anywhere in sight.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

from leadquali.app.api_keys import KeyEnvironment
from leadquali.app.tenants import (
    DEFAULT_ROTATION_OVERLAP,
    ApiKeyRecord,
    IssuedApiKey,
    TenantAdminError,
    TenantRecord,
    TenantService,
    TenantStatus,
)
from leadquali.config import Settings, get_settings
from leadquali.domain.tenant_config import TenantConfigError
from leadquali.observability import configure_logging

__all__ = ["EXIT_FAILED", "EXIT_INPUT_ERROR", "ServiceFactory", "build_parser", "main"]

#: A command that was understood and could not be carried out: no such tenant, a duplicate
#: slug, a config the rubric model rejects. Distinct from a usage error so that a script
#: can tell "you typed it wrong" from "the system said no".
EXIT_FAILED: Final[int] = 1

#: A usage or input problem — an unreadable file, an unparseable argument, no command.
#: The same code ``cli.py`` reserves for it.
EXIT_INPUT_ERROR: Final[int] = 2

ServiceFactory = Callable[[Settings], TenantService]
"""How ``main`` gets a service. Injected so the tests never touch Postgres or AWS."""


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m leadquali.tenantctl",
        description="Create and administer tenants, their rubrics and their API keys.",
    )
    commands = parser.add_subparsers(dest="command")

    create = commands.add_parser("create", help="Onboard a tenant from a config file.")
    create.add_argument("slug", help="The tenant's slug; must match the config's tenant_id.")
    create.add_argument("config", type=Path, help="Path to the tenant's rubric JSON.")
    create.add_argument(
        "--name",
        default=None,
        help="Human-readable name (default: the config's own 'name' field).",
    )

    listing = commands.add_parser("list", help="List every tenant.")
    _add_json(listing)

    show = commands.add_parser("show", help="Show one tenant.")
    show.add_argument("slug")
    _add_json(show)

    update = commands.add_parser("update-config", help="Replace a tenant's rubric.")
    update.add_argument("slug")
    update.add_argument("config", type=Path, help="Path to the replacement rubric JSON.")

    for name, help_text in (
        ("suspend", "Stop ingest for a tenant. Reversible with 'resume'."),
        ("resume", "Let a suspended tenant ingest again."),
        ("disable", "Stop ingest for a tenant that is gone for good."),
    ):
        status = commands.add_parser(name, help=help_text)
        status.add_argument("slug")

    issue = commands.add_parser("issue-key", help="Issue an API key. Shown once.")
    issue.add_argument("slug")
    issue.add_argument("--label", default=None, help="A note, e.g. 'acme marketing site'.")
    issue.add_argument(
        "--env",
        choices=sorted(member.value for member in KeyEnvironment),
        default=KeyEnvironment.LIVE.value,
        help=(
            "Which environment the key is for; written into the key itself so a test key "
            f"pasted into production config is obvious (default: {KeyEnvironment.LIVE.value})."
        ),
    )

    rotate = commands.add_parser(
        "rotate-key",
        help="Issue a replacement key and put the old one on a deadline.",
    )
    rotate.add_argument("slug")
    rotate.add_argument("key_id", help="The key_id being retired; see 'list-keys'.")
    rotate.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_ROTATION_OVERLAP.days,
        help=(
            "How long the old key keeps working, in days "
            f"(default: {DEFAULT_ROTATION_OVERLAP.days}). Zero retires it at once, which "
            "is what to use for a key that has leaked."
        ),
    )

    revoke = commands.add_parser("revoke-key", help="Kill a key now.")
    revoke.add_argument("slug")
    revoke.add_argument("key_id")

    keys = commands.add_parser("list-keys", help="List a tenant's keys. Never shows a key.")
    keys.add_argument("slug")
    _add_json(keys)

    return parser


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON document instead of a human-readable table.",
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
        service_factory: Builds the :class:`~leadquali.app.tenants.TenantService`.
            ``None`` wires the real Postgres store, argon2 hasher and Secrets Manager
            provisioner — which is why it is a parameter: nothing in the tests does.
        settings: Configuration to build from. ``None`` reads the process settings.
        stdout: Where human and JSON output goes. ``None`` is the process's stdout, forced
            to UTF-8 so a tenant name with an umlaut in it does not crash the command on a
            Windows console.
        stderr: Where warnings and errors go.

    Returns:
        ``0``, :data:`EXIT_FAILED` or :data:`EXIT_INPUT_ERROR`.
    """
    # Logs go to stderr, never stdout: `--json` and the bare key line are both parsed by
    # something, and one interleaved log line would break them.
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
        print(f"tenantctl: {error}", file=err)
        return EXIT_INPUT_ERROR

    try:
        return _dispatch(args, service, out, err)
    except (TenantAdminError, TenantConfigError) as error:
        print(f"tenantctl: {error}", file=err)
        return EXIT_FAILED
    except OSError as error:
        print(f"tenantctl: {error}", file=err)
        return EXIT_INPUT_ERROR


def _dispatch(args: argparse.Namespace, service: TenantService, out: TextIO, err: TextIO) -> int:
    """Run the parsed command. Errors propagate to :func:`main`, which renders them."""
    match args.command:
        case "create":
            document = _read_config(args.config)
            name = args.name if args.name is not None else str(document.get("name", args.slug))
            record = service.create_tenant(slug=args.slug, name=name, config=document)
            print(f"created tenant {record.slug} ({record.name}), id {record.id}", file=out)
            # "hmac secret:" and not "signing secret:", and matching what `show` prints:
            # this is the ARN of the secret, not its value, and an operator who mistakes one
            # for the other pastes an ARN into a customer's site configuration.
            print(f"hmac secret: {record.hmac_secret_ref}", file=out)
            print(
                f"next: issue a key with `python -m leadquali.tenantctl issue-key {record.slug}`",
                file=err,
            )
            return 0

        case "list":
            tenants = service.list_tenants()
            if args.as_json:
                print(json.dumps([_tenant_json(t) for t in tenants], indent=2), file=out)
            else:
                _print_tenants(tenants, out)
            return 0

        case "show":
            record = service.get_tenant(slug=args.slug)
            if args.as_json:
                print(json.dumps(_tenant_json(record), indent=2), file=out)
            else:
                _print_tenant(record, out)
            return 0

        case "update-config":
            record = service.update_config(slug=args.slug, config=_read_config(args.config))
            print(
                f"updated the rubric for {record.slug} at {record.updated_at.isoformat()}", file=out
            )
            return 0

        case "suspend" | "resume" | "disable":
            status = {
                "suspend": TenantStatus.SUSPENDED,
                "resume": TenantStatus.ACTIVE,
                "disable": TenantStatus.DISABLED,
            }[args.command]
            record = service.set_status(slug=args.slug, status=status)
            print(f"{record.slug} is now {record.status.value}", file=out)
            if status is not TenantStatus.ACTIVE:
                print(
                    "their forms now get 403 on every submission, effective immediately.",
                    file=err,
                )
            return 0

        case "issue-key":
            issued = service.issue_key(
                slug=args.slug, label=args.label, environment=KeyEnvironment(args.env)
            )
            _print_issued_key(issued, out, err)
            return 0

        case "rotate-key":
            overlap = timedelta(days=args.overlap_days)
            issued = service.rotate_key(slug=args.slug, key_id=args.key_id, overlap=overlap)
            print(
                f"old key {args.key_id} stops working in {args.overlap_days} day(s); "
                "give the customer the new one below and have them deploy it before then.",
                file=err,
            )
            _print_issued_key(issued, out, err)
            return 0

        case "revoke-key":
            killed = service.revoke_key(slug=args.slug, key_id=args.key_id)
            stamped = killed.revoked_at.isoformat() if killed.revoked_at else "now"
            print(f"revoked {killed.key_prefix} at {stamped}", file=out)
            print("effective on the next request; nothing is cached.", file=err)
            return 0

        case "list-keys":
            keys = service.list_keys(slug=args.slug)
            if args.as_json:
                print(json.dumps([_key_json(key) for key in keys], indent=2), file=out)
            else:
                _print_keys(keys, out)
            return 0

    # argparse only produces the commands above; this is the compiler's assurance, not a
    # runtime branch anyone can reach.
    raise AssertionError(f"unhandled command {args.command!r}")


# ---------------------------------------------------------------------------- output


def _print_issued_key(issued: IssuedApiKey, out: TextIO, err: TextIO) -> None:
    """Print a key exactly once, alone on a line, with the warning on the other stream."""
    print(f"issued {issued.record.key_prefix} for tenant use.", file=err)
    print(issued.key, file=out)
    print(
        "^ this is the only time this key will ever be shown. It is stored as an argon2 "
        "hash and cannot be recovered. If it is lost, revoke it and issue another.",
        file=err,
    )


def _print_tenants(tenants: Sequence[TenantRecord], out: TextIO) -> None:
    if not tenants:
        print("(no tenants)", file=out)
        return
    width = max(len(record.slug) for record in tenants)
    for record in tenants:
        print(
            f"{record.slug:<{width}}  {record.status.value:<9}  {record.name}",
            file=out,
        )


def _print_tenant(record: TenantRecord, out: TextIO) -> None:
    print(f"slug:        {record.slug}", file=out)
    print(f"name:        {record.name}", file=out)
    print(f"id:          {record.id}", file=out)
    print(f"status:      {record.status.value}", file=out)
    print(f"hmac secret: {record.hmac_secret_ref or '(none)'}", file=out)
    print(
        f"rate limit:  {record.rate_limit_per_minute}/min, burst {record.rate_limit_burst}",
        file=out,
    )
    print(f"created:     {record.created_at.isoformat()}", file=out)
    print(f"updated:     {record.updated_at.isoformat()}", file=out)


def _print_keys(keys: Sequence[ApiKeyRecord], out: TextIO) -> None:
    if not keys:
        print("(no keys)", file=out)
        return
    for record in keys:
        state = "revoked" if record.revoked_at is not None else "active"
        if record.revoked_at is None and record.expires_at is not None:
            state = f"expires {record.expires_at.isoformat()}"
        used = record.last_used_at.isoformat() if record.last_used_at else "never"
        print(
            f"{record.key_prefix}  {state:<34}  last used {used}  {record.label or ''}".rstrip(),
            file=out,
        )


def _tenant_json(record: TenantRecord) -> dict[str, Any]:
    """One tenant as JSON. Carries the config, and no secret of any kind."""
    return {
        "slug": record.slug,
        "name": record.name,
        "id": str(record.id),
        "status": record.status.value,
        "hmac_secret_ref": record.hmac_secret_ref,
        "rate_limit_per_minute": record.rate_limit_per_minute,
        "rate_limit_burst": record.rate_limit_burst,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "config": dict(record.config),
    }


def _key_json(record: ApiKeyRecord) -> dict[str, Any]:
    """One key as JSON. There is no hash in :class:`ApiKeyRecord` to leak."""
    return {
        "key_id": record.key_id,
        "key_prefix": record.key_prefix,
        "label": record.label,
        "created_at": record.created_at.isoformat(),
        "expires_at": _iso_or_none(record.expires_at),
        "revoked_at": _iso_or_none(record.revoked_at),
        "last_used_at": _iso_or_none(record.last_used_at),
    }


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# ----------------------------------------------------------------------------- input


def _read_config(path: Path) -> Mapping[str, Any]:
    """Read a rubric document, refusing anything that is not a JSON object.

    Validation proper is :class:`~leadquali.domain.tenant_config.TenantConfig`'s, inside
    the service, so that the CLI and the seed script cannot disagree about what a valid
    rubric is. What happens here is only "can this file be read at all".

    Raises:
        OSError: the file is missing or unreadable.
        TenantAdminError: it is not JSON, or not a JSON object.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise OSError(f"no such config file: {path}") from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TenantAdminError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(document, dict):
        raise TenantAdminError(f"{path} must hold a JSON object, got {type(document).__name__}")
    return document


def _utf8(stream: TextIO) -> TextIO:
    """Force UTF-8 on a standard stream where the platform allows it.

    A tenant name with an umlaut or a euro sign in it would otherwise raise
    ``UnicodeEncodeError`` on a Windows console's default code page — and it would do so
    *after* the key had been issued, which is the worst possible moment to lose output.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        # A stream that cannot be reconfigured (a pytest capture, a pipe someone has
        # already written to) is not a reason to fail a command.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")
    return stream


def _default_service_factory(settings: Settings) -> TenantService:
    """Wire the real store, hasher, secret provisioner and clock.

    Imported inside the function, not at module scope, so that ``import
    leadquali.tenantctl`` — which the tests do — pulls in neither SQLAlchemy's engine
    machinery nor ``boto3``.
    """
    from leadquali.adapters.clock_system import SystemClock
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
    from leadquali.adapters.secrets_manager import TenantSecretsProvisioner
    from leadquali.adapters.store_tenants import PostgresTenantAdminStore

    return TenantService(
        store=PostgresTenantAdminStore.from_env(settings),
        hasher=Argon2KeyHasher(),
        secrets=TenantSecretsProvisioner.from_env(settings),
        clock=SystemClock(),
    )


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m leadquali.tenantctl`
    raise SystemExit(main())
