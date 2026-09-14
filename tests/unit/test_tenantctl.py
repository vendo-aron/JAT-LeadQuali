"""``python -m leadquali.tenantctl``, driven end to end against in-memory doubles.

The real parser, the real service and the real output formatting run in every test here;
only Postgres, Secrets Manager and the KDF are doubles. What is being checked is what an
operator actually experiences: the exit code, which stream each line lands on, and the two
things that would be genuinely dangerous to get wrong — a key printed somewhere it can be
read twice, and a bad config file that lands in the database anyway.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from leadquali.app.api_keys import parse_api_key
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import TenantService, TenantStatus
from leadquali.config import Settings
from leadquali.tenantctl import EXIT_FAILED, EXIT_INPUT_ERROR, build_parser, main
from tests.fakes import (
    FakeClock,
    FakeSecretHasher,
    FakeTenantSecrets,
    InMemoryTenantAdminStore,
)

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

A_CONFIG: dict[str, Any] = {
    "tenant_id": "acme-demo",
    "name": "Acme Industrial Automation (demo)",
    "icp_description": "Discrete manufacturers replacing hand-written line-side reporting.",
    "thresholds": {"hot": 65.0, "warm": 40.0, "cold": 20.0},
    "routing_rules": {
        "hot": {"action": "email_sales", "destination": "neugeschaeft@acme-demo.invalid"},
        "warm": {"action": "escalate_human", "destination": "triage@acme-demo.invalid"},
        "cold": {"action": "email_sales", "destination": "nurture@acme-demo.invalid"},
        "disqualified": {"action": "suppress"},
    },
}


class Console:
    """A run of the CLI: its exit code and each stream, kept apart."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def out_lines(self) -> list[str]:
        return [line for line in self.out.splitlines() if line]


class Harness:
    """The CLI wired to in-memory doubles, with the store reachable for assertions."""

    def __init__(self) -> None:
        self.store = InMemoryTenantAdminStore()
        self.hasher = FakeSecretHasher()
        self.secrets = FakeTenantSecrets()
        self.service = TenantService(
            store=self.store,
            hasher=self.hasher,
            secrets=self.secrets,
            clock=FakeClock(start=NOW, step_ms=0),
        )

    @property
    def factory(self) -> Callable[[Settings], TenantService]:
        def build(settings: Settings) -> TenantService:
            del settings
            return self.service

        return build

    def run(self, *argv: str) -> Console:
        out, err = io.StringIO(), io.StringIO()
        code = main(
            list(argv),
            service_factory=self.factory,
            settings=Settings(),
            stdout=out,
            stderr=err,
        )
        return Console(code, out.getvalue(), err.getvalue())


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "acme-demo.json"
    path.write_text(json.dumps(A_CONFIG), encoding="utf-8")
    return path


@pytest.fixture
def onboarded(harness: Harness, config_file: Path) -> Harness:
    assert harness.run("create", "acme-demo", str(config_file)).code == 0
    return harness


# ------------------------------------------------------------------------ the parser


DOCUMENTED_COMMANDS = frozenset(
    {
        "create",
        "list",
        "show",
        "update-config",
        "suspend",
        "resume",
        "disable",
        "issue-key",
        "rotate-key",
        "revoke-key",
        "list-keys",
    }
)


def test_every_documented_command_exists_and_no_others() -> None:
    """``docs/tenant-onboarding.md`` names these; one that quietly disappeared, or one
    nobody wrote down, would strand the runbook either way."""
    rendered = build_parser().format_help()
    listed = re.search(r"\{([a-z,\-]+)\}", rendered)
    assert listed is not None, rendered
    assert set(listed.group(1).split(",")) == DOCUMENTED_COMMANDS


def test_no_command_prints_help_and_exits_with_the_input_code(harness: Harness) -> None:
    result = harness.run()
    assert result.code == EXIT_INPUT_ERROR
    assert "usage:" in result.out


