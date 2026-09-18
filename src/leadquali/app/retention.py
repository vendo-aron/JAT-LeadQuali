"""Retention: how long a lead's personal data is kept, and how it stops being kept.

``docs/data-retention-policy.md`` is this module in the customer's language, and
``docs/deletion-requests.md`` is the runbook for :meth:`RetentionService.erase_subject`.
What follows is why the code is shaped the way it is.

Three tiers, and the whole design turns on them
------------------------------------------------

**Tier 1 — the raw payload** (``leads.raw_payload``). The personal data: a name, an
address, a phone number and whatever the person typed into the message box. Kept
:data:`DEFAULT_RAW_RETENTION_DAYS` days by default, overridable per tenant.

**Tier 2 — the assessment record** (``assessments``, ``routing_events``, ``feedback``, and
the ``leads`` row itself minus its payload). Scores, tier, tokens, cost. Kept
:data:`DEFAULT_ASSESSMENT_RETENTION_DAYS` days, because it is what the rubric is tuned
against and what a billing dispute is settled from.

**Tier 3 — aggregates** (``usage_daily``, CloudWatch metrics). Counts and sums per tenant
per day. Not personal data, kept indefinitely, and never touched by anything here.

**Not a tier at all — billing records** (``stripe_events.payload``, #35). Personal data
about a different subject: the *customer's own billing contact*, not an inbound lead. It is
a financial record, so the constraint on it is a statutory minimum rather than a policy
maximum, and nothing in this module touches it. :data:`COLUMN_DISPOSITION` says so, and the
period is a decision for a lawyer rather than a default in this file.

Tier 1 expiring without tier 2 expiring is the entire point. A lead whose payload has gone
still has its score, its tier and its routing history, so last quarter's conversion
analysis survives the person's data being deleted.

A tombstone, not a NULL
-----------------------

``leads.raw_payload`` is ``NOT NULL``, so the redaction writes
:func:`payload_tombstone` — ``{"redacted": true, "redacted_at": "<iso8601>"}`` — over it.
Making the column nullable would have been the smaller migration and the worse answer: a
``NULL`` payload is indistinguishable from a row some future writer got wrong, while a
tombstone says *we did this, on purpose, on this date* to anybody reading the table, and it
is what the admin lead page renders instead of an empty panel.

The marker cannot collide with a real payload. A stored payload is
:meth:`~leadquali.prompts.lead.LeadSubmission.model_dump` of seven string-or-null fields
plus an ``extra`` object whose values are also strings or null, so a top-level JSON
``true`` is a value the ingest path cannot produce. That is what makes
:func:`is_tombstone` — and the ``@>`` predicate the adapter filters on — exact rather than
heuristic.

``reasoning`` is not automatically safe
---------------------------------------

The model's prose routinely quotes the lead back: "Priya at Northstar wrote from
priya@… asking about Q4". #13 found this in the CLI report and fixed it there. It means a
lead whose payload has been tombstoned can still have an address sitting in
``assessments.reasoning`` for the rest of tier 2, so :meth:`RetentionService.
redact_expired_payloads` redacts the reasoning of every lead it tombstones, in the same
pass, with the same redactor the log formatters use
(:func:`~leadquali.observability.pii.redact_emails`).

The redaction happens **here**, in the application layer, rather than as a
``regexp_replace`` in the adapter's SQL. One definition of "what an address looks like" is
the whole reason that function exists; a second one written in POSIX regex would drift from
it silently, and the copy that drifted would be the one running against customer data.
The cost is that the reasoning of one batch is read into memory to be rewritten, which is
why the batch size is bounded.

What it does **not** cover is stated plainly in the policy document: an address is a
pattern and a person's name is not. Nothing in this module can tell that "spoke to the VP
of RevOps in Marylebone" identifies somebody. Tier 2 expiring is what closes that, and it
is why tier 2 has an end.

Batching, and why every call reports a number
----------------------------------------------

The first run against a year of leads must not be one transaction holding one lock. Every
destructive store method takes a ``batch_size``, does at most that much work, and returns
how much it did; the service loops until a pass does nothing, logging progress. That also
makes idempotence observable rather than asserted: the second run reports zero.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.ports import ClockPort
from leadquali.observability.events import log_retention_erased, log_retention_purged
from leadquali.observability.pii import contact_email_hash, redact_emails

__all__ = [
    "COLUMN_DISPOSITION",
    "DEFAULT_ASSESSMENT_RETENTION_DAYS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RAW_RETENTION_DAYS",
    "MAX_BATCH_SIZE",
    "TOMBSTONE_AT_KEY",
    "TOMBSTONE_KEY",
    "ChildCounts",
    "ErasureReceipt",
    "ErasureRequest",
    "ExpiredCounts",
    "PurgeReport",
    "PurgedRecords",
    "RetentionError",
    "RetentionPolicy",
    "RetentionService",
    "RetentionStorePort",
    "SubjectFootprint",
    "UnknownTenantError",
    "is_tombstone",
    "payload_tombstone",
]

LOGGER: Final = logging.getLogger(__name__)

DEFAULT_RAW_RETENTION_DAYS: Final[int] = 90
"""How long a lead's raw payload is kept when a tenant has not chosen otherwise.

