"""``POST /webhooks/stripe`` and ``POST /billing/portal``, exercised as a stranger would.

The webhook endpoint is public, unauthenticated until an HMAC says otherwise, and it
changes billing state. Everything asserted here runs with no database, no Docker and no
Stripe key, because the properties are the ones that must never regress quietly: an
unsigned request is refused, a forged one is refused, a replay is a no-op, and a request
that verifies is stored and answered 200 before any work happens.

The request bodies are the JSON fixtures in ``tests/fixtures/stripe`` — hand-built, see the
README there — signed here with a known secret.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.revenue_none import UnknownRevenue
from leadquali.api.signing import (
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    StaticCredentials,
    StaticTenantCredentials,
    StoredApiKey,
    sign,
)
from leadquali.api.stripe_signing import STRIPE_SIGNATURE_HEADER, signature_header
from leadquali.api.webhooks import (
    PORTAL_PATH,
    WEBHOOK_PATH,
    BillingDeps,
    register_billing_routes,
)
from leadquali.app.api_keys import ApiKeyParts, KeyEnvironment
from leadquali.app.billing import BillingService, EventStatus
from leadquali.app.metering import MeteringService
from leadquali.app.tenants import TenantStatus
from tests.fakes import FakeClock, InMemoryBillingStore, InMemoryMeteringStore, RecordingBilling

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stripe"

TENANT = "acme-demo"
CUSTOMER = "cus_acme"
WHSEC = "whsec_" + "t" * 40
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

KEY_ID = "5b1f0a7c2e9d4368"
KEY_SECRET = "kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI"
API_KEY = ApiKeyParts(environment=KeyEnvironment.LIVE, key_id=KEY_ID, secret=KEY_SECRET).text
SIGNING_SECRET = "local-signing-secret-of-adequate-length"
VERIFIER = Argon2KeyHasher()
KEY_HASH = VERIFIER.hash_secret(KEY_SECRET)
PORTAL_RETURN = "https://acme.example/billing"


def fixture(name: str) -> bytes:
    """One recorded-shaped Stripe event, as raw bytes — what the signature covers."""
    return (FIXTURES / f"{name}.json").read_bytes()


def tenant_credentials(*, status: str = "active") -> StaticCredentials:
    return StaticCredentials(
        {
            TENANT: StaticTenantCredentials(
                tenant_id=TENANT,
                signing_secret=SIGNING_SECRET.encode("utf-8"),
                keys=(StoredApiKey(key_id=KEY_ID, key_hash=KEY_HASH),),
                status=status,
            )
        },
        verifier=VERIFIER,
        now=lambda: NOW,
    )


class Harness:
    """An app carrying only the billing routes, wired to in-memory doubles."""

    def __init__(self, *, tenant_status: str = "active") -> None:
        self.clock = FakeClock(NOW, step_ms=0)
        self.store = InMemoryBillingStore()
        self.store.given_tenant(
            TENANT,
            status=TenantStatus(tenant_status),
            stripe_customer_id=CUSTOMER,
            stripe_subscription_id="sub_acme",
        )
        self.billing = RecordingBilling()
        self.metering_store = InMemoryMeteringStore()
        self.service = BillingService(
            store=self.store,
            billing=self.billing,
            metering=MeteringService(
                store=self.metering_store, clock=self.clock, revenue=UnknownRevenue()
            ),
            clock=self.clock,
        )
        self.deps = BillingDeps(
            service=self.service,
            webhook_secret=WHSEC,
            clock=self.clock,
            credentials=tenant_credentials(status=tenant_status),
            portal_return_url=PORTAL_RETURN,
        )
        app = FastAPI()
        register_billing_routes(app, self.deps)
        self.client = TestClient(app)

    def post_webhook(
        self,
        body: bytes,
        *,
        secret: str = WHSEC,
        timestamp: datetime | None = None,
        header: str | None = "sign",
    ) -> Any:
        headers: dict[str, str] = {"content-type": "application/json"}
        if header == "sign":
            headers[STRIPE_SIGNATURE_HEADER] = signature_header(
                secret=secret,
                body=body,
                timestamp=int((timestamp or NOW).timestamp()),
            )
        elif header is not None:
            headers[STRIPE_SIGNATURE_HEADER] = header
        return self.client.post(WEBHOOK_PATH, content=body, headers=headers)

    def post_portal(self, *, tenant: str = TENANT, body: bytes = b"{}") -> Any:
        timestamp = str(int(NOW.timestamp()))
        nonce = "portal-nonce-0001"
        headers = {
            HEADER_TENANT: tenant,
            HEADER_KEY: API_KEY,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(
                secret=SIGNING_SECRET.encode("utf-8"),
                method="POST",
                path=PORTAL_PATH,
                tenant_id=tenant,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
            ),
            "content-type": "application/json",
        }
        return self.client.post(PORTAL_PATH, content=body, headers=headers)


@pytest.fixture
def harness() -> Iterator[Harness]:
    yield Harness()


# ------------------------------------------------------------------- the happy path


def test_a_correctly_signed_webhook_is_stored_and_answered_200(harness: Harness) -> None:
    response = harness.post_webhook(fixture("invoice_payment_failed"))
    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert list(harness.store.events) == ["evt_invoice_payment_failed"]
    assert harness.store.events["evt_invoice_payment_failed"].status is EventStatus.PENDING


def test_the_route_does_no_billing_work_of_its_own(harness: Harness) -> None:
    """200 fast, process later. The tenant is untouched until the drain runs — which is
    what stops a database blip during handling from turning into a 500, a Stripe retry and
    a half-applied event."""
    harness.post_webhook(fixture("subscription_deleted"))
    assert harness.store.tenants[TENANT].status is TenantStatus.ACTIVE
    assert harness.store.status_writes == []

    harness.service.process_pending()
    assert harness.store.tenants[TENANT].status is TenantStatus.SUSPENDED


def test_the_body_that_is_verified_is_the_body_that_is_stored(harness: Harness) -> None:
    """Raw bytes in, parsed once, stored as parsed — never re-serialised and re-checked."""
    body = fixture("invoice_payment_failed")
    harness.post_webhook(body)
    assert harness.store.events["evt_invoice_payment_failed"].payload == json.loads(body)


# ----------------------------------------------------------------------- idempotency


def test_a_replayed_webhook_changes_nothing(harness: Harness) -> None:
    """The acceptance criterion. Stripe retries a delivery it did not see a 200 for, and
    the retry carries the same event id and a fresh signature."""
    body = fixture("subscription_deleted")
    first = harness.post_webhook(body)
    second = harness.post_webhook(body, timestamp=NOW + timedelta(seconds=30))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(harness.store.events) == 1
    assert harness.store.inserts == 2

    harness.service.process_pending()
    harness.service.process_pending()
    assert harness.store.status_writes == [(TENANT, TenantStatus.SUSPENDED)]


def test_a_replay_while_the_first_copy_is_still_pending_is_a_no_op(harness: Harness) -> None:
    body = fixture("invoice_payment_failed")
    harness.post_webhook(body)
    assert harness.store.events["evt_invoice_payment_failed"].status is EventStatus.PENDING
    assert harness.post_webhook(body).status_code == 200
    assert len(harness.store.events) == 1
    assert harness.store.events["evt_invoice_payment_failed"].attempts == 0


# ------------------------------------------------------------------------ rejection


def test_an_unsigned_webhook_is_rejected_with_400(harness: Harness) -> None:
    response = harness.post_webhook(fixture("invoice_payment_failed"), header=None)
    assert response.status_code == 400
    assert harness.store.events == {}


def test_a_wrongly_signed_webhook_is_rejected_with_400(harness: Harness) -> None:
    response = harness.post_webhook(fixture("invoice_payment_failed"), secret="whsec_" + "x" * 40)
    assert response.status_code == 400
    assert harness.store.events == {}


def test_a_tampered_body_is_rejected_with_400(harness: Harness) -> None:
    """The signature is over the bytes. One changed character is a different message."""
    body = fixture("invoice_payment_failed")
    headers = {
        STRIPE_SIGNATURE_HEADER: signature_header(
            secret=WHSEC, body=body, timestamp=int(NOW.timestamp())
        )
    }
    tampered = body.replace(b"cus_acme", b"cus_evil")
    assert len(tampered) == len(body)
    response = harness.client.post(WEBHOOK_PATH, content=tampered, headers=headers)
    assert response.status_code == 400
    assert harness.store.events == {}


@pytest.mark.parametrize(
    "header",
    ["", "junk", "t=abc,v1=deadbeef", f"v1={'a' * 64}", f"t={int(NOW.timestamp())}"],
    ids=["empty", "junk", "bad timestamp", "no timestamp", "no signature"],
)
def test_a_malformed_signature_header_is_rejected_with_400(harness: Harness, header: str) -> None:
    response = harness.post_webhook(fixture("invoice_payment_failed"), header=header)
    assert response.status_code == 400


def test_a_stale_webhook_is_rejected_with_400(harness: Harness) -> None:
    response = harness.post_webhook(
        fixture("invoice_payment_failed"), timestamp=NOW - timedelta(hours=2)
    )
    assert response.status_code == 400
    assert harness.store.events == {}


def test_every_rejection_says_exactly_the_same_thing(harness: Harness) -> None:
    """A different message per failure is a free oracle: it tells whoever is probing which
    part of the request was wrong, and it tells an attacker holding a stale capture that
    their signature was otherwise fine."""
    bodies = [
        harness.post_webhook(fixture("invoice_payment_failed"), header=None),
        harness.post_webhook(fixture("invoice_payment_failed"), header="junk"),
        harness.post_webhook(fixture("invoice_payment_failed"), secret="whsec_" + "x" * 40),
        harness.post_webhook(fixture("invoice_payment_failed"), timestamp=NOW - timedelta(days=1)),
    ]
    assert {response.status_code for response in bodies} == {400}
    assert len({response.text for response in bodies}) == 1


def test_a_verified_body_that_is_not_a_stripe_event_is_rejected_with_400(
    harness: Harness,
) -> None:
    """Authenticated is not the same as well-formed. A body with no ``id`` has no
    idempotency key, so there is nothing to store it under."""
    for body in (
        b"not json",
        b"[]",
        b'{"type":"invoice.paid"}',
        b'{"id":"evt_1"}',
        b'{"id":1,"type":"x"}',
    ):
        response = harness.post_webhook(body)
        assert response.status_code == 400, body
    assert harness.store.events == {}


def test_an_oversized_body_is_refused_before_it_is_verified(harness: Harness) -> None:
    """Refused on size, so a stranger cannot make us HMAC an arbitrary number of megabytes."""
    response = harness.post_webhook(b"x" * (harness.deps.max_body_bytes + 1))
    assert response.status_code == 413
    assert harness.store.events == {}


def test_a_store_failure_is_a_500_so_stripe_retries(harness: Harness) -> None:
    """The one case where failing loudly is right. A 200 we could not honour would make
    Stripe consider the event delivered and never send it again."""

    def explode(*, event: Any) -> bool:
        raise RuntimeError("the database is gone")

    harness.store.insert_event = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        harness.post_webhook(fixture("invoice_payment_failed"))


# --------------------------------------------------------------- the customer portal


def test_a_signed_portal_request_returns_a_url(harness: Harness) -> None:
    response = harness.post_portal()
    assert response.status_code == 200
    assert response.json() == {"url": harness.billing.portal_url}
    assert harness.billing.portal_calls == [(TENANT, CUSTOMER, PORTAL_RETURN)]


def test_the_portal_return_url_comes_from_configuration_not_from_the_request() -> None:
    """A return url taken off the request body would make an authenticated endpoint into a
    redirector to any page the caller names, with Stripe's domain in front of it."""
    harness = Harness()
    body = json.dumps({"return_url": "https://phishing.example/"}).encode()
    timestamp = str(int(NOW.timestamp()))
    nonce = "portal-nonce-0002"
    response = harness.client.post(
        PORTAL_PATH,
        content=body,
        headers={
            HEADER_TENANT: TENANT,
            HEADER_KEY: API_KEY,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(
                secret=SIGNING_SECRET.encode("utf-8"),
                method="POST",
                path=PORTAL_PATH,
                tenant_id=TENANT,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
            ),
        },
    )
    assert response.status_code == 200
    assert harness.billing.portal_calls == [(TENANT, CUSTOMER, PORTAL_RETURN)]


