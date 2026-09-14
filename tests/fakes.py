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

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
)
from leadquali.app.enrichment import Enrichment
from leadquali.app.feedback import UnknownLeadError, Verdict
from leadquali.app.metering import (
    BillingPeriod,
    DailySpend,
    MeteringError,
    TenantQuota,
    UsageTotals,
    is_billable,
)
from leadquali.app.ports import RecordedFeedback, RoutingOutcome, StoredLead
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import (
    ApiKeyRecord,
    TenantAlreadyExistsError,
    TenantRecord,
    TenantStatus,
    UnknownApiKeyError,
    UnknownTenantError,
)
from leadquali.domain.models import Action, LeadAssessment, RoutingDecision
from leadquali.domain.tenant_config import TenantConfig, TenantNotFoundError
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
        self.quotas = dict(quotas or {})
        self._computed_at = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)

    # ------------------------------------------------------------------------ seeding

    def add_lead(self, *, tenant_id: str, received_at: datetime) -> None:
        """Record a submission, as ingest would."""
        self.leads.append(MeteredLead(tenant_id=tenant_id, received_at=received_at))

    def add_assessment(self, *, tenant_id: str, created_at: datetime, **values: Any) -> None:
        """Record an assessment attempt, as the worker would."""
        self.assessments.append(
            MeteredAssessment(tenant_id=tenant_id, created_at=created_at, **values)
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
            leads_billable=sum(1 for row in assessed if is_billable(input_tokens=row.input_tokens)),
            assessments_failed=sum(1 for row in assessed if row.status == "failed"),
            input_tokens=sum(row.input_tokens for row in assessed),
            output_tokens=sum(row.output_tokens for row in assessed),
            cache_read_tokens=sum(row.cache_read_tokens for row in assessed),
            cache_creation_tokens=sum(row.cache_creation_tokens for row in assessed),
            cost_usd=sum((row.cost_usd for row in assessed), Decimal(0)),
            computed_at=self._computed_at,
        )
        # A replacement, never an increment — the property the real upsert exists for.
        self.rows[(tenant_id, day)] = totals
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