Ninety days is the shortest window that still covers the thing the payload is needed for
after qualification: a sales conversation that started from the form, a customer asking
"what exactly did this person send us?", and a rubric complaint investigated a quarter
later. Mirrors the server default on ``tenants.raw_retention_days``;
``tests/unit/test_db_schema.py`` pins the two together."""

DEFAULT_ASSESSMENT_RETENTION_DAYS: Final[int] = 730
"""How long the assessment record is kept when a tenant has not chosen otherwise.

Two years. It is the window over which the rubric is tuned and conversion is measured, and
it is long enough that a billing dispute about a period the customer has already closed can
still be settled from the rows it was computed from. Mirrors
``tenants.assessment_retention_days``."""

DEFAULT_BATCH_SIZE: Final[int] = 500
"""Rows one destructive statement may touch.

Small enough that the row locks it takes are held for milliseconds and a cancelled job
leaves a coherent database; large enough that a year of backlog clears in minutes rather
than hours. The service loops, so this is a bound on one transaction, never on one run."""

MAX_BATCH_SIZE: Final[int] = 10_000
"""The largest batch an operator may ask for.

A bound rather than a suggestion: the reason to raise the batch size at 3am is impatience,
and an unbounded ``--batch-size`` is a table lock with a flag in front of it."""

TOMBSTONE_KEY: Final[str] = "redacted"
"""The marker key. ``true`` as a JSON boolean, which a real payload cannot contain."""

TOMBSTONE_AT_KEY: Final[str] = "redacted_at"
"""When the payload was replaced, ISO-8601 UTC. The evidence half of the tombstone."""


class RetentionError(RuntimeError):
    """A retention operation could not be carried out."""


class UnknownTenantError(RetentionError):
    """There is no such tenant."""


def payload_tombstone(*, redacted_at: dt.datetime) -> dict[str, Any]:
    """The document written over an expired ``leads.raw_payload``.

    Args:
        redacted_at: when the redaction happened. Rendered as ISO-8601; a naive datetime is
            refused rather than assumed to be UTC, because a tombstone that is wrong about
            *when* is worse than one that is not there.

    Returns:
        ``{"redacted": True, "redacted_at": "<iso8601>"}``.

    Raises:
        ValueError: ``redacted_at`` carries no timezone.
    """
    if redacted_at.tzinfo is None:
        raise ValueError("redacted_at must be timezone-aware; a tombstone records an instant")
    return {
        TOMBSTONE_KEY: True,
        TOMBSTONE_AT_KEY: redacted_at.astimezone(dt.UTC).isoformat(),
    }


def is_tombstone(payload: Mapping[str, Any] | None) -> bool:
    """Whether ``payload`` is a redaction marker rather than a submission.

    The check is ``payload["redacted"] is True`` and not merely truthy: a form field
    literally called ``redacted`` would arrive as the *string* ``"true"``, which is truthy
    and is not this.
    """
    return bool(payload) and payload is not None and payload.get(TOMBSTONE_KEY) is True


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """One tenant's two windows, as stored on its ``tenants`` row."""

    tenant_id: str
    raw_retention_days: int
    assessment_retention_days: int

    def __post_init__(self) -> None:
        """Refuse a policy the database's CHECK constraints would refuse."""
        if self.raw_retention_days <= 0 or self.assessment_retention_days <= 0:
            raise ValueError("retention windows must be positive numbers of days")
        if self.raw_retention_days > self.assessment_retention_days:
            raise ValueError(
                "raw_retention_days must not exceed assessment_retention_days: keeping the "
                "payload longer than the record it belongs to inverts the tier split"
            )

    def payload_cutoff(self, now: dt.datetime) -> dt.datetime:
        """Leads received before this instant have an expired payload (tier 1)."""
        return now - dt.timedelta(days=self.raw_retention_days)

    def lead_cutoff(self, now: dt.datetime) -> dt.datetime:
        """Leads received before this instant are past tier 2 and are deleted outright."""
        return now - dt.timedelta(days=self.assessment_retention_days)


