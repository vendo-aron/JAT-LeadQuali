"""The repository inventory the isolation sweep is driven from, and the argument recipes.

Kept apart from the tests so that both halves of the sweep — the offline one that reads the
SQL a method emits, and the ``integration`` one that runs it against a real Postgres — are
driven from *the same* table. Two tables would drift, and the half that drifted would be
the half that does not run in an environment without Docker.

How a newly added repository method is meant to fail
----------------------------------------------------

Adding a public method to one of :data:`REPOSITORIES` and nothing else makes
``test_repository_isolation.py`` fail, in one of two ways and never by skipping:

* it has no tenant-scoping parameter and is not on :data:`ALLOWLIST` or in
  :data:`FLEET_METHODS` — the enumeration test fails and names it;
* it has one and has no entry in :data:`RECIPES` — the sweep fails and tells the author to
  add a recipe here.

That is the whole mechanism, and it is why the arguments are written out by hand. A harness
that synthesised them from type hints would produce a test that passes because the call
raised before it reached the database, which is indistinguishable from a test that passes
because the filter is where it should be.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, NamedTuple

from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.metering_postgres import PostgresMeteringStore
from leadquali.adapters.retention_postgres import PostgresRetentionStore
from leadquali.adapters.store_admin import (
    PostgresAdminQueryStore,
    PostgresConfigVersionStore,
    PostgresGoldenPromotionStore,
)
from leadquali.adapters.store_billing import PostgresBillingStore
from leadquali.adapters.store_postgres import (
    PostgresFeedbackStore,
    PostgresLeadStore,
    PostgresTenantConfigSource,
    tenant_uuid,
)
from leadquali.adapters.store_tenants import PostgresIngestCredentials, PostgresTenantAdminStore
from leadquali.app.admin_views import LeadFilter
from leadquali.app.api_keys import ApiKeyParts, KeyEnvironment
from leadquali.app.assessment_result import AssessmentFailed
from leadquali.app.billing import usage_external_id
from leadquali.app.feedback import Verdict
from leadquali.app.metering import BillingPeriod
from leadquali.app.ports import RoutingOutcome
from leadquali.app.retention import ErasureRequest, payload_tombstone
from leadquali.app.tenants import TenantStatus
from leadquali.domain.models import Action, EscalationReason, Tier
from leadquali.domain.routing import system_failure
from leadquali.prompts.lead import LeadSubmission
from tests.sqlcapture import CannedResult

__all__ = [
    "ALLOWLIST",
    "ASSESSMENT_A",
    "ASSESSMENT_B",
    "DAY",
    "EXCLUDED_REPOSITORIES",
    "FLEET_METHODS",
    "KEY_ID_A",
    "KEY_ID_B",
    "KEY_ID_NEW",
    "KEY_SECRET_A",
    "KEY_SECRET_B",
    "LEAD_A",
    "LEAD_B",
    "NOW",
    "RECIPES",
    "REPOSITORIES",
    "SEPTEMBER",
    "SIGNING_SECRET_REF",
    "SUBMISSION_A",
    "SUBMISSION_B",
    "TENANT_A",
    "TENANT_A_UUID",
    "TENANT_B",
    "TENANT_B_UUID",
    "TENANT_PARAMETERS",
    "ArgumentRecipe",
    "CrossTenant",
    "DictSecretResolver",
    "Repository",
    "api_key_for",
    "recipe_for",
    "tenant_parameter_of",
]

# --------------------------------------------------------------------------- the tenants

#: The two tenants every module in this package uses. Deliberately unlike each other in
#: every character: a log-isolation assertion that searched for ``"acme"`` inside
#: ``"acme-demo"`` would pass or fail for reasons that have nothing to do with tenancy.
TENANT_A: Final[str] = "alpha-instruments"
TENANT_B: Final[str] = "zenith-freight"

TENANT_A_UUID: Final[uuid.UUID] = tenant_uuid(TENANT_A)
TENANT_B_UUID: Final[uuid.UUID] = tenant_uuid(TENANT_B)

#: One lead each, with fixed ids so a cross-tenant call can name a row that really exists
#: and really belongs to somebody else.
LEAD_A: Final[str] = "3a5c9e10-0b47-4d2f-9c61-7e8a04b5d213"
LEAD_B: Final[str] = "c1d2e3f4-0506-4708-890a-b1c2d3e4f506"

#: One assessment each, with fixed ids so a cross-tenant write can name a row that really
#: belongs to somebody else. Only #37's reasoning redaction addresses an assessment by id.
ASSESSMENT_A: Final[str] = "9f2c1d40-5a63-4b18-8e7f-0c4d2a6b9e11"
ASSESSMENT_B: Final[str] = "7e6d5c4b-3a29-4180-9f6e-5d4c3b2a1908"

SUBMISSION_A: Final[str] = "submission-alpha-0001"
SUBMISSION_B: Final[str] = "submission-zenith-0001"

KEY_ID_A: Final[str] = "a1a1a1a1a1a1a1a1"
KEY_ID_B: Final[str] = "b2b2b2b2b2b2b2b2"
#: A third key id, issued during the sweep. Distinct from both seeded keys so that
#: ``add_key`` exercises a successful write under B rather than the unique-index refusal a
#: duplicate would produce — which would be a test of ``uq_tenant_api_keys_key_id``, not of
#: tenancy.
KEY_ID_NEW: Final[str] = "c3c3c3c3c3c3c3c3"
KEY_SECRET_A: Final[str] = "Wm9uYWxBbHBoYVNlY3JldE1hdGVyaWFsRm9yVGVzdHM"
KEY_SECRET_B: Final[str] = "WmVuaXRoRnJlaWdodFNlY3JldE1hdGVyaWFsRm9yVGVz"

SIGNING_SECRET_REF: Final[str] = "arn:aws:secretsmanager:eu-west-1:0:secret:signing"

#: #35's Stripe identifiers. A has both, so that a billing write made as B is checked
#: against a tenant that genuinely *has* something to overwrite — a snapshot of two
#: NULL columns before and after proves nothing. B's customer id is what B's own calls
#: use; the unique constraint that stops the two from ever being the same value is
#: proved on its own in ``tests/integration/test_store_billing.py``, because a recipe
#: that expected an IntegrityError would be testing the constraint rather than the
#: filter this sweep is about.
STRIPE_CUSTOMER_A: Final[str] = "cus_alphainstruments"
STRIPE_CUSTOMER_B: Final[str] = "cus_zenithfreight"
STRIPE_SUBSCRIPTION_A: Final[str] = "sub_alphainstruments"
STRIPE_SUBSCRIPTION_B: Final[str] = "sub_zenithfreight"

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)
DAY: Final[dt.date] = dt.date(2026, 9, 3)
SEPTEMBER: Final[BillingPeriod] = BillingPeriod.of_month(2026, 9)

#: The parameter names that mean "this call is about one tenant", most specific first. Three
#: of them because the control plane addresses a tenant by its slug and the data plane by its
#: port-level id, and #36's admin queries spell the slug ``tenant_slug``.
#:
#: The bare ``slug`` entry carries an assumption worth naming: it holds only while ``slug``
#: means *tenant* slug everywhere in the adapters. The moment something grows a slug of its
#: own — a saved view, a config version, a golden-set name — a method taking it would be
#: read as tenant-scoped and swept with the wrong argument. That shows up as a recipe whose
#: cross-tenant call does not behave, not as a silent pass, but the cheaper fix is to rename
#: the parameter or add it here explicitly.
TENANT_PARAMETERS: Final[tuple[str, ...]] = ("tenant_id", "tenant_slug", "slug")


class DictSecretResolver:
    """A :class:`~leadquali.config.SecretResolver` over a dict, for the credential source.

    :class:`~leadquali.adapters.store_tenants.PostgresIngestCredentials` needs one to
    construct. It is never reached on a cross-tenant lookup — the rejection happens before
    the signing secret is fetched — which is itself worth asserting, so this records its
    calls.
    """

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {SIGNING_SECRET_REF: "s" * 40})
        self.calls: list[str] = []

    def resolve(self, secret_arn: str) -> str:
        """Return the secret's value, remembering that it was asked for."""
        self.calls.append(secret_arn)
        return self.secrets[secret_arn]

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:
        """Not used by the credential source; present so the Protocol is satisfied."""
        raise AssertionError(f"the credential source should never parse {secret_arn} as JSON")


