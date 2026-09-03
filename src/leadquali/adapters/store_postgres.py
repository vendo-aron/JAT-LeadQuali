"""Postgres implementations of the store and tenant-config ports.

Two adapters live here, both over the same tables and the same session factory:
:class:`PostgresLeadStore` (:class:`~leadquali.app.ports.LeadStorePort`) and
:class:`PostgresTenantConfigSource` (:class:`~leadquali.app.ports.TenantConfigPort`). The
schema itself is :mod:`leadquali.adapters.db_schema` and is not touched here — this module
is only the behaviour, which is why the migration environment can import the tables without
importing connection handling.

What the whole module is organised around
-----------------------------------------

**Invariant 4: every method takes a tenant and every statement filters on it.** Not one
query in this file can be executed without a tenant predicate — including the "obviously
unambiguous" ones where ``lead_id`` is a UUID and could not collide. That is not
defensiveness about collisions, it is the property #32's isolation suite exists to attack:
an adapter that filters on the tenant *cannot* serve tenant B's row to tenant A even when
a caller is confused, and the way to keep that true a year from now is for there to be no
code path in which it is optional. The upsert goes further and repeats the tenant in the
``ON CONFLICT ... WHERE`` clause, so the predicate is in the SQL even where the unique
index already implies it.

**Idempotency is the database's job, not the worker's.** SQS is at-least-once, so
:meth:`PostgresLeadStore.upsert_lead` is one ``INSERT ... ON CONFLICT DO UPDATE ...
RETURNING`` against ``uq_leads_tenant_id_submission_id``. A ``SELECT`` followed by an
``INSERT`` would be correct in a test and wrong in production: two workers handling the
same redelivered message interleave between the two statements, one of them raises a
uniqueness error, and the lead that was already stored looks like a crash. ``DO UPDATE``
rather than ``DO NOTHING`` because ``DO NOTHING`` returns no row for the loser of that race
— and the follow-up ``SELECT`` may not see the winner's row until it commits — whereas
``DO UPDATE`` waits for the concurrent transaction, then returns the surviving row. The
"update" writes the submission id back onto itself: it changes nothing, and it is what
makes ``RETURNING`` produce a row in both branches.

``is_new`` then comes from ``RETURNING id, (xmax = 0)``: on a row this statement inserted
the system column ``xmax`` is zero, and on one it updated it holds the current transaction
id. It is the standard Postgres idiom for "did my ``ON CONFLICT`` insert or update", and it
is the only way to answer it in a single round trip.

**No SQL is assembled from strings.** Every statement is built from SQLAlchemy Core
constructs over the mapped tables, so tenant ids, submission ids and lead payloads travel
as bound parameters. The single literal fragment in the file is the constant
``(xmax = 0)``, which contains no input of any kind.

Connection handling, and how it meets #27
-----------------------------------------

There is **no engine at import time**. :func:`engine_for` is memoised per URL and builds
the engine on first use, so importing this module — which the Lambda bundle does at cold
start, before any configuration may have been resolved — neither opens a socket nor fails
in a place where the traceback has no request to attach itself to. The first call inside
the container creates it; every later invocation on that warm container reuses it. That is
the "one connection per container" shape a Lambda wants:

* ``pool_size=1`` with ``max_overflow=0`` — a Lambda container serves one invocation at a
  time, so a second pooled connection would sit idle holding a backend slot. It also makes
  the arithmetic in #27 trivial: **reserved concurrency is the connection budget.** N
  concurrent workers hold at most N server connections, and the number to reason about is
  the one already written in the function's configuration.
* ``pool_pre_ping=True`` and a ``pool_recycle`` below any proxy or server idle timeout. A
  warm container can sit unused for minutes; RDS Proxy (or the server, or a NAT idle
  timer) may have dropped the connection in the meantime, and the pre-ping turns what would
  be a failed invocation into a transparent reconnect.
* Prepared statements are disabled on the psycopg connection (``prepare_threshold=None``).
  Server-side prepared statements are session state, and session state is what makes RDS
  Proxy *pin* a client to a backend connection — pinning defeats the multiplexing that is
  the reason for putting the proxy in front of Lambda at all. Giving that up costs a plan
  per statement; keeping it would cost the proxy's whole purpose.
* Every method opens a session, does its work and commits. There are no long-lived
  transactions and nothing is held across an invocation — another pinning trigger, and the
  reason a worker that dies mid-run leaves no locks behind for the redelivery to wait on.

The store takes its ``sessionmaker`` as a constructor argument rather than reaching for a
global, so the entrypoint decides (#26 wires one per container, the integration tests bind
one to a transaction they roll back), and :meth:`PostgresLeadStore.from_env` is the
convenience that reads ``DATABASE_URL`` through :class:`~leadquali.config.Settings`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    Engine,
    create_engine,
    func,
    insert,
    literal_column,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import Assessment, Feedback, Lead, RoutingEvent, Tenant
from leadquali.adapters.seed import tenant_id_for
from leadquali.app.assessment_result import AssessmentOutcome, CallMetering
from leadquali.app.feedback import UnknownLeadError, Verdict
from leadquali.app.ports import RecordedFeedback, RoutingOutcome, StoredLead
from leadquali.config import Settings, get_settings
from leadquali.domain.models import Action, RoutingDecision
from leadquali.domain.tenant_config import (
    TENANT_ID_PATTERN,
    TenantConfig,
    TenantConfigError,
    TenantNotFoundError,
)
from leadquali.prompts.lead import LeadSubmission

__all__ = [
    "FEEDBACK_IDEMPOTENCY_CONSTRAINT",
    "LEAD_IDEMPOTENCY_CONSTRAINT",
    "UNKNOWN_MODEL_ID",
    "UNKNOWN_PROMPT_VERSION",
    "PostgresFeedbackStore",
    "PostgresLeadStore",
    "PostgresTenantConfigSource",
    "assessment_values",
    "contact_email_hash",
    "engine_for",
    "lead_uuid",
    "session_factory",
    "tenant_uuid",
]

LOGGER = logging.getLogger(__name__)

#: The unique constraint ``upsert_lead`` resolves its conflict against. Named rather than
#: inferred from columns so that a rename in #15's schema breaks loudly here.
LEAD_IDEMPOTENCY_CONSTRAINT: Final[str] = "uq_leads_tenant_id_submission_id"

#: The unique constraint ``record_feedback`` resolves its conflict against: one verdict per
#: rater per lead, which is what makes a second click an update rather than a second row.
FEEDBACK_IDEMPOTENCY_CONSTRAINT: Final[str] = "uq_feedback_tenant_id_lead_id_rater"

#: SQLSTATE for ``foreign_key_violation``. The composite ``(tenant_id, lead_id)`` key is the
#: only one this module can trip, so it means "no such lead for this tenant" and nothing else.
_FOREIGN_KEY_VIOLATION: Final[str] = "23503"

#: Recorded as the model provenance of an attempt that never reached the model — a
#: connection error or a timeout has no ``model_id`` to report, and the columns are NOT
#: NULL because every *billed* row must have them. A sentinel is honest and greppable;
#: guessing the configured model would put a lie in the billing table.
UNKNOWN_MODEL_ID: Final[str] = "unknown"
UNKNOWN_PROMPT_VERSION: Final[str] = "unknown"

#: ``assessments.status`` values. Mirrors ``db_schema.ASSESSMENT_STATUSES``.
ASSESSMENT_STATUS_OK: Final[str] = "ok"
ASSESSMENT_STATUS_FAILED: Final[str] = "failed"

#: ``leads.status`` values this adapter writes. Mirrors ``db_schema.LEAD_STATUSES``:
#: ingest leaves a lead ``received``, an assessment moves it to ``qualified`` or
#: ``failed``, and a final routing event moves it to ``routed``.
LEAD_STATUS_QUALIFIED: Final[str] = "qualified"
LEAD_STATUS_FAILED: Final[str] = "failed"
LEAD_STATUS_ROUTED: Final[str] = "routed"

#: Engine defaults; see the module docstring for why each one is what it is.
DEFAULT_POOL_SIZE: Final[int] = 1
DEFAULT_MAX_OVERFLOW: Final[int] = 0
DEFAULT_POOL_RECYCLE_SECONDS: Final[int] = 300
DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[int] = 5

_TENANT_SLUG_RE: Final[re.Pattern[str]] = re.compile(TENANT_ID_PATTERN)

#: Scales of the two ``Numeric`` columns this adapter writes floats into.
_SCORE_QUANTUM: Final[Decimal] = Decimal("0.01")
_CONFIDENCE_QUANTUM: Final[Decimal] = Decimal("0.001")
_COST_QUANTUM: Final[Decimal] = Decimal("0.000001")


# --------------------------------------------------------------------------- identities


def tenant_uuid(tenant_id: str) -> uuid.UUID:
    """Resolve a port-level tenant id to the ``tenants.id`` primary key.

    The ports speak in ``str`` tenant ids and the database keys on UUIDs, and the mapping
    between them already exists: ``scripts/seed.py`` derives a tenant's row id from its
    slug with UUID5 (:func:`leadquali.adapters.seed.tenant_id_for`), which is what makes
    seeding idempotent and gives the default tenant the same id in every environment. This
    function is that same mapping and imports it rather than restating it — two spellings
    of "which row is this tenant" is how a lead ends up filed under a tenant that does not
    exist.

    A value that is already a UUID is taken as the row id itself, so a caller holding an id
    read out of the database does not have to know about slugs.

    Raises:
        ValueError: the id is neither a UUID nor a valid tenant slug. Refused here rather
            than passed to the query, because tenant ids arrive from queue messages and
            request payloads.
    """
    try:
        return uuid.UUID(tenant_id)
    except ValueError:
        pass
    if not _TENANT_SLUG_RE.match(tenant_id):
        raise ValueError(
            f"tenant id {tenant_id!r} is neither a UUID nor a valid slug "
            f"(expected {TENANT_ID_PATTERN})"
        )
    return tenant_id_for(tenant_id)


def lead_uuid(lead_id: str) -> uuid.UUID:
    """Parse a port-level lead id into the UUID the tables key on.

    Raises:
        ValueError: not a UUID. A lead id is only ever obtained from
            :meth:`PostgresLeadStore.upsert_lead`, so anything else is a bug in the caller
            and is worth a clear message rather than an empty result set.
    """
    try:
        return uuid.UUID(lead_id)
    except ValueError:
        raise ValueError(f"lead id {lead_id!r} is not a UUID") from None


def contact_email_hash(email: str | None) -> str | None:
    """SHA-256 of the normalised contact address, or ``None`` when there is no address.

    Invariant 5: this is what a log line or a metric carries so one person's leads can be
    correlated without their address ever leaving ``leads.raw_payload``. Normalised —
    stripped and lowercased — so that ``Ada@Example.com`` and ``ada@example.com`` correlate
    to the same person, which is the entire point of storing it.

    Not a secret and not reversible-proof: an email address has little entropy, so this
    defends against casual disclosure in logs, not against a determined attacker with the
    hash. It is never returned to a caller and never logged next to the address.
    """
    if email is None:
        return None
    normalised = email.strip().lower()
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- engine & sessions


@lru_cache(maxsize=8)
def engine_for(url: str) -> Engine:
    """The process-wide engine for one database URL, created on first use.

    Memoised rather than module-level so that importing this module opens nothing: see the
    module docstring on cold starts. Keyed by URL, so a test pointing at a throwaway
    database does not share a pool with the process's real one.
    """
    return create_engine(
        url,
        pool_size=DEFAULT_POOL_SIZE,
        max_overflow=DEFAULT_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=DEFAULT_POOL_RECYCLE_SECONDS,
        connect_args={
            "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
            # Session state pins an RDS Proxy client to a backend connection; see the
            # module docstring.
            "prepare_threshold": None,
        },
    )


@lru_cache(maxsize=8)
def session_factory(url: str) -> sessionmaker[Session]:
    """The process-wide session factory for one database URL.

    ``expire_on_commit=False`` because every method here commits and then returns plain
    values: re-fetching a row after the transaction it belongs to has ended is a round trip
    bought for nothing.
    """
    return sessionmaker(bind=engine_for(url), expire_on_commit=False)


def session_factory_from_env(settings: Settings | None = None) -> sessionmaker[Session]:
    """The session factory for the configured ``DATABASE_URL``.

    Raises:
        RuntimeError: ``DATABASE_URL`` was never set. Loud at wiring time rather than at
            the first lead.
    """
    resolved = settings if settings is not None else get_settings()
    return session_factory(resolved.require_database_url())


# ------------------------------------------------------------------------- value mapping


def assessment_values(*, outcome: AssessmentOutcome, decision: RoutingDecision) -> dict[str, Any]:
    """The ``assessments`` columns for one attempt — successful or not.

    Split out as a pure function because the interesting half of this mapping is a shape
    rule enforced by a CHECK constraint (``output_present_iff_ok``), and a pure function is
    testable without a database: a successful run carries every model-output column plus
    the verdict code derived from them, and a failed run carries none of them and must say
    why. Getting that wrong is not a silent bug — the insert is rejected — but finding out
    at 3am against live traffic is worse than finding out in a unit test.

    Metering is written whenever it is present, on success **or** failure: a refusal is an
    HTTP 200 that Anthropic bills for, and a ``max_tokens`` truncation is billed too, so a
    failure that was charged for has to appear in the same ``SUM`` as a success. When it is
    absent — the call never completed — the NOT NULL provenance columns take
    :data:`UNKNOWN_MODEL_ID` / :data:`UNKNOWN_PROMPT_VERSION`, the counters stay zero, and
    ``latency_ms`` still records how long the failing attempt took.
    """
    values: dict[str, Any]
    if outcome.ok:
        assessment = outcome.assessment
        values = {
            "status": ASSESSMENT_STATUS_OK,
            # Set on a *successful* assessment too, when the confidence gate rejected it.
            "escalation_reason": _reason_value(decision),
            "tier": decision.tier.value,
            "total_score": _quantise(decision.total_score, _SCORE_QUANTUM),
            "dimension_scores": assessment.dimension_scores.model_dump(mode="json"),
            "extracted": assessment.extracted.model_dump(mode="json"),
            "reasoning": assessment.reasoning,
            "confidence": _quantise(assessment.confidence, _CONFIDENCE_QUANTUM),
            "missing_information": list(assessment.missing_information),
        }
    else:
        values = {
            "status": ASSESSMENT_STATUS_FAILED,
            # NOT NULL for a failure under the CHECK, and taken from the outcome rather
            # than the decision: the outcome is the observation, the decision is what
            # policy made of it, and only the first is guaranteed to carry a reason.
            "escalation_reason": outcome.reason.value,
            "tier": None,
            "total_score": None,
            # ``null()``, not ``None``. A Python ``None`` bound to a JSONB column is sent
            # as the JSON value ``null``, which is a perfectly good *value*: it satisfies
            # ``dimension_scores IS NOT NULL`` and so a failed assessment written that way
            # is rejected by ``output_present_iff_ok``, which is the constraint that keeps
            # a half-written row from masquerading as a real assessment. The two columns
            # below are the only JSONB ones this branch nulls; the rest are ordinary types
            # where ``None`` already means SQL NULL.
            "dimension_scores": null(),
            "extracted": null(),
            "reasoning": None,
            "confidence": None,
            # Not part of the all-or-nothing group: "nothing was reported missing" is as
            # true of a failed run as of a clean one.
            "missing_information": [],
        }

    metering: CallMetering | None = outcome.metering
    if metering is not None:
        values |= {
            "model_id": metering.model_id,
            "prompt_version": metering.prompt_version,
            "effort": metering.effort,
            "input_tokens": metering.input_tokens,
            "output_tokens": metering.output_tokens,
            "cache_read_tokens": metering.cache_read_tokens,
            "cache_creation_tokens": metering.cache_creation_tokens,
            "cost_usd": metering.cost_usd.quantize(_COST_QUANTUM),
            "latency_ms": metering.latency_ms,
        }
    else:
        values |= {
            "model_id": UNKNOWN_MODEL_ID,
            "prompt_version": UNKNOWN_PROMPT_VERSION,
            "effort": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_usd": Decimal(0),
            # Narrowed rather than assumed: a success always carries metering, so the
            # only outcome that reaches this branch is a failure, which knows how long
            # it took before it gave up.
            "latency_ms": 0 if outcome.ok else outcome.latency_ms,
        }
    return values


def _reason_value(decision: RoutingDecision) -> str | None:
    return decision.escalation_reason.value if decision.escalation_reason is not None else None


def _quantise(value: float, quantum: Decimal) -> Decimal:
    """A float onto the scale of its ``Numeric`` column, via ``str`` to avoid binary dust.

    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827; ``Decimal("0.1")`` is 0.1.
    The column is ``Numeric`` precisely so sums over it stay exact, and going through the
    binary value would put the imprecision back.
    """
    return Decimal(str(value)).quantize(quantum)


# ------------------------------------------------------------------------------- stores


class PostgresLeadStore:
    """:class:`~leadquali.app.ports.LeadStorePort` over the Postgres schema of #15.

    Substitutable for ``tests.fakes.InMemoryLeadStore``: both are exercised by the same
    contract suite (``tests/contract/lead_store_contract.py``), because a double that has
    drifted from the adapter it stands in for is worse than no double at all.

    Every method opens its own short transaction and commits before returning. Failures are
    raised, never swallowed: a store that cannot write is not a degraded mode, it is a
    reason to leave the message on the queue.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use. See :func:`session_factory`."""
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresLeadStore:
        """A store over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresLeadStore:
        """A store over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    # ----------------------------------------------------------------------- leads

    def upsert_lead(
        self,
        *,
        tenant_id: str,
        submission_id: str,
        submission: LeadSubmission,
        source: str,
        received_at: datetime,
    ) -> StoredLead:
        """Insert this lead, or return the existing one for the same submission id.

        One statement, and it is the whole idempotency guarantee: see the module docstring
        for why it is ``ON CONFLICT DO UPDATE ... RETURNING (xmax = 0)`` and not a
        ``SELECT`` followed by an ``INSERT``.

        A redelivery deliberately does **not** overwrite the stored payload, source or
        ``received_at``: the first delivery is the record of what arrived and when, and a
        retry that rewrote it would quietly erase the queue latency the columns exist to
        measure.
        """
        tenant = tenant_uuid(tenant_id)
        if not submission_id:
            # ``ck_leads_submission_id_not_blank`` would reject this anyway; saying so here
            # names the offending argument instead of quoting a constraint.
            raise ValueError("submission_id must not be blank; it is the idempotency key")

        insertion = pg_insert(Lead).values(
            tenant_id=tenant,
            submission_id=submission_id,
            raw_payload=submission.model_dump(mode="json"),
            source=source,
            received_at=received_at,
            contact_email_hash=contact_email_hash(submission.email),
        )
        statement = insertion.on_conflict_do_update(
            constraint=LEAD_IDEMPOTENCY_CONSTRAINT,
            # A no-op write of the conflicting column onto itself. DO UPDATE is what makes
            # RETURNING produce the surviving row under a concurrent redelivery; there is
            # nothing about the lead we want to change.
            set_={"submission_id": insertion.excluded.submission_id},
            # Redundant against the conflict target, which already contains tenant_id, and
            # kept because invariant 4 is "every statement filters on the tenant" with no
            # exceptions for the ones that are provably safe.
            where=Lead.tenant_id == tenant,
        ).returning(Lead.id, literal_column("(xmax = 0)", Boolean))

        with self._sessions.begin() as session:
            row = session.execute(statement).one()
        return StoredLead(lead_id=str(row[0]), is_new=bool(row[1]))

    def already_routed(self, *, tenant_id: str, lead_id: str) -> bool:
        """Whether this lead has a *final* routing event — dispatched or suppressed.

        #15's ``routing_events`` has no ``outcome`` column, so finality is read off the two
        columns that carry it: a dispatch stamps ``dispatched_at``, and a suppression is
        the ``suppress`` action (a suppression involves no external call, so it cannot
        fail). A failed send leaves ``dispatched_at`` NULL under a dispatching action and
        is therefore not final — which is the point: one SES outage must not convert a
        retryable failure into a permanently lost lead.
        """
        tenant = tenant_uuid(tenant_id)
        lead = lead_uuid(lead_id)
        statement = (
            select(RoutingEvent.id)
            .where(
                RoutingEvent.tenant_id == tenant,
                RoutingEvent.lead_id == lead,
                or_(
                    RoutingEvent.dispatched_at.is_not(None),
                    RoutingEvent.action == Action.SUPPRESS.value,
                ),
            )
            .limit(1)
        )
        with self._sessions.begin() as session:
            return session.execute(statement).first() is not None

    # ----------------------------------------------------------------- assessments

    def record_assessment(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        outcome: AssessmentOutcome,
        decision: RoutingDecision,
        recorded_at: datetime,
    ) -> None:
        """Record what the model said and what policy concluded from it.

        A failed attempt is a row like any other — see :func:`assessment_values` — and the
        lead's status follows it: ``qualified`` when there is an assessment, ``failed``
        when there is not. The two writes share one transaction, so a lead is never left
        claiming a state its assessment history does not support.

        ``recorded_at`` is written to ``created_at`` rather than left to the server's
        ``now()``: the pipeline reads its clock through a port so tests are deterministic
        and a replay can re-run a lead with the timestamps it originally had, and a row
        stamped by the server would quietly opt out of that.
        """
        tenant = tenant_uuid(tenant_id)
        lead = lead_uuid(lead_id)
        statement = insert(Assessment).values(
            tenant_id=tenant,
            lead_id=lead,
            created_at=recorded_at,
            **assessment_values(outcome=outcome, decision=decision),
        )
        status = LEAD_STATUS_QUALIFIED if outcome.ok else LEAD_STATUS_FAILED
        with self._sessions.begin() as session:
            session.execute(statement)
            self._set_lead_status(session, tenant=tenant, lead=lead, status=status)

    # --------------------------------------------------------------- routing events

    def record_routing_event(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        action: Action,
        destination: str | None,
        outcome: RoutingOutcome,
        provider_message_id: str | None,
        occurred_at: datetime,
        detail: str,
    ) -> None:
        """Record where the lead went, or why it did not go anywhere.

        The outcome is encoded in the columns #15's table has rather than stored as a word:
        ``DISPATCHED`` stamps ``dispatched_at``, ``SUPPRESSED`` is the ``suppress`` action
        with ``dispatched_at`` left NULL (nothing was dispatched), and ``FAILED`` is a
        dispatching action that never got its stamp. :meth:`already_routed` reads finality
        back out of exactly those two columns.

        ``detail`` has no column to go in, so it is logged rather than dropped: it is one
        short PII-free line by contract (invariant 5), and squeezing it into
        ``provider_message_id`` — the one field an operator traces a delivery complaint
        with — would corrupt the column that has a job. Giving it a column of its own is a
        migration, and this issue does not own the schema; it is flagged in the report.

        Raises:
            ValueError: ``action`` and ``outcome`` disagree about suppression. Since
                suppression is encoded *as* the action, a ``SUPPRESSED`` outcome under
                another action would be invisible to :meth:`already_routed` and the lead
                would be processed again, and a ``FAILED`` suppression would be read as
                final though nothing happened. Neither combination is meaningful, and both
                are better refused than silently mis-recorded.
        """
        suppressing = action is Action.SUPPRESS
        if suppressing is not (outcome is RoutingOutcome.SUPPRESSED):
            raise ValueError(
                f"action {action.value!r} and outcome {outcome.value!r} disagree: "
                "a suppression is recorded as the 'suppress' action and nothing else is"
            )

        tenant = tenant_uuid(tenant_id)
        lead = lead_uuid(lead_id)
        final = outcome is not RoutingOutcome.FAILED
        statement = insert(RoutingEvent).values(
            tenant_id=tenant,
            lead_id=lead,
            action=action.value,
            destination=destination,
            dispatched_at=occurred_at if outcome is RoutingOutcome.DISPATCHED else None,
            provider_message_id=provider_message_id,
            created_at=occurred_at,
        )
        with self._sessions.begin() as session:
            session.execute(statement)
            if final:
                self._set_lead_status(session, tenant=tenant, lead=lead, status=LEAD_STATUS_ROUTED)
        # PII-free by the port's contract: the decision's note or a failure's class, never
        # lead content and never an address.
        LOGGER.info(
            "routing event recorded tenant=%s lead=%s action=%s outcome=%s detail=%s",
            tenant,
            lead,
            action.value,
            outcome.value,
            detail,
        )

    # ---------------------------------------------------------------------- helpers

    @staticmethod
    def _set_lead_status(
        session: Session, *, tenant: uuid.UUID, lead: uuid.UUID, status: str
    ) -> None:
        """Move one lead's lifecycle status, tenant-scoped like everything else."""
        session.execute(
            update(Lead).where(Lead.tenant_id == tenant, Lead.id == lead).values(status=status)
        )


