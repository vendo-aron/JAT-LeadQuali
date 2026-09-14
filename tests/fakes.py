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
from datetime import UTC, datetime, timedelta
from typing import Any

from leadquali.app.assessment_result import (
    AssessmentFailed,
    AssessmentOutcome,
    AssessmentSucceeded,
)
from leadquali.app.enrichment import Enrichment
from leadquali.app.feedback import UnknownLeadError, Verdict
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
