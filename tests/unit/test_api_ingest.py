"""``POST /leads``: the public edge, exercised the way a stranger would exercise it.

The happy path is one test. The rest is what the endpoint has to survive: a forged
signature, a key for a tenant that does not exist, a replayed request, a body far too
large, JSON that is not JSON, a bot that filled the honeypot, a bot that posted in 40 ms,
the same submission twice, and a payload carrying fields nobody has ever heard of.

Two properties get their own tests because they are the ones that quietly rot:

* the 202 never waits on model work, asserted with an assessor that fails the test if the
  request thread ever reaches it;
* a suppressed lead is still on the record, because "we filtered it" and "we lost it" must
  never be the same outcome (invariant 3).
"""

from __future__ import annotations

import ast
import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.adapters.tenant_config_json import JsonFileTenantConfigLoader, default_tenants_dir
from leadquali.api.main import (
    HEALTH_PATH,
    INGEST_PATH,
    IngestDeps,
    build_deps,
    create_app,
)
from leadquali.api.ratelimit import FixedWindowRateLimiter
from leadquali.api.schemas import MAX_BODY_BYTES
from leadquali.api.signing import (
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TENANT,
    HEADER_TIMESTAMP,
    MAX_CLOCK_SKEW_SECONDS,
    IngestCredential,
    StaticCredentials,
    hash_api_key,
    sign,
)
from leadquali.app.assessment_result import AssessmentFailed, AssessmentOutcome
from leadquali.app.ingest import IngestService, QueuedLead
from leadquali.app.ports import RoutingOutcome
from leadquali.config import Settings
from leadquali.domain.models import Action, EscalationReason
from leadquali.domain.tenant_config import TenantConfig
from tests.fakes import FakeClock, InMemoryLeadStore

TENANT = "default"
API_KEY = "lq_live_5b1f0a7c2e9d4368"
SECRET = "local-signing-secret-of-adequate-length"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

CREDENTIALS = StaticCredentials(
    {
        TENANT: IngestCredential(
            tenant_id=TENANT,
            api_key_sha256=hash_api_key(API_KEY),
            signing_secret=SECRET.encode("utf-8"),
        )
    }
)

VALID_FORM: dict[str, Any] = {
    "full_name": "Ada Lovelace",
    "email": "ada@analytical-engines.co.uk",
    "company": "Analytical Engines",
    "role": "VP Engineering",
    "message": "We take about 400 inbound enquiries a month and cannot triage them by hand.",
}


def payload(**overrides: Any) -> dict[str, Any]:
    """A well-formed request envelope, with any part of it replaceable."""
    body: dict[str, Any] = {
        "submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33",
        "form": dict(VALID_FORM),
        "elapsed_ms": 12_000,
    }
    body.update(overrides)
    return body


