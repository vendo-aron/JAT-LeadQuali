"""The SQL the retention adapter emits, read without a database.

The only module in this codebase that issues a ``DELETE`` against customer data, so the
properties below are the ones worth proving before a server is involved — and proving them
here rather than only in ``tests/integration/test_retention_postgres.py`` is deliberate:
Docker is not available in every environment this suite runs in, and a property asserted
only by a test that skips is not asserted.

Four things are checked, in rough order of what a mistake would cost:

* the destructive statements touch **``leads`` and nothing else** — in particular no
  statement anywhere in this adapter deletes from ``tenants``;
* every batch is **bounded** by the ``LIMIT`` the caller asked for, and takes its rows with
  ``FOR UPDATE SKIP LOCKED`` so a scheduled run and an operator's run do not deadlock;
* the tier-1 update **skips rows already tombstoned**, which is the whole of its
  idempotence;
* the payload scan **escapes ``LIKE`` metacharacters**, so an address containing ``%`` — a
  legal local part — cannot become a wildcard that matches every lead the tenant has.

Tenant scoping is not checked here. It is checked for every method at once by
``tests/isolation/test_repository_isolation.py``, which reads the same compiled SQL.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import ClauseElement

from leadquali.adapters.retention_postgres import PostgresRetentionStore
from leadquali.app.retention import ErasureRequest, payload_tombstone
from leadquali.observability import contact_email_hash
from tests.sqlcapture import CannedResult, SqlCapture, parameters, sql_text

TENANT = "acme"
NOW = dt.datetime(2026, 9, 17, 9, 0, tzinfo=dt.UTC)
CUTOFF = NOW - dt.timedelta(days=90)
LEAD_ID = "3a5c9e10-0b47-4d2f-9c61-7e8a04b5d213"
SUBJECT = "ada.lovelace+jat37@analytical-engines-quali.co.uk"
_HASH = contact_email_hash(SUBJECT)
assert _HASH is not None, "the fixture address must hash"
SUBJECT_HASH: str = _HASH


@pytest.fixture
def capture() -> SqlCapture:
    return SqlCapture()


def store(capture: SqlCapture) -> PostgresRetentionStore:
    return PostgresRetentionStore(capture.sessions)


def only(statements: tuple[ClauseElement, ...]) -> ClauseElement:
    assert len(statements) == 1, f"expected one statement, got {len(statements)}"
    return statements[0]


# ------------------------------------------------------------------ nothing deletes a tenant


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("redact_expired_payloads", {"cutoff": CUTOFF, "tombstone": {"redacted": True}}),
        ("purge_expired_leads", {"cutoff": CUTOFF}),
    ],
)
def test_the_destructive_statements_touch_only_leads(
    capture: SqlCapture, method: str, arguments: dict[str, Any]
) -> None:
    """``tenants`` is never the target of anything here.

    The design says there is no code path that removes a tenant row; the schema's
    ``ON DELETE RESTRICT`` is the backstop and this is the assertion that the backstop is
    never even approached. ``leads`` is the only table named because the child rows go by
    cascade, inside the server.
    """
    called = getattr(store(capture), method)
    sql = sql_text(only(capture.run(lambda: called(tenant_id=TENANT, batch_size=10, **arguments))))

    assert " tenants" not in sql
    assert "leads" in sql


def test_the_purge_is_a_delete_from_leads_and_names_no_child_table(capture: SqlCapture) -> None:
    """The cascade does the rest, which is why the child tables are absent from the SQL.

    A version of this that deleted the children by hand would be a second place the
    schema's delete rules are written down, and the one that drifts is the one nobody
    tests against.
    """
    sql = sql_text(
        only(
            capture.run(
                lambda: store(capture).purge_expired_leads(
                    tenant_id=TENANT, cutoff=CUTOFF, batch_size=10
                )
            )
        )
    )

    assert sql.startswith("delete from leads")
    for table in ("assessments", "routing_events", "feedback", "golden_promotions"):
        assert table not in sql


# ------------------------------------------------------------------------ bounded batches


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("redact_expired_payloads", {"cutoff": CUTOFF, "tombstone": {"redacted": True}}),
        ("purge_expired_leads", {"cutoff": CUTOFF}),
    ],
)
def test_every_batch_is_bounded_and_takes_its_rows_without_blocking(
    capture: SqlCapture, method: str, arguments: dict[str, Any]
) -> None:
    """``LIMIT :batch``, ordered oldest first, ``FOR UPDATE SKIP LOCKED``.

    Together these are what make a first run against a year of leads a sequence of short
    transactions that drain the most overdue data first, and what lets the nightly job and
    an operator with the same command run at the same time.
    """
    called = getattr(store(capture), method)
    statement = only(capture.run(lambda: called(tenant_id=TENANT, batch_size=250, **arguments)))
    sql = sql_text(statement)

    assert "limit" in sql
    assert "order by leads.received_at" in sql
    assert "for update" in sql and "skip locked" in sql
    assert 250 in parameters(statement).values()


def test_the_payload_redaction_skips_rows_it_has_already_redacted(capture: SqlCapture) -> None:
    """The whole of its idempotence, and the reason ``redacted_at`` is not rewritten nightly.

    ``@>`` — JSONB containment — rather than a key test, because containment is exact: a
    stored payload's values are all strings, so the JSON boolean ``true`` cannot appear in
    one by accident.
    """
    statement = only(
        capture.run(
            lambda: store(capture).redact_expired_payloads(
                tenant_id=TENANT,
                cutoff=CUTOFF,
                tombstone=payload_tombstone(redacted_at=NOW),
                batch_size=10,
            )
        )
    )
    sql = sql_text(statement)

    assert "@>" in sql
    assert "not ((leads.raw_payload @>" in sql
    assert {"redacted": True} in parameters(statement).values()


def test_tier_two_deletes_a_lead_whose_payload_is_already_a_tombstone(
    capture: SqlCapture,
) -> None:
    """The mirror of the test above: the containment filter must *not* be on the delete.

    If it were, every lead the tier-1 pass had redacted would become undeletable and tier 2
    would never end — the exact bug that copying the tier-1 statement would produce.
    """
    sql = sql_text(
        only(
            capture.run(
                lambda: store(capture).purge_expired_leads(
                    tenant_id=TENANT, cutoff=CUTOFF, batch_size=10
                )
            )
        )
    )

    assert "@>" not in sql


def test_the_redaction_writes_the_tombstone_and_touches_no_other_column(
    capture: SqlCapture,
) -> None:
    """Tier 1 is an ``UPDATE`` of one column. The score, the tier and the hash all stay."""
    statement = only(
        capture.run(
            lambda: store(capture).redact_expired_payloads(
                tenant_id=TENANT,
                cutoff=CUTOFF,
                tombstone=payload_tombstone(redacted_at=NOW),
                batch_size=10,
            )
        )
    )
    sql = sql_text(statement)

    assert sql.startswith("update leads set raw_payload=")
    assert "contact_email_hash=" not in sql
    assert "received_at=" not in sql
    assert "status=" not in sql


# ------------------------------------------------------------------------- the payload scan


def test_the_payload_scan_searches_the_whole_document_as_text(capture: SqlCapture) -> None:
    """Keys included, because an address can be a *key* in a form nobody modelled."""
    sql = sql_text(
        only(capture.run(lambda: store(capture).leads_mentioning(tenant_id=TENANT, needle=SUBJECT)))
    )

    assert "cast(leads.raw_payload as text)" in sql
    assert "ilike" in sql


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("a%b@x.test", "%a\\%b@x.test%"),
        ("a_b@x.test", "%a\\_b@x.test%"),
        ("a\\b@x.test", "%a\\\\b@x.test%"),
        (SUBJECT, f"%{SUBJECT}%"),
    ],
)
def test_the_scan_escapes_like_metacharacters(
    capture: SqlCapture, needle: str, expected: str
) -> None:
    """``%`` and ``_`` are legal in a local part and are wildcards in ``LIKE``.

    Unescaped, ``%@example.test`` would match every lead at that domain and an erasure
    request would delete other people's data. This is the one input to this adapter that
    comes from outside, so it is the one that gets escaped.
    """
    statement = only(
        capture.run(lambda: store(capture).leads_mentioning(tenant_id=TENANT, needle=needle))
    )

    assert expected in parameters(statement).values()


def test_the_subject_lookup_goes_by_hash_and_never_by_address(capture: SqlCapture) -> None:
    """The indexed path, and the reason ``contact_email_hash`` exists."""
    statement = only(
        capture.run(
            lambda: store(capture).leads_for_subject(tenant_id=TENANT, subject_hash=SUBJECT_HASH)
        )
    )
    values = {str(value) for value in parameters(statement).values()}

    assert "leads.contact_email_hash =" in sql_text(statement)
    assert SUBJECT_HASH in values
    assert SUBJECT not in values


# ------------------------------------------------------------------------------- erasure


def test_an_erasure_counts_deletes_and_files_the_evidence_in_one_transaction(
    capture: SqlCapture,
) -> None:
    """Three statements, in the only order that works, and they commit together.

    Counting after the delete is too late — the cascade happens inside the server and
    reports nothing back — and writing the audit row in a second transaction would allow a
    deletion nobody can prove, or a proof of one that did not happen.
    """
    request = ErasureRequest(
        subject_hash=SUBJECT_HASH,
        lead_ids=(LEAD_ID,),
        matched_by_hash=1,
        matched_by_payload_scan=0,
        requested_by="SUP-4471",
        completed_at=NOW,
    )
    statements = capture.run(
        lambda: store(capture).erase(tenant_id=TENANT, request=request),
        results=[CannedResult(row=(1, 1, 0, 0)), CannedResult(row=(LEAD_ID,))],
    )
    kinds = [sql_text(statement) for statement in statements]

    assert len(kinds) == 3
    assert kinds[0].startswith("select")
    for table in ("assessments", "routing_events", "feedback", "golden_promotions"):
        assert table in kinds[0]
    assert kinds[1].startswith("delete from leads")
    assert kinds[2].startswith("insert into erasure_log")


def test_the_audit_row_carries_the_hash_and_no_address(capture: SqlCapture) -> None:
    """The one place an erasure could put the address back into the database.

    Asserted on the bound parameters rather than on the column list, because a value is
    what actually reaches the server — and this table exists precisely so that a record of
    a deletion is not itself a copy of what was deleted.
    """
    request = ErasureRequest(
        subject_hash=SUBJECT_HASH,
        lead_ids=(LEAD_ID,),
        matched_by_hash=1,
        matched_by_payload_scan=0,
        requested_by="SUP-4471",
        completed_at=NOW,
    )
    statements = capture.run(
        lambda: store(capture).erase(tenant_id=TENANT, request=request),
        results=[CannedResult(row=(1, 1, 0, 0)), CannedResult(row=(LEAD_ID,))],
    )
    bound = {str(value) for value in parameters(statements[2]).values()}

    assert SUBJECT_HASH in bound
    assert not any("@" in value for value in bound), bound


def test_an_erasure_that_finds_nothing_still_writes_the_audit_row(capture: SqlCapture) -> None:
    """No delete is issued — there is nothing to delete — and the evidence is filed anyway."""
    request = ErasureRequest(
        subject_hash=SUBJECT_HASH,
        lead_ids=(),
        matched_by_hash=0,
        matched_by_payload_scan=0,
        requested_by="SUP-4471",
        completed_at=NOW,
    )
    statements = capture.run(
        lambda: store(capture).erase(tenant_id=TENANT, request=request),
        results=[CannedResult(row=(0, 0, 0, 0))],
    )
    kinds = [sql_text(statement) for statement in statements]

    assert len(kinds) == 2
    assert not any(kind.startswith("delete") for kind in kinds)
    assert kinds[1].startswith("insert into erasure_log")


# ------------------------------------------------------------------------------- policies


def test_a_dry_run_reads_both_tiers_in_one_statement(capture: SqlCapture) -> None:
    """So the two numbers an operator is shown describe the same instant."""
    sql = sql_text(
        only(
            capture.run(
                lambda: store(capture).count_expired(
                    tenant_id=TENANT,
                    payload_cutoff=CUTOFF,
                    lead_cutoff=NOW - dt.timedelta(days=730),
                )
            )
        )
    )

    assert sql.count("count(*)") == 2
    assert "insert" not in sql and "update" not in sql and "delete" not in sql


def test_the_fleet_worklist_reads_the_windows_and_nothing_else(capture: SqlCapture) -> None:
    """The one method here that takes no tenant. It returns the breakdown, never a total."""
    sql = sql_text(only(capture.run(lambda: store(capture).fleet_retention_policies())))

    assert "tenants.raw_retention_days" in sql
    assert "tenants.assessment_retention_days" in sql
    assert "icp_config" not in sql
    assert "hmac_secret_ref" not in sql