def test_an_unsigned_portal_request_is_rejected_with_401(harness: Harness) -> None:
    response = harness.client.post(PORTAL_PATH, content=b"{}")
    assert response.status_code == 401
    assert harness.billing.portal_calls == []


def test_a_portal_request_signed_for_another_path_is_rejected(harness: Harness) -> None:
    """The path is inside the signed string, so an ingest signature cannot be replayed at
    the billing endpoint."""
    body = b"{}"
    timestamp = str(int(NOW.timestamp()))
    nonce = "portal-nonce-0003"
    response = harness.client.post(
        PORTAL_PATH,
        content=body,
        headers={
            HEADER_TENANT: TENANT,
            HEADER_KEY: API_KEY,
            HEADER_TIMESTAMP: timestamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(
                secret=SIGNING_SECRET.encode("utf-8"),
                method="POST",
                path="/leads",
                tenant_id=TENANT,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
            ),
        },
    )
    assert response.status_code == 401


def test_a_replayed_portal_request_is_rejected(harness: Harness) -> None:
    """Its own replay guard, not ingest's: the two endpoints share no collaborator."""
    assert harness.post_portal().status_code == 200
    assert harness.post_portal().status_code == 401


def test_a_suspended_tenant_is_told_so_rather_than_told_nothing() -> None:
    """A 403 with a reason, the same exception ingest makes. The caller has already proved
    who it is, so there is no enumeration left to protect, and the integrator deserves to
    know that the account and not the integration is the problem.

    It is also a real limitation, written down here so it is not discovered by a customer:
    a *suspended* tenant cannot open the portal from this endpoint, so self-service
    recovery happens during the seven-day grace period (when the tenant is still active) or
    through the hosted invoice link in Stripe's own dunning email. See
    ``docs/billing-integration.md``.
    """
    harness = Harness(tenant_status="suspended")
    response = harness.post_portal()
    assert response.status_code == 403
    assert harness.billing.portal_calls == []