@dataclass(frozen=True, slots=True)
class ExpiredCounts:
    """What a dry run found waiting for the next apply."""

    tenant_id: str
    payloads: int
    """Leads whose payload is past tier 1 and is not already a tombstone."""

    leads: int
    """Leads past tier 2, which are deleted whole."""

    @property
    def total(self) -> int:
        """Everything the next run would touch."""
        return self.payloads + self.leads


class PurgedRecords(StrEnum):
    """The classes of record one purge run destroys, and the keys of its report.

    An enum rather than three named fields on the report, because "what does retention
    cover?" is a list that grows: a column that can hold personal data added anywhere in the
    schema becomes a member here, a step in :meth:`RetentionService.purge_tenant` and an
    entry in :data:`COLUMN_DISPOSITION`, and everything that renders a report — the CLI, the
    log line, the Lambda's result — picks it up without being edited.
    """

    LEAD_PAYLOADS = "lead_payloads"
    """``leads.raw_payload`` values replaced by a tombstone. Tier 1."""

    ASSESSMENT_REASONING = "assessment_reasoning"
    """``assessments.reasoning`` rows that had an address taken out. Tier 1, same pass."""

    LEADS = "leads"
    """Whole ``leads`` rows deleted, children cascaded. Tier 2."""


#: Every column in the schema that may hold personal data, and what retention does with it.
#:
#: Keyed exactly like ``adapters.db_schema.PERSONAL_DATA_COLUMNS``, and
#: ``tests/unit/test_retention.py`` asserts the two cover the same set of columns. That is
#: the mechanism: a column that can hold personal data is added to the schema's inventory,
#: and until somebody has said here what the retention job does about it, the suite fails.
#: A column whose honest answer is "nothing, it goes when its lead does" is a legitimate
#: entry — the point is that the answer is written down, not that it is always a purge.
#:
#: Plain strings rather than a reference to the schema, because ``app`` may not import
#: ``adapters`` (CLAUDE.md's layering rule) and this is a statement of policy, not a query.
COLUMN_DISPOSITION: Final[Mapping[tuple[str, str], str]] = {
    ("leads", "raw_payload"): (
        "replaced with a tombstone at the end of tier 1; the row itself goes at tier 2"
    ),
    ("assessments", "reasoning"): (
        "addresses redacted in the same pass that tombstones the lead's payload, because "
        "the model quotes the lead; the row goes at tier 2. Redaction is address-shaped "
        "only — a name in the model's prose survives until tier 2 ends"
    ),
    ("feedback", "notes"): (
        "not redacted, and deliberately: it is a sales rep's own words about a lead, the "
        "product's only training signal, and rewriting it would corrupt the feedback loop "
        "for a text that is not reliably identifying. It goes at tier 2 with its lead"
    ),
    ("golden_promotions", "note"): (
        "not redacted, for the same reason as feedback.notes: a staff rationale about one "
        "lead, deleted with the lead at tier 2. The golden-set line rendered from it is "
        "pseudonymised separately by #22's strip_pii before anything is committed"
    ),
    ("stripe_events", "payload"): (
        "**not touched by this job, deliberately.** It is a financial record about the "
        "customer's own billing contact rather than an inbound lead, so it carries a "
        "statutory *minimum* retention measured in years instead of a policy maximum "
        "measured in days, and applying the lead tiers to it would destroy evidence we are "
        "required to keep. The period, and whether an erasure request reaches it at all, "
        "are on docs/dpa-draft.md's list of decisions a lawyer must make; until one does, "
        "it is retained indefinitely and no code path deletes it"
    ),
}


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """What one run of the purge did for one tenant, per class of record."""

    tenant_id: str
    ran_at: dt.datetime
    dry_run: bool
    counts: Mapping[PurgedRecords, int] = field(default_factory=dict)

    def count(self, kind: PurgedRecords) -> int:
        """How many rows of one class this run touched. Zero when it touched none."""
        return self.counts.get(kind, 0)

    @property
    def changed(self) -> bool:
        """Whether this run wrote anything. False on the second run, by construction."""
        return any(self.counts.values())

    def render(self) -> str:
        """One line per class of record, for an operator watching a run."""
        prefix = "would purge" if self.dry_run else "purged"
        body = ", ".join(f"{kind.value}={self.count(kind)}" for kind in PurgedRecords)
        return f"{self.tenant_id}: {prefix} {body}"


