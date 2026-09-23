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
from leadquali.adapters.store_admin import (
    PostgresAdminQueryStore,
    PostgresConfigVersionStore,
    PostgresGoldenPromotionStore,
)
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
from leadquali.app.feedback import Verdict
from leadquali.app.metering import BillingPeriod
from leadquali.app.ports import RoutingOutcome
from leadquali.app.tenants import TenantStatus
from leadquali.domain.models import Action, EscalationReason, Tier
from leadquali.domain.routing import system_failure
from leadquali.prompts.lead import LeadSubmission
from tests.sqlcapture import CannedResult

__all__ = [
    "ALLOWLIST",
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
    Repository(PostgresIngestCredentials, _ingest_credentials),
    # #36's staff console. Three classes, and the first of them is the read surface over
    # every tenant's leads, assessments and feedback — the one the completeness test was
    # written after finding it unswept on this branch.
    Repository(PostgresAdminQueryStore, PostgresAdminQueryStore),
    Repository(PostgresConfigVersionStore, PostgresConfigVersionStore),
    Repository(PostgresGoldenPromotionStore, PostgresGoldenPromotionStore),
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
}


#: The documented fleet-wide exceptions (#33). Reconciliation against Anthropic's invoice
#: and the infrastructure-cost allocation both need figures across every tenant, and a sum
#: with no tenant attached to it is exactly what the scoping rule exists to prevent — so
#: they return the *breakdown*, and they carry ``fleet_`` in their names to say so. The
#: sweep checks that the name and the absence of a tenant parameter agree;
#: ``test_metering_isolation.py`` checks that no fleet result reaches one tenant's report.
FLEET_METHODS: Final[Mapping[type, frozenset[str]]] = {
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
