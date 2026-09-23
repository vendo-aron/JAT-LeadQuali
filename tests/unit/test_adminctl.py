"""``python -m leadquali.adminctl``: the command two error messages promise exists.

It did not. ``config.py``'s ``require_admin_credentials`` and ``admin_auth``'s
``load_staff_credentials`` both tell an operator to run it, and ``docs/admin.md`` §2 gave
a Python snippet instead — so first-time setup dead-ended on the message written to prevent
a dead end. The last test here is the one that keeps those three strings honest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from leadquali import adminctl
from leadquali.app.admin_auth import (
    MIN_SESSION_SECRET_BYTES,
    StaffAuthenticator,
    load_staff_credentials,
)
from leadquali.config import Settings

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery staple"


class Prompts:
    """A scripted ``getpass``, so this runs without a terminal."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, message: str) -> str:
        self.asked.append(message)
        return self.answers.pop(0)


class Printed:
    """Everything the command wrote to stdout."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def only(self) -> str:
        assert len(self.lines) == 1, f"expected one line, got {self.lines}"
        return self.lines[0]


@pytest.fixture
def printed() -> Iterator[Printed]:
    yield Printed()


# ----------------------------------------------------------------------------- secret


def test_the_generated_secret_clears_the_bound_the_signer_enforces(printed: Printed) -> None:
    """Minting something the session signer would then refuse is the one unforgivable bug
    in a command whose entire job is to mint it."""
    assert adminctl._print_secret(out=printed) == 0

    assert len(printed.only.encode("utf-8")) >= MIN_SESSION_SECRET_BYTES


def test_two_secrets_are_not_the_same_secret(printed: Printed) -> None:
    adminctl._print_secret(out=printed)
    adminctl._print_secret(out=printed)

    assert printed.lines[0] != printed.lines[1]


def test_the_secret_survives_an_environment_variable_and_a_shell(printed: Printed) -> None:
    """It travels through a console field and a ``.env``; a quoting surprise there is an
    outage nobody can read off the error."""
    adminctl._print_secret(out=printed)

    assert re.fullmatch(r"[A-Za-z0-9_-]+", printed.only), printed.only


# ------------------------------------------------------------------------------- hash


def test_a_hashed_password_verifies_against_the_real_authenticator(printed: Printed) -> None:
    """End to end: what the command prints is what the login accepts."""
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher

    code = adminctl._hash_password(None, prompt=Prompts(PASSWORD, PASSWORD), out=printed)

    assert code == 0
    authenticator = StaffAuthenticator(
        credentials={"ada": printed.only}, verifier=Argon2KeyHasher()
    )
    import datetime as dt

    now = dt.datetime(2026, 9, 16, 9, 0, tzinfo=dt.UTC)
    assert authenticator.authenticate(username="ada", password=PASSWORD, now=now).authenticated
    assert not authenticator.authenticate(username="ada", password="wrong", now=now).authenticated


def test_naming_a_user_prints_a_credential_document_the_loader_accepts(
    printed: Printed,
) -> None:
    code = adminctl._hash_password("ada", prompt=Prompts(PASSWORD, PASSWORD), out=printed)

    assert code == 0
    assert load_staff_credentials(printed.only).keys() == {"ada"}
    assert json.loads(printed.only)["ada"].startswith("$argon2")


def test_a_mistyped_confirmation_hashes_nothing(printed: Printed) -> None:
    """A hash of a mistyped password is indistinguishable from the intended one, and the
    operator finds out at the login screen."""
    code = adminctl._hash_password(None, prompt=Prompts(PASSWORD, "something else"), out=printed)

    assert code == 2
    assert printed.lines == []


def test_a_password_too_short_to_be_one_is_refused(printed: Printed) -> None:
    code = adminctl._hash_password(None, prompt=Prompts("short", "short"), out=printed)

    assert code == 2
    assert printed.lines == []


def test_the_password_is_never_echoed_and_never_an_argument() -> None:
    """It is read through ``getpass``, so it is not in the shell history nor in ``ps``."""
    prompts = Prompts(PASSWORD, PASSWORD)
    adminctl._hash_password(None, prompt=prompts, out=Printed())

    assert len(prompts.asked) == 2
    parser_source = Path(adminctl.__file__).read_text(encoding="utf-8")
    assert '"password"' not in parser_source, "a password argument would be in ps output"


def test_the_printed_hash_carries_no_trace_of_the_password(printed: Printed) -> None:
    adminctl._hash_password("ada", prompt=Prompts(PASSWORD, PASSWORD), out=printed)

    assert PASSWORD not in printed.only
    for word in PASSWORD.split():
        assert word not in printed.only


# ---------------------------------------------------------------------------- the CLI


def test_the_module_is_runnable_and_both_commands_are_reachable() -> None:
    """``argparse`` refuses an unknown subcommand, which is how a typo is reported."""
    assert adminctl.subcommands() == {"hash", "secret"}
    # The declared set and the parser's are the same set: every name is parseable.
    for command in sorted(adminctl.subcommands()):
        assert adminctl.build_parser().parse_args([command]).command == command
    assert adminctl.main(["secret"]) == 0
    with pytest.raises(SystemExit):
        adminctl.main(["nonsense"])
    with pytest.raises(SystemExit):
        adminctl.main([])


# ------------------------------------------------- the strings that promise it exists


def test_everything_that_names_this_command_names_one_that_exists() -> None:
    """The defect this module was added for, stated so it cannot come back.

    Every place in the shipping code and the docs that tells an operator to run
    ``python -m leadquali.adminctl <command>`` must name a subcommand this module has.
    """
    referenced: set[str] = set()
    for path in [
        *(REPO / "src").rglob("*.py"),
        REPO / "docs" / "admin.md",
    ]:
        text = path.read_text(encoding="utf-8")
        referenced.update(re.findall(r"leadquali\.adminctl\s+([a-z-]+)", text))

    assert referenced, "nothing references the command, so this test proves nothing"
    assert referenced <= adminctl.subcommands(), (
        f"these are promised and do not exist: {sorted(referenced - adminctl.subcommands())}"
    )


def test_the_settings_error_still_points_at_the_command() -> None:
    """The message an operator actually hits on a fresh deployment."""
    with pytest.raises(RuntimeError, match=r"leadquali\.adminctl hash"):
        Settings(admin_credentials=None).require_admin_credentials()
