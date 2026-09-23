"""The repository sweep again, against a real Postgres, driven by the same recipes.

``test_repository_isolation.py`` proves that every statement carries a tenant predicate and
binds the tenant it was given. That is what can be proved without a server, and it is where
the weight of this suite sits, because this module **skips when Docker is not running** and
a property asserted only here is a property that is not asserted.

What only a database can say is the other half, and it is worth having:

* a predicate can be present and *wrong* — comparing a column to itself, or to a constant —
  and the compiled SQL looks fine either way;
* the structural guarantee is not a predicate at all. The composite
  ``(tenant_id, lead_id) → leads (tenant_id, id)`` foreign keys make a cross-tenant child
  row **unrepresentable**, and the only way to see that is to try to write one and be
  refused by the server;
* "and nothing of tenant A's changed" is a statement about rows, which needs rows.

Every recipe gets the same two assertions: the outcome its
:class:`~tests.isolation.repositories.CrossTenant` value names, and a byte-for-byte
comparison of every row tenant A owns, taken before the call and after it. The second one is
the universal post-condition and it is why the recipes do not each have to spell out "and
nothing of A's was touched".

Run it with ``docker compose up -d`` and ``DATABASE_URL`` exported; see
``docs/local-database.md``.
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

import pytest
from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.db_schema import (
    Assessment,
    Feedback,
    Lead,
    RoutingEvent,
    Tenant,
    TenantApiKey,
    UsageDaily,
    UsageReportRecord,
)
from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.store_tenants import PostgresIngestCredentials
from leadquali.api.signing import ACTIVE_STATUS, AuthFailure, CredentialRejected, IngestCredential
from leadquali.app.billing import usage_external_id
from leadquali.app.feedback import Verdict
from leadquali.domain.models import Action, Tier
from leadquali.domain.tenant_config import TenantConfig
from tests.isolation.repositories import (
    DAY,
    KEY_ID_A,
    KEY_ID_B,
    KEY_SECRET_A,
    KEY_SECRET_B,
    LEAD_A,
    SIGNING_SECRET_REF,
    STRIPE_CUSTOMER_A,
    STRIPE_SUBSCRIPTION_A,
    SUBMISSION_A,
    TENANT_A,
    TENANT_A_UUID,
    TENANT_B,
    TENANT_B_UUID,
    ArgumentRecipe,
    CrossTenant,
    DictSecretResolver,
    Repository,
    api_key_for,
    recipe_for,
    tenant_parameter_of,
)
from tests.isolation.test_repository_isolation import SWEEP, _sweep_id

pytestmark = pytest.mark.integration

#: Tenant A's figures, chosen so that a total which has absorbed the fleet's rows cannot
#: coincidentally equal the right answer.
QUOTA_A: Final[int] = 4_242
TOKENS_A: Final[int] = 9_871
COST_A: Final[Decimal] = Decimal("0.098710")

NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)

#: One argon2 hasher for the module. The KDF is ~19 MiB and several milliseconds a call, and
#: nothing here is measuring that.
VERIFIER: Final[Argon2KeyHasher] = Argon2KeyHasher()

#: Every table that carries tenant-owned rows, with the column that says whose they are.
#: ``tenants`` is its own tenant, keyed by ``id``.
OWNED_TABLES: Final[tuple[tuple[Any, Any], ...]] = (
    (Tenant, Tenant.id),
    (TenantApiKey, TenantApiKey.tenant_id),
    (Lead, Lead.tenant_id),
    (Assessment, Assessment.tenant_id),
    (RoutingEvent, RoutingEvent.tenant_id),
    (Feedback, Feedback.tenant_id),
    (UsageDaily, UsageDaily.tenant_id),
    # #35's usage ledger: the row that says a customer has been billed for a day.
    #
    # ``stripe_events`` is deliberately **not** here. Its tenant_id is nullable by
    # design — a webhook is stored before we know whose it is — so a snapshot keyed on
    # a tenant would watch the attributed rows and miss the unattributed ones, which
    # are the majority and the interesting ones. The two statements that could write
    # across that table are addressed by Stripe's own primary key and are covered by
    # name in ``tests/isolation/test_billing_isolation.py``.
    (UsageReportRecord, UsageReportRecord.tenant_id),
)

#: Strings that, appearing anywhere in a result handed to tenant B, mean tenant A's data
#: crossed the boundary.
A_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        TENANT_A,
        str(TENANT_A_UUID),
        LEAD_A,
        SUBMISSION_A,
        KEY_ID_A,
        str(QUOTA_A),
        str(TOKENS_A),
        # #35: a Stripe customer id names the account that gets invoiced, so one of
        # A's appearing in an answer handed to B is a billing identifier crossing the
        # boundary — the same class of leak as a lead id.
        STRIPE_CUSTOMER_A,
        STRIPE_SUBSCRIPTION_A,
    }
)


def _config_document(slug: str, *, destination: str) -> dict[str, Any]:
    """A valid tenant configuration, so ``PostgresTenantConfigSource.get`` can parse it back."""
    document: dict[str, Any] = TenantConfig.model_validate(
        {
            "tenant_id": slug,
            "name": slug.title(),
            "icp_description": f"The ideal customer of {slug}.",
            "routing_rules": {
                tier.value: {"action": Action.EMAIL_SALES.value, "destination": destination}
                for tier in Tier
            },
        }
    ).model_dump(mode="json")
    return document


@pytest.fixture
def seeded(isolation_db: Connection) -> Connection:
    """Two tenants, each with a key, and a full set of rows for tenant A.

    Both tenants exist, because the interesting cross-tenant call is one made *by a real
    customer* rather than by a name nobody has. Only A has leads, assessments, routing
    events, feedback and a rollup: that asymmetry is what makes "B read A's figures" visible
    as a non-zero answer instead of as two matching zeroes.
    """
    connection = isolation_db
    # A carries #35's Stripe identifiers and B does not. The asymmetry is the point, as
    # it is everywhere else in this fixture: a snapshot of two NULL columns before and
    # after a cross-tenant write proves nothing, while a snapshot of A's real customer id
    # proves that B's link_customer did not repoint A's billing at B's Stripe account.
    for slug, row_id, key_id, secret, quota, customer, subscription in (
        (
            TENANT_A,
            TENANT_A_UUID,
            KEY_ID_A,
            KEY_SECRET_A,
            QUOTA_A,
            STRIPE_CUSTOMER_A,
            STRIPE_SUBSCRIPTION_A,
        ),
        (TENANT_B, TENANT_B_UUID, KEY_ID_B, KEY_SECRET_B, 100, None, None),
    ):
        connection.execute(
            insert(Tenant).values(
                id=row_id,
                slug=slug,
                name=slug.title(),
                status=ACTIVE_STATUS,
                icp_config=_config_document(slug, destination=f"sales@{slug}.example.test"),
                hmac_secret_ref=f"{SIGNING_SECRET_REF}-{slug}",
                monthly_lead_quota=quota,
                quota_alert_fraction=Decimal("0.80"),
                stripe_customer_id=customer,
                stripe_subscription_id=subscription,
            )
        )
        connection.execute(
            insert(TenantApiKey).values(
                tenant_id=row_id,
                key_id=key_id,
                key_prefix=f"lq_live_{key_id}",
                key_hash=VERIFIER.hash_secret(secret),
                label=f"{slug} primary",
            )
        )

    connection.execute(
        insert(Lead).values(
            id=uuid.UUID(LEAD_A),
            tenant_id=TENANT_A_UUID,
            submission_id=SUBMISSION_A,
            raw_payload={"email": "ada@alpha.example.test"},
            source="web_form",
            status="routed",
            contact_email_hash="a" * 64,
            received_at=NOW,
            created_at=NOW,
        )
    )
    connection.execute(
        insert(Assessment).values(
            tenant_id=TENANT_A_UUID,
            lead_id=uuid.UUID(LEAD_A),
            created_at=NOW,
            status="ok",
            tier=Tier.WARM.value,
            total_score=Decimal("61.00"),
            dimension_scores={"icp_fit": 20},
            extracted={},
            reasoning="seeded",
            confidence=Decimal("0.900"),
            missing_information=[],
            model_id="claude-opus-5",
            prompt_version="rubric_v1",
            effort="medium",
            input_tokens=TOKENS_A,
            output_tokens=101,
            cost_usd=COST_A,
            latency_ms=2000,
        )
    )
    connection.execute(
        insert(RoutingEvent).values(
            tenant_id=TENANT_A_UUID,
            lead_id=uuid.UUID(LEAD_A),
            action=Action.EMAIL_SALES.value,
            destination="sales@alpha.example.test",
            dispatched_at=NOW,
            provider_message_id="seeded-message",
            created_at=NOW,
        )
    )
    connection.execute(
        insert(Feedback).values(
            tenant_id=TENANT_A_UUID,
            lead_id=uuid.UUID(LEAD_A),
            rater="dest:" + "f" * 32,
            verdict=Verdict.GOOD.value,
            notes="seeded",
            created_at=NOW,
        )
    )
    connection.execute(
        insert(UsageDaily).values(
            tenant_id=TENANT_A_UUID,
            usage_date=DAY,
            leads_ingested=1,
            leads_assessed=1,
            leads_billable=1,
            assessments_failed=0,
            input_tokens=TOKENS_A,
            output_tokens=101,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=COST_A,
            computed_at=NOW,
        )
    )
    connection.execute(
        insert(UsageReportRecord).values(
            tenant_id=TENANT_A_UUID,
            usage_date=DAY,
            reported_at=NOW,
            external_id=usage_external_id(tenant_id=TENANT_A, usage_date=DAY),
            quantity=1,
        )
    )
    return connection


def snapshot(connection: Connection, owner: uuid.UUID) -> Mapping[str, list[str]]:
    """Every row one tenant owns, rendered as sorted strings.

    Strings rather than tuples so that a ``Decimal``, a ``datetime`` and a ``jsonb`` all
    compare by value without anybody having to think about it, and sorted in Python rather
    than in SQL because ordering a ``jsonb`` column server-side is a detail this has no
    reason to depend on.
    """
    rows: dict[str, list[str]] = {}
    for table, column in OWNED_TABLES:
        result = connection.execute(select(table.__table__).where(column == owner))
        rows[str(table.__tablename__)] = sorted(repr(tuple(row)) for row in result)
    return rows


def call_as_b(repository: Repository, method: str, recipe: ArgumentRecipe, factory: Any) -> Any:
    """Invoke one method as tenant B, with tenant A's row ids in its arguments."""
    store = repository.build(factory)
    bound = getattr(store, method)
    parameter = tenant_parameter_of(inspect.signature(bound).parameters)
    assert parameter is not None, f"{repository.name}.{method} lost its tenant parameter"
    arguments = dict(recipe.arguments)
    arguments.setdefault(parameter, TENANT_B)
    return bound(**arguments)


