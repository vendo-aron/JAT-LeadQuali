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
from typing import Any, Final

from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.metering_postgres import PostgresMeteringStore
from leadquali.adapters.store_postgres import (
    PostgresFeedbackStore,
    PostgresLeadStore,
    PostgresTenantConfigSource,
    tenant_uuid,
)
from leadquali.adapters.store_tenants import PostgresIngestCredentials, PostgresTenantAdminStore
from leadquali.app.api_keys import ApiKeyParts, KeyEnvironment
from leadquali.app.assessment_result import AssessmentFailed
from leadquali.app.feedback import Verdict
from leadquali.app.metering import BillingPeriod
from leadquali.app.ports import RoutingOutcome
from leadquali.app.tenants import TenantStatus
from leadquali.domain.models import Action, EscalationReason
from leadquali.domain.routing import system_failure
from leadquali.prompts.lead import LeadSubmission
from tests.sqlcapture import CannedResult

__all__ = [
    "ALLOWLIST",
    "DAY",
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

#: The parameter names that mean "this call is about one tenant". Two of them because the
#: control plane addresses a tenant by its slug and the data plane by its port-level id;
#: both become a tenant predicate in the statement that comes out the other end.
TENANT_PARAMETERS: Final[tuple[str, ...]] = ("tenant_id", "slug")


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

    ``last_used_coarseness=None`` switches off the ``last_used_at`` write, which is a second
    statement issued *after* the credential decision and is no part of it. Leaving it on
    would put an untenanted ``UPDATE`` into the sweep's captured statements while saying
    nothing about isolation; it gets a test of its own instead.
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
)


#: Public methods that legitimately take no tenant, each with the reason it is allowed to.
#: Short by construction: anything that is not a constructor or a deliberate fleet-wide
#: operator tool does not belong here, it belongs in :data:`RECIPES`.
ALLOWLIST: Final[Mapping[type, Mapping[str, str]]] = {
    PostgresLeadStore: {
        "from_url": "constructor: takes a database URL and issues no statement",
        "from_env": "constructor: reads DATABASE_URL and issues no statement",
    },
    PostgresFeedbackStore: {
        "from_url": "constructor",
        "from_env": "constructor",
    },
    PostgresTenantConfigSource: {
        "from_url": "constructor",
        "from_env": "constructor",
    },
    PostgresTenantAdminStore: {
        "from_url": "constructor",
        "from_env": "constructor",
        "list_tenants": (
            "the control plane's own enumeration. It answers 'who are our customers?' for "
            "an operator running tenantctl, it is on no request path, and a tenant filter "
            "would make it meaningless. It returns whole rows, so it is the one method "
            "here that must never be reachable from an authenticated tenant context."
        ),
    },
    PostgresMeteringStore: {
        "from_url": "constructor",
        "from_env": "constructor",
    },
    PostgresIngestCredentials: {},
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
                "which is compared in Python. The sweep therefore asserts the behaviour: a "
                "real key presented under another tenant's name is refused, and refused as "
                "UNKNOWN_TENANT so it is indistinguishable from a key that does not exist. "
                "Recorded as an exception in docs/tenant-isolation.md."
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