@pytest.mark.parametrize("command", ["issue-key", "rotate-key"])
def test_there_is_no_way_to_supply_a_key(command: str) -> None:
    """Every argument in ``api/signing``'s "argon2 is affordable" case rests on the key
    being ours: 64 uniform random bits of key_id and 256 of secret. A caller-supplied key
    would make both claims false, so there is no flag that takes one — asserted by trying
    to pass one and being refused, rather than by reading the help text."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "acme-demo", "--key", "lq_live_whatever"])


# ------------------------------------------------------------------------- creating


def test_create_onboards_a_tenant_from_a_file(harness: Harness, config_file: Path) -> None:
    result = harness.run("create", "acme-demo", str(config_file))

    assert result.code == 0
    record = harness.store.tenants["acme-demo"]
    assert record.name == "Acme Industrial Automation (demo)"
    assert record.config == A_CONFIG
    assert record.id == tenant_id_for("acme-demo")
    assert "acme-demo" in result.out


def test_create_reports_the_provisioned_secret_reference_not_a_secret(
    harness: Harness, config_file: Path
) -> None:
    result = harness.run("create", "acme-demo", str(config_file))
    assert harness.secrets.created["acme-demo"] in result.out
    assert "arn:" in result.out


def test_create_takes_the_name_from_the_config_unless_told_otherwise(
    harness: Harness, config_file: Path
) -> None:
    harness.run("create", "acme-demo", str(config_file), "--name", "Acme GmbH")
    assert harness.store.tenants["acme-demo"].name == "Acme GmbH"


def test_a_duplicate_create_fails_without_a_traceback(harness: Harness, config_file: Path) -> None:
    harness.run("create", "acme-demo", str(config_file))
    result = harness.run("create", "acme-demo", str(config_file))

    assert result.code == EXIT_FAILED
    assert result.err.startswith("tenantctl: ")
    assert "already exists" in result.err
    assert result.out == ""


def test_a_missing_config_file_is_an_input_error(harness: Harness, tmp_path: Path) -> None:
    result = harness.run("create", "acme-demo", str(tmp_path / "nope.json"))
    assert result.code == EXIT_INPUT_ERROR
    assert "nope.json" in result.err


def test_a_config_file_that_is_not_json_is_an_error_naming_the_file(
    harness: Harness, tmp_path: Path
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = harness.run("create", "acme-demo", str(path))
    assert result.code == EXIT_FAILED
    assert "broken.json" in result.err


def test_a_config_that_would_break_scoring_never_reaches_the_store(
    harness: Harness, tmp_path: Path
) -> None:
    """The "a bad paste cannot take a tenant down" criterion, at the operator's end."""
    path = tmp_path / "broken.json"
    broken = {**A_CONFIG, "thresholds": {"hot": 20.0, "warm": 40.0, "cold": 65.0}}
    path.write_text(json.dumps(broken), encoding="utf-8")

    result = harness.run("create", "acme-demo", str(path))

    assert result.code == EXIT_FAILED
    assert "thresholds" in result.err
    assert harness.store.tenants == {}
    assert harness.secrets.calls == 0


# ------------------------------------------------------------------------- reading


def test_list_shows_every_tenant_with_its_status(onboarded: Harness) -> None:
    result = onboarded.run("list")
    assert result.code == 0
    assert "acme-demo" in result.out
    assert "active" in result.out


def test_list_is_honest_about_an_empty_database(harness: Harness) -> None:
    assert "(no tenants)" in harness.run("list").out


def test_list_json_is_one_parseable_document(onboarded: Harness) -> None:
    result = onboarded.run("list", "--json")
    payload = json.loads(result.out)
    assert [entry["slug"] for entry in payload] == ["acme-demo"]
    assert payload[0]["config"] == A_CONFIG


def test_show_json_carries_the_config_and_no_secret(onboarded: Harness) -> None:
    onboarded.run("issue-key", "acme-demo")
    payload = json.loads(onboarded.run("show", "acme-demo", "--json").out)

    assert payload["slug"] == "acme-demo"
    assert payload["id"] == str(tenant_id_for("acme-demo"))
    assert payload["hmac_secret_ref"].startswith("arn:")
    assert "key" not in payload
    assert "$argon2" not in json.dumps(payload)


def test_show_of_an_unknown_tenant_is_a_clean_failure(harness: Harness) -> None:
    result = harness.run("show", "nobody")
    assert result.code == EXIT_FAILED
    assert "nobody" in result.err
    assert result.out == ""


# ---------------------------------------------------------------- updating a rubric


def test_update_config_replaces_the_stored_rubric(onboarded: Harness, tmp_path: Path) -> None:
    path = tmp_path / "v2.json"
    path.write_text(json.dumps({**A_CONFIG, "min_confidence": 0.8}), encoding="utf-8")

    result = onboarded.run("update-config", "acme-demo", str(path))

    assert result.code == 0
    assert onboarded.store.tenants["acme-demo"].config["min_confidence"] == 0.8


def test_a_rejected_update_leaves_the_tenant_on_its_old_rubric(
    onboarded: Harness, tmp_path: Path
) -> None:
    """The failure this whole feature is defended against: a paste at 5pm that would
    mis-route every lead overnight. The tenant keeps running on what it had."""
    before = dict(onboarded.store.tenants["acme-demo"].config)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({**A_CONFIG, "weights": {"vibes": 3.0}}), encoding="utf-8")

    result = onboarded.run("update-config", "acme-demo", str(path))

    assert result.code == EXIT_FAILED
    assert onboarded.store.tenants["acme-demo"].config == before


