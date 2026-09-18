"""``python -m leadquali.adminctl`` — the two secrets the staff admin cannot start without.

First-time setup for ``/admin`` needs an ``ADMIN_SESSION_SECRET`` and an
``ADMIN_CREDENTIALS`` map of usernames to argon2 hashes. Neither can be typed by hand: one
is 32 bytes of randomness and the other is a KDF's output. Before this module existed both
of the error messages that tell an operator what is missing pointed at a command that did
not exist, so the setup path dead-ended on exactly the message written to prevent a dead
end.

Two commands, and deliberately no more:

``hash``
    Read a password without echoing it, confirm it, and print its argon2 encoding. The
    password is never written to a file, never passed as an argument (where it would be in
    the shell history and in ``ps``), and never logged.

``secret``
    Print fresh signing material for ``ADMIN_SESSION_SECRET``.

There is no command that *writes* a credential anywhere. Putting the value into Secrets
Manager or into a ``.env`` is the operator's step, with their own credentials, and keeping
it that way means this module needs no AWS access at all — see ``docs/admin.md`` §2.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from collections.abc import Callable, Sequence
from typing import Final

from leadquali.app.admin_auth import MIN_SESSION_SECRET_BYTES, load_staff_credentials

__all__ = ["build_parser", "main", "subcommands"]

#: How many bytes of randomness ``secret`` mints. The minimum the session signer accepts is
#: :data:`~leadquali.app.admin_auth.MIN_SESSION_SECRET_BYTES`; this is that, rendered as
#: URL-safe base64, so the printed string is comfortably longer than the bound it has to
#: clear even after encoding.
SECRET_BYTES: Final[int] = MIN_SESSION_SECRET_BYTES

#: Shortest staff password this will hash. Not a policy anybody can enforce at the login —
#: the hash is all that reaches the system — but refusing to *mint* a hash for something
#: unusable is the one moment where saying so costs nothing.
MIN_PASSWORD_CHARS: Final[int] = 12

#: The subcommands, named once. Every error message and document that tells an operator to
#: run this command must name one of these; ``tests/unit/test_adminctl.py`` greps the
#: shipping code and ``docs/admin.md`` and checks exactly that, because the reason this
#: module exists is that they previously named a command that did not.
SUBCOMMANDS: Final[tuple[str, ...]] = ("hash", "secret")


def build_parser() -> argparse.ArgumentParser:
    """The argument parser.

    Public so a test can ask which subcommands exist without *running* them — ``hash``
    reads a password from the terminal, and the thing worth checking is that every error
    message and document pointing at this command names one it has.
    """
    parser = argparse.ArgumentParser(
        prog="python -m leadquali.adminctl",
        description="Generate the secrets the staff admin needs. See docs/admin.md.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    hash_command = commands.add_parser("hash", help="hash a staff password for ADMIN_CREDENTIALS")
    hash_command.add_argument(
        "username",
        nargs="?",
        help="print a ready-to-paste one-entry JSON map for this username",
    )
    commands.add_parser("secret", help="generate an ADMIN_SESSION_SECRET")
    return parser


def subcommands() -> frozenset[str]:
    """Every subcommand this module implements.

    Read from :data:`SUBCOMMANDS` rather than out of ``argparse``'s internals, and kept
    honest by ``test_adminctl.py`` parsing each one.
    """
    return frozenset(SUBCOMMANDS)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    arguments = build_parser().parse_args(argv)
    if arguments.command == "secret":
        return _print_secret()
    return _hash_password(arguments.username)


def _print_secret(out: Callable[[str], None] = print) -> int:
    """Print fresh signing material.

    ``token_urlsafe`` rather than raw bytes: the value travels through an environment
    variable and a Secrets Manager console field, and a base64 alphabet survives both
    without quoting surprises.
    """
    out(secrets.token_urlsafe(SECRET_BYTES))
    return 0


def _hash_password(
    username: str | None,
    *,
    prompt: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
) -> int:
    """Read a password twice, hash it, and print the encoding.

    The hasher is imported inside the function rather than at module scope, so that
    ``--help`` and ``secret`` do not pay for loading the KDF — and so this module keeps
    working in an environment where ``argon2-cffi`` is absent right up until the point it
    is actually needed.

    Args:
        username: When given, the output is a one-entry JSON map ready to paste into
            ``ADMIN_CREDENTIALS``. When omitted, just the hash.
        prompt: Injected so a test can drive this without a terminal.
        out: Injected for the same reason.

    Returns:
        ``0`` on success, ``2`` when the two entries differ or the password is too short —
        both operator errors, and both worth a distinct exit code from a crash.
    """
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher

    password = prompt("Staff password: ")
    if len(password) < MIN_PASSWORD_CHARS:
        _fail(f"a staff password needs at least {MIN_PASSWORD_CHARS} characters")
        return 2
    if password != prompt("Again: "):
        # Refused rather than hashed: a hash of a mistyped password is indistinguishable
        # from a hash of the intended one, and the operator finds out at the login screen.
        _fail("those did not match; nothing was hashed")
        return 2

    encoded = Argon2KeyHasher().hash_secret(password)
    if username is None:
        out(encoded)
        return 0
    # Validated before it is printed, so a username the loader would refuse is refused
    # here — where it costs a retype — rather than at the first login attempt.
    document = json.dumps({username: encoded})
    load_staff_credentials(document)
    out(document)
    return 0


def _fail(message: str) -> None:
    """Report an operator error on stderr, so stdout stays pasteable."""
    print(message, file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover - exercised by running the command
    raise SystemExit(main())
