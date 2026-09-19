"""Retention and deletion requests, asserted without a database.

**None of this is `integration`-marked, and that is the point.** #31's review found a
production credential path whose only test was Docker-gated, and #33's hid three mutations
to a billing query the same way. The two properties this file exists for are in that
category and worse, because they are promises made to somebody outside the company:

* an **erasure receipt never contains the address** — it is written to a log line and
  handed to a requester's own data controller;
* the model's **`reasoning` has addresses taken out of it** when a lead's payload is
  redacted, because the model quotes the lead and a tombstoned payload with the address
  still sitting in the assessment text is a policy that does nothing.

Both are asserted here, against the real :class:`~leadquali.app.retention.RetentionService`
over :class:`~tests.fakes.InMemoryRetentionStore`, which implements the port's semantics
rather than canning answers. ``tests/unit/test_retention_postgres.py`` proves the SQL the
adapter emits says the same thing, also with no database;
``tests/integration/test_retention_postgres.py`` runs it against a real server for anyone
who has one.

The address used below is distinctive enough that a substring search cannot produce a false
negative, and shaped like a real address so that the redactor's pattern matches it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from leadquali.app.retention import (
    DEFAULT_ASSESSMENT_RETENTION_DAYS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_RAW_RETENTION_DAYS,
    MAX_BATCH_SIZE,
    TOMBSTONE_AT_KEY,
    TOMBSTONE_KEY,
    ErasureReceipt,
    PurgedRecords,
    RetentionPolicy,
    RetentionService,
    UnknownTenantError,
    is_tombstone,
    payload_tombstone,
)
from leadquali.observability import EMAIL_REDACTION, contact_email_hash
from tests.fakes import FakeClock, InMemoryRetentionStore
from tests.logcapture import capture_json_logs

TENANT = "acme"
OTHER_TENANT = "zenith-freight"
NOW = dt.datetime(2026, 9, 17, 9, 0, tzinfo=dt.UTC)

#: The subject of every erasure below. Distinctive, and address-shaped so the redactor
#: would match it if it ever reached a receipt or a log line.
SUBJECT = "ada.lovelace+jat37@analytical-engines-quali.co.uk"

#: A second person on the same tenant, so an erasure that deleted everything would fail.
BYSTANDER = "charles.babbage@difference-engines-quali.co.uk"

#: The model's prose, quoting the lead back at us — the ordinary behaviour #13 found, not
#: an attack. The company name is in here too: the redactor cannot help with that, and the
#: tests below say so rather than implying otherwise.
REASONING_WITH_ADDRESS = (
    f"Strong fit. The enquiry came from {SUBJECT} at Analytical Engines Ltd, who says they "
    "run 40 difference engines and need routing before the Michaelmas board meeting."
)

REASONING_WITHOUT_ADDRESS = (
    "Strong fit on company size and urgency; no budget signal either way in the message."
)


def service(store: InMemoryRetentionStore) -> RetentionService:
    """The real service over the in-memory store, with a clock that does not drift."""
    return RetentionService(store=store, clock=FakeClock(start=NOW, step_ms=0))


def seeded(
    *,
    raw_days: int = DEFAULT_RAW_RETENTION_DAYS,
    assessment_days: int = DEFAULT_ASSESSMENT_RETENTION_DAYS,
) -> InMemoryRetentionStore:
    """One tenant with its windows, and no leads yet."""
    store = InMemoryRetentionStore()
    store.given_tenant(
        TENANT, raw_retention_days=raw_days, assessment_retention_days=assessment_days
    )
    return store


def days_ago(days: int) -> dt.datetime:
    """An instant ``days`` before the fixed ``NOW``."""
    return NOW - dt.timedelta(days=days)


# ------------------------------------------------------------------- the policy itself


def test_the_defaults_are_the_documented_ones() -> None:
    """``docs/data-retention-policy.md`` quotes these two numbers at a customer."""
    assert DEFAULT_RAW_RETENTION_DAYS == 90
    assert DEFAULT_ASSESSMENT_RETENTION_DAYS == 730


def test_the_two_cutoffs_are_the_windows_counted_back_from_now() -> None:
    policy = RetentionPolicy(tenant_id=TENANT, raw_retention_days=90, assessment_retention_days=730)

    assert policy.payload_cutoff(NOW) == NOW - dt.timedelta(days=90)
    assert policy.lead_cutoff(NOW) == NOW - dt.timedelta(days=730)


@pytest.mark.parametrize(("raw", "assessment"), [(0, 730), (-1, 730), (90, 0), (90, -5)])
def test_a_window_of_zero_days_is_not_a_policy(raw: int, assessment: int) -> None:
    """Zero would have the next run redact the lead being qualified right now."""
    with pytest.raises(ValueError, match="positive"):
        RetentionPolicy(
            tenant_id=TENANT, raw_retention_days=raw, assessment_retention_days=assessment
        )


def test_the_payload_cannot_outlive_the_record_it_belongs_to() -> None:
    """The tier split only means something in one direction; see the CHECK constraint."""
    with pytest.raises(ValueError, match="must not exceed"):
        RetentionPolicy(tenant_id=TENANT, raw_retention_days=400, assessment_retention_days=90)


def test_setting_a_policy_is_refused_before_it_reaches_the_database() -> None:
    """An operator gets the sentence, not a constraint name — and nothing is written."""
    store = seeded()

    with pytest.raises(ValueError, match="must not exceed"):
        service(store).set_policy(
            tenant_id=TENANT, raw_retention_days=400, assessment_retention_days=90
        )

    assert store.retention_policy(tenant_id=TENANT).raw_retention_days == 90


def test_a_policy_for_a_tenant_that_does_not_exist_is_refused() -> None:
    with pytest.raises(UnknownTenantError):
        service(seeded()).policy_for("nobody")


# --------------------------------------------------------------------- the tombstone


def test_a_tombstone_says_what_happened_and_when() -> None:
    marker = payload_tombstone(redacted_at=NOW)

    assert marker == {TOMBSTONE_KEY: True, TOMBSTONE_AT_KEY: "2026-09-17T09:00:00+00:00"}
    assert is_tombstone(marker)


def test_a_tombstone_refuses_to_record_an_instant_it_does_not_have() -> None:
    """A naive datetime would be silently interpreted, and the date is the evidence."""
    with pytest.raises(ValueError, match="timezone-aware"):
        payload_tombstone(redacted_at=dt.datetime(2026, 9, 17, 9, 0))


def test_a_form_field_called_redacted_is_not_a_tombstone() -> None:
    """The one way a real payload could be mistaken for a marker, and it cannot.

    A stored payload's values are strings or null — ``LeadForm`` stringifies every unknown
    field — so a form with a ``redacted`` box produces ``"true"``, which is truthy and is
    not ``True``. The check is identity against ``True`` for exactly this reason.
    """
    assert not is_tombstone({"redacted": "true", "email": SUBJECT})
    assert not is_tombstone({"email": SUBJECT})
    assert not is_tombstone({})
    assert not is_tombstone(None)


# ------------------------------------------------------------------------ tier 1: payloads


def test_a_run_with_neither_flag_writes_nothing() -> None:
    """Dry is the default for the one job in the system whose purpose is deleting data."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)

    report = service(store).purge_tenant(tenant_id=TENANT)

    assert report.dry_run
    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 1
    assert store.leads[0].raw_payload["email"] == SUBJECT, "a dry run redacted a payload"