class Harness:
    """An app wired to in-memory doubles, plus the signing the client would do."""

    def __init__(
        self,
        *,
        store: InMemoryLeadStore | None = None,
        queue: InProcessLeadQueue | None = None,
        clock: FakeClock | None = None,
        deps_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.store = store if store is not None else InMemoryLeadStore()
        self.queue = queue if queue is not None else InProcessLeadQueue()
        self.clock = clock if clock is not None else FakeClock(start=NOW, step_ms=0)
        self.deps = IngestDeps(
            service=IngestService(store=self.store, queue=self.queue, clock=self.clock),
            credentials=CREDENTIALS,
            clock=self.clock,
            **(deps_overrides or {}),
        )
        self.client = TestClient(create_app(self.deps))

    def headers(
        self,
        body: bytes,
        *,
        tenant: str = TENANT,
        api_key: str = API_KEY,
        secret: str = SECRET,
        timestamp: datetime | None = None,
        nonce: str = "nonce-000000000001",
        path: str = INGEST_PATH,
    ) -> dict[str, str]:
        stamp = str(int((timestamp if timestamp is not None else NOW).timestamp()))
        return {
            HEADER_TENANT: tenant,
            HEADER_KEY: api_key,
            HEADER_TIMESTAMP: stamp,
            HEADER_NONCE: nonce,
            HEADER_SIGNATURE: sign(
                secret=secret.encode("utf-8"),
                method="POST",
                path=path,
                tenant_id=tenant,
                timestamp=stamp,
                nonce=nonce,
                body=body,
            ),
            "Content-Type": "application/json",
        }

    def post(self, body: dict[str, Any] | bytes | None = None, **header_kwargs: Any) -> Any:
        """Sign and send one request. Byte-identical to what a customer's form does."""
        raw = body if isinstance(body, bytes) else json.dumps(payload() if body is None else body)
        content = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        return self.client.post(
            INGEST_PATH, content=content, headers=self.headers(content, **header_kwargs)
        )


@pytest.fixture
def harness() -> Iterator[Harness]:
    built = Harness()
    yield built
    built.queue.close()


# ---------------------------------------------------------------------- the happy path


def test_a_signed_lead_is_accepted_persisted_and_enqueued(harness: Harness) -> None:
    response = harness.post()

    assert response.status_code == 202
    assert response.json() == {
        "submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33",
        # RFC 3339 in UTC, ``Z``-suffixed: the form echoes it into its own logs, so the
        # exact rendering is part of the contract #30 documents.
        "received_at": "2026-09-03T12:00:00Z",
        "status": "accepted",
    }
    assert response.headers["cache-control"] == "no-store"
    assert list(harness.store.leads) == [(TENANT, "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33")]
    assert [lead.submission_id for lead in harness.queue.pending()] == [
        "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33"
    ]


def test_the_stored_submission_is_the_form_the_visitor_filled_in(harness: Harness) -> None:
    harness.post()
    stored = harness.queue.pending()[0].submission
    assert stored.email == VALID_FORM["email"]
    assert stored.full_name == VALID_FORM["full_name"]
    assert stored.message == VALID_FORM["message"]


def test_health_needs_no_credentials(harness: Harness) -> None:
    response = harness.client.get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------------------- authentication


def test_a_bad_signature_is_rejected(harness: Harness) -> None:
    response = harness.post(secret="a-different-secret-of-adequate-length")
    assert response.status_code == 401
    assert harness.store.leads == {}


def test_an_unknown_tenant_and_a_bad_key_are_indistinguishable(harness: Harness) -> None:
    """The endpoint must not be an oracle for which customers exist."""
    unknown = harness.post(tenant="does-not-exist")
    bad_key = harness.post(api_key="lq_live_wrong", nonce="nonce-000000000002")

    assert unknown.status_code == bad_key.status_code == 401
    assert unknown.json() == bad_key.json()
    assert set(unknown.headers) - {"date", "server"} == set(bad_key.headers) - {"date", "server"}
    assert "tenant" not in json.dumps(unknown.json()).lower()


def test_a_request_with_no_credentials_at_all_is_rejected(harness: Harness) -> None:
    response = harness.client.post(INGEST_PATH, json=payload())
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication failed"}


@pytest.mark.parametrize(
    "header", [HEADER_TENANT, HEADER_KEY, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE]
)
def test_every_auth_header_is_required(harness: Harness, header: str) -> None:
    raw = json.dumps(payload()).encode()
    headers = harness.headers(raw)
    del headers[header]
    response = harness.client.post(INGEST_PATH, content=raw, headers=headers)
    assert response.status_code == 401


def test_a_body_changed_after_signing_is_rejected(harness: Harness) -> None:
    """The signature covers the raw bytes, so a tampered field invalidates it."""
    raw = json.dumps(payload()).encode()
    headers = harness.headers(raw)
    tampered = json.dumps(payload(submission_id="tampered-0000000001")).encode()
    response = harness.client.post(INGEST_PATH, content=tampered, headers=headers)
    assert response.status_code == 401
    assert harness.store.leads == {}


def test_a_stale_timestamp_is_rejected(harness: Harness) -> None:
    stale = NOW - timedelta(seconds=MAX_CLOCK_SKEW_SECONDS + 30)
    assert harness.post(timestamp=stale).status_code == 401


def test_a_replayed_request_is_rejected_the_second_time(harness: Harness) -> None:
    """The identical bytes, headers and nonce, sent twice: the second is refused."""
    raw = json.dumps(payload()).encode()
    headers = harness.headers(raw)

    first = harness.client.post(INGEST_PATH, content=raw, headers=headers)
    second = harness.client.post(INGEST_PATH, content=raw, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 401
    assert len(harness.store.leads) == 1


def test_authentication_happens_before_the_body_is_parsed(harness: Harness) -> None:
    """Malformed JSON from an unauthenticated caller is a 401, never a 422.

    Parsing is the expensive part; doing it for a stranger is the whole vulnerability, and
    a 422 here would also tell them their signature was fine.
    """
    response = harness.client.post(
        INGEST_PATH, content=b"{not json at all", headers={HEADER_TENANT: TENANT}
    )
    assert response.status_code == 401


# ------------------------------------------------------------------ size and schema


def test_an_oversized_body_is_refused_before_it_is_parsed() -> None:
    harness = Harness(deps_overrides={"max_body_bytes": 2_048})
    oversized = payload(form={**VALID_FORM, "message": "x" * 8_000})
    response = harness.post(oversized)

    assert response.status_code == 413
    assert response.json() == {"detail": "request body is larger than the limit"}
    assert harness.store.leads == {}


def test_an_oversized_body_is_refused_even_without_a_declared_length() -> None:
    """A chunked request declares no length, so the limit is enforced on the stream."""
    harness = Harness(deps_overrides={"max_body_bytes": 1_024})

    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 256

    response = harness.client.post(INGEST_PATH, content=chunks(), headers={HEADER_TENANT: TENANT})
    assert response.status_code == 413


def test_the_default_body_limit_is_generous_enough_for_a_real_enquiry(harness: Harness) -> None:
    assert MAX_BODY_BYTES >= 16 * 1024
    long_but_real = payload(form={**VALID_FORM, "message": "We need help. " * 500})
    assert harness.post(long_but_real).status_code == 202


def test_malformed_json_is_a_422_with_a_useful_message(harness: Harness) -> None:
    response = harness.post(b'{"submission_id": "abc", ')
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "the submission did not validate"
    assert body["errors"]


@pytest.mark.parametrize(
    "bad",
    [
        {"form": dict(VALID_FORM)},
        {"submission_id": "short", "form": dict(VALID_FORM)},
        {"submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33"},
        {"submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33", "form": "not-an-object"},
        {
            "submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33",
            "form": {"email": 42},
        },
        {
            "submission_id": "0f3b2a5c-7d21-4c86-9d0f-2b4e6a8c1d33",
            "form": {**VALID_FORM, "nested": {"a": 1}},
        },
    ],
)
def test_a_payload_that_does_not_validate_is_refused(harness: Harness, bad: dict[str, Any]) -> None:
    response = harness.post(bad)
    assert response.status_code == 422
    assert harness.store.leads == {}


def test_an_unknown_envelope_field_is_refused(harness: Harness) -> None:
    """The envelope is our protocol: a key we do not know means we disagree about it."""
    response = harness.post(payload(utm_source="newsletter"))
    assert response.status_code == 422
    assert any("utm_source" in error["field"] for error in response.json()["errors"])


def test_unknown_form_fields_are_kept_rather_than_dropped(harness: Harness) -> None:
    """A field nobody anticipated is exactly the one that carries the buying signal."""
    response = harness.post(
        payload(form={**VALID_FORM, "budget_band": "50-100k", "seats": 250, "trial": True})
    )
    assert response.status_code == 202
    extra = harness.queue.pending()[0].submission.extra
    assert extra == {"budget_band": "50-100k", "seats": "250", "trial": "True"}


def test_a_validation_error_does_not_echo_the_submitted_values_back(harness: Harness) -> None:
    """Invariant 5: an error body is copied into logs and support tickets."""
    response = harness.post(
        payload(submission_id="short", form={**VALID_FORM, "email": "ada@analytical-engines.co.uk"})
    )
    assert response.status_code == 422
    assert "ada@analytical-engines.co.uk" not in response.text


# ------------------------------------------------------------------ spam pre-filters


def test_a_honeypot_hit_is_answered_202_and_recorded_as_suppressed(harness: Harness) -> None:
    response = harness.post(payload(honeypot="http://cheap-seo.example"))

    assert response.status_code == 202, "a bot must not learn that it was caught"
    assert response.json()["status"] == "accepted"
    assert len(harness.store.leads) == 1, "invariant 3: suppressed, not dropped"
    (event,) = harness.store.routing_events
    assert event.action is Action.SUPPRESS
    assert event.outcome is RoutingOutcome.SUPPRESSED
    assert "honeypot" in event.detail
    assert harness.queue.pending() == (), "spam must never reach the model"


def test_a_submission_faster_than_a_human_is_suppressed(harness: Harness) -> None:
    response = harness.post(payload(elapsed_ms=40))

    assert response.status_code == 202
    assert len(harness.store.leads) == 1
    assert "too_fast" in harness.store.routing_events[0].detail
    assert harness.queue.pending() == ()


def test_an_obviously_fake_email_domain_is_suppressed(harness: Harness) -> None:
    response = harness.post(payload(form={**VALID_FORM, "email": "bot@mailinator.com"}))

    assert response.status_code == 202
    assert "fake_email_domain" in harness.store.routing_events[0].detail
    assert harness.queue.pending() == ()


def test_a_suppressed_and_an_accepted_lead_are_answered_identically(harness: Harness) -> None:
    accepted = harness.post()
    suppressed = harness.post(
        payload(submission_id="1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d", honeypot="x"),
        nonce="nonce-000000000002",
    )
    assert accepted.status_code == suppressed.status_code
    assert set(accepted.json()) == set(suppressed.json())
    assert suppressed.json()["status"] == "accepted"


# ---------------------------------------------------------------------- idempotency


def test_the_same_submission_id_twice_yields_one_lead(harness: Harness) -> None:
    first = harness.post()
    second = harness.post(nonce="nonce-000000000002")

    assert first.status_code == second.status_code == 202
    assert first.json()["submission_id"] == second.json()["submission_id"]
    assert len(harness.store.leads) == 1


# ------------------------------------------------------------------------- latency


class ForbiddenAssessor:
    """A :class:`~leadquali.app.ports.LeadAssessorPort` that must never run on the request.

    It fails the test if it is called from the thread that handled the HTTP request. That
    is the property the whole 202-and-enqueue design exists for: a Claude call takes
    seconds, and a form post that waits on one produces timeouts and duplicate
    submissions.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.request_thread: int | None = None

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        del config, rendered_lead
        if self.request_thread is not None and threading.get_ident() == self.request_thread:
            pytest.fail("the ingest handler did model work; the 202 must never wait on it")
        self.calls += 1
        return AssessmentFailed(
            reason=EscalationReason.API_ERROR, detail="not a real assessment", latency_ms=0
        )


def test_the_handler_never_reaches_the_model(harness: Harness) -> None:
    """Nothing on the request path touches an assessor — there is not even one wired in."""
    assessor = ForbiddenAssessor()
    assert harness.post().status_code == 202
    assert assessor.calls == 0
    assert "assessor" not in {field.name for field in harness.deps.__dataclass_fields__.values()}


def test_model_work_happens_on_the_worker_thread_not_the_request_thread() -> None:
    """With a worker wired in, the assessor runs — and never on the request's thread."""
    assessor = ForbiddenAssessor()
    config = JsonFileTenantConfigLoader(default_tenants_dir()).get("default")
    done = threading.Event()

    def worker(lead: QueuedLead) -> None:
        assessor.assess(config=config, rendered_lead=lead.submission_id)
        done.set()

    class MarkingQueue(InProcessLeadQueue):
        """Records the thread the endpoint enqueued on, which is the forbidden one."""

        def enqueue(self, lead: QueuedLead) -> str | None:
            assessor.request_thread = threading.get_ident()
            return super().enqueue(lead)

    queue = MarkingQueue(worker=worker)
    harness = Harness(queue=queue)
    try:
        assert harness.post().status_code == 202
        assert done.wait(timeout=5)
    finally:
        queue.close()

    assert assessor.calls == 1


def test_the_handler_answers_well_inside_the_latency_budget() -> None:
    """Plan §3: 202 in under 200 ms with the model excluded. Fakes, so this measures us."""
    harness = Harness()
    elapsed: list[float] = []
    for index in range(20):
        started = time.perf_counter()
        response = harness.post(
            payload(submission_id=f"budget-{index:012d}"), nonce=f"nonce-{index:012d}"
        )
        elapsed.append((time.perf_counter() - started) * 1_000)
        assert response.status_code == 202

    elapsed.sort()
    p95 = elapsed[int(len(elapsed) * 0.95) - 1]
    assert p95 < 200, f"p95 was {p95:.1f}ms"


def test_the_ingest_modules_import_nothing_that_could_call_a_model() -> None:
    """Structural, so it keeps holding as the endpoint grows.

    A latency test can be made to pass by a fast mock. This cannot: the modules on the
    request path do not import the assessor port, the Anthropic adapter or the enricher, so
    reaching a model from here would have to start with an import a reviewer would see.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "leadquali"
    forbidden = {"llm_anthropic", "enrich_email", "enrich_null", "LeadAssessorPort", "EnricherPort"}
    for relative in ("api/main.py", "api/schemas.py", "api/signing.py", "app/ingest.py"):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                imported.update((node.module or "").split("."))
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
        assert not (forbidden & imported), f"{relative} imports {sorted(forbidden & imported)}"


# ---------------------------------------------------------------- rate limit and logs


def test_a_tenant_over_its_allowance_is_rate_limited() -> None:
    harness = Harness(
        deps_overrides={"rate_limiter": FixedWindowRateLimiter(limit=2, window_seconds=60)}
    )
    codes = [
        harness.post(
            payload(submission_id=f"rate-{index:012d}"), nonce=f"nonce-{index:012d}"
        ).status_code
        for index in range(3)
    ]
    assert codes == [202, 202, 429]


def test_a_rate_limited_response_says_when_to_come_back() -> None:
    harness = Harness(
        deps_overrides={"rate_limiter": FixedWindowRateLimiter(limit=1, window_seconds=60)}
    )
    harness.post(payload(submission_id="rate-000000000001"), nonce="nonce-000000000001")
    refused = harness.post(payload(submission_id="rate-000000000002"), nonce="nonce-000000000002")
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1


def test_no_contact_address_ever_reaches_the_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Invariant 5: the hash correlates a person's leads; the address is never written."""
    with caplog.at_level(logging.DEBUG):
        harness.post()

    assert "ada@analytical-engines.co.uk" not in caplog.text
    assert "Ada Lovelace" not in caplog.text
    assert VALID_FORM["message"] not in caplog.text
    # 64 hex characters: the contact hash, which is what correlation is done on.
    assert "contact=" in caplog.text
    hashed = caplog.text.split("contact=")[1].split()[0]
    assert len(hashed) == 64


def test_a_rejected_request_logs_the_reason_but_answers_with_nothing(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        response = harness.post(tenant="does-not-exist")

    assert response.json() == {"detail": "authentication failed"}
    assert "unknown_tenant" in caplog.text


# ------------------------------------------------------------------------- wiring


def test_the_production_wiring_refuses_to_start_without_a_database() -> None:
    """A misconfigured deployment must fail at wiring, in front of whoever deployed it."""
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_deps(Settings(database_url=None, ingest_credentials=None))


def test_the_production_wiring_refuses_to_start_without_ingest_credentials() -> None:
    """There is no "no keys configured means no authentication" mode. That is the point."""
    with pytest.raises(RuntimeError, match="INGEST_CREDENTIALS"):
        build_deps(Settings(database_url="postgresql+psycopg://x/y", ingest_credentials=None))


def test_importing_the_module_builds_an_app_without_touching_a_database() -> None:
    """``import leadquali.api.main`` runs at Lambda cold start and under ``--reload``."""
    routes = {getattr(route, "path", None) for route in create_app().routes}
    assert {INGEST_PATH, HEALTH_PATH} <= routes