def assert_outcome(expected: CrossTenant, result: Any, *, where: str) -> None:
    """The per-recipe assertion. ``RAISES`` is handled by the caller."""
    rendered = repr(result)
    leaked = sorted(token for token in A_IDENTIFIERS if token in rendered)
    assert not leaked, f"{where}: the result handed to tenant B carries {leaked}\n{rendered}"

    if expected is CrossTenant.RETURNS_NONE:
        assert result is None, f"{where}: expected None, got {rendered}"
    elif expected is CrossTenant.RETURNS_EMPTY:
        assert not result, f"{where}: expected an empty answer, got {rendered}"
    elif expected is CrossTenant.RETURNS_OWN:
        assert result, f"{where}: expected this tenant's own answer, got {rendered}"
    elif expected is CrossTenant.REFUSED:
        assert isinstance(result, CredentialRejected), f"{where}: expected a refusal, {rendered}"
        assert result.failure is AuthFailure.UNKNOWN_TENANT, result


@pytest.mark.parametrize("case", SWEEP, ids=_sweep_id)
def test_a_cross_tenant_call_never_reads_or_writes_the_other_tenants_rows(
    case: tuple[Repository, str],
    seeded: Connection,
    sessions: sessionmaker[Session],
) -> None:
    """Every recipe, called as tenant B against tenant A's row ids, on a real server.

    The snapshot is taken before and after every call including the ones that raise, because
    a statement that raised on its *second* statement may already have written its first.
    """
    repository, method = case
    recipe = recipe_for(repository.cls, method)
    before = snapshot(seeded, TENANT_A_UUID)
    where = f"{repository.name}.{method}"

    if recipe.expected is CrossTenant.RAISES:
        with pytest.raises(Exception) as raised:
            call_as_b(repository, method, recipe, sessions)
        assert not isinstance(raised.value, AssertionError), raised.value
    else:
        assert_outcome(
            recipe.expected,
            call_as_b(repository, method, recipe, sessions),
            where=where,
        )

    assert snapshot(seeded, TENANT_A_UUID) == before, (
        f"{where} changed rows belonging to tenant {TENANT_A}"
    )