def test_an_expired_payload_is_tombstoned_and_its_assessment_survives() -> None:
    """The whole point of the tier split, in one test."""
    store = seeded()
    lead_id = store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITHOUT_ADDRESS,
    )

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 1
    assert is_tombstone(store.lead(lead_id).raw_payload)
    assert SUBJECT not in json.dumps(store.lead(lead_id).raw_payload)
    # The record, its score and its routing event are all still there.
    assert store.reasoning_of(lead_id) == [REASONING_WITHOUT_ADDRESS]
    assert len(store.routing_events) == 1
    # And the pseudonym survives, which is what makes a later deletion request answerable.
    assert store.lead(lead_id).contact_email_hash == contact_email_hash(SUBJECT)


def test_a_payload_inside_the_window_is_left_alone() -> None:
    store = seeded()
    lead_id = store.given_lead(tenant_id=TENANT, received_at=days_ago(30), email=SUBJECT)

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 0
    assert store.lead(lead_id).raw_payload["email"] == SUBJECT


def test_running_the_purge_twice_changes_nothing_the_second_time() -> None:
    """Idempotence, observed rather than asserted: the second run reports zero.

    The mechanism is the tombstone filter — a row already redacted is not selected — so
    this also proves ``redacted_at`` is not rewritten on every nightly run, which would
    make the date on the tombstone a lie about when the data actually went.
    """
    store = seeded()
    lead_id = store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)
    retention = service(store)

    first = retention.purge_tenant(tenant_id=TENANT, dry_run=False)
    stamped = store.lead(lead_id).raw_payload[TOMBSTONE_AT_KEY]
    second = retention.purge_tenant(tenant_id=TENANT, dry_run=False)

    assert first.changed
    assert not second.changed
    assert second.count(PurgedRecords.LEAD_PAYLOADS) == 0
    assert store.lead(lead_id).raw_payload[TOMBSTONE_AT_KEY] == stamped