def api_key_for(key_id: str, secret: str) -> str:
    """The presented form of one API key, as a customer's form would send it."""
    return ApiKeyParts(environment=KeyEnvironment.LIVE, key_id=key_id, secret=secret).text


# ------------------------------------------------------------------------- the vocabulary


class CrossTenant(StrEnum):
    """What a repository method must do when it is handed another tenant's row.

    Whatever the value, **one post-condition applies to every recipe**: after the call,
    every row belonging to tenant A is byte-for-byte what it was before. That is the
    ``writes_nothing`` half of the guarantee, it is not a per-method choice, and it is
    checked by the snapshot in the integration sweep rather than being spelled out twenty
    times here. These values say what the *caller* sees on top of it.
    """

    RETURNS_NONE = "returns_none"
    """The answer is ``None``: there is no such row for this tenant."""

    RETURNS_EMPTY = "returns_empty"
    """The answer is empty or false — no rows, nothing routed, nothing to list."""

    RETURNS_OWN = "returns_own"
    """The call succeeds with a non-empty answer, and that answer is about the tenant that
    asked. Checked as "none of tenant A's identifiers appears anywhere in the result"
    rather than as "tenant B's identifier does appear", because several of these results
    are records that carry no tenant field at all — an ``ApiKeyRecord`` is a key id and a
    prefix, and demanding B's slug in it would only test the double."""

    RAISES = "raises"
    """The call is refused. Usually the composite ``(tenant_id, lead_id)`` foreign key or a
    'no such row for this tenant' lookup — the structural half of the guarantee at work."""

    REFUSED = "refused"
    """The call returns a refusal *value* rather than raising or answering. Only the
    credential source does this: its whole contract is that a rejection carries the reason
    (:class:`~leadquali.api.signing.CredentialRejected`) so an operator can tell a
    customer's misconfigured form from somebody probing the endpoint."""

    WRITES_NOTHING = "writes_nothing"
    """The call completes and touches only the calling tenant's own rows. There is nothing
    further to assert about the return value; the snapshot is the assertion."""