def test_the_fixture_actually_seeded_something(seeded: Connection) -> None:
    """A snapshot comparison between two empty sets is not evidence of anything.

    So the fixture is checked: tenant A owns a row in every table the sweep watches, and
    tenant B owns only the two a customer starts with.
    """
    owned = snapshot(seeded, TENANT_A_UUID)
    assert all(rows for rows in owned.values()), owned
    assert len(owned) == len(OWNED_TABLES)

    other = snapshot(seeded, TENANT_B_UUID)
    assert other["tenants"] and other["tenant_api_keys"]
    assert not other["leads"] and not other["usage_daily"]


def test_the_composite_foreign_key_refuses_a_cross_tenant_child_row(
    seeded: Connection, sessions: sessionmaker[Session]
) -> None:
    """The structural half, stated on its own because it is the strongest thing here.

    Every child table's ``(tenant_id, lead_id)`` foreign key points at
    ``leads (tenant_id, id)``, so a row claiming tenant B and tenant A's lead is not merely
    filtered out of a later read — the server will not store it. That is a guarantee no
    amount of application code can undo, which is the argument
    ``docs/tenant-isolation.md`` makes for not adding row-level security on top of it.
    """
    for table in (Assessment, RoutingEvent, Feedback):
        with pytest.raises(IntegrityError), sessions.begin() as session:
            session.execute(
                insert(table).values(
                    tenant_id=TENANT_B_UUID,
                    lead_id=uuid.UUID(LEAD_A),
                    created_at=NOW,
                    **_minimum_columns(table),
                )
            )