@dataclass(frozen=True, slots=True)
class ChildCounts:
    """Rows hanging off a set of leads, per table.

    Counted before the delete rather than derived from it: ``ON DELETE CASCADE`` removes
    them inside the server and reports nothing back, and "we deleted four rows across three
    tables" is the sentence a controller is owed.
    """

    assessments: int = 0
    routing_events: int = 0
    feedback: int = 0
    golden_promotions: int = 0

    @property
    def total(self) -> int:
        """Every child row across every table."""
        return self.assessments + self.routing_events + self.feedback + self.golden_promotions


@dataclass(frozen=True, slots=True)
class SubjectFootprint:
    """What is held about one person, without deleting any of it.

    Answers two questions that are the same query: what would an erasure remove, and — when
    every count is zero — the evidenced "we hold no data about this person" that a
    controller is entitled to when the retention job got there first.

    It carries the **hash**, never the address, for the same reason
    :class:`ErasureReceipt` does.
    """

    tenant_id: str
    subject_hash: str
    lead_ids: tuple[str, ...]
    matched_by_hash: int
    matched_by_payload_scan: int
    children: ChildCounts

    @property
    def leads(self) -> int:
        """How many lead rows this person appears in."""
        return len(self.lead_ids)

    @property
    def empty(self) -> bool:
        """Whether nothing at all is held. The "no data held" answer, evidenced."""
        return not self.lead_ids


@dataclass(frozen=True, slots=True)
class ErasureReceipt:
    """Proof that one person's data was deleted, in a form that can be handed over.

    **This object never carries the address.** It is written to a log line, stored in
    ``erasure_log`` and sometimes sent to the requester's own data controller, and each of
    those is a place invariant 5 governs. The subject is identified by
    :func:`~leadquali.observability.pii.contact_email_hash` — the same hash
    ``leads.contact_email_hash`` holds, so the controller can recompute it from the address
    they already have and satisfy themselves that this receipt is about their request.

    ``tests/unit/test_retention.py`` asserts the absence structurally: a receipt is built
    from a real erasure of a real address and every field, rendered and as JSON, is searched
    for it.
    """

    tenant_id: str
    subject_hash: str
    leads_deleted: int
    children: ChildCounts
    matched_by_hash: int
    matched_by_payload_scan: int
    """Leads the hash did not find, and the payload scan did. A non-zero value here means
    the person's address was in a *form field other than the contact field* — the "please
    cc my colleague" case — and it is worth reading, because it is the only signal that the
    tenant's form collects addresses somewhere nobody modelled."""

    requested_by: str
    completed_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        """The receipt as JSON-safe plain data, for a file or a log field."""
        document = asdict(self)
        document["children"] = asdict(self.children)
        document["completed_at"] = self.completed_at.astimezone(dt.UTC).isoformat()
        document["rows_deleted"] = self.leads_deleted + self.children.total
        return document

    def render(self) -> str:
        """The receipt as text, for pasting into a reply to the requester."""
        children = self.children
        return "\n".join(
            [
                "LeadQuali erasure receipt",
                f"  tenant:              {self.tenant_id}",
                f"  subject (SHA-256):   {self.subject_hash}",
                f"  completed at:        {self.completed_at.astimezone(dt.UTC).isoformat()}",
                f"  requested by:        {self.requested_by}",
                "",
                f"  leads deleted:       {self.leads_deleted}",
                f"    found by hash:     {self.matched_by_hash}",
                f"    found by scan:     {self.matched_by_payload_scan}",
                f"  assessments:         {children.assessments}",
                f"  routing events:      {children.routing_events}",
                f"  feedback:            {children.feedback}",
                f"  golden promotions:   {children.golden_promotions}",
                "",
                "No email address appears in this receipt. The hash above is SHA-256 of the",
                "address lowercased and stripped, and can be recomputed to check this is",
                "the right subject.",
            ]
        )


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    """Everything the store needs to carry out one erasure and file the evidence.

    A parameter object rather than nine keyword arguments, because the store method is the
    atomic one — the delete and the audit row commit together — and a signature that long
    is where an argument gets passed in the wrong position on the day somebody is editing
    it at 3am.
    """

    subject_hash: str
    lead_ids: tuple[str, ...]
    matched_by_hash: int
    matched_by_payload_scan: int
    requested_by: str
    completed_at: dt.datetime