@dataclass(frozen=True, slots=True)
class ArgumentRecipe:
    """How to call one repository method, and what it must do across a tenant boundary.

    Args:
        arguments: every argument except the tenant-scoping one, which the sweep injects.
            A recipe *may* name the tenant argument itself when the method wants something
            other than a slug — ``create_tenant`` takes a :class:`uuid.UUID`.
        expected: what a cross-tenant call must do. See :class:`CrossTenant`.
        results: one :class:`~tests.sqlcapture.CannedResult` for every statement the method
            issues *except the last*, so the offline sweep sees all of them and the method
            still never interprets a row it was not given.
        python_scoped: the tenant check is a comparison in Python rather than a predicate in
            the SQL. True for exactly one method, and it routes the sweep to a behavioural
            assertion instead of a SQL one.
        note: why this method is shaped the way it is, where that is not obvious.
    """

    arguments: Mapping[str, Any] = field(default_factory=dict)
    expected: CrossTenant = CrossTenant.WRITES_NOTHING
    results: tuple[CannedResult, ...] = ()
    python_scoped: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class Repository:
    """One concrete repository class, and how to build it over a session factory."""

    cls: type
    build: Callable[[sessionmaker[Session]], Any]

    @property
    def name(self) -> str:
        """The class name, for test ids and failure messages."""
        return self.cls.__name__


def _ingest_credentials(sessions: sessionmaker[Session]) -> PostgresIngestCredentials:
    """The request-path credential source, with the clock pinned and the touch write off.

    ``last_used_coarseness=None`` switches off the ``last_used_at`` write. It is a second
    statement issued *after* the credential decision and is no part of it, so it would tell
    the sweep nothing about how that decision is scoped — and it is private, which puts it
    outside the enumeration either way. It is tenant-scoped, and
    ``test_the_last_used_write_is_tenant_scoped_like_every_other_write`` says so directly.
    """
    return PostgresIngestCredentials(
        sessions,
        verifier=Argon2KeyHasher(),
        resolver=DictSecretResolver(),
        now=lambda: NOW,
        last_used_coarseness=None,
    )


#: Every concrete class in the codebase that reads or writes tenant-owned rows. Listed once,
#: here; the tests iterate this and never name a class of their own.
REPOSITORIES: Final[tuple[Repository, ...]] = (
    Repository(PostgresLeadStore, PostgresLeadStore),
    Repository(PostgresFeedbackStore, PostgresFeedbackStore),
    Repository(PostgresTenantConfigSource, PostgresTenantConfigSource),
    Repository(PostgresTenantAdminStore, PostgresTenantAdminStore),
    Repository(PostgresMeteringStore, PostgresMeteringStore),
    Repository(PostgresRetentionStore, PostgresRetentionStore),
    Repository(PostgresIngestCredentials, _ingest_credentials),
    # #36's staff console. Three classes, and the first of them is the read surface over
    # every tenant's leads, assessments and feedback — the one the completeness test was
    # written after finding it unswept on this branch.
    Repository(PostgresAdminQueryStore, PostgresAdminQueryStore),
    Repository(PostgresConfigVersionStore, PostgresConfigVersionStore),
    Repository(PostgresGoldenPromotionStore, PostgresGoldenPromotionStore),
    # #35's billing store. The one repository here that owns a table which is *deliberately*
    # not tenant-scoped on insert — see its entry in ALLOWLIST — so it is also the one where
    # the line between "cannot be scoped" and "was not scoped" has to be drawn by hand.
    Repository(PostgresBillingStore, PostgresBillingStore),
)


#: Concrete adapter classes that take a ``sessionmaker`` and are deliberately *not* swept,
#: each with the reason. Empty, and it should stay that way: a class that reaches the
#: database on behalf of a tenant belongs in :data:`REPOSITORIES` with recipes, not here.
#: ``test_every_adapter_over_a_session_factory_is_swept`` fails naming anything missing from
#: both, which is how a whole repository class stops being able to go uncovered.
EXCLUDED_REPOSITORIES: Final[Mapping[str, str]] = {
    "PostgresUnitOfWork": (
        "#36's ambient transaction. It takes a session factory and issues no statement of "
        "its own: atomic() opens one session, binds it to a ContextVar and commits or "
        "rolls back at the block's edge, so every statement inside it belongs to a store "
        "that is swept here in its own right. There is no query to scope and no tenant to "
        "scope it to — the config write and its audit row are each checked where they are "
        "written. tests/unit/test_unit_of_work.py covers the transaction semantics against "
        "a real engine."
    ),
}

#: The two constructor exemptions, written out once. They are the only entries on
#: :data:`ALLOWLIST` that repeat, and a reason short enough to repeat is a reason short
#: enough to be a rubber stamp — so they say what makes the exemption safe rather than
#: naming the category.
_FROM_URL: Final[str] = (
    "constructor: takes a database URL, builds a session factory and issues no statement"
)
_FROM_ENV: Final[str] = (
    "constructor: reads DATABASE_URL through Settings and issues no statement of its own"
)