def test_a_tenant_with_no_stripe_customer_gets_a_409_not_a_new_customer() -> None:
    harness = Harness()
    harness.store.given_tenant(TENANT, stripe_customer_id=None)
    response = harness.post_portal()
    assert response.status_code == 409
    assert harness.billing.portal_calls == []


# -------------------------------------------------------------------------- wiring


def test_the_billing_routes_do_not_share_ingests_dependencies() -> None:
    """``BillingDeps`` is its own object. The webhook handler holds no lead store and no
    queue, so it cannot reach them however the app is assembled."""
    fields = set(BillingDeps.__dataclass_fields__)
    assert "service" in fields
    assert not fields & {"store", "queue", "rate_limiter", "spam_policy"}


def test_the_webhook_path_is_the_one_the_stripe_endpoint_is_configured_with() -> None:
    assert WEBHOOK_PATH == "/webhooks/stripe"
    assert PORTAL_PATH == "/billing/portal"


def test_an_unreadable_dependency_on_the_portal_is_a_503_not_a_401() -> None:
    """#31's review fix, honoured here too. A credential store we could not read is not
    the caller's fault, and answering 401 would tell an integrator their key is wrong when
    it is fine. The ``Retry-After`` is the only answer that asks them to come back."""
    from leadquali.app.credentials import AuthFailure, CredentialRejected

    harness = Harness()

    class Unavailable:
        def resolve(self, *, tenant_id: str, api_key: str) -> CredentialRejected:
            return CredentialRejected(AuthFailure.UNAVAILABLE)

    harness.deps = BillingDeps(
        service=harness.service,
        webhook_secret=WHSEC,
        clock=harness.clock,
        credentials=Unavailable(),
        portal_return_url=PORTAL_RETURN,
    )
    app = FastAPI()
    register_billing_routes(app, harness.deps)
    harness.client = TestClient(app)

    response = harness.post_portal()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert harness.billing.portal_calls == []