def test_an_update_naming_another_tenant_is_refused(onboarded: Harness, tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps({**A_CONFIG, "tenant_id": "someone-else"}), encoding="utf-8")
    result = onboarded.run("update-config", "acme-demo", str(path))
    assert result.code == EXIT_FAILED
    assert "tenant_id" in result.err


# ----------------------------------------------------------------------- suspension


def test_suspend_and_resume_move_the_status(onboarded: Harness) -> None:
    def status() -> TenantStatus:
        return onboarded.store.tenants["acme-demo"].status

    assert onboarded.run("suspend", "acme-demo").code == 0
    assert status() is TenantStatus.SUSPENDED

    assert onboarded.run("resume", "acme-demo").code == 0
    assert status() is TenantStatus.ACTIVE


def test_disable_is_available_for_a_customer_who_has_gone(onboarded: Harness) -> None:
    assert onboarded.run("disable", "acme-demo").code == 0
    assert onboarded.store.tenants["acme-demo"].status is TenantStatus.DISABLED


def test_suspending_warns_about_what_the_customer_will_see(onboarded: Harness) -> None:
    result = onboarded.run("suspend", "acme-demo")
    assert "403" in result.err
    assert "suspended" in result.out


def test_suspending_an_unknown_tenant_fails(harness: Harness) -> None:
    assert harness.run("suspend", "nobody").code == EXIT_FAILED


# ----------------------------------------------------------------------------- keys


def test_issue_key_prints_the_key_once_alone_on_a_line(onboarded: Harness) -> None:
    """``tenantctl issue-key acme | tail -1`` has to be a sane thing to do, and the
    warning must not be on the stream being piped."""
    result = onboarded.run("issue-key", "acme-demo", "--label", "acme website")

    assert result.code == 0
    assert len(result.out_lines) == 1
    key = result.out_lines[0]
    parsed = parse_api_key(key)
    assert parsed is not None
    assert parsed.key_id in onboarded.store.keys


def test_the_warning_that_a_key_is_shown_once_is_on_stderr(onboarded: Harness) -> None:
    result = onboarded.run("issue-key", "acme-demo")
    assert "only time" in result.err
    assert "only time" not in result.out


def test_the_issued_key_is_not_recoverable_from_anything_else(onboarded: Harness) -> None:
    """The acceptance criterion, asserted from the operator's side: after the key has been
    printed, no command and no stored value will ever show it again."""
    key = onboarded.run("issue-key", "acme-demo").out_lines[0]

    listed = onboarded.run("list-keys", "acme-demo")
    shown = onboarded.run("show", "acme-demo", "--json")
    stored = json.dumps(
        {
            "tenants": {slug: str(record) for slug, record in onboarded.store.tenants.items()},
            "hashes": onboarded.store.hashes,
        }
    )

    assert key not in listed.out
    assert key not in shown.out
    assert key not in stored


def test_a_test_key_says_so_in_the_key(onboarded: Harness) -> None:
    key = onboarded.run("issue-key", "acme-demo", "--env", "test").out_lines[0]
    assert key.startswith("lq_test_")


def test_an_unknown_environment_is_a_usage_error(onboarded: Harness) -> None:
    with pytest.raises(SystemExit) as caught:
        onboarded.run("issue-key", "acme-demo", "--env", "staging")
    assert caught.value.code == 2


def test_list_keys_never_shows_a_hash(onboarded: Harness) -> None:
    onboarded.run("issue-key", "acme-demo", "--label", "acme website")
    result = onboarded.run("list-keys", "acme-demo")

    assert result.code == 0
    assert "lq_live_" in result.out
    assert "acme website" in result.out
    assert "$argon2" not in result.out


def test_list_keys_json_is_parseable_and_carries_no_hash(onboarded: Harness) -> None:
    onboarded.run("issue-key", "acme-demo")
    payload = json.loads(onboarded.run("list-keys", "acme-demo", "--json").out)

    assert len(payload) == 1
    assert set(payload[0]) == {
        "key_id",
        "key_prefix",
        "label",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
    }


def test_list_keys_is_honest_about_a_tenant_with_none(onboarded: Harness) -> None:
    assert "(no keys)" in onboarded.run("list-keys", "acme-demo").out


# ------------------------------------------------------------------------- rotation