#: Public methods that legitimately take no tenant, each with the reason it is allowed to.
#: Short by construction: anything that is not a constructor or a deliberate fleet-wide
#: operator tool does not belong here, it belongs in :data:`RECIPES`.
ALLOWLIST: Final[Mapping[type, Mapping[str, str]]] = {
    PostgresLeadStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresFeedbackStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresTenantConfigSource: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresTenantAdminStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
        "list_tenants": (
            "the control plane's own enumeration. It answers 'who are our customers?' for "
            "an operator running tenantctl, it is on no request path, and a tenant filter "
            "would make it meaningless. It returns whole rows, so it is the one method "
            "here that must never be reachable from an authenticated tenant context."
        ),
    },
    PostgresMeteringStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresRetentionStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresIngestCredentials: {},
    PostgresAdminQueryStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresConfigVersionStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    PostgresGoldenPromotionStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
    },
    # #35's billing store, and the longest set of exemptions in this table. Every one of
    # them is a method over ``stripe_events``, which is the documented exception to
    # invariant 4: a Stripe webhook names a *customer*, and which of our tenants that is
    # cannot be known until a database read the verifying route deliberately does not make.
    # Refusing to store an event we cannot attribute would mean discarding the only record
    # that it arrived.
    #
    # The exception is bounded in one direction and it is the direction that matters: it
    # covers the methods that touch an event *before* attribution, and no others. Every
    # read that is about a tenant — billing_tenant, usage_reported — is in RECIPES and is
    # swept like anything else, and the two fleet-wide worklists say so in their names.
    # ``test_the_billing_exception_covers_only_unattributed_events`` holds that line.
    PostgresBillingStore: {
        "from_url": _FROM_URL,
        "from_env": _FROM_ENV,
        "insert_event": (
            "the webhook route's whole body. It runs before we know whose event this is — "
            "the payload names a Stripe customer, and resolving that to a tenant is a "
            "second query the route does not make, because its contract is verify, insert, "
            "200. The row it writes has tenant_id NULL by design; a tenant predicate here "
            "would have nothing to compare against and nothing to protect."
        ),
        "pending_events": (
            "the drain's worklist, and every row it can return has tenant_id NULL. That is "
            "structural rather than incidental: the only statement that writes tenant_id "
            "is mark_event_processed, which sets status='processed' in the same UPDATE, so "
            "a pending row is by construction an unattributed one. There is no tenant to "
            "scope this to until the handler resolves one."
        ),
        "mark_event_attempt_failed": (
            "counts one failed attempt on an event addressed by Stripe's globally unique "
            "evt_... primary key. The row is still pending and therefore still "
            "unattributed; adding a tenant predicate would filter on a NULL column and "
            "silently update nothing, which is worse than not filtering."
        ),
        "mark_event_processed": (
            "the statement that performs the attribution. Its tenant_id argument is the "
            "value being *written* into the row, not a filter for finding it — the row is "
            "found by its evt_... primary key, and its tenant_id is NULL until this "
            "UPDATE sets it, so a WHERE on the tenant would match zero rows. This is the "
            "one entry here that takes a tenant and is still exempt, and "
            "test_the_attribution_write_names_the_tenant_it_writes covers it by name "
            "rather than leaving it uncovered."
        ),
        "tenant_for_customer": (
            "the lookup that *produces* the tenant every other billing read is then scoped "
            "to. It is handed a Stripe customer id and answers with a slug or None; there "
            "is no tenant to filter on, because finding out which one it is is the "
            "question. It returns a slug and nothing else, so it cannot leak a row."
        ),
    },
}


#: The documented fleet-wide exceptions (#33). Reconciliation against Anthropic's invoice
#: and the infrastructure-cost allocation both need figures across every tenant, and a sum
#: with no tenant attached to it is exactly what the scoping rule exists to prevent — so
#: they return the *breakdown*, and they carry ``fleet_`` in their names to say so. The
#: sweep checks that the name and the absence of a tenant parameter agree;
#: ``test_metering_isolation.py`` checks that no fleet result reaches one tenant's report.
FLEET_METHODS: Final[Mapping[type, frozenset[str]]] = {
    # #35's two billing worklists, named by the same rule and for the same reason. Both
    # answer "who should this scheduled job iterate over?" — a list of customers rather
    # than any customer's data — and both are on no request path. Renaming them to carry
    # the prefix was the useful outcome of putting this store through the sweep: they were
    # `billable_tenants` and `tenants_in_expired_dunning`, which look at a call site exactly
    # like queries somebody forgot to filter.
    PostgresBillingStore: frozenset({"fleet_billable_tenants", "fleet_tenants_in_expired_dunning"}),
    PostgresMeteringStore: frozenset(
        {
            "fleet_billable_leads",
            "fleet_daily_spend",
            # The quota sweep's worklist: the slugs of every tenant that has a plan. A list
            # of customers rather than any customer's data, on no request path, and exactly
            # what the naming rule is for — an operator job that has to know who to iterate
            # over says so in its own name instead of quietly dropping a filter.
            "fleet_tenants_with_quota",
        }
    ),
    PostgresRetentionStore: frozenset(
        {
            # The scheduled purge's worklist: every tenant and the two retention windows
            # on its row. A job that deletes customer data has to know who to run for, and
            # one that took a tenant would simply never run for the tenant somebody forgot
            # to list — which is the failure mode the naming rule exists to make visible.
            # It returns the per-tenant breakdown, never a count and never a total.
            "fleet_retention_policies",
        }
    ),
}


class _LeadRow(NamedTuple):
    """The shape ``PostgresAdminQueryStore.lead_detail`` reads off its first ``SELECT``.

    Named columns rather than a tuple, because that is how the adapter consumes the row —
    and because a method that starts reading a column this does not have should fail by
    name here rather than quietly receiving something plausible.
    """

    id: uuid.UUID
    submission_id: str
    source: str
    received_at: dt.datetime
    contact_email_hash: str | None
    raw_payload: Mapping[str, Any]