@runtime_checkable
class RetentionStorePort(Protocol):
    """Where retention reads its windows and does its deleting.

    Every method takes ``tenant_id`` and filters on it (invariant 4), except
    :meth:`fleet_retention_policies`, which is the scheduled job's worklist and says so in
    its name — the same convention #33's ``fleet_*`` methods established.

    Every method raises on failure. A retention job that swallowed an error would report
    "nothing to do" and leave personal data in place, which is the one failure mode this
    module must not have.
    """

    def fleet_retention_policies(self) -> Sequence[RetentionPolicy]:
        """Every tenant's windows, for the scheduled sweep to iterate over.

        Fleet-wide on purpose: a daily purge has to know who to run for, and a job that
        took a tenant would simply never run for the tenant somebody forgot to list.
        Returns the per-tenant breakdown, never a total.
        """
        ...

    def retention_policy(self, *, tenant_id: str) -> RetentionPolicy:
        """One tenant's windows.

        Raises:
            UnknownTenantError: no such tenant.
        """
        ...

    def set_retention_policy(
        self, *, tenant_id: str, raw_retention_days: int, assessment_retention_days: int
    ) -> RetentionPolicy:
        """Write one tenant's windows and return them as stored.

        Raises:
            UnknownTenantError: no such tenant.
            RetentionError: the database refused the windows.
        """
        ...

    def count_expired(
        self, *, tenant_id: str, payload_cutoff: dt.datetime, lead_cutoff: dt.datetime
    ) -> ExpiredCounts:
        """How much a run would touch, writing nothing."""
        ...

    def redact_expired_payloads(
        self,
        *,
        tenant_id: str,
        cutoff: dt.datetime,
        tombstone: Mapping[str, Any],
        batch_size: int,
    ) -> Sequence[str]:
        """Tombstone at most ``batch_size`` expired payloads.

        Must skip rows that are already tombstones, or the job never terminates and every
        run rewrites ``redacted_at``.

        Returns:
            The ids of the leads this call redacted. Empty means there is no more work.
        """
        ...

    def reasoning_for_leads(self, *, tenant_id: str, lead_ids: Sequence[str]) -> Mapping[str, str]:
        """Every non-empty ``assessments.reasoning`` for these leads, by assessment id."""
        ...

    def replace_reasoning(self, *, tenant_id: str, replacements: Mapping[str, str]) -> int:
        """Overwrite the named assessments' ``reasoning``. Returns rows changed."""
        ...

    def purge_expired_leads(self, *, tenant_id: str, cutoff: dt.datetime, batch_size: int) -> int:
        """Delete at most ``batch_size`` leads past tier 2. Returns how many went.

        The composite ``ON DELETE CASCADE`` takes the assessments, routing events, feedback
        and golden promotions with them. The ``tenants`` foreign key is ``RESTRICT``, so no
        tenant can be removed as a side effect of this.
        """
        ...

    def leads_for_subject(self, *, tenant_id: str, subject_hash: str) -> Sequence[str]:
        """Lead ids whose ``contact_email_hash`` is this subject. The indexed path."""
        ...

    def leads_mentioning(self, *, tenant_id: str, needle: str) -> Sequence[str]:
        """Lead ids whose stored payload contains ``needle`` anywhere.

        The second net, and it is slow by design: a substring match over the whole JSONB
        document, bounded to one tenant, with no index that can serve it. It exists because
        an address can be in a form field other than the contact field — "please cc my
        colleague", a second contact box, a message that quotes the sender — and the hash
        only knows about the contact field.
        """
        ...

    def count_lead_children(self, *, tenant_id: str, lead_ids: Sequence[str]) -> ChildCounts:
        """Rows that will cascade when these leads go, per table.

        Not every child of a ``leads`` row cascades in the same way across the schema, so
        this counts the tables that do rather than inferring them: ``assessments``,
        ``routing_events``, ``feedback`` and ``golden_promotions``.
        """
        ...

    def erase(self, *, tenant_id: str, request: ErasureRequest) -> ErasureReceipt:
        """Delete these leads and write the audit row, in **one** transaction.

        One method rather than a delete followed by a record, because the two must not be
        able to come apart. Deleting the rows and then failing to write the audit row would
        leave an erasure nobody can prove, and proving it is half the obligation; writing
        the row first would leave a claim that is not true. Either both commit or neither
        does, and the operator re-runs.

        Returns:
            The receipt as stored, so the counts on the paper and the counts in the table
            are the same numbers rather than two computations of them.
        """
        ...