class PostgresFeedbackStore:
    """:class:`~leadquali.app.ports.FeedbackStorePort` over the ``feedback`` table.

    A separate class from :class:`PostgresLeadStore` because the callers are separate — the
    pipeline writes leads from a worker, a rep's browser writes feedback through the API —
    and an endpoint handed a feedback writer cannot reach the lead lifecycle by accident.
    They share the session factory, so a process doing both still opens one pool.

    Conventions are #16's, unchanged: ``tenant_id`` on the method and in the statement
    (invariant 4, including in the ``ON CONFLICT ... WHERE`` clause where the unique index
    already implies it), one short transaction per call, failures raised rather than
    swallowed.

    **Idempotency is the database's job here too.** One ``INSERT ... ON CONFLICT DO UPDATE``
    against ``uq_feedback_tenant_id_lead_id_rater``: a mail scanner's prefetch, a rep's
    double tap and a change of mind three days later all resolve to the same row. A
    ``SELECT`` followed by an ``INSERT`` would duplicate under exactly the concurrency this
    endpoint sees — a phone retrying a POST on a flaky connection.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use. See :func:`session_factory`."""
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresFeedbackStore:
        """A feedback store over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresFeedbackStore:
        """A feedback store over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    def record_feedback(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        rater: str,
        verdict: Verdict,
        notes: str | None,
        recorded_at: datetime,
    ) -> RecordedFeedback:
        """Record this rater's verdict on this lead, replacing any verdict they gave before.

        The prior verdict is read in the same transaction before the upsert, because the
        page the rep lands on says "changed from good lead to bad lead" and ``RETURNING``
        after ``DO UPDATE`` yields the *new* row, not the one that was there. Under a
        genuine race the reader may see the other writer's value — which changes one
        sentence of wording and never the row, since the write is still one atomic
        statement.

        ``created_at`` moves forward on an update: the row *is* the current verdict, and
        leaving it stamped with a verdict that has since been replaced would misdate the
        training data. #15's table has no ``updated_at``, so the first-recorded time is not
        kept; that is flagged in the report rather than smuggled into ``notes``.

        Raises:
            UnknownLeadError: this tenant has no such lead. Read off the composite foreign
                key rather than from a prior existence check, so there is no window between
                the check and the write — and so a link that outlived #37's retention job
                produces a page that says so instead of a 500.
        """
        tenant = tenant_uuid(tenant_id)
        lead = lead_uuid(lead_id)
        if not rater:
            raise ValueError("rater must not be blank; feedback with no subject is unattributable")

        previous = select(Feedback.verdict).where(
            Feedback.tenant_id == tenant,
            Feedback.lead_id == lead,
            Feedback.rater == rater,
        )
        insertion = pg_insert(Feedback).values(
            tenant_id=tenant,
            lead_id=lead,
            rater=rater,
            verdict=verdict.value,
            notes=notes,
            created_at=recorded_at,
        )
        statement = insertion.on_conflict_do_update(
            constraint=FEEDBACK_IDEMPOTENCY_CONSTRAINT,
            set_={
                "verdict": insertion.excluded.verdict,
                "created_at": insertion.excluded.created_at,
                # A click carrying no note must not erase the sentence the rep typed last
                # time: the new value wins only when there is one.
                "notes": func.coalesce(insertion.excluded.notes, Feedback.notes),
            },
            # Redundant against the conflict target and kept anyway: invariant 4 has no
            # exceptions for the statements that are provably safe.
            where=Feedback.tenant_id == tenant,
        ).returning(literal_column("(xmax = 0)", Boolean))

        try:
            with self._sessions.begin() as session:
                existing = session.execute(previous).scalar_one_or_none()
                inserted = bool(session.execute(statement).scalar_one())
        except IntegrityError as error:
            if _is_foreign_key_violation(error):
                raise UnknownLeadError(
                    f"tenant '{tenant_id}' has no lead {lead_id}; a link can outlive its row"
                ) from None
            raise

        return RecordedFeedback(
            verdict=verdict,
            created=inserted,
            previous_verdict=Verdict(existing) if existing is not None else None,
        )


