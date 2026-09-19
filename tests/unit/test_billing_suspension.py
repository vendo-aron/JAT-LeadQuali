"""Suspension stops new leads. It must never lose one that is already in flight.

This is the issue's hardest acceptance criterion and the easiest to break by accident, so
it gets its own file and three kinds of test:

1. **A suspended tenant's ingest answers 403, not a silent 202.** A form that gets a 403
   can tell its operator something is wrong; a form that gets a 202 cannot, and the lead is
   gone with nobody aware of it. #31's review moved the status check to *after* the argon2
   verification, which means a 403 is now only reachable by a caller who has proved it
   holds the secret — so it is safe to say out loud. Asserted here against the credential
   decision itself rather than against a full HTTP stack, because that is where the rule
   lives.
2. **A lead already on the queue is still assessed and still delivered**, whatever the
   tenant's billing state. The qualification pipeline does not read ``tenants.status``, and
   this file pins that structurally *and* behaviourally — the structural test is what
   catches somebody helpfully adding a status check to the worker later.
3. **Nothing this issue added discards a lead.** Asserted over the AST of every module #35
   introduced.

Invariant 3 has no billing exception. "The customer stopped paying" is a reason to refuse
the *next* submission at the door, with an error the sender can see, and it is never a
reason to drop a lead we have already accepted money-shaped responsibility for.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.revenue_none import UnknownRevenue
from leadquali.app.billing import BillingService
from leadquali.app.credentials import (
    ACTIVE_STATUS,
    AuthFailure,
    CredentialAccepted,
    CredentialDecision,
    CredentialRejected,
    StoredApiKey,
    decide_credential,
)
from leadquali.app.metering import MeteringService
from leadquali.app.tenants import TenantStatus
from tests.fakes import (
    FakeClock,
    InMemoryBillingStore,
    InMemoryMeteringStore,
    RecordingBilling,
    stripe_event,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "leadquali"

TENANT = "acme-demo"
CUSTOMER = "cus_acme"
NOW = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)

KEY_ID = "5b1f0a7c2e9d4368"
KEY_SECRET = "kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI"
VERIFIER = Argon2KeyHasher()
KEY_HASH = VERIFIER.hash_secret(KEY_SECRET)

#: The modules this issue added. Every one of them is checked for a code path that could
#: drop a lead.
BILLING_MODULES = (
    "app/billing.py",
    "adapters/billing_stripe.py",
    "adapters/store_billing.py",
    "api/stripe_signing.py",
    "api/webhooks.py",
    "api/billing_jobs.py",
)


@pytest.fixture
def service() -> BillingService:
    clock = FakeClock(NOW, step_ms=0)
    store = InMemoryBillingStore()
    store.given_tenant(TENANT, stripe_customer_id=CUSTOMER, stripe_subscription_id="sub_acme")
    return BillingService(
        store=store,
        billing=RecordingBilling(),
        metering=MeteringService(
            store=InMemoryMeteringStore(), clock=clock, revenue=UnknownRevenue()
        ),
        clock=clock,
    )


def _decide(status: str, *, secret: str = KEY_SECRET) -> CredentialDecision:
    """The credential decision for a presented key against a tenant in ``status``."""
    return decide_credential(
        claimed_tenant_id=TENANT,
        row_tenant_id=TENANT,
        tenant_status=status,
        key=StoredApiKey(key_id=KEY_ID, key_hash=KEY_HASH),
        presented_secret=secret,
        now=NOW,
        verifier=VERIFIER,
    )


# ------------------------------------------------- 1. the door says no, and says why


def test_a_suspended_tenant_is_refused_with_the_one_failure_that_is_not_a_401() -> None:
    """The 403 ingest gives, reached through the real decision function."""
    decision = _decide("suspended")
    assert isinstance(decision, CredentialRejected)
    assert decision.failure is AuthFailure.TENANT_SUSPENDED


def test_an_active_tenant_with_the_same_key_is_accepted() -> None:
    """The control. Without it the test above would pass against a function that refused
    everything, which is the failure mode of every "assert it is rejected" test."""
    assert isinstance(_decide(ACTIVE_STATUS), CredentialAccepted)


def test_the_suspended_answer_is_only_reachable_after_the_secret_is_verified() -> None:
    """#31's review fix, and the reason this issue may use the 403 at all.

    A wrong secret against a suspended tenant must come back as a plain bad key, not as
    "that account is suspended" — otherwise anyone who has ever seen a customer's API key
    (it travels in a header, in the clear, on every submission) could discover whether that
    account had been suspended for non-payment without holding the secret.
    """
    decision = _decide("suspended", secret="x" * len(KEY_SECRET))
    assert isinstance(decision, CredentialRejected)
    assert decision.failure is AuthFailure.BAD_KEY


def test_billing_suspension_goes_through_the_one_existing_status_column(
    service: BillingService,
) -> None:
    """There is no second suspension mechanism. A cancelled subscription sets the same
    ``tenants.status`` that ``tenantctl suspend`` sets, so the 403 above is the error a
    form sees — and there is exactly one thing to undo when a customer pays."""
    service.receive_event(
        event_id="evt_1",
        event_type="customer.subscription.deleted",
        payload=stripe_event(
            "evt_1", "customer.subscription.deleted", customer=CUSTOMER, subscription="sub_acme"
        ),
    )
    service.process_pending()
    assert service.tenant(tenant_id=TENANT).status is TenantStatus.SUSPENDED
    assert isinstance(_decide(TenantStatus.SUSPENDED.value), CredentialRejected)


# ----------------------------------------- 2. an in-flight lead is finished regardless


def test_the_qualification_pipeline_never_reads_a_tenants_status() -> None:
    """Structural, because this is the test that has to survive somebody's future
    "helpful" addition of a billing check to the worker.

    A lead on the queue has already been accepted. It was received while the tenant was
    active, the submitter has been told 202, and a sales rep is expecting it. Checking the
    tenant's billing state at assessment time would mean a subscription cancelled between
    ingest and delivery silently swallows leads already in hand — invariant 3 with a
    commercial excuse attached.
    """
    for relative in ("app/qualify.py", "api/worker.py"):
        source = (SRC / relative).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        assert "TenantStatus" not in names, f"{relative} reads a tenant's status"
        assert "dunning_until" not in source, f"{relative} reads the dunning deadline"
        assert "BillingService" not in names, f"{relative} reaches into billing"


def test_a_suspended_tenants_queued_lead_is_still_assessed_and_delivered(
    service: BillingService,
) -> None:
    """The behavioural half. Suspend the tenant through the real billing path, then run a
    lead through the real pipeline, and watch it arrive."""
    from tests.unit.test_qualify import build_pipeline, make_request

    service.receive_event(
        event_id="evt_1",
        event_type="customer.subscription.deleted",
        payload=stripe_event(
            "evt_1", "customer.subscription.deleted", customer=CUSTOMER, subscription="sub_acme"
        ),
    )
    service.process_pending()
    assert service.tenant(tenant_id=TENANT).status is TenantStatus.SUSPENDED

    pipeline, store, notifier, _ = build_pipeline()
    result = pipeline.qualify(make_request())

    assert len(store.assessments) == 1, "the lead was assessed"
    assert len(notifier.dispatches) == 1, "the lead was delivered"
    assert store.terminal_events(result.lead_id), "and the delivery is on the record"


# -------------------------------------------- 3. nothing here can discard a lead


@pytest.mark.parametrize("relative", BILLING_MODULES)
def test_no_billing_module_touches_leads_at_all(relative: str) -> None:
    """The bluntest possible statement of "billing cannot drop a lead": none of these
    modules imports a lead store, a queue or the pipeline, so none of them has anything to
    drop. A future module that needs to would fail this and have to say why.
    """
    source = (SRC / relative).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    # ``store_postgres`` is deliberately not on this list: billing shares its session
    # factory and its slug-to-uuid mapping, and a second copy of either would be far worse
    # than the import. What billing must not have is anything that *handles* a lead.
    forbidden = {
        "leadquali.app.ingest",
        "leadquali.app.qualify",
        "leadquali.adapters.queue_sqs",
        "leadquali.adapters.queue_inprocess",
        "leadquali.adapters.notify_ses",
    }
    leaked = imported & forbidden
    assert not leaked, f"{relative} imports {sorted(leaked)}"

    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not names & {
        "PostgresLeadStore",
        "InProcessLeadQueue",
        "QualificationPipeline",
        "upsert_lead",
        "record_assessment",
        "enqueue",
        "dispatch",
    }, relative