#: What the lead SELECT answers so the sweep reaches the three statements behind it. The
#: payload is deliberately a probe's rather than a plausible lead's: nothing about this row
#: is asserted on, and a realistic one would invite somebody to start asserting on it.
_A_LEAD_ROW: Final[CannedResult] = CannedResult(
    row=_LeadRow(
        id=uuid.UUID(LEAD_A),
        submission_id=SUBMISSION_A,
        source="web_form",
        received_at=NOW,
        contact_email_hash="a" * 64,
        raw_payload={"probe": "cross-tenant"},
    )
)


_OUTCOME = AssessmentFailed(
    reason=EscalationReason.API_ERROR, detail="the provider returned 503", latency_ms=41
)
_DECISION = system_failure(EscalationReason.API_ERROR, "the provider returned 503")
_SUBMISSION = LeadSubmission(
    full_name="Cross Tenant Probe",
    email="probe@isolation.invalid",
    company="Isolation Probe Ltd",
    role="Head of Nothing",
    message="This submission exists only to be written under the wrong tenant.",
    extra={},
)


#: The instant every retention recipe counts back from, and the two cutoffs derived from
#: it. Far enough in the past that both tiers have something to find in the integration
#: fixture, and fixed so the compiled SQL is comparable run to run.
RETENTION_CUTOFF: Final[dt.datetime] = NOW - dt.timedelta(days=90)
RETENTION_LEAD_CUTOFF: Final[dt.datetime] = NOW - dt.timedelta(days=730)

#: The tombstone a tier-1 redaction writes. Built by the application's own function rather
#: than typed out, so a change to the marker's shape reaches this sweep too.
RETENTION_TOMBSTONE: Final[dict[str, Any]] = payload_tombstone(redacted_at=NOW)

#: A subject hash for the erasure recipes. Not derived from either tenant's fixture
#: address: the point of these calls is that they find nothing under the wrong tenant.
SUBJECT_HASH: Final[str] = "0" * 64