class RetentionService:
    """The retention job and the deletion-request path, over a store and a clock."""

    def __init__(self, *, store: RetentionStorePort, clock: ClockPort) -> None:
        """Take the store to act through and the clock that decides what "now" is."""
        self._store = store
        self._clock = clock

    # ------------------------------------------------------------------- the schedule

    def policies(self) -> Sequence[RetentionPolicy]:
        """Every tenant's windows — the scheduled sweep's worklist."""
        return self._store.fleet_retention_policies()

    def policy_for(self, tenant_id: str) -> RetentionPolicy:
        """One tenant's windows.

        Raises:
            UnknownTenantError: no such tenant.
        """
        return self._store.retention_policy(tenant_id=tenant_id)

    def set_policy(
        self, *, tenant_id: str, raw_retention_days: int, assessment_retention_days: int
    ) -> RetentionPolicy:
        """Override one tenant's windows.

        Validated here before it is sent, so an operator gets the sentence explaining why
        rather than a constraint name. The database checks it again regardless — a row
        written by psql during an incident has to be as well-formed as one written here.

        Raises:
            ValueError: the windows are not positive, or raw exceeds assessment.
            UnknownTenantError: no such tenant.
        """
        RetentionPolicy(
            tenant_id=tenant_id,
            raw_retention_days=raw_retention_days,
            assessment_retention_days=assessment_retention_days,
        )
        return self._store.set_retention_policy(
            tenant_id=tenant_id,
            raw_retention_days=raw_retention_days,
            assessment_retention_days=assessment_retention_days,
        )

    def pending(self, *, tenant_id: str, now: dt.datetime | None = None) -> ExpiredCounts:
        """What a run would touch for this tenant, writing nothing."""
        moment = now if now is not None else self._clock.now()
        policy = self._store.retention_policy(tenant_id=tenant_id)
        return self._store.count_expired(
            tenant_id=tenant_id,
            payload_cutoff=policy.payload_cutoff(moment),
            lead_cutoff=policy.lead_cutoff(moment),
        )

    def purge_tenant(
        self,
        *,
        tenant_id: str,
        now: dt.datetime | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        dry_run: bool = True,
    ) -> PurgeReport:
        """Run both tiers for one tenant. Dry by default — see :meth:`purge_all`.

        Order matters and is tier 1 then tier 2: redacting payloads first means a lead that
        is about to be deleted anyway costs one pointless update, while the other order
        would leave a window where a crashed job had deleted the newest expiries and not
        the oldest.

        Raises:
            ValueError: ``batch_size`` is outside ``1..MAX_BATCH_SIZE``.
            UnknownTenantError: no such tenant.
        """
        moment = now if now is not None else self._clock.now()
        checked = _checked_batch_size(batch_size)
        policy = self._store.retention_policy(tenant_id=tenant_id)

        if dry_run:
            counts = self._store.count_expired(
                tenant_id=tenant_id,
                payload_cutoff=policy.payload_cutoff(moment),
                lead_cutoff=policy.lead_cutoff(moment),
            )
            # No reasoning figure on a dry run, and it is a gap rather than a zero: finding
            # out how many reasoning texts hold an address means reading and redacting every
            # one of them, which is the work the dry run exists to avoid. The document says
            # so; a number invented here would be believed.
            return PurgeReport(
                tenant_id=tenant_id,
                ran_at=moment,
                dry_run=True,
                counts={
                    PurgedRecords.LEAD_PAYLOADS: counts.payloads,
                    PurgedRecords.LEADS: counts.leads,
                },
            )

        payloads, reasoning = self._redact_payloads(
            tenant_id=tenant_id, cutoff=policy.payload_cutoff(moment), batch_size=checked
        )
        purged = self._purge_leads(
            tenant_id=tenant_id, cutoff=policy.lead_cutoff(moment), batch_size=checked
        )
        report = PurgeReport(
            tenant_id=tenant_id,
            ran_at=moment,
            dry_run=False,
            counts={
                PurgedRecords.LEAD_PAYLOADS: payloads,
                PurgedRecords.ASSESSMENT_REASONING: reasoning,
                PurgedRecords.LEADS: purged,
            },
        )
        if report.changed:
            log_retention_purged(
                LOGGER,
                tenant_id=tenant_id,
                payloads_redacted=payloads,
                reasoning_redacted=reasoning,
                leads_purged=purged,
            )
        return report

    def purge_all(
        self,
        *,
        now: dt.datetime | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        dry_run: bool = True,
    ) -> Sequence[PurgeReport]:
        """Run the purge for every tenant, oldest first.

        ``dry_run`` defaults to ``True`` everywhere it appears in this module. A retention
        job is the one piece of code in the system whose entire job is deleting customer
        data, and the default for a destructive operation is the one you would want if the
        argument were dropped on the way in.
        """
        return [
            self.purge_tenant(
                tenant_id=policy.tenant_id, now=now, batch_size=batch_size, dry_run=dry_run
            )
            for policy in self._store.fleet_retention_policies()
        ]

    def _redact_payloads(
        self, *, tenant_id: str, cutoff: dt.datetime, batch_size: int
    ) -> tuple[int, int]:
        """Tombstone expired payloads batch by batch. Returns (payloads, reasoning rows)."""
        payloads = 0
        reasoning = 0
        while True:
            tombstone = payload_tombstone(redacted_at=self._clock.now())
            redacted = self._store.redact_expired_payloads(
                tenant_id=tenant_id,
                cutoff=cutoff,
                tombstone=tombstone,
                batch_size=batch_size,
            )
            if not redacted:
                return payloads, reasoning
            payloads += len(redacted)
            reasoning += self._redact_reasoning_of(tenant_id=tenant_id, lead_ids=redacted)

    def _redact_reasoning_of(self, *, tenant_id: str, lead_ids: Sequence[str]) -> int:
        """Take addresses out of the model's prose for these leads. Returns rows changed.

        Only rows that actually change are written back: the overwhelming majority of
        reasoning texts quote no address, and rewriting them all would turn one tenant's
        purge into an update of every assessment row it has.
        """
        stored = self._store.reasoning_for_leads(tenant_id=tenant_id, lead_ids=lead_ids)
        replacements = {
            assessment_id: redacted
            for assessment_id, reasoning in stored.items()
            if (redacted := redact_emails(reasoning)) != reasoning
        }
        if not replacements:
            return 0
        return self._store.replace_reasoning(tenant_id=tenant_id, replacements=replacements)

    def _purge_leads(self, *, tenant_id: str, cutoff: dt.datetime, batch_size: int) -> int:
        """Delete leads past tier 2 batch by batch. Returns how many went."""
        purged = 0
        while True:
            deleted = self._store.purge_expired_leads(
                tenant_id=tenant_id, cutoff=cutoff, batch_size=batch_size
            )
            if deleted <= 0:
                return purged
            purged += deleted

    # ----------------------------------------------------------------- deletion requests

    def find_subject(self, *, tenant_id: str, email: str) -> SubjectFootprint:
        """What is held about this person, deleting nothing.

        Both nets are cast: the indexed lookup on ``contact_email_hash``, and the payload
        scan for an address sitting in some other form field. Use this to answer a
        controller before acting, and to evidence "no data held" when the answer is nothing.

        Raises:
            ValueError: ``email`` is blank.
        """
        subject_hash = _subject_hash(email)
        by_hash = list(
            self._store.leads_for_subject(tenant_id=tenant_id, subject_hash=subject_hash)
        )
        seen = set(by_hash)
        by_scan = [
            lead_id
            for lead_id in self._store.leads_mentioning(
                tenant_id=tenant_id, needle=email.strip().lower()
            )
            if lead_id not in seen
        ]
        lead_ids = tuple(by_hash + by_scan)
        children = (
            self._store.count_lead_children(tenant_id=tenant_id, lead_ids=lead_ids)
            if lead_ids
            else ChildCounts()
        )
        return SubjectFootprint(
            tenant_id=tenant_id,
            subject_hash=subject_hash,
            lead_ids=lead_ids,
            matched_by_hash=len(by_hash),
            matched_by_payload_scan=len(by_scan),
            children=children,
        )

    def erase_subject(
        self,
        *,
        tenant_id: str,
        email: str,
        now: dt.datetime | None = None,
        requested_by: str,
    ) -> ErasureReceipt:
        """Delete everything held about one person, and return the proof.

        The leads go and the composite ``ON DELETE CASCADE`` takes their assessments,
        routing events, feedback and golden promotions. The children are counted *before*
        the delete, because the cascade happens inside the server and reports nothing back.
        The delete and the ``erasure_log`` row commit in one transaction
        (:meth:`RetentionStorePort.erase`): an erasure that cannot be evidenced and a
        claimed erasure that did not happen are both worse than a failure the operator can
        see and re-run.

        An erasure that finds nothing is still an erasure: it returns a receipt with zero
        counts and writes the audit row anyway, because "we checked on this date and held
        nothing" is exactly the evidence a controller asks for when the retention job got
        there first.

        Args:
            tenant_id: whose data. Erasure is per tenant: the same person can be a lead of
                two customers, and each controller may only erase their own copy.
            email: the address to erase. Never stored, never logged, never in the receipt.
            now: the completion instant. ``None`` reads the clock.
            requested_by: who asked and how it was verified — a ticket reference, not a
                name. Recorded in ``erasure_log`` so the audit row says on whose authority.

        Returns:
            The :class:`ErasureReceipt`, which carries the hash and never the address.

        Raises:
            ValueError: ``email`` or ``requested_by`` is blank.
            RetentionError: the delete or the audit write failed.
        """
        if not requested_by.strip():
            raise ValueError("requested_by must say who asked; an unattributed erasure is not one")
        moment = now if now is not None else self._clock.now()
        footprint = self.find_subject(tenant_id=tenant_id, email=email)
        receipt = self._store.erase(
            tenant_id=tenant_id,
            request=ErasureRequest(
                subject_hash=footprint.subject_hash,
                lead_ids=footprint.lead_ids,
                matched_by_hash=footprint.matched_by_hash,
                matched_by_payload_scan=footprint.matched_by_payload_scan,
                requested_by=requested_by.strip(),
                completed_at=moment,
            ),
        )
        log_retention_erased(
            LOGGER,
            tenant_id=tenant_id,
            subject_hash=receipt.subject_hash,
            leads_deleted=receipt.leads_deleted,
            rows_deleted=receipt.leads_deleted + receipt.children.total,
            matched_by_payload_scan=receipt.matched_by_payload_scan,
            requested_by=receipt.requested_by,
        )
        return receipt


def _subject_hash(email: str) -> str:
    """The subject identifier: the pipeline's own hash of the address.

    Imported from :mod:`leadquali.observability.pii` rather than recomputed, because a
    deletion request that hashed differently from ``leads.contact_email_hash`` would find
    nothing and report success.

    Raises:
        ValueError: the address is blank, so there is no subject to look for.
    """
    digest = contact_email_hash(email)
    if digest is None:
        raise ValueError("an erasure request needs an email address to identify the subject")
    return digest


def _checked_batch_size(batch_size: int) -> int:
    """Refuse a batch size that would make one transaction a table lock."""
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}, got {batch_size}")
    return batch_size