def test_the_purge_loops_until_it_drains_rather_than_doing_one_batch() -> None:
    """A batch size is a bound on one transaction, never on one run.

    Three expired leads through a batch of one: all three are redacted, and the store saw
    more than one call. A service that did a single batch would leave two rows of somebody's
    personal data in place and report success.
    """
    store = seeded()
    for age in (300, 250, 200):
        store.given_lead(tenant_id=TENANT, received_at=days_ago(age), email=SUBJECT)

    report = service(store).purge_tenant(tenant_id=TENANT, batch_size=1, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 3
    assert all(is_tombstone(row.raw_payload) for row in store.leads)
    assert store.batches.count(1) > 1


@pytest.mark.parametrize("batch_size", [0, -1, MAX_BATCH_SIZE + 1])
def test_an_unbounded_batch_is_refused(batch_size: int) -> None:
    """The reason to raise the batch size at 3am is impatience."""
    store = seeded()

    with pytest.raises(ValueError, match="batch_size"):
        service(store).purge_tenant(tenant_id=TENANT, batch_size=batch_size, dry_run=False)


def test_the_default_batch_size_is_the_documented_one() -> None:
    assert DEFAULT_BATCH_SIZE == 500
    assert DEFAULT_BATCH_SIZE < MAX_BATCH_SIZE


# ------------------------------------------------- tier 1: the model's own prose (§3)


def test_redacting_a_payload_takes_the_address_out_of_the_reasoning_too() -> None:
    """The property that makes tier 1 mean anything.

    Without this, a lead whose payload was tombstoned on day 90 still has their address in
    ``assessments.reasoning`` for the next 21 months, and the retention policy is a document
    that describes something the system does not do.
    """
    store = seeded()
    lead_id = store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITH_ADDRESS,
    )

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    stored = store.reasoning_of(lead_id)
    assert report.count(PurgedRecords.ASSESSMENT_REASONING) == 1
    assert SUBJECT not in (stored[0] or ""), "the address survived in the model's prose"
    assert EMAIL_REDACTION in (stored[0] or "")
    # The rest of the sentence is intact: the assessment is still readable, which is why
    # this is a redaction rather than deleting the column.
    assert "run 40 difference engines" in (stored[0] or "")


def test_the_redaction_is_address_shaped_and_says_so() -> None:
    """The honest limit, asserted so the policy document cannot overclaim.

    A company name is not a pattern. ``docs/data-retention-policy.md`` says this in as many
    words; the test is here so that the sentence stays true and so that anybody who widens
    the redactor has to come and change it deliberately.
    """
    store = seeded()
    lead_id = store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITH_ADDRESS,
    )

    service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert "Analytical Engines Ltd" in (store.reasoning_of(lead_id)[0] or "")