def test_rotate_key_issues_a_new_key_and_dates_the_old_one(onboarded: Harness) -> None:
    old = parse_api_key(onboarded.run("issue-key", "acme-demo").out_lines[0])
    assert old is not None

    result = onboarded.run("rotate-key", "acme-demo", old.key_id)

    assert result.code == 0
    new = parse_api_key(result.out_lines[0])
    assert new is not None and new.key_id != old.key_id
    _, old_record = onboarded.store.keys[old.key_id]
    assert old_record.expires_at is not None
    assert (old_record.expires_at - NOW).days == 7


def test_rotate_key_says_how_long_the_customer_has(onboarded: Harness) -> None:
    old = parse_api_key(onboarded.run("issue-key", "acme-demo").out_lines[0])
    assert old is not None
    result = onboarded.run("rotate-key", "acme-demo", old.key_id, "--overlap-days", "3")
    assert "3 day" in result.err
    _, record = onboarded.store.keys[old.key_id]
    assert record.expires_at is not None
    assert (record.expires_at - NOW).days == 3


def test_a_zero_overlap_retires_the_old_key_at_once(onboarded: Harness) -> None:
    """What to reach for when a key has leaked."""
    old = parse_api_key(onboarded.run("issue-key", "acme-demo").out_lines[0])
    assert old is not None
    onboarded.run("rotate-key", "acme-demo", old.key_id, "--overlap-days", "0")
    _, record = onboarded.store.keys[old.key_id]
    assert not record.is_live(NOW)


def test_rotating_a_key_that_is_not_this_tenants_fails(
    harness: Harness, config_file: Path, tmp_path: Path
) -> None:
    harness.run("create", "acme-demo", str(config_file))
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps({**A_CONFIG, "tenant_id": "other-co", "name": "Other Co"}), encoding="utf-8"
    )
    harness.run("create", "other-co", str(other))
    theirs = parse_api_key(harness.run("issue-key", "other-co").out_lines[0])
    assert theirs is not None

    result = harness.run("rotate-key", "acme-demo", theirs.key_id)

    assert result.code == EXIT_FAILED
    assert theirs.key_id in result.err


# ----------------------------------------------------------------------- revocation


def test_revoke_key_stamps_the_row_and_says_so(onboarded: Harness) -> None:
    issued = parse_api_key(onboarded.run("issue-key", "acme-demo").out_lines[0])
    assert issued is not None

    result = onboarded.run("revoke-key", "acme-demo", issued.key_id)

    assert result.code == 0
    assert issued.key_id in result.out
    assert "next request" in result.err
    _, record = onboarded.store.keys[issued.key_id]
    assert record.revoked_at == NOW


def test_revoking_another_tenants_key_fails_and_changes_nothing(
    harness: Harness, config_file: Path, tmp_path: Path
) -> None:
    harness.run("create", "acme-demo", str(config_file))
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps({**A_CONFIG, "tenant_id": "other-co", "name": "Other Co"}), encoding="utf-8"
    )
    harness.run("create", "other-co", str(other))
    theirs = parse_api_key(harness.run("issue-key", "other-co").out_lines[0])
    assert theirs is not None

    result = harness.run("revoke-key", "acme-demo", theirs.key_id)

    assert result.code == EXIT_FAILED
    _, record = harness.store.keys[theirs.key_id]
    assert record.revoked_at is None


def test_revoking_an_unknown_key_fails(onboarded: Harness) -> None:
    assert onboarded.run("revoke-key", "acme-demo", "0" * 16).code == EXIT_FAILED


# ------------------------------------------------------------------------- wiring


def test_a_missing_database_url_is_reported_rather_than_guessed() -> None:
    """The factory is what needs the database, so a misconfigured environment fails before
    any command runs — in front of whoever typed it, not as a stack trace."""

    def explode(settings: Settings) -> TenantService:
        del settings
        raise RuntimeError("DATABASE_URL is not set")

    out, err = io.StringIO(), io.StringIO()
    code = main(["list"], service_factory=explode, stdout=out, stderr=err)

    assert code == EXIT_INPUT_ERROR
    assert "DATABASE_URL" in err.getvalue()
    assert out.getvalue() == ""


def test_no_command_ever_writes_a_key_or_a_hash_to_the_log_stream(
    onboarded: Harness,
) -> None:
    """stderr is where diagnostics go and where a log handler is attached, so it is the
    stream a key must never appear on."""
    issued = onboarded.run("issue-key", "acme-demo", "--label", "acme website")
    key = issued.out_lines[0]

    assert key not in issued.err
    parsed = parse_api_key(key)
    assert parsed is not None
    assert parsed.secret not in issued.err
    # The prefix is not secret and is *supposed* to be there, so an operator can match the
    # line they are looking at to a row in `list-keys`.
    assert parsed.prefix in issued.err
