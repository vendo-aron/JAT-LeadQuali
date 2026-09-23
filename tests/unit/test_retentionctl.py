"""``retentionctl``: the real parser, the real service, no database.

The command exists for the day somebody has to delete a person's data on a regulator's
clock, so the properties worth pinning are the ones that fail badly on that day:

* **dry run is the default**, for every command that writes;
* **the address is an input and never an output** — not on stdout, not in ``--json``, not
  in the receipt file, not in the log line the run emits;
* a **receipt file is written only after the erasure committed**, so a file that exists
  describes something that happened;
* ``find`` is read-only, always, and says "no data held" in as many words when it is — which
  is the answer a controller gets when the retention job got there first.

The scheduled half is at the bottom: the Lambda applies rather than dry-running, because a
cron entry that changes nothing would look like compliance.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from leadquali.api.retention import EVENT_RETENTION_RUN, lambda_handler, run_retention
from leadquali.app.retention import PurgedRecords, RetentionService
from leadquali.config import Environment, Settings
from leadquali.observability import EMAIL_REDACTION, contact_email_hash
from leadquali.retentionctl import EXIT_FAILED, EXIT_INPUT_ERROR, main
from tests.fakes import FakeClock, InMemoryRetentionStore
from tests.logcapture import capture_json_logs

TENANT = "acme"
OTHER_TENANT = "zenith-freight"
NOW = dt.datetime(2026, 9, 17, 9, 0, tzinfo=dt.UTC)
SUBJECT = "ada.lovelace+jat37@analytical-engines-quali.co.uk"
SUBJECT_HASH = contact_email_hash(SUBJECT)

REASONING = f"Strong fit; the enquiry came from {SUBJECT} and mentions a Q4 deadline."


class Harness:
    """A store, the real service over it, and the streams the command wrote to."""

    def __init__(self, *, tenants: tuple[str, ...] = (TENANT,)) -> None:
        self.store = InMemoryRetentionStore()
        for slug in tenants:
            self.store.given_tenant(slug)
        self.service = RetentionService(store=self.store, clock=FakeClock(start=NOW, step_ms=0))
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run(self, *argv: str) -> int:
        """Run one command through the real parser and the real service."""
        return main(
            list(argv),
            service_factory=lambda _settings: self.service,
            settings=Settings(env=Environment.LOCAL),
            stdout=self.out,
            stderr=self.err,
        )

    @property
    def stdout(self) -> str:
        return self.out.getvalue()

    @property
    def stderr(self) -> str:
        return self.err.getvalue()

    def json(self) -> Any:
        return json.loads(self.stdout)

    def given_expired_lead(self, *, tenant_id: str = TENANT, email: str = SUBJECT) -> str:
        return self.store.given_lead(
            tenant_id=tenant_id,
            received_at=NOW - dt.timedelta(days=200),
            email=email,
            reasoning=REASONING,
        )

    def given_recent_lead(self, *, tenant_id: str = TENANT, email: str = SUBJECT) -> str:
        return self.store.given_lead(
            tenant_id=tenant_id, received_at=NOW - dt.timedelta(days=5), email=email
        )


# ------------------------------------------------------------------------------ usage


def test_no_command_prints_help_and_fails() -> None:
    harness = Harness()

    assert harness.run() == EXIT_INPUT_ERROR
    assert "retentionctl" in harness.stdout


def test_an_unknown_tenant_is_a_failure_not_a_traceback() -> None:
    harness = Harness()

    assert harness.run("policy", "nobody") == EXIT_FAILED
    assert "nobody" in harness.stderr


def test_a_window_the_database_would_refuse_is_an_input_error() -> None:
    """The operator gets the sentence explaining why, and nothing is written."""
    harness = Harness()

    exit_code = harness.run(
        "policy", TENANT, "--raw-days", "400", "--assessment-days", "90", "--apply"
    )

    assert exit_code == EXIT_INPUT_ERROR
    assert "must not exceed" in harness.stderr
    assert harness.store.retention_policy(tenant_id=TENANT).raw_retention_days == 90


# -------------------------------------------------------------------------- purge


def test_purge_without_a_flag_writes_nothing() -> None:
    """The default for the one command in the system whose purpose is deleting data."""
    harness = Harness()
    lead_id = harness.given_expired_lead()

    assert harness.run("purge") == 0
    assert "Dry run" in harness.stdout
    assert harness.store.lead(lead_id).raw_payload["email"] == SUBJECT


def test_an_explicit_dry_run_is_the_same_as_no_flag() -> None:
    harness = Harness()
    lead_id = harness.given_expired_lead()

    assert harness.run("purge", "--dry-run") == 0
    assert harness.store.lead(lead_id).raw_payload["email"] == SUBJECT


def test_apply_and_dry_run_together_are_refused_by_the_parser() -> None:
    harness = Harness()

    with pytest.raises(SystemExit):
        harness.run("purge", "--apply", "--dry-run")


def test_purge_with_apply_redacts_and_says_what_it_did() -> None:
    harness = Harness()
    lead_id = harness.given_expired_lead()

    assert harness.run("purge", "--apply") == 0
    assert f"{PurgedRecords.LEAD_PAYLOADS.value}=1" in harness.stdout
    assert f"{PurgedRecords.ASSESSMENT_REASONING.value}=1" in harness.stdout
    assert SUBJECT not in json.dumps(harness.store.lead(lead_id).raw_payload)


def test_a_dry_run_says_which_number_it_cannot_give() -> None:
    """Finding out how many reasoning texts hold an address means redacting them.

    A zero there would be read as "none", which is a different claim from "not counted",
    so the command says so rather than printing a number it did not measure.
    """
    harness = Harness()
    harness.given_expired_lead()

    harness.run("purge")

    assert PurgedRecords.ASSESSMENT_REASONING.value in harness.stdout
    assert "not counted on a dry run" in harness.stdout


def test_purge_with_no_tenant_runs_for_every_tenant() -> None:
    harness = Harness(tenants=(TENANT, OTHER_TENANT))
    harness.given_expired_lead(tenant_id=TENANT)
    harness.given_expired_lead(tenant_id=OTHER_TENANT, email="someone@zenith.example")

    assert harness.run("purge", "--apply") == 0
    assert TENANT in harness.stdout
    assert OTHER_TENANT in harness.stdout


def test_purge_for_one_tenant_leaves_the_others_alone() -> None:
    harness = Harness(tenants=(TENANT, OTHER_TENANT))
    harness.given_expired_lead(tenant_id=TENANT)
    other = harness.given_expired_lead(tenant_id=OTHER_TENANT, email="someone@zenith.example")

    assert harness.run("purge", OTHER_TENANT, "--apply") == 0
    assert harness.store.lead(other).raw_payload.get("email") is None
    assert OTHER_TENANT in harness.stdout
    assert TENANT not in harness.stdout


def test_purge_json_is_keyed_by_the_class_of_record() -> None:
    """So a class added later appears without the tool being edited."""
    harness = Harness()
    harness.given_expired_lead()

    assert harness.run("purge", "--apply", "--json") == 0
    document = harness.json()
    assert set(document[0]["counts"]) == {kind.value for kind in PurgedRecords}
    assert document[0]["dry_run"] is False


def test_an_unbounded_batch_size_is_refused() -> None:
    harness = Harness()
    harness.given_expired_lead()

    assert harness.run("purge", "--batch-size", "0", "--apply") == EXIT_INPUT_ERROR
    assert "batch_size" in harness.stderr


# ------------------------------------------------------------------------- policy


def test_policy_shows_both_windows() -> None:
    harness = Harness()

    assert harness.run("policy", TENANT) == 0
    assert "90 days" in harness.stdout
    assert "730 days" in harness.stdout


def test_changing_a_window_without_apply_says_what_it_would_do() -> None:
    harness = Harness()

    assert harness.run("policy", TENANT, "--raw-days", "30") == 0
    assert "Re-run with --apply" in harness.stdout
    assert harness.store.retention_policy(tenant_id=TENANT).raw_retention_days == 90


def test_changing_a_window_with_apply_writes_it() -> None:
    harness = Harness()

    assert harness.run("policy", TENANT, "--raw-days", "30", "--apply") == 0
    assert harness.store.retention_policy(tenant_id=TENANT).raw_retention_days == 30
    assert "30 days" in harness.stdout


def test_changing_one_window_leaves_the_other_where_it_was() -> None:
    harness = Harness()

    harness.run("policy", TENANT, "--raw-days", "30", "--apply")

    policy = harness.store.retention_policy(tenant_id=TENANT)
    assert (policy.raw_retention_days, policy.assessment_retention_days) == (30, 730)


# --------------------------------------------------------------------------- find


def test_find_is_read_only_and_carries_no_address() -> None:
    """Run first, and it is what a controller is answered with."""
    harness = Harness()
    harness.given_recent_lead()

    assert harness.run("find", TENANT, "--email", SUBJECT) == 0
    assert SUBJECT not in harness.stdout
    assert SUBJECT_HASH is not None and SUBJECT_HASH in harness.stdout
    assert harness.store.leads != []
    assert harness.store.erasures == []


def test_find_says_no_data_held_in_as_many_words() -> None:
    """The answer when the retention job got there first, and it has to be unambiguous.

    An empty table or a row of zeroes reads as "the tool did not work"; a sentence does
    not, and this one is meant to be quoted back to a requester.
    """
    harness = Harness()

    assert harness.run("find", TENANT, "--email", SUBJECT) == 0
    assert "No data held" in harness.stdout


def test_find_json_carries_lead_ids_and_no_address() -> None:
    harness = Harness()
    harness.given_recent_lead()

    assert harness.run("find", TENANT, "--email", SUBJECT, "--json") == 0
    document = harness.json()
    assert document["leads"] == 1
    assert document["subject_hash"] == SUBJECT_HASH
    assert "@" not in harness.stdout


# -------------------------------------------------------------------------- erase


def test_erase_without_apply_deletes_nothing() -> None:
    harness = Harness()
    harness.given_recent_lead()

    assert harness.run("erase", TENANT, "--email", SUBJECT, "--requested-by", "SUP-1") == 0
    assert "Dry run" in harness.stdout
    assert harness.store.leads != []
    assert harness.store.erasures == []


def test_erase_with_apply_deletes_and_prints_a_receipt() -> None:
    harness = Harness()
    harness.given_recent_lead()

    exit_code = harness.run(
        "erase", TENANT, "--email", SUBJECT, "--requested-by", "SUP-4471", "--apply"
    )

    assert exit_code == 0
    assert "erasure receipt" in harness.stdout
    assert SUBJECT not in harness.stdout
    assert SUBJECT_HASH is not None and SUBJECT_HASH in harness.stdout
    assert harness.store.leads == []
    assert len(harness.store.erasures) == 1


def test_erase_requires_somebody_to_have_asked() -> None:
    """``--requested-by`` is required by the parser, so this cannot be forgotten."""
    harness = Harness()

    with pytest.raises(SystemExit):
        harness.run("erase", TENANT, "--email", SUBJECT, "--apply")


def test_a_receipt_file_is_written_and_carries_no_address(tmp_path: Path) -> None:
    """The artifact the requester is shown. It is a file, so it is the easiest to forward."""
    harness = Harness()
    harness.given_recent_lead()
    destination = tmp_path / "receipt.txt"

    harness.run(
        "erase",
        TENANT,
        "--email",
        SUBJECT,
        "--requested-by",
        "SUP-4471",
        "--apply",
        "--receipt",
        str(destination),
    )

    written = destination.read_text(encoding="utf-8")
    assert SUBJECT not in written
    assert SUBJECT_HASH is not None and SUBJECT_HASH in written
    assert "leads deleted:       1" in written


def test_no_receipt_file_is_written_on_a_dry_run(tmp_path: Path) -> None:
    """A receipt file that exists always describes something that happened."""
    harness = Harness()
    harness.given_recent_lead()
    destination = tmp_path / "receipt.txt"

    harness.run(
        "erase",
        TENANT,
        "--email",
        SUBJECT,
        "--requested-by",
        "SUP-1",
        "--receipt",
        str(destination),
    )

    assert not destination.exists()


def test_a_receipt_file_that_cannot_be_written_does_not_undo_the_erasure(
    tmp_path: Path,
) -> None:
    """The deletion is done and the audit row is in the database; those are the durable half.

    Exiting non-zero here would invite somebody to re-run the command, which would write a
    *second* audit row saying nothing was found — a worse record than a warning on stderr.
    """
    harness = Harness()
    harness.given_recent_lead()
    unwritable = tmp_path / "no-such-directory" / "receipt.txt"

    exit_code = harness.run(
        "erase",
        TENANT,
        "--email",
        SUBJECT,
        "--requested-by",
        "SUP-4471",
        "--apply",
        "--receipt",
        str(unwritable),
    )

    assert exit_code == 0
    assert "the erasure succeeded; the receipt file did not" in harness.stderr
    assert harness.store.leads == []
    assert len(harness.store.erasures) == 1


def test_erase_json_is_the_receipt_and_has_no_address_in_it() -> None:
    harness = Harness()
    harness.given_recent_lead()

    harness.run(
        "erase", TENANT, "--email", SUBJECT, "--requested-by", "SUP-4471", "--apply", "--json"
    )

    document = harness.json()
    assert document["subject_hash"] == SUBJECT_HASH
    assert document["leads_deleted"] == 1
    assert "@" not in harness.stdout


def test_the_command_never_logs_the_address_it_was_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The address is on the argument list; it must not reach the log group.

    Invariant 5 applies to an operator's terminal session as much as to the pipeline, and
    this is the one command in the system that is *handed* an address on purpose.

    Captured from the process's real stderr rather than through
    :func:`~tests.logcapture.capture_json_logs`, because ``main`` configures logging onto
    ``sys.stderr`` itself — keeping ``--json`` on stdout parseable — and a capture that
    replaced that handler would be asserting about a stream the command does not use.
    """
    harness = Harness()
    harness.given_recent_lead()

    harness.run("erase", TENANT, "--email", SUBJECT, "--requested-by", "SUP-4471", "--apply")

    logged = capsys.readouterr().err
    assert "retention.erased" in logged, "nothing was logged, so this test proves nothing"
    assert SUBJECT not in logged
    assert EMAIL_REDACTION not in logged, "a call site tried to log an address"
    assert SUBJECT_HASH is not None and SUBJECT_HASH in logged


