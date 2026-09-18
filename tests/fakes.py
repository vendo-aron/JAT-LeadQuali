"""In-memory doubles for every port the pipeline touches.

They live outside ``tests/unit`` because #16, #17, #19 and #21 all need the same ones: a
worker test, an ingest test and a notifier test each want a store that behaves like the
real one without a database, and three private copies would drift the first time a port
changed. Nothing here talks to a network, a disk or a clock.

Each double can be told to fail — ``InMemoryLeadStore(fail_on={"record_assessment"})``,
``RecordingNotifier(fail_times=1)`` — because the interesting half of the pipeline is what
happens when a collaborator is broken, and a test that can only exercise the happy path
proves nothing about invariant 3.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from leadquali.app.admin_views import (
    AgreementPoint,
    FeedbackNote,
    LeadAssessmentRow,
    LeadDetail,
    LeadFilter,
    LeadPage,
    LeadRow,
    PageCursor,
    RerunCandidate,
    ReviewRow,
    RoutingRow,
    TierCount,
)
from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
)
from leadquali.app.config_versions import ConfigVersion, UnknownConfigVersionError
from leadquali.app.enrichment import Enrichment
from leadquali.app.feedback import UnknownLeadError, Verdict
from leadquali.app.golden_promotion import GoldenPromotion
from leadquali.app.metering import (
    BillingPeriod,
    DailySpend,
    MeteringError,
    TenantQuota,
    UsageTotals,
    is_billable,
)
from leadquali.app.ports import RecordedFeedback, RoutingOutcome, StoredLead
from leadquali.app.retention import (
    DEFAULT_ASSESSMENT_RETENTION_DAYS,
    DEFAULT_RAW_RETENTION_DAYS,
    ChildCounts,
    ErasureReceipt,
    ErasureRequest,
    ExpiredCounts,
    RetentionPolicy,
    is_tombstone,
)
from leadquali.app.retention import UnknownTenantError as RetentionUnknownTenantError
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import (
    ApiKeyRecord,
    TenantAlreadyExistsError,
    TenantRecord,
    TenantStatus,
    UnknownApiKeyError,
    UnknownTenantError,
)
from leadquali.domain.models import Action, LeadAssessment, RoutingDecision, Tier
from leadquali.domain.tenant_config import TenantConfig, TenantNotFoundError
from leadquali.observability import contact_email_hash
from leadquali.prompts.lead import LeadSubmission


class FakeStoreError(RuntimeError):
    """The store is unreachable. Stands in for a psycopg ``OperationalError``."""


class FakeNotifierError(RuntimeError):
    """The notifier refused the message. Stands in for a botocore ``ClientError``."""


class FakeAssessorError(RuntimeError):
    """The assessor raised, which its port says it must not. Tested precisely for that."""


class FakeEnricherError(RuntimeError):
    """Enrichment blew up. Must never cost the lead."""


@dataclass(frozen=True, slots=True)
class RecordedAssessment:
    """One ``record_assessment`` call."""

    tenant_id: str
    lead_id: str
    outcome: AssessmentOutcome
    decision: RoutingDecision
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RecordedRoutingEvent:
    """One ``record_routing_event`` call."""

    tenant_id: str
    lead_id: str
    action: Action
    destination: str | None
    outcome: RoutingOutcome
    provider_message_id: str | None
    occurred_at: datetime
    detail: str


@dataclass(frozen=True, slots=True)
class RecordedDispatch:
    """One ``dispatch`` call."""

    tenant_id: str
    lead_id: str
    destination: str
    submission: LeadSubmission
    decision: RoutingDecision
    assessment: LeadAssessment | None


class InMemoryLeadStore:
    """A ``LeadStorePort`` with the real one's uniqueness behaviour and none of its I/O.

    ``(tenant_id, submission_id)`` is unique, exactly as ``uq_leads_tenant_id_submission_id``
    makes it in #15's schema, so replaying a lead returns the same ``lead_id`` with
    ``is_new=False``.
    """

    def __init__(self, *, fail_on: Iterable[str] = ()) -> None:
        self.fail_on = set(fail_on)
        self.leads: dict[tuple[str, str], str] = {}
        self.payloads: dict[str, LeadSubmission] = {}
        self.assessments: list[RecordedAssessment] = []
        self.routing_events: list[RecordedRoutingEvent] = []
        self._next_id = 1

    def _guard(self, method: str) -> None:
        if method in self.fail_on:
            raise FakeStoreError(f"store unavailable during {method}")

    def upsert_lead(
        self,
        *,
        tenant_id: str,
        submission_id: str,
        submission: LeadSubmission,
        source: str,
        received_at: datetime,
    ) -> StoredLead:
        self._guard("upsert_lead")
        del source, received_at
        key = (tenant_id, submission_id)
        existing = self.leads.get(key)
        if existing is not None:
            return StoredLead(lead_id=existing, is_new=False)
        lead_id = f"lead-{self._next_id:04d}"
        self._next_id += 1
        self.leads[key] = lead_id
        self.payloads[lead_id] = submission
        return StoredLead(lead_id=lead_id, is_new=True)

    def already_routed(self, *, tenant_id: str, lead_id: str) -> bool:
        self._guard("already_routed")
        return any(
            event.tenant_id == tenant_id
            and event.lead_id == lead_id
            and event.outcome is not RoutingOutcome.FAILED
            for event in self.routing_events
        )

    def record_assessment(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        outcome: AssessmentOutcome,
        decision: RoutingDecision,
        recorded_at: datetime,
    ) -> None:
        self._guard("record_assessment")
        self.assessments.append(
            RecordedAssessment(
                tenant_id=tenant_id,
                lead_id=lead_id,
                outcome=outcome,
                decision=decision,
                recorded_at=recorded_at,
            )
        )

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
        self._guard("record_routing_event")
        self.routing_events.append(
            RecordedRoutingEvent(
                tenant_id=tenant_id,
                lead_id=lead_id,
                action=action,
                destination=destination,
                outcome=outcome,
                provider_message_id=provider_message_id,
                occurred_at=occurred_at,
                detail=detail,
            )
        )

    # ------------------------------------------------------------------ assertions

    def terminal_events(self, lead_id: str) -> list[RecordedRoutingEvent]:
        """Events that count as "this lead has been dealt with"."""
        return [
            event
            for event in self.routing_events
            if event.lead_id == lead_id and event.outcome is not RoutingOutcome.FAILED
        ]


@dataclass
class FeedbackRow:
    """One row of the ``feedback`` table, as the in-memory store keeps it."""

    tenant_id: str
    lead_id: str
    rater: str
    verdict: Verdict
    notes: str | None
    created_at: datetime


class InMemoryFeedbackStore:
    """A ``FeedbackStorePort`` with the real one's uniqueness behaviour and none of its I/O.

    ``(tenant_id, lead_id, rater)`` is unique, exactly as
    ``uq_feedback_tenant_id_lead_id_rater`` makes it in #15's schema, so a prefetched link,
    a double tap and a change of mind all land on one row. ``known_leads`` mirrors the
    composite foreign key: a lead nobody has heard of raises ``UnknownLeadError`` here just
    as the database raises it there, which is the path a link outliving #37's retention job
    takes.
    """

    def __init__(
        self, *, known_leads: Iterable[tuple[str, str]] | None = None, fail: bool = False
    ) -> None:
        self.rows: dict[tuple[str, str, str], FeedbackRow] = {}
        self.known_leads = set(known_leads) if known_leads is not None else None
        self.fail = fail
        self.calls = 0

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
        self.calls += 1
        if self.fail:
            raise FakeStoreError("store unavailable during record_feedback")
        if self.known_leads is not None and (tenant_id, lead_id) not in self.known_leads:
            raise UnknownLeadError(f"tenant '{tenant_id}' has no lead {lead_id}")

        key = (tenant_id, lead_id, rater)
        existing = self.rows.get(key)
        self.rows[key] = FeedbackRow(
            tenant_id=tenant_id,
            lead_id=lead_id,
            rater=rater,
            verdict=verdict,
            # A click with no note leaves the previous one alone, as COALESCE does.
            notes=notes if notes is not None else (existing.notes if existing else None),
            created_at=recorded_at,
        )
        return RecordedFeedback(
            verdict=verdict,
            created=existing is None,
            previous_verdict=existing.verdict if existing is not None else None,
        )


class RecordingNotifier:
    """A ``NotifierPort`` that remembers what it was asked to send.

    ``fail_times`` makes the first N attempts raise, which is how a transient SES outage
    followed by an SQS redelivery is simulated.
    """

    def __init__(self, *, fail_times: int = 0, message_id: str | None = "provider-msg-1") -> None:
        self.fail_times = fail_times
        self.message_id = message_id
        self.dispatches: list[RecordedDispatch] = []
        self.attempts = 0

    def dispatch(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        destination: str,
        submission: LeadSubmission,
        decision: RoutingDecision,
        assessment: LeadAssessment | None,
    ) -> str | None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise FakeNotifierError("provider rejected the message")
        self.dispatches.append(
            RecordedDispatch(
                tenant_id=tenant_id,
                lead_id=lead_id,
                destination=destination,
                submission=submission,
                decision=decision,
                assessment=assessment,
            )
        )
        return self.message_id


class ScriptedAssessor:
    """A ``LeadAssessorPort`` returning canned outcomes and remembering its prompts.

    ``raises`` covers the case the port forbids but a buggy adapter can still produce: the
    pipeline must survive an assessor that throws, because the alternative is a lost lead.
    """

    def __init__(
        self,
        outcomes: AssessmentOutcome | Sequence[AssessmentOutcome],
        *,
        raises: Exception | None = None,
    ) -> None:
        if isinstance(outcomes, AssessmentSucceeded | AssessmentFailed):
            self.outcomes: list[AssessmentOutcome] = [outcomes]
        else:
            self.outcomes = list(outcomes)
        self.raises = raises
        self.prompts: list[str] = []
        self.configs: list[TenantConfig] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        self.prompts.append(rendered_lead)
        self.configs.append(config)
        if self.raises is not None:
            raise self.raises
        index = min(len(self.prompts) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


class StaticConfigSource:
    """A ``TenantConfigPort`` backed by a dict."""

    def __init__(self, configs: Mapping[str, TenantConfig]) -> None:
        self.configs = dict(configs)
        self.calls: list[str] = []

    def get(self, tenant_id: str) -> TenantConfig:
        self.calls.append(tenant_id)
        try:
            return self.configs[tenant_id]
        except KeyError:
            raise TenantNotFoundError(f"tenant '{tenant_id}': no configuration") from None


class StaticEnricher:
    """An ``EnricherPort`` that always returns the same enrichment, or always raises."""

    def __init__(self, enrichment: Enrichment | None = None, *, raises: Exception | None = None):
        self.enrichment = enrichment if enrichment is not None else Enrichment.none()
        self.raises = raises
        self.calls: list[str] = []

    def enrich(self, *, tenant_id: str, submission: LeadSubmission) -> Enrichment:
        del submission
        self.calls.append(tenant_id)
        if self.raises is not None:
            raise self.raises
        return self.enrichment


@dataclass
class FakeClock:
    """A ``ClockPort`` that advances by a fixed step on every read.

    Wall time and the monotonic counter move together so a test can assert on both an
    ordering of timestamps and a latency without any real waiting.
    """

    start: datetime = field(default_factory=lambda: datetime(2026, 9, 2, 12, 0, tzinfo=UTC))
    step_ms: int = 10
    ticks: int = 0

    def now(self) -> datetime:
        value = self.start + timedelta(milliseconds=self.step_ms * self.ticks)
        self.ticks += 1
        return value

    def monotonic_ms(self) -> int:
        value = self.step_ms * self.ticks
        self.ticks += 1
        return value


class InMemoryTenantAdminStore:
    """A :class:`~leadquali.app.tenants.TenantAdminStorePort` over two dicts.

    Behaves like the Postgres one in the ways the service depends on: a duplicate slug is
    refused, a key is only reachable through its own tenant, and a listing never carries a
    hash. It deliberately does *not* hold the key hashes it is given in anything the
    service can read back, because the service must never be able to get at one.
    """

    def __init__(self) -> None:
        self.tenants: dict[str, TenantRecord] = {}
        #: ``key_id -> (slug, record)``. The hash lives in :attr:`hashes`, apart.
        self.keys: dict[str, tuple[str, ApiKeyRecord]] = {}
        self.hashes: dict[str, str] = {}
        self._clock = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

    # ------------------------------------------------------------------ tenant CRUD

    def create_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        slug: str,
        name: str,
        config: Mapping[str, Any],
        hmac_secret_ref: str | None,
    ) -> TenantRecord:
        if slug in self.tenants:
            raise TenantAlreadyExistsError(f"tenant '{slug}' already exists")
        record = TenantRecord(
            id=tenant_id,
            slug=slug,
            name=name,
            status=TenantStatus.ACTIVE,
            config=dict(config),
            hmac_secret_ref=hmac_secret_ref,
            rate_limit_per_minute=60,
            rate_limit_burst=10,
            created_at=self._clock,
            updated_at=self._clock,
        )
        self.tenants[slug] = record
        return record

    def get_tenant(self, *, slug: str) -> TenantRecord | None:
        return self.tenants.get(slug)

    def list_tenants(self) -> Sequence[TenantRecord]:
        return sorted(self.tenants.values(), key=lambda record: (record.created_at, record.slug))

    def update_config(self, *, slug: str, config: Mapping[str, Any]) -> TenantRecord:
        return self._update(slug, config=dict(config))

    def set_status(self, *, slug: str, status: TenantStatus) -> TenantRecord:
        return self._update(slug, status=status)

    def rate_limit_for(self, tenant_id: str) -> Any:
        from leadquali.api.ratelimit import TenantRateLimit

        found = self.tenants.get(tenant_id)
        if found is None:
            return None
        return TenantRateLimit(per_minute=found.rate_limit_per_minute, burst=found.rate_limit_burst)

    # -------------------------------------------------------------------------- keys

    def add_key(
        self, *, slug: str, key_id: str, key_prefix: str, key_hash: str, label: str | None
    ) -> ApiKeyRecord:
        if slug not in self.tenants:
            raise UnknownTenantError(f"no tenant '{slug}'")
        record = ApiKeyRecord(
            key_id=key_id,
            key_prefix=key_prefix,
            label=label,
            created_at=self._clock,
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
        )
        self.keys[key_id] = (slug, record)
        self.hashes[key_id] = key_hash
        return record

    def list_keys(self, *, slug: str) -> Sequence[ApiKeyRecord]:
        return [record for owner, record in self.keys.values() if owner == slug]

    def expire_key(self, *, slug: str, key_id: str, expires_at: datetime) -> ApiKeyRecord:
        return self._update_key(slug, key_id, expires_at=expires_at)

    def revoke_key(self, *, slug: str, key_id: str, revoked_at: datetime) -> ApiKeyRecord:
        owner, existing = self._owned(slug, key_id)
        if existing.revoked_at is not None:
            return existing
        return self._update_key(owner, key_id, revoked_at=revoked_at)

    # ---------------------------------------------------------------- unit of work

    def snapshot(self) -> tuple[dict[str, TenantRecord], dict[str, tuple[str, ApiKeyRecord]]]:
        """State to restore if the surrounding :class:`FakeUnitOfWork` rolls back.

        A shallow copy of each dict is enough because every value is a frozen dataclass:
        an update replaces the value rather than mutating it.
        """
        return dict(self.tenants), dict(self.keys)

    def restore(
        self, snapshot: tuple[dict[str, TenantRecord], dict[str, tuple[str, ApiKeyRecord]]]
    ) -> None:
        self.tenants, self.keys = dict(snapshot[0]), dict(snapshot[1])

    # ----------------------------------------------------------------------- internals

    def _update(self, slug: str, **changes: Any) -> TenantRecord:
        existing = self.tenants.get(slug)
        if existing is None:
            raise UnknownTenantError(f"no tenant '{slug}'")
        updated = replace(existing, updated_at=self._clock, **changes)
        self.tenants[slug] = updated
        return updated

    def _owned(self, slug: str, key_id: str) -> tuple[str, ApiKeyRecord]:
        found = self.keys.get(key_id)
        if found is None or found[0] != slug:
            raise UnknownApiKeyError(f"tenant '{slug}' has no key {key_id!r}")
        return found

    def _update_key(self, slug: str, key_id: str, **changes: Any) -> ApiKeyRecord:
        owner, existing = self._owned(slug, key_id)
        updated = replace(existing, **changes)
        self.keys[key_id] = (owner, updated)
        return updated


class FakeSecretHasher:
    """A :class:`~leadquali.app.tenants.SecretHasherPort` that is instant and reversible.

    Reversible on purpose: a test that wants to prove the service never hands a secret to
    the store can look at what the store received and see the secret in it, which is a
    stronger assertion than "the argon2 string is opaque". The real hasher is exercised in
    ``test_keyhash_argon2.py``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def hash_secret(self, secret: str) -> str:
        self.calls += 1
        return f"$argon2id$fake${secret}"