def _minimum_columns(table: Any) -> Mapping[str, Any]:
    """The NOT NULL columns each child table needs beyond tenant, lead and timestamp."""
    if table is Assessment:
        return {
            "status": "ok",
            "model_id": "claude-opus-5",
            "prompt_version": "rubric_v1",
            "tier": Tier.WARM.value,
            "total_score": Decimal("50.00"),
            "dimension_scores": {},
            "extracted": {},
            "reasoning": "probe",
            "confidence": Decimal("0.900"),
        }
    if table is RoutingEvent:
        return {"action": Action.EMAIL_SALES.value, "destination": "intruder@example.test"}
    return {"rater": "dest:" + "e" * 32, "verdict": Verdict.BAD.value}


def test_a_real_key_is_refused_under_another_tenants_name_against_the_database(
    seeded: Connection, sessions: sessionmaker[Session]
) -> None:
    """The credential source's Python-side tenant check, on real rows.

    Its ``SELECT`` is keyed on ``key_id`` alone — that is the documented exception in
    ``docs/tenant-isolation.md`` — so the offline sweep asserts the behaviour against a
    canned row. This is the same assertion against the real join, and it also proves the
    seeded hashes verify, which is the positive control the rejection needs.
    """
    source = PostgresIngestCredentials(
        sessions,
        verifier=VERIFIER,
        resolver=DictSecretResolver(
            {f"{SIGNING_SECRET_REF}-{slug}": "s" * 40 for slug in (TENANT_A, TENANT_B)}
        ),
        now=lambda: NOW,
        last_used_coarseness=None,
    )

    accepted = source.resolve(tenant_id=TENANT_A, api_key=api_key_for(KEY_ID_A, KEY_SECRET_A))
    assert isinstance(accepted, IngestCredential)
    assert accepted.tenant_id == TENANT_A

    refused = source.resolve(tenant_id=TENANT_B, api_key=api_key_for(KEY_ID_A, KEY_SECRET_A))
    assert isinstance(refused, CredentialRejected)
    assert refused.failure is AuthFailure.UNKNOWN_TENANT


def test_every_recipe_in_the_sweep_ran(seeded: Connection) -> None:
    """A guard against the parameterisation collapsing to nothing.

    ``SWEEP`` is built by introspection at import time. If that ever returned an empty
    tuple the module above would report a full pass with no tests in it.
    """
    assert len(SWEEP) >= 15, [_sweep_id(case) for case in SWEEP]