def test_reasoning_with_no_address_in_it_is_not_rewritten() -> None:
    """Only rows that change are written back.

    Rewriting every reasoning text would turn one tenant's nightly purge into an update of
    every assessment they have ever had, for no effect on any of them.
    """
    store = seeded()
    store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITHOUT_ADDRESS,
    )

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert report.count(PurgedRecords.LEAD_PAYLOADS) == 1
    assert report.count(PurgedRecords.ASSESSMENT_REASONING) == 0


def test_another_leads_reasoning_is_not_touched() -> None:
    """The redaction follows the leads the run actually redacted, not the whole table."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)
    recent = store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(10),
        email=BYSTANDER,
        reasoning=REASONING_WITH_ADDRESS,
    )

    service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert store.reasoning_of(recent) == [REASONING_WITH_ADDRESS]


# ------------------------------------------------------------------------ tier 2: leads


def test_a_lead_past_tier_two_is_deleted_with_everything_hanging_off_it() -> None:
    store = seeded(raw_days=90, assessment_days=180)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(400), email=SUBJECT)

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert report.count(PurgedRecords.LEADS) == 1
    assert store.leads == []
    assert store.assessments == []
    assert store.routing_events == []


def test_tier_two_takes_a_lead_whose_payload_is_already_a_tombstone() -> None:
    """Otherwise a redacted lead would never be deleted and tier 2 would never end."""
    store = seeded(raw_days=90, assessment_days=180)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(400), email=SUBJECT)
    retention = service(store)
    retention.purge_tenant(tenant_id=TENANT, dry_run=False)

    assert store.leads == []


def test_the_sweep_runs_for_every_tenant() -> None:
    store = InMemoryRetentionStore()
    store.given_tenant(TENANT)
    store.given_tenant(OTHER_TENANT)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)
    store.given_lead(tenant_id=OTHER_TENANT, received_at=days_ago(200), email=BYSTANDER)

    reports = service(store).purge_all(dry_run=False)

    assert [report.tenant_id for report in reports] == [TENANT, OTHER_TENANT]
    assert all(is_tombstone(row.raw_payload) for row in store.leads)


def test_one_tenants_purge_leaves_another_tenants_leads_alone() -> None:
    """Invariant 4 at the service level: a window is per tenant and so is the deleting."""
    store = InMemoryRetentionStore()
    store.given_tenant(TENANT, raw_retention_days=30)
    store.given_tenant(OTHER_TENANT, raw_retention_days=365)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)
    other = store.given_lead(tenant_id=OTHER_TENANT, received_at=days_ago(200), email=BYSTANDER)

    service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert store.lead(other).raw_payload["email"] == BYSTANDER


# ------------------------------------------------------------------- deletion requests


def test_an_erasure_deletes_the_persons_leads_and_everything_that_cascades() -> None:
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(20), email=SUBJECT)
    kept = store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=BYSTANDER)

    receipt = service(store).erase_subject(
        tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471, verified by reply-to"
    )

    assert receipt.leads_deleted == 2
    assert receipt.children.assessments == 2
    assert receipt.children.routing_events == 2
    assert receipt.matched_by_hash == 2
    assert receipt.matched_by_payload_scan == 0
    assert [row.lead_id for row in store.leads] == [kept]


def test_the_receipt_never_contains_the_address() -> None:
    """The load-bearing assertion of this file.

    The receipt is logged and is sometimes sent to the requester's own data controller, so
    an address in it would be a disclosure made by the very act of proving a deletion.
    Checked against the rendered text, the JSON and every field value — not against a list
    of field names somebody has to remember to keep current.
    """
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    rendered = receipt.render()
    document = json.dumps(receipt.as_dict())
    for blob in (rendered, document, repr(receipt)):
        assert SUBJECT not in blob
        assert "ada.lovelace" not in blob
        assert "analytical-engines" not in blob
    # Nothing address-shaped under any other spelling either.
    assert "@" not in document
    # The hash is there instead, and it is the one the pipeline writes, so a controller can
    # recompute it from the address they already hold.
    assert receipt.subject_hash == contact_email_hash(SUBJECT)
    assert receipt.subject_hash in rendered


def test_no_field_of_a_receipt_holds_anything_that_looks_like_the_address() -> None:
    """Structural, so a field added later cannot quietly carry one.

    Every value the dataclass holds is stringified and searched, rather than a fixed list
    of names being checked — which is the shape of assertion #13 found passing while the
    thing it guarded leaked.
    """
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    values = json.dumps(receipt.as_dict())
    assert "@" not in values, f"a receipt field carries an address-shaped value: {values}"
    assert set(ErasureReceipt.__dataclass_fields__) == {
        "tenant_id",
        "subject_hash",
        "leads_deleted",
        "children",
        "matched_by_hash",
        "matched_by_payload_scan",
        "requested_by",
        "completed_at",
    }


def test_the_payload_scan_catches_an_address_in_a_field_nobody_modelled() -> None:
    """The "please cc my colleague" case, which the hash cannot see.

    The contact field names somebody else entirely, so ``contact_email_hash`` is the
    bystander's. The subject's address is in the message, and the second net finds it.
    """
    store = seeded()
    store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(10),
        email=BYSTANDER,
        submission={"message": f"Please copy my colleague {SUBJECT} on anything you send."},
    )

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 1
    assert receipt.matched_by_hash == 0
    assert receipt.matched_by_payload_scan == 1
    assert store.leads == []


def test_a_lead_found_by_both_nets_is_only_counted_once() -> None:
    """The two nets overlap on the ordinary case: the contact field is in the payload."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 1
    assert receipt.matched_by_hash == 1
    assert receipt.matched_by_payload_scan == 0