def _is_foreign_key_violation(error: IntegrityError) -> bool:
    """Whether this integrity error is the composite lead foreign key rejecting the row.

    Matched on SQLSTATE rather than on the message, which is localised and version-specific.
    Anything else — a broken CHECK, a duplicate that somehow escaped the upsert — is a bug
    here, and is re-raised rather than reported to a rep as "that lead is gone".
    """
    return getattr(error.orig, "sqlstate", None) == _FOREIGN_KEY_VIOLATION


class PostgresTenantConfigSource:
    """:class:`~leadquali.app.ports.TenantConfigPort` over ``tenants.icp_config``.

    The Phase 1 file loader (``adapters/tenant_config_json.py``) and this class are
    interchangeable at the entrypoint: both hand back a fully validated
    :class:`~leadquali.domain.tenant_config.TenantConfig` or raise, because a tenant whose
    policy cannot be loaded must never have its leads routed by someone else's policy.
    ``scripts/seed.py`` stores the config document verbatim, so the column is handed
    straight to the model rather than reassembled from parts.

    Reads are uncached, exactly as the file loader's are: the document is a few hundred
    bytes, and picking up an operator's edit without a redeploy is worth far more than the
    round trip. Caching it per container would also mean the tenant that changed its
    routing table waits for a cold start to see the change.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """Take the session factory to use. See :func:`session_factory`."""
        self._sessions = sessions

    @classmethod
    def from_url(cls, url: str) -> PostgresTenantConfigSource:
        """A config source over the memoised engine for ``url``."""
        return cls(session_factory(url))

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> PostgresTenantConfigSource:
        """A config source over the configured ``DATABASE_URL``."""
        return cls(session_factory_from_env(settings))

    def get(self, tenant_id: str) -> TenantConfig:
        """Return the validated configuration for ``tenant_id``.

        Raises:
            TenantNotFoundError: no such tenant row.
            TenantConfigError: the row exists and its ``icp_config`` is not a valid
                configuration — including the case where it declares a different tenant
                than the one asked for, which means the seed and the caller disagree about
                identity and is not something to guess about.
        """
        try:
            tenant = tenant_uuid(tenant_id)
        except ValueError as exc:
            raise TenantConfigError(f"tenant '{tenant_id}': {exc}") from exc

        statement = select(Tenant.icp_config).where(Tenant.id == tenant)
        with self._sessions.begin() as session:
            document = session.execute(statement).scalar_one_or_none()

        if document is None:
            raise TenantNotFoundError(
                f"tenant '{tenant_id}': no row in tenants (id {tenant}); seed it first"
            )

        config = TenantConfig.from_dict(document)
        if tenant_id != str(tenant) and config.tenant_id != tenant_id:
            raise TenantConfigError(
                f"tenant '{tenant_id}': icp_config declares tenant_id "
                f"'{config.tenant_id}'; the row and its config must agree"
            )
        return config