class FakeTenantSecrets:
    """A :class:`~leadquali.app.tenants.TenantSecretsPort` with moto's idempotency but none
    of its setup: a second create for the same tenant returns the first ARN."""

    def __init__(self) -> None:
        self.created: dict[str, str] = {}
        self.calls = 0

    def create_tenant_hmac_secret(self, slug: str) -> str:
        self.calls += 1
        return self.created.setdefault(slug, f"arn:aws:secretsmanager:eu-west-1:0:secret:{slug}")


class ExplodingTenantSecrets:
    """Provisioning that always fails, for the "nothing was written" assertions."""

    def create_tenant_hmac_secret(self, slug: str) -> str:
        raise RuntimeError(f"secrets manager is down (asked for {slug})")


def tenant_row(slug: str, **overrides: Any) -> TenantRecord:
    """A :class:`~leadquali.app.tenants.TenantRecord` with plausible defaults."""
    values: dict[str, Any] = {
        "id": tenant_id_for(slug),
        "slug": slug,
        "name": slug.title(),
        "status": TenantStatus.ACTIVE,
        "config": {},
        "hmac_secret_ref": None,
        "rate_limit_per_minute": 60,
        "rate_limit_burst": 10,
        "created_at": datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return TenantRecord(**values)


# ------------------------------------------------------------------------------ metering


@dataclass(frozen=True, slots=True)
class MeteredLead:
    """One row of ``leads``, reduced to what the meter counts."""

    tenant_id: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class MeteredAssessment:
    """One row of ``assessments``, reduced to what the meter counts."""

    tenant_id: str
    created_at: datetime
    lead_id: str
    """Which lead this attempt was made on. Several attempts can share one — a dispatch
    failure re-raises and SQS redelivers — and that is exactly what ``leads_billable``
    has to collapse, so the double has to be able to express it."""

    status: str = "ok"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Decimal = Decimal(0)


class InMemoryMeteringStore:
    """A :class:`~leadquali.app.metering.MeteringStorePort` over three lists.

    It is a real rollup, not a canned answer: :meth:`rollup_day` recomputes the day from
    the seeded leads and assessments and *replaces* the stored row, exactly as the SQL
    does, and :meth:`usage_for_period` reads only the stored rows. That is what lets the
    unit tests exercise the properties that matter — the three counts diverging, the
    rollup being idempotent, a period reading back what was rolled up — without a
    database, while ``tests/integration/test_metering_postgres.py`` proves the SQL agrees.

    The billing rule itself is imported rather than restated: this double and the adapter
    both answer to :func:`~leadquali.app.metering.is_billable`.
    """

    def __init__(self, *, quotas: Mapping[str, TenantQuota] | None = None) -> None:
        self.leads: list[MeteredLead] = []
        self.assessments: list[MeteredAssessment] = []
        #: The ``usage_daily`` table: ``(tenant_id, day) -> row``.
        self.rows: dict[tuple[str, date], UsageTotals] = {}
        #: Every ``rollup_day`` call, in order, so a test can prove how many were made.
        self.rollups: list[tuple[str, date]] = []
        #: Every ``compute_day`` call — the live read the quota check makes.
        self.computed: list[tuple[str, date]] = []
        self.quotas = dict(quotas or {})
        self._computed_at = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
        self._attempts = 0

    # ------------------------------------------------------------------------ seeding

    def add_lead(self, *, tenant_id: str, received_at: datetime) -> None:
        """Record a submission, as ingest would."""
        self.leads.append(MeteredLead(tenant_id=tenant_id, received_at=received_at))

    def add_assessment(
        self, *, tenant_id: str, created_at: datetime, lead_id: str | None = None, **values: Any
    ) -> None:
        """Record an assessment attempt, as the worker would.

        ``lead_id`` defaults to a fresh one, so a test that does not care reads as "one
        attempt, one lead". Pass the same id twice to model a redelivery.
        """
        self._attempts += 1
        self.assessments.append(
            MeteredAssessment(
                tenant_id=tenant_id,
                created_at=created_at,
                lead_id=lead_id if lead_id is not None else f"lead-{self._attempts}",
                **values,
            )
        )

    def given_quota(self, quota: TenantQuota) -> None:
        """Seed a tenant's plan directly, the way a ``tenants`` row would already have one.

        Distinct from :meth:`set_quota`, which is the port method under test and refuses a
        tenant it has never heard of.
        """
        self.quotas[quota.tenant_id] = quota

    # -------------------------------------------------------------------------- port

    def rollup_day(self, *, tenant_id: str, day: date) -> UsageTotals:
        self.rollups.append((tenant_id, day))
        totals = self.compute_day(tenant_id=tenant_id, day=day)
        # A replacement, never an increment — the property the real upsert exists for.
        self.rows[(tenant_id, day)] = totals
        return totals

    def compute_day(self, *, tenant_id: str, day: date) -> UsageTotals:
        self.computed.append((tenant_id, day))
        leads = [
            lead
            for lead in self.leads
            if lead.tenant_id == tenant_id and lead.received_at.date() == day
        ]
        assessed = [
            row
            for row in self.assessments
            if row.tenant_id == tenant_id and row.created_at.date() == day
        ]
        totals = UsageTotals(
            tenant_id=tenant_id,
            period=BillingPeriod.of_day(day),
            leads_ingested=len(leads),
            leads_assessed=len(assessed),
            # Distinct leads, not attempts: one lead redelivered three times after a
            # dispatch failure is one billable lead. The token sums below still count
            # every attempt, because we paid for every attempt.
            leads_billable=len(
                {row.lead_id for row in assessed if is_billable(input_tokens=row.input_tokens)}
            ),
            assessments_failed=sum(1 for row in assessed if row.status == "failed"),
            input_tokens=sum(row.input_tokens for row in assessed),
            output_tokens=sum(row.output_tokens for row in assessed),
            cache_read_tokens=sum(row.cache_read_tokens for row in assessed),
            cache_creation_tokens=sum(row.cache_creation_tokens for row in assessed),
            cost_usd=sum((row.cost_usd for row in assessed), Decimal(0)),
            computed_at=self._computed_at,
        )
        return totals

    def usage_for_period(self, *, tenant_id: str, period: BillingPeriod) -> UsageTotals:
        rows = self.daily_usage(tenant_id=tenant_id, period=period)
        stamps = [row.computed_at for row in rows if row.computed_at is not None]
        return UsageTotals(
            tenant_id=tenant_id,
            period=period,
            leads_ingested=sum(row.leads_ingested for row in rows),
            leads_assessed=sum(row.leads_assessed for row in rows),
            leads_billable=sum(row.leads_billable for row in rows),
            assessments_failed=sum(row.assessments_failed for row in rows),
            input_tokens=sum(row.input_tokens for row in rows),
            output_tokens=sum(row.output_tokens for row in rows),
            cache_read_tokens=sum(row.cache_read_tokens for row in rows),
            cache_creation_tokens=sum(row.cache_creation_tokens for row in rows),
            cost_usd=sum((row.cost_usd for row in rows), Decimal(0)),
            computed_at=max(stamps) if stamps else None,
        )

    def daily_usage(self, *, tenant_id: str, period: BillingPeriod) -> Sequence[UsageTotals]:
        return [
            self.rows[(tenant_id, day)] for day in period.dates() if (tenant_id, day) in self.rows
        ]

    def quota_for(self, *, tenant_id: str) -> TenantQuota:
        quota = self.quotas.get(tenant_id)
        if quota is None:
            raise MeteringError(f"no tenant '{tenant_id}'")
        return quota

    def set_quota(
        self, *, tenant_id: str, monthly_lead_quota: int | None, alert_fraction: Decimal
    ) -> TenantQuota:
        if tenant_id not in self.quotas and tenant_id not in {stored for stored, _ in self.rows}:
            # The real store finds the tenant row; here, a tenant is one that has either a
            # quota or some usage, which is as much identity as this double has.
            raise MeteringError(f"no tenant '{tenant_id}'")
        quota = TenantQuota(
            tenant_id=tenant_id,
            monthly_lead_quota=monthly_lead_quota,
            alert_fraction=alert_fraction,
        )
        self.quotas[tenant_id] = quota
        return quota

    def fleet_tenants_with_quota(self) -> Sequence[str]:
        return sorted(
            tenant_id
            for tenant_id, quota in self.quotas.items()
            if quota.monthly_lead_quota is not None
        )

    def fleet_billable_leads(self, *, period: BillingPeriod) -> Mapping[str, int]:
        totals: dict[str, int] = {}
        for (tenant_id, day), row in self.rows.items():
            if period.contains(day):
                totals[tenant_id] = totals.get(tenant_id, 0) + row.leads_billable
        return totals

    def fleet_daily_spend(self, *, period: BillingPeriod) -> Sequence[DailySpend]:
        by_day: dict[date, DailySpend] = {}
        for (_, day), row in sorted(self.rows.items()):
            if not period.contains(day):
                continue
            running = by_day.get(day)
            by_day[day] = DailySpend(
                usage_date=day,
                input_tokens=row.input_tokens + (running.input_tokens if running else 0),
                output_tokens=row.output_tokens + (running.output_tokens if running else 0),
                cache_read_tokens=(
                    row.cache_read_tokens + (running.cache_read_tokens if running else 0)
                ),
                cache_creation_tokens=(
                    row.cache_creation_tokens + (running.cache_creation_tokens if running else 0)
                ),
                cost_usd=row.cost_usd + (running.cost_usd if running else Decimal(0)),
            )
        return [by_day[day] for day in sorted(by_day)]


class StaticRevenue:
    """A :class:`~leadquali.app.metering.RevenuePort` that answers from a dict.

    ``None`` for a tenant it has never heard of, which is the same answer
    :class:`~leadquali.adapters.revenue_none.UnknownRevenue` gives for everyone — so a
    test can cover both branches of the margin arithmetic with one double.
    """

    def __init__(self, amounts: Mapping[str, Decimal] | None = None) -> None:
        self.amounts = dict(amounts or {})

    def revenue_usd(self, *, tenant_id: str, period: BillingPeriod) -> Decimal | None:
        del period
        return self.amounts.get(tenant_id)


# ------------------------------------------------------------------------- admin (#36)


class InMemoryConfigVersionStore:
    """A :class:`~leadquali.app.config_versions.ConfigVersionStorePort` over a dict.

    Allocates the version number from what it holds, exactly as the Postgres one allocates
    it from the table, so a test that asserts "version 2 followed version 1" is asserting
    the same rule in both.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[ConfigVersion]] = {}
        self.appends = 0

    def seed(
        self, *, tenant_slug: str, config: Mapping[str, Any], changed_by: str
    ) -> ConfigVersion:
        """Write version 1 the way the migration does, without counting as an append."""
        return self._append(
            tenant_slug=tenant_slug,
            config=config,
            changed_by=changed_by,
            changed_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
            note="seeded from the tenant's stored config",
        )

    def append(
        self,
        *,
        tenant_slug: str,
        config: Mapping[str, Any],
        changed_by: str,
        changed_at: datetime,
        note: str | None,
    ) -> ConfigVersion:
        self.appends += 1
        return self._append(
            tenant_slug=tenant_slug,
            config=config,
            changed_by=changed_by,
            changed_at=changed_at,
            note=note,
        )

    def list_versions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[ConfigVersion]:
        newest_first = sorted(
            self.rows.get(tenant_slug, ()), key=lambda row: row.version, reverse=True
        )
        return newest_first if limit is None else newest_first[:limit]

    def get_version(self, *, tenant_slug: str, version: int) -> ConfigVersion:
        for row in self.rows.get(tenant_slug, ()):
            if row.version == version:
                return row
        raise UnknownConfigVersionError(f"tenant '{tenant_slug}' has no config version {version}")

    # ------------------------------------------------------------------- assertions

    def versions(self, tenant_slug: str) -> list[ConfigVersion]:
        """Every version for one tenant, oldest first."""
        return sorted(self.rows.get(tenant_slug, ()), key=lambda row: row.version)

    def snapshot(self) -> dict[str, list[ConfigVersion]]:
        """State to restore if the surrounding unit of work rolls back."""
        return {slug: list(rows) for slug, rows in self.rows.items()}

    def restore(self, snapshot: dict[str, list[ConfigVersion]]) -> None:
        self.rows = {slug: list(rows) for slug, rows in snapshot.items()}

    def _append(
        self,
        *,
        tenant_slug: str,
        config: Mapping[str, Any],
        changed_by: str,
        changed_at: datetime,
        note: str | None,
    ) -> ConfigVersion:
        rows = self.rows.setdefault(tenant_slug, [])
        row = ConfigVersion(
            tenant_slug=tenant_slug,
            version=max((existing.version for existing in rows), default=0) + 1,
            config=dict(config),
            changed_by=changed_by,
            changed_at=changed_at,
            note=note,
        )
        rows.append(row)
        return row


class ExplodingConfigVersionStore(InMemoryConfigVersionStore):
    """A version store whose append always fails, after the config write has happened.

    The double that drives the one property worth the most: a config saved with no audit
    row must be impossible. It still counts the attempt, so a test can tell "the append
    raised" from "the append was never reached".
    """

    def append(
        self,
        *,
        tenant_slug: str,
        config: Mapping[str, Any],
        changed_by: str,
        changed_at: datetime,
        note: str | None,
    ) -> ConfigVersion:
        del config, changed_by, changed_at, note
        self.appends += 1
        raise FakeStoreError(f"could not append a config version for '{tenant_slug}'")


class SupportsSnapshot(Protocol):
    """A fake that can be rolled back by :class:`FakeUnitOfWork`."""

    def snapshot(self) -> Any:
        """Capture the state to restore on a rollback."""
        ...

    def restore(self, snapshot: Any) -> None:
        """Put the captured state back."""
        ...


class FakeUnitOfWork:
    """A :class:`~leadquali.app.config_versions.UnitOfWorkPort` that really does roll back.

    It snapshots every participant on entry and restores them if the block raises, which
    makes "these two writes are atomic" a property a unit test can actually observe rather
    than a claim about SQL nobody here can run. What it deliberately does *not* prove is
    that the Postgres implementation opens one transaction — that is asserted against a
    real engine in ``tests/unit/test_unit_of_work.py``.
    """

    def __init__(self, *participants: SupportsSnapshot) -> None:
        self.participants = participants
        self.depth = 0
        self.rollbacks = 0

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Run the block, undoing every participant's writes if it raises."""
        snapshots = [(participant, participant.snapshot()) for participant in self.participants]
        self.depth += 1
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            for participant, snapshot in snapshots:
                participant.restore(snapshot)
            raise
        finally:
            self.depth -= 1


class InMemoryGoldenPromotionStore:
    """A :class:`~leadquali.app.golden_promotion.GoldenPromotionStorePort` over a dict.

    Keyed on ``(tenant_slug, lead_id)``, exactly as
    ``uq_golden_promotions_tenant_id_lead_id`` keys it in the schema, so the idempotency a
    test asserts here is the idempotency the database enforces there.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], GoldenPromotion] = {}

    def record(
        self,
        *,
        tenant_slug: str,
        lead_id: str,
        case_id: str,
        expected_tier: Tier,
        promoted_by: str,
        note: str,
        promoted_at: datetime,
    ) -> tuple[GoldenPromotion, bool]:
        key = (tenant_slug, lead_id)
        existing = self.rows.get(key)
        if existing is not None:
            # The first label is the one that was reviewed. A later click reads it back
            # rather than replacing the tier it recorded.
            return existing, False
        row = GoldenPromotion(
            tenant_slug=tenant_slug,
            lead_id=lead_id,
            case_id=case_id,
            expected_tier=expected_tier,
            promoted_by=promoted_by,
            note=note,
            promoted_at=promoted_at,
        )
        self.rows[key] = row
        return row, True

    def list_promotions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[GoldenPromotion]:
        newest_first = sorted(
            (row for (slug, _), row in self.rows.items() if slug == tenant_slug),
            key=lambda row: (row.promoted_at, row.case_id),
            reverse=True,
        )
        return newest_first if limit is None else newest_first[:limit]

    def promoted_lead_ids(self, *, tenant_slug: str, lead_ids: Sequence[str]) -> frozenset[str]:
        wanted = set(lead_ids)
        return frozenset(
            lead_id for (slug, lead_id) in self.rows if slug == tenant_slug and lead_id in wanted
        )


@dataclass
class AdminLead:
    """One lead and its one assessment, as the in-memory admin query store keeps them.

    Flattened on purpose: the Postgres query the browser runs is an ``assessments`` scan
    joined to ``leads``, so a double that modelled two collections would be modelling a
    shape the real query does not have.
    """

    lead_id: str
    tenant_slug: str
    submission_id: str
    created_at: datetime
    tier: Tier | None = Tier.HOT
    total_score: Decimal | None = Decimal("82.00")
    confidence: Decimal | None = Decimal("0.900")
    status: str = "ok"
    escalation_reason: str | None = None
    industry: str | None = "logistics"
    company: str | None = "Northwind"
    raw_payload: Mapping[str, Any] = field(default_factory=dict)
    verdict: Verdict | None = None
    rater: str = "rep-1"
    feedback_notes: str | None = None
    assessment_id: str | None = None

    @property
    def row_id(self) -> str:
        """The assessment's id — what the keyset cursor pages on."""
        return self.assessment_id if self.assessment_id is not None else f"assess-{self.lead_id}"


class InMemoryAdminQueryStore:
    """An :class:`~leadquali.app.admin_views.AdminQueryPort` with the real one's ordering.

    The one behaviour worth modelling faithfully is the keyset cursor: rows are ordered by
    ``(created_at, id)`` descending and a page resumes *strictly after* the cursor, which
    is the same rule ``PostgresAdminQueryStore`` writes as a row-value comparison. A double
    that paged by list index would let the pagination test pass against SQL that skips
    rows under insertion, which is the exact bug keyset paging exists to prevent.
    """

    def __init__(self, leads: Iterable[AdminLead] = ()) -> None:
        self.leads: list[AdminLead] = list(leads)

    def add(self, lead: AdminLead) -> AdminLead:
        """Insert a lead, including mid-traversal."""
        self.leads.append(lead)
        return lead

    # ------------------------------------------------------------------------ browsing

    def browse_leads(
        self, *, criteria: LeadFilter, cursor: PageCursor | None, limit: int
    ) -> LeadPage:
        matching = [lead for lead in self._ordered() if self._matches(lead, criteria)]
        if cursor is not None:
            matching = [
                lead
                for lead in matching
                if (lead.created_at, lead.row_id) < (cursor.created_at, cursor.row_id)
            ]
        # One more than asked for, so "is there another page?" costs a row rather than a
        # COUNT(*) — the same trick the SQL uses.
        window = matching[: limit + 1]
        rows = tuple(self._row(lead) for lead in window[:limit])
        return LeadPage(
            rows=rows,
            next_cursor=rows[-1].cursor if len(window) > limit and rows else None,
        )

    def lead_detail(self, *, tenant_slug: str, lead_id: str) -> LeadDetail | None:
        for lead in self.leads:
            if lead.tenant_slug == tenant_slug and lead.lead_id == lead_id:
                return self._detail(lead)
        return None

    def feedback_review(
        self,
        *,
        tenant_slug: str,
        tier: Tier,
        verdict: Verdict,
        start: date,
        end: date,
        limit: int,
    ) -> Sequence[ReviewRow]:
        found = [
            lead
            for lead in self._ordered()
            if lead.tenant_slug == tenant_slug
            and lead.tier is tier
            and lead.verdict is verdict
            and start <= lead.created_at.date() <= end
        ]
        return [self._review_row(lead) for lead in found[:limit]]

    def tier_mix(self, *, tenant_slug: str, start: date, end: date) -> Sequence[TierCount]:
        counts: dict[Tier | None, int] = {}
        for lead in self.leads:
            if lead.tenant_slug == tenant_slug and start <= lead.created_at.date() <= end:
                counts[lead.tier] = counts.get(lead.tier, 0) + 1
        return [
            TierCount(tier=tier, count=counts[tier])
            for tier in sorted(counts, key=lambda found: found.rank if found else -1, reverse=True)
        ]

    def feedback_agreement(
        self, *, tenant_slug: str, start: date, end: date
    ) -> Sequence[AgreementPoint]:
        by_day: dict[date, dict[Verdict, int]] = {}
        for lead in self.leads:
            if lead.verdict is None or lead.tenant_slug != tenant_slug:
                continue
            day = lead.created_at.date()
            if not start <= day <= end:
                continue
            tally = by_day.setdefault(day, {})
            tally[lead.verdict] = tally.get(lead.verdict, 0) + 1
        return [
            AgreementPoint(
                day=day,
                good=by_day[day].get(Verdict.GOOD, 0),
                bad=by_day[day].get(Verdict.BAD, 0),
                unsure=by_day[day].get(Verdict.UNSURE, 0),
            )
            for day in sorted(by_day)
        ]

    def rerun_candidates(self, *, tenant_slug: str, limit: int) -> Sequence[RerunCandidate]:
        found = [lead for lead in self._ordered() if lead.tenant_slug == tenant_slug]
        return [
            RerunCandidate(
                lead_id=lead.lead_id,
                submission_id=lead.submission_id,
                submission=LeadSubmission(**dict(lead.raw_payload)),
                assessed_at=lead.created_at,
                previous_tier=lead.tier,
                previous_score=lead.total_score,
            )
            for lead in found[:limit]
        ]

    # ----------------------------------------------------------------------- internals

    def _ordered(self) -> list[AdminLead]:
        return sorted(self.leads, key=lambda lead: (lead.created_at, lead.row_id), reverse=True)

    @staticmethod
    def _matches(lead: AdminLead, criteria: LeadFilter) -> bool:
        if lead.tenant_slug != criteria.tenant_slug:
            return False
        if criteria.tier is not None and lead.tier is not criteria.tier:
            return False
        day = lead.created_at.date()
        if criteria.start is not None and day < criteria.start:
            return False
        if criteria.end is not None and day > criteria.end:
            return False
        if criteria.min_confidence is not None and (
            lead.confidence is None or lead.confidence < criteria.min_confidence
        ):
            return False
        return not (
            criteria.max_confidence is not None
            and (lead.confidence is None or lead.confidence > criteria.max_confidence)
        )

    @staticmethod
    def _row(lead: AdminLead) -> LeadRow:
        return LeadRow(
            lead_id=lead.lead_id,
            assessment_id=lead.row_id,
            submission_id=lead.submission_id,
            created_at=lead.created_at,
            received_at=lead.created_at,
            tier=lead.tier,
            total_score=lead.total_score,
            confidence=lead.confidence,
            status=lead.status,
            escalation_reason=lead.escalation_reason,
            company=lead.company,
            industry=lead.industry,
            contact_email_hash=contact_email_hash(str(lead.raw_payload.get("email") or "")),
            verdict=lead.verdict,
        )

    @staticmethod
    def _review_row(lead: AdminLead) -> ReviewRow:
        assert lead.verdict is not None
        return ReviewRow(
            lead_id=lead.lead_id,
            assessed_at=lead.created_at,
            tier=lead.tier,
            total_score=lead.total_score,
            confidence=lead.confidence,
            industry=lead.industry,
            company=lead.company,
            verdict=lead.verdict,
            rater=lead.rater,
            notes=lead.feedback_notes,
            feedback_at=lead.created_at,
        )

    @staticmethod
    def _detail(lead: AdminLead) -> LeadDetail:
        return LeadDetail(
            lead_id=lead.lead_id,
            tenant_slug=lead.tenant_slug,
            submission_id=lead.submission_id,
            source="web_form",
            received_at=lead.created_at,
            contact_email_hash=contact_email_hash(str(lead.raw_payload.get("email") or "")),
            raw_payload=dict(lead.raw_payload),
            assessments=(
                LeadAssessmentRow(
                    assessment_id=lead.row_id,
                    created_at=lead.created_at,
                    status=lead.status,
                    tier=lead.tier,
                    total_score=lead.total_score,
                    confidence=lead.confidence,
                    escalation_reason=lead.escalation_reason,
                    dimension_scores={"icp_fit": 28},
                    extracted={"industry": lead.industry, "company_name": lead.company},
                    reasoning="strong fit",
                    missing_information=[],
                    model_id="claude-test",
                    prompt_version="v1",
                    effort=None,
                    cost_usd=Decimal("0.0180"),
                    latency_ms=900,
                ),
            ),
            routing=(
                RoutingRow(
                    action="email_sales",
                    destination="hot@example.com",
                    outcome="dispatched",
                    provider_message_id="ses-1",
                    created_at=lead.created_at,
                ),
            ),
            feedback=(
                ()
                if lead.verdict is None
                else (
                    FeedbackNote(
                        rater=lead.rater,
                        verdict=lead.verdict,
                        notes=lead.feedback_notes,
                        created_at=lead.created_at,
                    ),
                )
            ),
        )


# ------------------------------------------------------------------------------ retention


@dataclass(slots=True)
class RetainedLead:
    """One row of ``leads``, reduced to what retention and erasure look at."""

    tenant_id: str
    lead_id: str
    received_at: datetime
    raw_payload: dict[str, Any]
    contact_email_hash: str | None = None


@dataclass(slots=True)
class RetainedAssessment:
    """One row of ``assessments``, reduced to the column that can quote a lead."""

    tenant_id: str
    lead_id: str
    assessment_id: str
    reasoning: str | None = None


@dataclass(slots=True)
class RetainedChild:
    """One row of ``routing_events``, ``feedback`` or ``golden_promotions``."""

    tenant_id: str
    lead_id: str


class InMemoryRetentionStore:
    """A :class:`~leadquali.app.retention.RetentionStorePort` over four lists.

    A real implementation of the contract rather than a canned answer, because the
    properties worth proving are behavioural: the batch is a *bound* and the service loops
    until it drains; a second run changes nothing because the tombstone filter excludes what
    the first run wrote; the payload scan finds a lead the hash missed; the cascade counts
    come from rows that really exist. Every one of those is a statement about the port's
    semantics, and ``tests/integration/test_retention_postgres.py`` proves the SQL agrees.

    Docker is not available in every environment this suite runs in (see
    ``tests/sqlcapture.py``), so this double is what keeps those properties asserted rather
    than skipped.
    """

    def __init__(self, *, policies: Mapping[str, tuple[int, int]] | None = None) -> None:
        self.policies: dict[str, RetentionPolicy] = {
            slug: RetentionPolicy(
                tenant_id=slug,
                raw_retention_days=windows[0],
                assessment_retention_days=windows[1],
            )
            for slug, windows in (policies or {}).items()
        }
        self.leads: list[RetainedLead] = []
        self.assessments: list[RetainedAssessment] = []
        self.routing_events: list[RetainedChild] = []
        self.feedback_rows: list[RetainedChild] = []
        self.promotions: list[RetainedChild] = []
        #: Every audit row written, in order. The ``erasure_log`` table.
        self.erasures: list[ErasureReceipt] = []
        #: Every batch size the service asked for, so a test can prove it loops rather
        #: than asking for everything at once.
        self.batches: list[int] = []
        self._ids = 0

    # ------------------------------------------------------------------------ seeding

    def given_tenant(
        self,
        slug: str,
        *,
        raw_retention_days: int = DEFAULT_RAW_RETENTION_DAYS,
        assessment_retention_days: int = DEFAULT_ASSESSMENT_RETENTION_DAYS,
    ) -> None:
        """Seed a tenant with its two windows, as a ``tenants`` row would have them."""
        self.policies[slug] = RetentionPolicy(
            tenant_id=slug,
            raw_retention_days=raw_retention_days,
            assessment_retention_days=assessment_retention_days,
        )

    def given_lead(
        self,
        *,
        tenant_id: str,
        received_at: datetime,
        submission: Mapping[str, Any] | None = None,
        email: str | None = None,
        reasoning: str | None = None,
        routed: bool = True,
    ) -> str:
        """Seed one lead with an assessment and a routing event. Returns its id.

        ``email`` is hashed the way ingest hashes it, so a test that erases by address
        exercises the real join rather than a fixture that happens to agree.
        """
        self._ids += 1
        lead_id = f"lead-{self._ids:04d}"
        payload = dict(submission or {})
        if email is not None:
            payload.setdefault("email", email)
        self.leads.append(
            RetainedLead(
                tenant_id=tenant_id,
                lead_id=lead_id,
                received_at=received_at,
                raw_payload=payload,
                contact_email_hash=contact_email_hash(email),
            )
        )
        self.assessments.append(
            RetainedAssessment(
                tenant_id=tenant_id,
                lead_id=lead_id,
                assessment_id=f"assessment-{self._ids:04d}",
                reasoning=reasoning,
            )
        )
        if routed:
            self.routing_events.append(RetainedChild(tenant_id=tenant_id, lead_id=lead_id))
        return lead_id

    def lead(self, lead_id: str) -> RetainedLead:
        """The seeded lead with this id, for an assertion about its payload."""
        for row in self.leads:
            if row.lead_id == lead_id:
                return row
        raise KeyError(lead_id)

    def reasoning_of(self, lead_id: str) -> list[str | None]:
        """Every stored ``reasoning`` for one lead, for a test that greps the row."""
        return [row.reasoning for row in self.assessments if row.lead_id == lead_id]

    # -------------------------------------------------------------------- the port

    def fleet_retention_policies(self) -> Sequence[RetentionPolicy]:
        """Every tenant's windows, in insertion order."""
        return list(self.policies.values())

    def retention_policy(self, *, tenant_id: str) -> RetentionPolicy:
        """One tenant's windows."""
        try:
            return self.policies[tenant_id]
        except KeyError as error:
            raise RetentionUnknownTenantError(f"no such tenant: {tenant_id}") from error

    def set_retention_policy(
        self, *, tenant_id: str, raw_retention_days: int, assessment_retention_days: int
    ) -> RetentionPolicy:
        """Write one tenant's windows."""
        self.retention_policy(tenant_id=tenant_id)
        policy = RetentionPolicy(
            tenant_id=tenant_id,
            raw_retention_days=raw_retention_days,
            assessment_retention_days=assessment_retention_days,
        )
        self.policies[tenant_id] = policy
        return policy

    def count_expired(
        self, *, tenant_id: str, payload_cutoff: datetime, lead_cutoff: datetime
    ) -> ExpiredCounts:
        """How much a run would touch, writing nothing."""
        return ExpiredCounts(
            tenant_id=tenant_id,
            payloads=len(self._expiring(tenant_id, payload_cutoff)),
            leads=len(
                [
                    row
                    for row in self.leads
                    if row.tenant_id == tenant_id and row.received_at < lead_cutoff
                ]
            ),
        )

    def redact_expired_payloads(
        self,
        *,
        tenant_id: str,
        cutoff: datetime,
        tombstone: Mapping[str, Any],
        batch_size: int,
    ) -> Sequence[str]:
        """Tombstone at most ``batch_size`` expired payloads, oldest first."""
        self.batches.append(batch_size)
        batch = self._expiring(tenant_id, cutoff)[:batch_size]
        for row in batch:
            row.raw_payload = dict(tombstone)
        return [row.lead_id for row in batch]

    def reasoning_for_leads(self, *, tenant_id: str, lead_ids: Sequence[str]) -> Mapping[str, str]:
        """Every non-empty ``reasoning`` for these leads, by assessment id."""
        wanted = set(lead_ids)
        return {
            row.assessment_id: row.reasoning
            for row in self.assessments
            if row.tenant_id == tenant_id and row.lead_id in wanted and row.reasoning
        }

    def replace_reasoning(self, *, tenant_id: str, replacements: Mapping[str, str]) -> int:
        """Overwrite the named assessments' ``reasoning``."""
        changed = 0
        for row in self.assessments:
            if row.tenant_id == tenant_id and row.assessment_id in replacements:
                row.reasoning = replacements[row.assessment_id]
                changed += 1
        return changed

    def purge_expired_leads(self, *, tenant_id: str, cutoff: datetime, batch_size: int) -> int:
        """Delete at most ``batch_size`` leads past tier 2, cascading to the children."""
        self.batches.append(batch_size)
        doomed = sorted(
            (row for row in self.leads if row.tenant_id == tenant_id and row.received_at < cutoff),
            key=lambda row: row.received_at,
        )[:batch_size]
        self._delete([row.lead_id for row in doomed], tenant_id=tenant_id)
        return len(doomed)

    def leads_for_subject(self, *, tenant_id: str, subject_hash: str) -> Sequence[str]:
        """Lead ids whose stored hash is this subject."""
        return [
            row.lead_id
            for row in self.leads
            if row.tenant_id == tenant_id and row.contact_email_hash == subject_hash
        ]

    def leads_mentioning(self, *, tenant_id: str, needle: str) -> Sequence[str]:
        """Lead ids whose payload contains ``needle`` anywhere, case-insensitively.

        ``json.dumps`` of the payload rather than a walk of its values, because the adapter
        casts the whole JSONB document to text — keys included — and a double that searched
        only the values would pass a lead the real store would find.
        """
        return [
            row.lead_id
            for row in self.leads
            if row.tenant_id == tenant_id and needle.lower() in json.dumps(row.raw_payload).lower()
        ]

    def count_lead_children(self, *, tenant_id: str, lead_ids: Sequence[str]) -> ChildCounts:
        """Rows that will cascade when these leads go, per table."""
        wanted = set(lead_ids)

        def matching(rows: Sequence[Any]) -> int:
            return len(
                [row for row in rows if row.tenant_id == tenant_id and row.lead_id in wanted]
            )

        return ChildCounts(
            assessments=matching(self.assessments),
            routing_events=matching(self.routing_events),
            feedback=matching(self.feedback_rows),
            golden_promotions=matching(self.promotions),
        )

    def erase(self, *, tenant_id: str, request: ErasureRequest) -> ErasureReceipt:
        """Delete these leads and file the audit row, as one act."""
        children = self.count_lead_children(tenant_id=tenant_id, lead_ids=request.lead_ids)
        deleted = self._delete(request.lead_ids, tenant_id=tenant_id)
        receipt = ErasureReceipt(
            tenant_id=tenant_id,
            subject_hash=request.subject_hash,
            leads_deleted=deleted,
            children=children,
            matched_by_hash=request.matched_by_hash,
            matched_by_payload_scan=request.matched_by_payload_scan,
            requested_by=request.requested_by,
            completed_at=request.completed_at,
        )
        self.erasures.append(receipt)
        return receipt

    # ------------------------------------------------------------------------ internals

    def _expiring(self, tenant_id: str, cutoff: datetime) -> list[RetainedLead]:
        """Leads past tier 1 that are not already tombstones, oldest first."""
        return sorted(
            (
                row
                for row in self.leads
                if row.tenant_id == tenant_id
                and row.received_at < cutoff
                and not is_tombstone(row.raw_payload)
            ),
            key=lambda row: row.received_at,
        )

    def _delete(self, lead_ids: Sequence[str], *, tenant_id: str) -> int:
        """Remove these leads and everything that cascades from them."""
        doomed = set(lead_ids)

        def keep(rows: list[Any]) -> list[Any]:
            return [
                row for row in rows if not (row.tenant_id == tenant_id and row.lead_id in doomed)
            ]

        before = len(self.leads)
        self.leads = keep(self.leads)
        self.assessments = keep(self.assessments)
        self.routing_events = keep(self.routing_events)
        self.feedback_rows = keep(self.feedback_rows)
        self.promotions = keep(self.promotions)
        return before - len(self.leads)