def test_erasing_somebody_we_hold_nothing_about_still_writes_the_audit_row() -> None:
    """ "We checked on this date and held nothing" is only evidence if it was written down.

    It is also the correct answer to a controller whose subject the retention job already
    removed, which is the case ``docs/deletion-requests.md`` spends a section on.
    """
    store = seeded()

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 0
    assert receipt.children.total == 0
    assert len(store.erasures) == 1
    assert store.erasures[0].subject_hash == contact_email_hash(SUBJECT)


def test_an_erasure_is_per_tenant() -> None:
    """The same person can be a lead of two customers; each controller erases their own."""
    store = InMemoryRetentionStore()
    store.given_tenant(TENANT)
    store.given_tenant(OTHER_TENANT)
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)
    theirs = store.given_lead(tenant_id=OTHER_TENANT, received_at=days_ago(10), email=SUBJECT)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert receipt.leads_deleted == 1
    assert [row.lead_id for row in store.leads] == [theirs]


def test_the_audit_row_records_what_the_receipt_says() -> None:
    """Two artifacts, one set of facts. The receipt can be edited; the row cannot."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert store.erasures == [receipt]


def test_an_unattributed_erasure_is_refused() -> None:
    """The audit answers "on whose authority?", which needs an authority."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    with pytest.raises(ValueError, match="requested_by"):
        service(store).erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="  ")

    assert store.leads != []
    assert store.erasures == []


@pytest.mark.parametrize("email", ["", "   "])
def test_an_erasure_with_no_subject_is_refused(email: str) -> None:
    with pytest.raises(ValueError, match="email address"):
        service(seeded()).erase_subject(tenant_id=TENANT, email=email, requested_by="SUP-4471")


def test_find_subject_answers_without_deleting_anything() -> None:
    """The dry run of a deletion request, and the evidence for "no data held"."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    footprint = service(store).find_subject(tenant_id=TENANT, email=SUBJECT)

    assert not footprint.empty
    assert footprint.leads == 1
    assert footprint.children.assessments == 1
    assert store.leads != [], "a lookup deleted a lead"
    assert store.erasures == []


def test_find_subject_says_plainly_when_nothing_is_held() -> None:
    footprint = service(seeded()).find_subject(tenant_id=TENANT, email=SUBJECT)

    assert footprint.empty
    assert footprint.leads == 0
    assert footprint.subject_hash == contact_email_hash(SUBJECT)


def test_a_footprint_carries_no_address_either() -> None:
    """It is rendered to an operator's terminal before an erasure is confirmed."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    footprint = service(store).find_subject(tenant_id=TENANT, email=SUBJECT)

    assert SUBJECT not in repr(footprint)
    assert "email" not in footprint.__dataclass_fields__