# ------------------------------------------------------------------ the scheduled job


def test_the_scheduled_job_applies_rather_than_dry_running() -> None:
    """A cron entry that changes nothing is worse than none: it looks like compliance."""
    harness = Harness()
    lead_id = harness.given_expired_lead()

    summary = run_retention(harness.service)

    assert summary["tenants"] == 1
    assert summary["counts"][PurgedRecords.LEAD_PAYLOADS.value] == 1
    assert SUBJECT not in json.dumps(harness.store.lead(lead_id).raw_payload)


def test_the_scheduled_job_reports_only_the_tenants_it_changed() -> None:
    """So the invocation result is a list of what was destroyed, not a roll call."""
    harness = Harness(tenants=(TENANT, OTHER_TENANT))
    harness.given_expired_lead(tenant_id=TENANT)
    harness.given_recent_lead(tenant_id=OTHER_TENANT, email="someone@zenith.example")

    summary = run_retention(harness.service)

    assert [entry["tenant_id"] for entry in summary["purged"]] == [TENANT]
    assert summary["tenants"] == 2


def test_the_scheduled_job_ignores_whatever_the_event_says() -> None:
    """An EventBridge rule's input is a JSON blob in a console nobody reviews."""
    harness = Harness()
    harness.given_expired_lead()

    with capture_json_logs() as logs:
        summary = lambda_handler(
            {"tenant_id": "someone-else", "batch_size": 1_000_000},
            None,
            service=harness.service,
        )

    assert summary["tenants"] == 1
    assert logs.one(EVENT_RETENTION_RUN)["tenants"] == 1
    assert "someone-else" not in logs.text