#: Method name to the arguments it is called with and what a cross-tenant call must do.
#: An unmapped method is a hard failure, not a skip; see the module docstring.
RECIPES: Final[Mapping[type, Mapping[str, ArgumentRecipe]]] = {
    PostgresLeadStore: {
        "upsert_lead": ArgumentRecipe(
            arguments={
                "submission_id": SUBMISSION_A,
                "submission": _SUBMISSION,
                "source": "web_form",
                "received_at": NOW,
            },
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "A's submission id replayed under B. It is unique per tenant, so B gets a "
                "row of its own and A's row keeps its payload, source and received_at."
            ),
        ),
        "already_routed": ArgumentRecipe(
            arguments={"lead_id": LEAD_A},
            expected=CrossTenant.RETURNS_EMPTY,
            note="A's lead is dispatched; B must not be able to read that off it.",
        ),
        "record_assessment": ArgumentRecipe(
            arguments={
                "lead_id": LEAD_A,
                "outcome": _OUTCOME,
                "decision": _DECISION,
                "recorded_at": NOW,
            },
            expected=CrossTenant.RAISES,
            results=(CannedResult(),),
            note="The composite (tenant_id, lead_id) foreign key refuses the row outright.",
        ),
        "record_routing_event": ArgumentRecipe(
            arguments={
                "lead_id": LEAD_A,
                "action": Action.EMAIL_SALES,
                "destination": "intruder@zenith-freight.invalid",
                "outcome": RoutingOutcome.DISPATCHED,
                "provider_message_id": "provider-msg-probe",
                "occurred_at": NOW,
                "detail": "cross-tenant probe",
            },
            expected=CrossTenant.RAISES,
            results=(CannedResult(),),
            note=(
                "The one that would matter most: a routing event against A's lead naming "
                "B's inbox is A's lead being emailed to B."
            ),
        ),
    },
    PostgresFeedbackStore: {
        "record_feedback": ArgumentRecipe(
            arguments={
                "lead_id": LEAD_A,
                "rater": "dest:0123456789abcdef0123456789abcdef",
                "verdict": Verdict.BAD,
                "notes": "written across a tenant boundary",
                "recorded_at": NOW,
            },
            expected=CrossTenant.RAISES,
            results=(CannedResult(),),
            note=(
                "UnknownLeadError, off the same composite foreign key — which is why a "
                "leaked feedback link cannot poison another tenant's training data."
            ),
        ),
    },
    PostgresTenantConfigSource: {
        "get": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="B asks for config and gets B's rubric, thresholds and routing table.",
        ),
    },
    PostgresTenantAdminStore: {
        "create_tenant": ArgumentRecipe(
            arguments={
                "tenant_id": TENANT_B_UUID,
                "slug": TENANT_B,
                "name": "Zenith Freight",
                "config": {},
                "hmac_secret_ref": None,
            },
            expected=CrossTenant.RAISES,
            note="B already exists in the fixture, so this is the duplicate-slug refusal.",
        ),
        "get_tenant": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="Addressed by slug, so 'B' can only ever name B's row.",
        ),
        "update_config": ArgumentRecipe(
            arguments={"config": {"tenant_id": TENANT_A, "poisoned": True}},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "A config document *claiming* to be A's, written under B's slug. It lands "
                "on B's row and A's rubric is untouched, which is config isolation at the "
                "storage layer."
            ),
        ),
        "set_status": ArgumentRecipe(
            arguments={"status": TenantStatus.SUSPENDED},
            expected=CrossTenant.WRITES_NOTHING,
            note="Suspending B must not suspend A; test_auth_isolation.py proves the edge.",
        ),
        "rate_limit_for": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="B's allowance, never A's — otherwise one tenant sets another's throttle.",
        ),
        "add_key": ArgumentRecipe(
            arguments={
                "key_id": KEY_ID_NEW,
                "key_prefix": f"lq_live_{KEY_ID_NEW}",
                "key_hash": "$argon2id$v=19$m=19456,t=2,p=1$c2FsdHNhbHQ$aGFzaA",
                "label": "issued under B",
            },
            expected=CrossTenant.WRITES_NOTHING,
            results=(CannedResult(row=(TENANT_B_UUID,)),),
            note="Two statements: resolve the slug, then insert against the id it returned.",
        ),
        "list_keys": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="B's keys. A's key rows must not appear, hashes or no hashes.",
        ),
        "expire_key": ArgumentRecipe(
            arguments={"key_id": KEY_ID_A, "expires_at": NOW},
            expected=CrossTenant.RAISES,
            note=(
                "A's key id under B's slug. key_id is globally unique, so without the "
                "tenant predicate this would expire a competitor's live credential."
            ),
        ),
        "revoke_key": ArgumentRecipe(
            arguments={"key_id": KEY_ID_A, "revoked_at": NOW},
            expected=CrossTenant.RAISES,
            note="The same, and worse: revocation is immediate and cannot be undone.",
        ),
    },
    PostgresMeteringStore: {
        "rollup_day": ArgumentRecipe(
            arguments={"day": DAY},
            expected=CrossTenant.RETURNS_OWN,
            note="A day on which both tenants were active. B's rollup counts only B.",
        ),
        "compute_day": ArgumentRecipe(
            arguments={"day": DAY},
            expected=CrossTenant.RETURNS_OWN,
            note=(
                "Added by #33's review fixes after this sweep was written, and caught by "
                "it: the live quota read, and the only reporting path that still reaches "
                "into `assessments`. It shares `_day_source` with `rollup_day`, so the "
                "tenant predicate is in one place — which is a reason to check both, not "
                "one of them."
            ),
        ),
        "usage_for_period": ArgumentRecipe(
            arguments={"period": SEPTEMBER},
            expected=CrossTenant.RETURNS_OWN,
        ),
        "daily_usage": ArgumentRecipe(
            arguments={"period": SEPTEMBER},
            expected=CrossTenant.RETURNS_EMPTY,
            note="B has no rollup row in the fixture, so the honest answer is no rows.",
        ),
        "quota_for": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="A plan is commercial information; reading another tenant's is a leak.",
        ),
        "set_quota": ArgumentRecipe(
            arguments={"monthly_lead_quota": 10, "alert_fraction": Decimal("0.5")},
            expected=CrossTenant.WRITES_NOTHING,
            note="Writing B's plan must not move A's, which is money.",
        ),
    },
    PostgresRetentionStore: {
        "retention_policy": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note="A retention window is a contractual term; reading another's is a leak.",
        ),
        "set_retention_policy": ArgumentRecipe(
            arguments={"raw_retention_days": 30, "assessment_retention_days": 365},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "Shortening B's window must not shorten A's. This is the one write in the "
                "system that can bring forward the destruction of a customer's data."
            ),
        ),
        "count_expired": ArgumentRecipe(
            arguments={
                "payload_cutoff": RETENTION_CUTOFF,
                "lead_cutoff": RETENTION_LEAD_CUTOFF,
            },
            expected=CrossTenant.RETURNS_OWN,
            note="A dry run that counted the fleet would tell an operator to expect the "
            "wrong number and then delete a different set of rows.",
        ),
        "redact_expired_payloads": ArgumentRecipe(
            arguments={
                "cutoff": RETENTION_CUTOFF,
                "tombstone": RETENTION_TOMBSTONE,
                "batch_size": 10,
            },
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "The destructive one. A missing tenant filter here does not raise, does "
                "not fail and does not look wrong: it tombstones every expired lead in "
                "the fleet, and the rows it overwrote are gone."
            ),
        ),
        "reasoning_for_leads": ArgumentRecipe(
            arguments={"lead_ids": [LEAD_A]},
            expected=CrossTenant.RETURNS_EMPTY,
            note="A's lead id, read as B. The model's prose about A's lead is A's.",
        ),
        "replace_reasoning": ArgumentRecipe(
            arguments={"replacements": {ASSESSMENT_A: "redacted by the sweep"}},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "A's assessment id, written as B. One statement per row, so the sweep "
                "reads each one and each one names the tenant."
            ),
        ),
        "purge_expired_leads": ArgumentRecipe(
            arguments={"cutoff": RETENTION_LEAD_CUTOFF, "batch_size": 10},
            expected=CrossTenant.WRITES_NOTHING,
            note="The only DELETE in the codebase that runs on a schedule.",
        ),
        "leads_for_subject": ArgumentRecipe(
            arguments={"subject_hash": SUBJECT_HASH},
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "The same person can be a lead of two customers, and each controller may "
                "only erase their own copy. Unscoped, one customer's deletion request "
                "would destroy another customer's data."
            ),
        ),
        "leads_mentioning": ArgumentRecipe(
            arguments={"needle": "probe@isolation.invalid"},
            expected=CrossTenant.RETURNS_EMPTY,
            note="The slow second net. It reads payloads, so it is the worst one to leave "
            "unfiltered: a substring match across the fleet returns other customers' rows.",
        ),
        "count_lead_children": ArgumentRecipe(
            arguments={"lead_ids": [LEAD_A]},
            expected=CrossTenant.RETURNS_OWN,
            results=(CannedResult(row=(0,)), CannedResult(row=(0,)), CannedResult(row=(0,))),
            note=(
                "A's lead id counted as B. One statement per child table, so the sweep "
                "checks each of the four rather than one statement that mentions the "
                "tenant somewhere; three canned results so it sees all four."
            ),
        ),
        "erase": ArgumentRecipe(
            arguments={
                "request": ErasureRequest(
                    subject_hash=SUBJECT_HASH,
                    lead_ids=(LEAD_A,),
                    matched_by_hash=1,
                    matched_by_payload_scan=0,
                    requested_by="isolation-sweep",
                    completed_at=NOW,
                )
            },
            expected=CrossTenant.WRITES_NOTHING,
            results=(
                CannedResult(row=(0,)),
                CannedResult(row=(0,)),
                CannedResult(row=(0,)),
                CannedResult(row=(0,)),
                CannedResult(),
            ),
            note=(
                "Handed A's lead id while acting as B: the four child counts, the delete "
                "and the audit row are all filtered on B, so nothing of A's is read or "
                "removed and the erasure_log row is filed under B. Five canned results so "
                "the sweep sees all six statements — one per child table since #32's rule "
                "stopped accepting a tenant predicate buried in a scalar sub-select."
            ),
        ),
    },
    PostgresIngestCredentials: {
        "resolve": ArgumentRecipe(
            arguments={"api_key": api_key_for(KEY_ID_A, KEY_SECRET_A)},
            expected=CrossTenant.REFUSED,
            python_scoped=True,
            note=(
                "The one method whose tenant check is not a WHERE clause. The lookup is by "
                "key_id — globally unique, and the reason argon2 is affordable on the "
                "request path — and the row it finds carries the owning tenant's slug, "
                "which leadquali.app.credentials.decide_credential then compares in Python. "
                "The sweep therefore asserts the behaviour: a real key presented under "
                "another tenant's name is refused, and refused as UNKNOWN_TENANT so it is "
                "indistinguishable from a key that does not exist. test_auth_isolation.py "
                "drives that decision directly, since #31's review fixes it is the "
                "one place the rule is written. Recorded in docs/tenant-isolation.md."
            ),
        ),
    },
    PostgresAdminQueryStore: {
        "browse_leads": ArgumentRecipe(
            arguments={"criteria": LeadFilter(), "cursor": None, "limit": 25},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "The admin console's lead browser, and the method #32's completeness test "
                "was written after finding this whole class unswept. The tenant is a "
                "parameter of its own rather than a field of LeadFilter, which is what "
                "lets the sweep see it at all: a method whose tenant arrives inside a "
                "value object does not name its tenant, and invariant 4 asks it to. "
                "WRITES_NOTHING rather than RETURNS_EMPTY because a page with no rows is "
                "still a LeadPage and therefore truthy; the assertion that matters here is "
                "the standing one, that none of A's identifiers appears in B's answer."
            ),
        ),
        "lead_detail": ArgumentRecipe(
            arguments={"lead_id": LEAD_A},
            expected=CrossTenant.RETURNS_NONE,
            results=(_A_LEAD_ROW, CannedResult(), CannedResult()),
            note=(
                "A's lead id asked for under B's name — a staff member following a stale "
                "link, or a tenant id edited in a query string. Four statements, so the "
                "first is answered with a canned row: otherwise the method returns None at "
                "the first SELECT and the sweep never sees the three that read the "
                "assessments, the routing events and the feedback."
            ),
        ),
        "feedback_review": ArgumentRecipe(
            arguments={
                "tier": Tier.HOT,
                "verdict": Verdict.BAD,
                "start": dt.date(2026, 8, 4),
                "end": DAY,
                "limit": 50,
            },
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "'Every lead scored hot last month that the rep marked bad' — the query "
                "the storage design was made for. It joins assessments to feedback, so a "
                "dropped predicate would hand B both halves of A's training signal."
            ),
        ),
        "tier_mix": ArgumentRecipe(
            arguments={"start": dt.date(2026, 8, 4), "end": DAY},
            expected=CrossTenant.RETURNS_EMPTY,
            note="A dashboard aggregate. Unscoped it would report the fleet as one tenant.",
        ),
        "feedback_agreement": ArgumentRecipe(
            arguments={"start": dt.date(2026, 8, 4), "end": DAY},
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "Day-by-day verdict counts. The only admin read whose index leads on the "
                "tenant and then filters rather than seeking, which is why it is worth "
                "seeing the predicate rendered."
            ),
        ),
        "rerun_candidates": ArgumentRecipe(
            arguments={"limit": 25},
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "The input to a rubric re-run, and it carries raw_payload. Unscoped, a "
                "staff member previewing B's rubric would be sending A's leads to the "
                "model — which is the one path in the admin that leaves the database."
            ),
        ),
    },
    PostgresConfigVersionStore: {
        "append": ArgumentRecipe(
            arguments={
                "config": {"tenant_id": TENANT_A, "poisoned": True},
                "changed_by": "probe",
                "changed_at": NOW,
                "note": "written across a tenant boundary",
            },
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "An INSERT ... SELECT, so the sweep checks both halves: the row carries "
                "tenant_id, and the SELECT it is fed from is filtered on tenants.id. The "
                "document claims to be A's and lands on B's history, which is the same "
                "shape as update_config on the admin store above."
            ),
        ),
        "list_versions": ArgumentRecipe(
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "The audit trail. Every row holds a whole icp_config, so this is the "
                "history equivalent of list_tenants and it is emphatically not fleet-wide."
            ),
        ),
        "get_version": ArgumentRecipe(
            arguments={"version": 1},
            expected=CrossTenant.RAISES,
            note=(
                "Version numbers restart at 1 per tenant, so 'version 1' names a different "
                "row for every customer. B asking for it must get its own or nothing — "
                "UnknownConfigVersionError here, since only A has history in the fixture."
            ),
        ),
    },
    PostgresBillingStore: {
        "billing_tenant": ArgumentRecipe(
            expected=CrossTenant.RETURNS_OWN,
            note=(
                "B reads its own billing state: which Stripe customer it is, which "
                "subscription, and whether it is inside a dunning grace period. All three "
                "are commercial facts about an account, and the last one says out loud "
                "that a customer is behind on payment."
            ),
        ),
        "link_customer": ArgumentRecipe(
            arguments={"stripe_customer_id": STRIPE_CUSTOMER_B},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "The write that decides who gets invoiced for whose leads. The fixture "
                "gives A a customer id of its own, so the snapshot is comparing a real "
                "value before and after rather than two NULLs — an unfiltered UPDATE here "
                "would repoint A's billing at B's Stripe account."
            ),
        ),
        "set_subscription": ArgumentRecipe(
            arguments={"stripe_subscription_id": STRIPE_SUBSCRIPTION_B},
            expected=CrossTenant.WRITES_NOTHING,
            note="Same shape, same column family, and A's subscription must survive it.",
        ),
        "set_status": ArgumentRecipe(
            arguments={"status": TenantStatus.SUSPENDED},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "The only write in billing that stops new leads. Unfiltered it would let a "
                "cancelled subscription on one account suspend another customer's ingest, "
                "which is the 403 in test_billing_suspension.py pointed at the wrong "
                "tenant."
            ),
        ),
        "set_dunning_until": ArgumentRecipe(
            arguments={"until": NOW},
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "Starting a grace period against the wrong tenant is a suspension seven "
                "days later against the wrong tenant, by a sweep that will look entirely "
                "correct when it runs."
            ),
        ),
        "usage_reported": ArgumentRecipe(
            arguments={"usage_date": DAY},
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "A real cross-tenant read, because the fixture gives A a usage_reports row "
                "for exactly this day. B must be told 'not reported' — an unfiltered read "
                "would answer 'already reported' off A's ledger, and the caller's response "
                "to that is to skip the day, so B would simply never be billed for it."
            ),
        ),
        "record_usage_report": ArgumentRecipe(
            arguments={
                "usage_date": DAY,
                "quantity": 4242,
                "external_id": usage_external_id(tenant_id=TENANT_B, usage_date=DAY),
                "reported_at": NOW,
            },
            expected=CrossTenant.WRITES_NOTHING,
            note=(
                "The row that says a customer has been billed for a day. B writing the "
                "same day A already has must land on B's own row and leave A's quantity "
                "alone; the primary key is (tenant_id, usage_date), so a dropped tenant "
                "would make the two collide and one of them silently not be recorded — "
                "which, because the caller skips a day it believes is recorded, is a day "
                "nobody is charged for. The identifier is derived from B and the day, as "
                "BillingService derives it."
            ),
        ),
    },
    PostgresGoldenPromotionStore: {
        "record": ArgumentRecipe(
            arguments={
                "lead_id": LEAD_A,
                "case_id": "real_probe_00000001",
                "expected_tier": Tier.WARM,
                "promoted_by": "probe",
                "note": "promoted across a tenant boundary by a cross-tenant probe",
                "promoted_at": NOW,
            },
            expected=CrossTenant.RAISES,
            results=(CannedResult(),),
            note=(
                "The composite (tenant_id, lead_id) foreign key refuses it, exactly as it "
                "refuses a cross-tenant assessment. The canned empty result is what makes "
                "the sweep see the second statement: ON CONFLICT DO NOTHING returns no row "
                "when it collides, and the method then reads the existing promotion back."
            ),
        ),
        "list_promotions": ArgumentRecipe(
            expected=CrossTenant.RETURNS_EMPTY,
            note="Which of this tenant's leads are in the eval set. Never another's.",
        ),
        "promoted_lead_ids": ArgumentRecipe(
            arguments={"lead_ids": [LEAD_A]},
            expected=CrossTenant.RETURNS_EMPTY,
            note=(
                "Asked once per review page for a page of lead ids. Handed A's id under "
                "B's name it must answer 'not promoted' rather than confirming that a lead "
                "B cannot see exists at all."
            ),
        ),
    },
}


def tenant_parameter_of(parameters: Mapping[str, Any]) -> str | None:
    """The name of the tenant-scoping parameter in a signature, or ``None``."""
    for candidate in TENANT_PARAMETERS:
        if candidate in parameters:
            return candidate
    return None


def recipe_for(cls: type, method: str) -> ArgumentRecipe:
    """The recipe for one method, or a failure that tells the author to write one."""
    recipes = RECIPES.get(cls, {})
    if method not in recipes:
        raise AssertionError(
            f"{cls.__name__}.{method} takes a tenant and has no isolation recipe.\n"
            f"Add one to RECIPES[{cls.__name__}] in tests/isolation/repositories.py: the "
            "arguments to call it with, and what it must do when it is handed another "
            "tenant's row. This failure is the point of the sweep — a method that is not "
            "in the table is a method nothing is checking."
        )
    return recipes[method]