# ------------------------------------------------------------------------- the log lines


def test_neither_retention_event_carries_an_address() -> None:
    """Invariant 5 on the two events this module emits, through the real formatter."""
    store = seeded()
    store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITH_ADDRESS,
    )
    retention = service(store)

    with capture_json_logs() as logs:
        retention.purge_tenant(tenant_id=TENANT, dry_run=False)
        retention.erase_subject(tenant_id=TENANT, email=SUBJECT, requested_by="SUP-4471")

    assert logs.text, "nothing was logged, so this test proves nothing"
    assert SUBJECT not in logs.text
    assert EMAIL_REDACTION not in logs.text, "a call site tried to log an address"
    assert logs.one("retention.erased")["contact_email_hash"] == contact_email_hash(SUBJECT)


def test_a_run_that_changed_nothing_emits_no_purge_line() -> None:
    """A daily no-op line would train everybody to ignore the one that matters."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=SUBJECT)

    with capture_json_logs() as logs:
        service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert [record for record in logs.records() if record.get("event") == "retention.purged"] == []


def test_the_purge_line_says_what_it_destroyed() -> None:
    store = seeded()
    store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=SUBJECT,
        reasoning=REASONING_WITH_ADDRESS,
    )

    with capture_json_logs() as logs:
        service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    line: dict[str, Any] = logs.one("retention.purged")
    assert line["tenant_id"] == TENANT
    assert line["payloads_redacted"] == 1
    assert line["reasoning_redacted"] == 1


def test_a_report_renders_every_class_of_record_it_covers() -> None:
    """So a class added later appears in the operator's output without an edit."""
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(200), email=SUBJECT)

    rendered = service(store).purge_tenant(tenant_id=TENANT, dry_run=False).render()

    for kind in PurgedRecords:
        assert kind.value in rendered


def test_a_non_ascii_address_is_redacted_out_of_the_reasoning_too() -> None:
    """The German-lead case, end to end through the service.

    Until #37 widened :func:`~leadquali.observability.pii.redact_emails`, the pattern was
    ASCII-only and matched this address **not at all**. The payload would have been
    tombstoned on schedule, the policy document would have said the right thing, and the
    address would have sat in ``assessments.reasoning`` for another 21 months. The redactor
    is tested directly in ``tests/unit/test_observability.py``; this is the same property
    stated where the obligation actually lives.
    """
    address = "anna@müller-logistik.de"
    store = seeded()
    lead_id = store.given_lead(
        tenant_id=TENANT,
        received_at=days_ago(200),
        email=address,
        reasoning=f"Good fit; the enquiry came from {address} and mentions a Q4 deadline.",
    )

    report = service(store).purge_tenant(tenant_id=TENANT, dry_run=False)

    assert report.count(PurgedRecords.ASSESSMENT_REASONING) == 1
    assert address not in (store.reasoning_of(lead_id)[0] or "")
    assert EMAIL_REDACTION in (store.reasoning_of(lead_id)[0] or "")


def test_a_non_ascii_address_can_be_erased() -> None:
    """The erasure path does not use the pattern at all, and this pins that.

    It hashes the address and substring-matches the payload, both of which are
    alphabet-agnostic. Worth an explicit test because the *redaction* path next to it was
    not, and somebody reading one could reasonably assume the other.
    """
    address = "anna@müller-logistik.de"
    store = seeded()
    store.given_lead(tenant_id=TENANT, received_at=days_ago(10), email=address)

    receipt = service(store).erase_subject(tenant_id=TENANT, email=address, requested_by="SUP-4471")

    assert receipt.leads_deleted == 1
    assert receipt.matched_by_hash == 1
    assert store.leads == []
    assert address not in receipt.render()
