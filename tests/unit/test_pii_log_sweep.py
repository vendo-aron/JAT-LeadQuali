"""Every event this system emits, swept for personal data — and enumerated from the code.

#21 proved that no PII reaches the logs, with a test that ran one lead through the pipeline.
Phase 5 then added tenant management, metering, an admin UI and a retention job, and none of
those events were covered by it. The fix is not "add them to the list": a list somebody
maintains is a list that falls behind, and the next event would opt out the same way.

So this file **discovers** the events instead. :func:`emitted_events` walks the AST of every
module under ``src/leadquali`` and finds each call that produces a log record with an
``event`` name, along with the field names that ride on it. Every discovered event must then
be accounted for in exactly one of two ways:

``SCENARIOS``
    A callable that drives the real code path that emits it, with a lead's real personal
    data in scope, and returns the strings that must not appear in the output. The sweep
    runs it under the real JSON formatter and searches the raw bytes.

``FIELDLESS_EVENTS``
    An event whose call site passes **no fields at all** and whose message is a literal —
    so there is nothing on the record that could carry a lead. That is not a waiver: it is
    checked against the AST, so the day somebody adds a field to one of these, this file
    fails and asks for a scenario.

An event that is in neither fails :func:`test_every_emitted_event_is_accounted_for` by name.
That is the mechanism, and it is the only reason to believe invariant 5 will still hold
after everyone who wrote it has moved on.

The events are also checked against ``docs/observability.md``, which is published as a
contract — #29 writes alarms against it and a runbook's saved query filters on it — so an
event that exists and is not written down is a gap in that contract too.

**The admin (#36) is the interesting case.** It renders raw payloads to a human by design,
which is allowed: invariant 5 is about logs and error pages, not about screens. What is not
allowed is a payload reaching a log line or an error page, so the admin scenarios below
drive the routes that hold a payload — including the one that raises while rendering it —
and sweep what came out.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest

# Imported from their own homes rather than reached through the test-module aliases below:
# mypy's strict mode refuses a name a module merely re-exports, and it is right to — reading
# `pipeline.InMemoryLeadStore` invites the reader to think it is the pipeline test's own.
#
# `api.retention`, `api.worker` and (transitively) `api.main` all call `configure_logging()`
# at import. Importing them here rather than inside a scenario is load-bearing: an import
# that happened inside a `capture_json_logs` block would replace the capture's handler, and
# every sweep below would pass against an empty buffer.
from leadquali.adapters.queue_inprocess import InProcessLeadQueue
from leadquali.api.admin import LOGIN_PATH
from leadquali.api.billing_jobs import drain_events, report_usage
from leadquali.api.retention import lambda_handler
from leadquali.api.worker import handle as handle_batch
from leadquali.app.ingest import IngestRequest, QueuedLead
from leadquali.app.metering import BillingPeriod, MeteringService, TenantQuota
from leadquali.app.retention import RetentionService
from leadquali.app.tenants import TenantStatus
from leadquali.observability import EMAIL_REDACTION
from tests.fakes import (
    FakeClock,
    FakeNotifierError,
    InMemoryLeadStore,
    InMemoryMeteringStore,
    InMemoryRetentionStore,
    RecordingNotifier,
    StaticRevenue,
    stripe_event,
)
from tests.logcapture import LogCapture, capture_json_logs
from tests.sqlcapture import CannedResult, SqlCapture

# Other test modules rather than fresh fixtures, because the doubles for the pipeline, the
# admin and the public edge already live there — and a second set of them would drift from
# the code they stand in for the day it changes. Imported at module scope for the same
# configure_logging reason as the block above.
from tests.unit import test_api_admin as admin
from tests.unit import test_api_ingest as ingest
from tests.unit import test_api_webhooks as webhooks
from tests.unit import test_observability_pipeline as pipeline

SOURCE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "leadquali"
OBSERVABILITY_DOC: Final[Path] = Path(__file__).resolve().parents[2] / "docs" / "observability.md"

#: Keyword arguments of :func:`~leadquali.observability.logs.log_event` that are not event
#: fields. Everything else a call site passes ends up on the record.
_NON_FIELD_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"level", "message", "exc_info", "metrics", "fields"}
)

#: Address-shaped runs, for the "nothing survived under another spelling" check. Kept
#: separate from ``observability.pii``'s own pattern on purpose: a leak that the production
#: redactor fails to recognise is exactly the leak worth catching, so this one is its own,
#: looser statement of what an address looks like.
_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"[^\s\"'<>@]{1,64}@[^\s\"'<>@]{1,255}\.\w{2,24}")


# --------------------------------------------------------------------------- discovery


@dataclass(frozen=True, slots=True)
class Emission:
    """One call site that writes a log record carrying an ``event`` name."""

    event: str
    module: str
    fields: frozenset[str]
    literal_message: bool
    """Whether the record's message is a constant. An f-string built from a lead is a leak
    the field check would not see."""


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME: ... = "literal"`` assignments, for resolving event names."""
    constants: dict[str, str] = {}
    for node in tree.body:
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            constants[target.id] = value.value
    return constants


def _resolve(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    """The string an event argument names, if it can be known statically."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute):
        return constants.get(node.attr)
    return None


def _log_event_emission(
    call: ast.Call, module: str, constants: Mapping[str, str]
) -> Emission | None:
    """An ``observability.logs.log_event(logger, EVENT, …)`` call site."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "log_event"):
        return None
    positional = call.args[1] if len(call.args) > 1 else None
    keyword = {word.arg: word.value for word in call.keywords if word.arg is not None}
    name = _resolve(positional or keyword.get("event"), constants)
    if name is None:
        raise AssertionError(
            f"{module} emits a log event whose name cannot be read statically. Name it with "
            "a module constant so this sweep can enumerate it."
        )
    fields = frozenset(keyword) - _NON_FIELD_KEYWORDS - {"event"}
    if any(word.arg is None for word in call.keywords) or "fields" in keyword:
        # `**something` or `fields=` — the names are not knowable here, so the event is
        # treated as carrying fields and needs a scenario. That is the safe direction.
        fields = fields | {"<dynamic>"}
    message = keyword.get("message")
    literal = message is None or isinstance(message, ast.Constant)
    return Emission(event=name, module=module, fields=fields, literal_message=literal)


def _extra_emission(call: ast.Call, module: str, constants: Mapping[str, str]) -> Emission | None:
    """A ``logger.info("…", extra={"event": …})`` call site, the other spelling."""
    keyword = {word.arg: word.value for word in call.keywords if word.arg is not None}
    extra = keyword.get("extra")
    if not isinstance(extra, ast.Dict):
        return None
    keys = [_resolve(key, constants) for key in extra.keys]
    if "event" not in keys:
        return None
    name = _resolve(extra.values[keys.index("event")], constants)
    if name is None:
        raise AssertionError(f"{module} emits a log event whose name cannot be read statically")
    fields = frozenset(key for key in keys if key is not None and key != "event")
    first = call.args[0] if call.args else None
    return Emission(
        event=name,
        module=module,
        fields=fields,
        literal_message=isinstance(first, ast.Constant),
    )


def emitted_events() -> dict[str, Emission]:
    """Every event name the source emits, with the fields its call site passes.

    Merged across call sites: two places emitting the same event contribute the union of
    their fields, because the sweep's question is "can *any* call site put a lead on this
    record", not "does this one".
    """
    trees = {
        str(path.relative_to(SOURCE_ROOT.parent)).replace("/", ".")[: -len(".py")]: ast.parse(
            path.read_text(encoding="utf-8")
        )
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    }
    # One table across the whole package, because the constants are *imported*: every admin
    # event name is defined in `observability/events.py` and used in `api/admin.py`, so a
    # per-module table would fail to resolve exactly the events Phase 5 added. The names are
    # `EVENT_*`-shaped and unique by construction — two modules defining the same one would
    # be its own bug — so a flat table is the right amount of machinery here.
    constants: dict[str, str] = {}
    for tree in trees.values():
        constants.update(_module_constants(tree))

    found: dict[str, Emission] = {}
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            emission = _log_event_emission(node, module, constants) or _extra_emission(
                node, module, constants
            )
            if emission is None:
                continue
            previous = found.get(emission.event)
            found[emission.event] = (
                emission
                if previous is None
                else Emission(
                    event=emission.event,
                    module=f"{previous.module}, {emission.module}",
                    fields=previous.fields | emission.fields,
                    literal_message=previous.literal_message and emission.literal_message,
                )
            )
    return found


EMITTED: Final[dict[str, Emission]] = emitted_events()


# ---------------------------------------------------------------------------- scenarios


@dataclass(frozen=True, slots=True)
class Planted:
    """What one scenario put into the system, and must not find in the output."""

    secrets: tuple[str, ...] = ()
    reason: str = ""


Scenario = Callable[[], Planted]

#: The lead every scenario plants. Distinctive enough that a substring search cannot
#: produce a false negative, and shaped like a real address so the formatter's own net would
#: match it — which is why :data:`EMAIL_REDACTION` appearing anywhere is also a failure.
LEAD_EMAIL: Final[str] = "ada.lovelace+sweep37@analytical-engines-quali.co.uk"

#: Not a pattern. Nothing in the formatter can recognise this, so the only reason it stays
#: out of the logs is that no call site ever passes it to one.
LEAD_MESSAGE: Final[str] = (
    "We run 40 difference engines in Marylebone and our punch-card vendor just doubled "
    "their price; I need routing sorted before the Michaelmas board meeting."
)

LEAD_NAME: Final[str] = "Augusta Ada King-Noel"
LEAD_COMPANY: Final[str] = "Analytical Engines Ltd"

#: Every string from the planted lead that must never be logged.
LEAD_SECRETS: Final[tuple[str, ...]] = (LEAD_EMAIL, LEAD_MESSAGE, LEAD_NAME, LEAD_COMPANY)

#: Not personal data, and swept for anyway: the scenarios that drive the secret cache put a
#: real value in the frame, and a log line that quoted one would be a different incident of
#: the same kind.
SECRET_VALUE: Final[str] = "the-secret-value-that-must-not-be-logged"

TENANT: Final[str] = "acme"
NOW: Final[dt.datetime] = dt.datetime(2026, 9, 17, 9, 0, tzinfo=dt.UTC)


def _pipeline_scenario(kind: str) -> Planted:
    """Drive #21's own pipeline fixtures, which already carry a lead's whole identity.

    Imported from ``test_observability_pipeline`` rather than rebuilt: that module is where
    the pipeline's doubles live, and a second set of them would drift from the pipeline the
    day it changes.
    """
    match kind:
        case "routed":
            store = InMemoryLeadStore()
            queue = InProcessLeadQueue()
            pipeline.ingest_service(store, queue).accept(
                IngestRequest(
                    tenant_id=pipeline.TENANT,
                    submission_id="sub-1",
                    submission=pipeline.SUBMISSION,
                )
            )
            built, _, _ = pipeline.build_pipeline(store=store)
            built.qualify(pipeline.request_for())
        case "duplicate":
            store = InMemoryLeadStore()
            queue = InProcessLeadQueue()
            pipeline.ingest_service(store, queue).accept(
                IngestRequest(
                    tenant_id=pipeline.TENANT,
                    submission_id="sub-1",
                    submission=pipeline.SUBMISSION,
                )
            )
            built, _, _ = pipeline.build_pipeline(store=store)
            built.qualify(pipeline.request_for())
            built.qualify(pipeline.request_for())
        case "dispatch_failed":
            notifier = RecordingNotifier()

            def explode(**kwargs: Any) -> str | None:
                # The realistic shape: a provider error reported with the thing that was
                # being sent. No address is planted by hand — `LeadSubmission` declares
                # every field `repr=False`, and whether that still holds is the property
                # under test. Planting one would only prove the formatter's net fires.
                raise FakeNotifierError(f"SES rejected this message: {kwargs['submission']!r}")

            notifier.dispatch = explode  # type: ignore[method-assign]  # a leaky double
            built, _, _ = pipeline.build_pipeline(notifier=notifier)
            with pytest.raises(FakeNotifierError):
                built.qualify(pipeline.request_for())
        case _:  # pragma: no cover - the match above is exhaustive over its callers
            raise AssertionError(kind)
    return Planted(secrets=pipeline.SECRETS)


def _suppressed_scenario() -> Planted:
    """A lead the model scores at the floor, so it is recorded and never contacted."""
    built, _, _ = pipeline.build_pipeline(outcome=pipeline.succeeded(score=1))
    built.qualify(pipeline.request_for())
    return Planted(secrets=pipeline.SECRETS)


def _quota_scenario() -> Planted:
    """#33's commercial event. No lead is in scope, and that is the assertion."""
    store = InMemoryMeteringStore()
    store.given_quota(
        TenantQuota(tenant_id=TENANT, monthly_lead_quota=1, alert_fraction=Decimal("0.5"))
    )
    store.add_lead(tenant_id=TENANT, received_at=NOW)
    store.add_assessment(tenant_id=TENANT, created_at=NOW, input_tokens=10)
    service = MeteringService(
        store=store, clock=FakeClock(start=NOW, step_ms=0), revenue=StaticRevenue()
    )
    service.quota_status(tenant_id=TENANT, period=BillingPeriod.of_month(NOW.year, NOW.month))
    return Planted(reason="a plan size and a count; no lead is reachable from this path")


def _retention_scenario(kind: str) -> Planted:
    """#37's two events, with a real lead and a real address in the model's prose."""
    store = InMemoryRetentionStore()
    store.given_tenant(TENANT)
    store.given_lead(
        tenant_id=TENANT,
        received_at=NOW - dt.timedelta(days=200),
        email=LEAD_EMAIL,
        submission={"full_name": LEAD_NAME, "company": LEAD_COMPANY, "message": LEAD_MESSAGE},
        reasoning=f"Good fit. {LEAD_NAME} wrote from {LEAD_EMAIL}: {LEAD_MESSAGE}",
    )
    service = RetentionService(store=store, clock=FakeClock(start=NOW, step_ms=0))
    if kind == "purged":
        service.purge_tenant(tenant_id=TENANT, dry_run=False)
    else:
        service.erase_subject(tenant_id=TENANT, email=LEAD_EMAIL, requested_by="SUP-4471")
    return Planted(secrets=LEAD_SECRETS)


def _retention_run_scenario() -> Planted:
    """The scheduled job's own line: how many tenants, and what the totals were."""
    store = InMemoryRetentionStore()
    store.given_tenant(TENANT)
    store.given_lead(
        tenant_id=TENANT,
        received_at=NOW - dt.timedelta(days=200),
        email=LEAD_EMAIL,
        submission={"full_name": LEAD_NAME, "company": LEAD_COMPANY, "message": LEAD_MESSAGE},
        reasoning=f"Good fit. {LEAD_NAME} wrote from {LEAD_EMAIL}.",
    )
    lambda_handler(
        {},
        None,
        service=RetentionService(store=store, clock=FakeClock(start=NOW, step_ms=0)),
    )
    return Planted(secrets=LEAD_SECRETS)


def _ingest_scenario(kind: str) -> Planted:
    """The public edge, driven through the real ASGI app with a signed request."""
    from leadquali.api.ratelimit import FixedWindowRateLimiter

    form = {
        "full_name": LEAD_NAME,
        "email": LEAD_EMAIL,
        "company": LEAD_COMPANY,
        "message": LEAD_MESSAGE,
    }
    body = ingest.payload(form=form)
    match kind:
        case "http.ingest":
            harness = ingest.Harness()
            harness.post(body)
            harness.queue.close()
        case "ingest.rejected":
            harness = ingest.Harness()
            harness.post(body, secret="a-signing-secret-that-is-not-the-right-one")
            harness.queue.close()
        case "ingest.rate_limited":
            harness = ingest.Harness(
                deps_overrides={"rate_limiter": FixedWindowRateLimiter(limit=1, window_seconds=60)}
            )
            harness.post(body)
            harness.post(body, nonce="nonce-000000000002")
            harness.queue.close()
        case _:  # pragma: no cover
            raise AssertionError(kind)
    return Planted(secrets=LEAD_SECRETS)


class _CredentialRow(NamedTuple):
    """The shape ``PostgresIngestCredentials.resolve`` reads off its one ``SELECT``."""

    slug: str
    status: str
    hmac_secret_ref: str | None
    key_hash: str
    revoked_at: None
    expires_at: None


class _ExplodingResolver:
    """A secret resolver whose store is down, and which says so with the ARN in the message.

    The realistic shape of the failure, and the reason the event exists: Secrets Manager
    throttling or a network blip, reported by a dependency whose error text we do not
    control. A lead's address is in the message too, because a wrapped exception really can
    quote whatever was in flight — which is what the formatter's last-resort net is for.
    """

    def resolve(self, secret_arn: str) -> str:
        raise RuntimeError(f"secretsmanager unavailable for {secret_arn} while {LEAD_EMAIL} waited")

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:  # pragma: no cover
        raise AssertionError(secret_arn)


def _credential_scenario(kind: str) -> Planted:
    """The two credential-path errors, over a canned ``tenant_api_keys`` row.

    They carry a tenant slug, a key id and — for the second — a Secrets Manager ARN. None of
    those is a lead's anything, and this is where that claim is checked rather than asserted:
    the lead's address is planted in the resolver's exception message, so the traceback path
    is swept as well as the fields.
    """
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
    from leadquali.adapters.store_tenants import PostgresIngestCredentials
    from tests.isolation.repositories import (
        KEY_ID_A,
        KEY_SECRET_A,
        SIGNING_SECRET_REF,
        TENANT_A,
        DictSecretResolver,
        api_key_for,
    )

    verifier = Argon2KeyHasher()
    capture = SqlCapture()
    resolver: Any = (
        _ExplodingResolver()
        if kind == "ingest.signing_secret_unavailable"
        else DictSecretResolver()
    )
    source = PostgresIngestCredentials(
        capture.sessions,
        verifier=verifier,
        resolver=resolver,
        now=lambda: NOW,
        last_used_coarseness=None,
    )
    row = _CredentialRow(
        slug=TENANT_A,
        status="active",
        hmac_secret_ref=(
            None if kind == "ingest.tenant_without_signing_secret" else SIGNING_SECRET_REF
        ),
        key_hash=verifier.hash_secret(KEY_SECRET_A),
        revoked_at=None,
        expires_at=None,
    )
    capture.sessions.reset([CannedResult(row=row)])
    source.resolve(tenant_id=TENANT_A, api_key=api_key_for(KEY_ID_A, KEY_SECRET_A))
    return Planted(secrets=LEAD_SECRETS)


def _admin_scenario(kind: str) -> Planted:
    """#36's surface, which renders payloads to a human by design.

    That is allowed. What is not allowed is one reaching a log line or an error page, so the
    scenarios that matter here are the ones holding a payload: the lead detail page, and the
    page that raises while rendering it.
    """
    harness = admin.Harness()
    secrets = tuple(str(value) for value in admin.A_PAYLOAD.values())
    match kind:
        case "admin.login_failed":
            harness.client(signed_in=False).post(
                LOGIN_PATH,
                data={"username": admin.STAFF, "password": "not the password"},
                follow_redirects=False,
            )
        case "admin.session_rejected":
            harness.client(signed_in=False).get("/admin/", follow_redirects=False)
        case "admin.csrf_rejected":
            harness.client().post(
                "/admin/review/promote",
                data={"tenant": admin.SLUG, "lead_id": "lead-0001", "expected_tier": "warm"},
                follow_redirects=False,
            )
        case "admin.config_changed":
            harness.client().post(
                f"/admin/tenants/{admin.SLUG}/config/apply",
                data=harness.form(
                    config=json.dumps(admin.config_document(min_confidence=0.75)),
                    note="tightening the confidence floor, for the sweep",
                ),
                follow_redirects=False,
            )
        case "admin.lead_promoted":
            harness.client().post(
                "/admin/review/promote",
                data=harness.form(
                    tenant=admin.SLUG,
                    lead_id="lead-0001",
                    expected_tier="warm",
                    note=admin.RATIONALE,
                ),
                follow_redirects=False,
            )
        case "admin.page_failed":

            def explode(*, tenant_slug: str, lead_id: str) -> Any:
                del tenant_slug, lead_id
                raise RuntimeError(f"could not decode row: {admin.A_PAYLOAD}")

            harness.queries.lead_detail = explode  # type: ignore[method-assign]
            response = harness.client().get(f"/admin/leads/lead-0001?tenant={admin.SLUG}")
            assert response.status_code == 500
            # The error *page* is swept too, not only the log: an echoed payload on a 500
            # is the same disclosure by a different route.
            for secret in secrets:
                assert secret not in response.text, "the error page echoed the lead"
        case _:  # pragma: no cover
            raise AssertionError(kind)
    return Planted(secrets=secrets)


def _rerun_scenario() -> Planted:
    """#36's re-run: every lead in a tenant, re-assessed against a candidate rubric."""
    harness = admin.Harness()
    harness.client().post(
        f"/admin/tenants/{admin.SLUG}/rerun",
        data=harness.form(
            config=json.dumps(
                admin.config_document(thresholds={"hot": 95.0, "warm": 55.0, "cold": 30.0})
            ),
            confirm="yes",
        ),
        follow_redirects=False,
    )
    return Planted(secrets=tuple(str(value) for value in admin.A_PAYLOAD.values()))


def _worker_scenario(kind: str) -> Planted:
    """#26's SQS handler, which is where a lead's whole payload arrives from the queue.

    The two most important scenarios in this file after the pipeline's own, because both of
    them are the *failure* paths of the one component that is handed a submission as its
    input. ``queue.qualify_failed`` logs with ``exception()``, so the traceback of whatever
    the pipeline raised — with the submission somewhere in the frames — is on the record.
    """
    message = QueuedLead(
        tenant_id=pipeline.TENANT,
        lead_id="3a5c9e10-0b47-4d2f-9c61-7e8a04b5d213",
        submission_id="sub-1",
        submission=pipeline.SUBMISSION,
        source="web_form",
        received_at=NOW,
        trace_id="0" * 32,
    ).to_message()
    if kind == "queue.undecodable_message":
        # Not decodable, and the body is the lead: the handler has to say "I could not read
        # message X" without quoting what it could not read.
        record = {"messageId": "msg-1", "body": json.dumps(message)[:60]}
        handle_batch({"Records": [record]}, None, pipeline=pipeline.build_pipeline()[0])
    else:
        built, _, _ = pipeline.build_pipeline()

        def explode(request: Any) -> Any:
            raise RuntimeError(f"the database went away while writing {request.submission!r}")

        # A double that raises with the submission in its message — the realistic shape of
        # a driver or serialiser failure, and the reason this scenario exists at all.
        built.qualify = explode  # type: ignore[method-assign]  # a leaky double
        handle_batch(
            {"Records": [{"messageId": "msg-1", "body": json.dumps(message)}]},
            None,
            pipeline=built,
        )
    return Planted(secrets=pipeline.SECRETS)


def _keyhash_scenario() -> Planted:
    """A ``tenant_api_keys`` row whose stored hash is not an argon2 string.

    A bad migration or a hand-edited row. The line carries the key id and must not carry the
    presented secret — which is the thing an over-helpful error message reaches for.
    """
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher

    secret = "a-presented-secret-that-must-not-be-logged"
    Argon2KeyHasher().verify_secret(key_id="a1a1a1a1a1a1a1a1", secret=secret, key_hash="not-argon2")
    return Planted(secrets=(secret,))


def _ratelimit_scenario() -> Planted:
    """The allowance source is down, and a paying customer's lead must not become a 500."""
    from leadquali.api.ratelimit import TenantRateLimiter

    class Unavailable:
        """A ``TenantRateLimitSource`` whose table is unreachable, loudly."""

        def rate_limit_for(self, tenant_id: str) -> Any:
            raise RuntimeError(f"the tenants table is unreachable; {LEAD_EMAIL} was waiting")

    TenantRateLimiter(Unavailable()).check(tenant_id=TENANT, now=NOW)
    return Planted(secrets=(LEAD_EMAIL,))


def _secrets_scenario(kind: str) -> Planted:
    """#28's provisioning lines, over ``moto``. No lead is reachable from any of them.

    Swept anyway, and by driving the real client rather than by assertion: these carry a
    secret **ARN** and a tenant slug, and the mistake worth catching is the day somebody
    puts the secret's value next to its name.
    """
    import boto3
    from moto import mock_aws

    from leadquali.adapters.secrets_manager import TenantSecretsProvisioner

    with mock_aws():
        client = boto3.client("secretsmanager", region_name="eu-west-1")
        provisioner = TenantSecretsProvisioner(client, environment="prod")
        arn = provisioner.create_tenant_hmac_secret(TENANT)
        if kind == "secrets.tenant_hmac_exists":
            provisioner.create_tenant_hmac_secret(TENANT)
        elif kind == "secrets.tenant_hmac_rotated":
            provisioner.rotate_tenant_hmac_secret(arn)
    return Planted(reason="a secret ARN and a tenant slug; no lead is reachable here")


def _secrets_refresh_scenario() -> Planted:
    """A cached secret whose refresh fails: serve the stale value, say so, name no value."""
    from leadquali.adapters.secrets_manager import SecretsManagerResolver

    class Flaky:
        """Answers once, then fails the way a throttled API does."""

        def __init__(self) -> None:
            self.calls = 0

        def get_secret_value(self, **kwargs: Any) -> dict[str, str]:
            del kwargs
            self.calls += 1
            if self.calls == 1:
                return {"SecretString": SECRET_VALUE}
            raise RuntimeError(f"throttled while {LEAD_EMAIL} waited")

    ticks = iter([0.0, 10_000.0, 10_000.0, 10_000.0, 10_000.0])
    resolver = SecretsManagerResolver(Flaky(), ttl_seconds=1, monotonic=lambda: next(ticks))
    arn = "arn:aws:secretsmanager:eu-west-1:0:secret:sweep"
    resolver.resolve(arn)
    resolver.resolve(arn)
    return Planted(secrets=(LEAD_EMAIL, SECRET_VALUE))


def _last_used_scenario() -> Planted:
    """Recording that a key was used must never fail a lead, and must name no secret."""
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
    from leadquali.adapters.store_tenants import PostgresIngestCredentials
    from tests.isolation.repositories import (
        KEY_ID_A,
        KEY_SECRET_A,
        SIGNING_SECRET_REF,
        TENANT_A,
        DictSecretResolver,
        api_key_for,
    )

    class Broken:
        """A session factory whose transaction fails on the way in."""

        def begin(self) -> Any:
            raise RuntimeError(f"the connection pool is exhausted; {LEAD_EMAIL} was waiting")

    verifier = Argon2KeyHasher()
    source = PostgresIngestCredentials(
        Broken(),  # type: ignore[arg-type]  # a session factory that only fails
        verifier=verifier,
        resolver=DictSecretResolver(),
        now=lambda: NOW,
    )
    source._touch(key_id=KEY_ID_A, tenant_slug=TENANT_A, now=NOW)
    del KEY_SECRET_A, SIGNING_SECRET_REF, api_key_for
    return Planted(secrets=(LEAD_EMAIL,))


# ------------------------------------------------------------------------ billing (#35)

#: The **customer's billing contact** — a different data subject from every lead above, and
#: the reason billing needs its own planted identity. A real Stripe invoice object carries
#: these three fields, and `stripe_events.payload` stores the body verbatim on purpose, so
#: they genuinely are in the frame of every handler below.
BILLING_NAME: Final[str] = "Grace Brewster Hopper"
BILLING_EMAIL: Final[str] = "grace.hopper+billing@analytical-engines-quali.co.uk"
BILLING_ADDRESS: Final[str] = "12 Marylebone High Street, London W1U 4PB"

BILLING_SECRETS: Final[tuple[str, ...]] = (BILLING_NAME, BILLING_EMAIL, BILLING_ADDRESS)


def _invoice_event(event_id: str, event_type: str, **overrides: Any) -> dict[str, Any]:
    """A Stripe event whose object carries the billing contact, the way a real one does.

    Built on top of #35's own ``stripe_event`` rather than beside it, so the shape this
    sweep exercises is the shape its tests exercise. The three contact fields are added
    because they are what makes the sweep mean anything: ``stripe_events.payload`` is one of
    the only two verbatim copies of somebody's data in the whole schema, and the question is
    whether any billing log line carries it.
    """
    event = stripe_event(event_id, event_type, **overrides)
    event["data"]["object"].update(
        {
            "customer_name": BILLING_NAME,
            "customer_email": BILLING_EMAIL,
            "customer_address": {"line1": BILLING_ADDRESS},
        }
    )
    return event


def _billing_harness(**kwargs: Any) -> Any:
    """#35's own webhook-route harness: the real service over in-memory doubles."""
    return webhooks.Harness(**kwargs)


def _billing_service() -> tuple[Any, Any]:
    """The billing service and its store, seeded with one linked tenant."""
    harness = _billing_harness()
    return harness.service, harness.store


#: The closed day the usage scenarios bill for. Yesterday, because a day that is not over
#: is never reported — which is #33's rule and is the reason a naive `today()` here would
#: make three scenarios silently emit nothing.
BILLED_DAY: Final[dt.date] = webhooks.NOW.date() - dt.timedelta(days=1)


def _billable_harness() -> Any:
    """A billing harness whose tenant has real billable usage on :data:`BILLED_DAY`.

    Seeded through the metering store's own rollup rather than by writing a number, so the
    quantity the meter event carries is the one #33's arithmetic produces.
    """
    harness = _billing_harness()
    moment = dt.datetime.combine(BILLED_DAY, dt.time(12, 0), tzinfo=dt.UTC)
    harness.metering_store.add_lead(tenant_id=webhooks.TENANT, received_at=moment)
    harness.metering_store.add_assessment(
        tenant_id=webhooks.TENANT, created_at=moment, input_tokens=512
    )
    harness.metering_store.rollup_day(tenant_id=webhooks.TENANT, day=BILLED_DAY)
    return harness


def _drain(service: Any, event: dict[str, Any]) -> None:
    service.receive_event(event_id=str(event["id"]), event_type=str(event["type"]), payload=event)
    service.process_pending()


def _billing_scenario(kind: str) -> Planted:
    """One billing event, driven through the real service with the contact in the payload."""
    service, store = _billing_service()
    customer = webhooks.CUSTOMER
    match kind:
        case "billing.webhook_received":
            event = _invoice_event("evt_1", "invoice.payment_succeeded", customer=customer)
            service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
        case "billing.event_ignored":
            _drain(service, _invoice_event("evt_1", "customer.created", customer=customer))
        case "billing.event_unattributed":
            # A Stripe account holds customers that are not our tenants. Nothing is wrong,
            # and the line must still not quote the invoice it could not attribute.
            _drain(service, _invoice_event("evt_1", "invoice.paid", customer="cus_stranger"))
        case "billing.invoice_settled":
            _drain(service, _invoice_event("evt_1", "invoice.paid", customer=customer))
        case "billing.invoice_settled_without_subscription":
            store.given_tenant(
                webhooks.TENANT,
                status=TenantStatus.SUSPENDED,
                stripe_customer_id=customer,
                stripe_subscription_id=None,
            )
            _drain(service, _invoice_event("evt_1", "invoice.paid", customer=customer))
        case "billing.dunning_started":
            _drain(service, _invoice_event("evt_1", "invoice.payment_failed", customer=customer))
        case "billing.dunning_continues":
            for index in (1, 2):
                _drain(
                    service,
                    _invoice_event(f"evt_{index}", "invoice.payment_failed", customer=customer),
                )
        case "billing.tenant_activated":
            store.given_tenant(
                webhooks.TENANT,
                status=TenantStatus.SUSPENDED,
                stripe_customer_id=customer,
                stripe_subscription_id="sub_acme",
            )
            _drain(
                service,
                _invoice_event(
                    "evt_1",
                    "customer.subscription.updated",
                    customer=customer,
                    subscription="sub_acme",
                    status="active",
                ),
            )
        case "billing.tenant_suspended":
            _drain(
                service,
                _invoice_event(
                    "evt_1",
                    "customer.subscription.deleted",
                    customer=customer,
                    subscription="sub_acme",
                    status="canceled",
                ),
            )
        case "billing.subscription_status_unknown":
            _drain(
                service,
                _invoice_event(
                    "evt_1",
                    "customer.subscription.updated",
                    customer=customer,
                    subscription="sub_acme",
                    status="a_status_stripe_invented_after_this_was_written",
                ),
            )
        case "billing.event_failed":
            # The store raises with the invoice in its message: the realistic shape of a
            # driver or serialiser failure, and the one path where the verbatim payload is
            # one `repr` away from the log.
            def explode(*, stripe_customer_id: str) -> Any:
                raise RuntimeError(f"could not read row for {stripe_customer_id}: {BILLING_EMAIL}")

            store.tenant_for_customer = explode  # a deliberately leaky double
            _drain(service, _invoice_event("evt_1", "invoice.paid", customer=customer))
        case "billing.customer_linked":
            # The billing contact's name and address are *arguments* to this call, which is
            # what makes it the most important billing line in this file.
            store.given_tenant(webhooks.TENANT, stripe_customer_id=None)
            service.link_customer(tenant_id=webhooks.TENANT, name=BILLING_NAME, email=BILLING_EMAIL)
        case "billing.usage_reported":
            _billable_harness().service.report_usage_for_day(
                tenant_id=webhooks.TENANT, usage_date=BILLED_DAY
            )
        case "billing.usage_too_old":
            service.report_usage_for_day(
                tenant_id=webhooks.TENANT,
                usage_date=webhooks.NOW.date() - dt.timedelta(days=365),
            )
        case "billing.usage_report_failed":
            billable = _billable_harness()

            def refuse(**kwargs: Any) -> Any:
                raise RuntimeError(f"stripe rejected the meter event for {BILLING_EMAIL}")

            # The processor raises with the contact in its message: an error string from a
            # dependency is exactly where personal data arrives without anybody deciding to
            # log one. Only the exception's *class* may reach the record.
            billable.billing.report_usage = refuse  # a deliberately leaky double
            billable.service.report_usage_for_all(usage_date=BILLED_DAY)
        case "billing.dunning_sweep":
            service.sweep_dunning()
        case _:  # pragma: no cover
            raise AssertionError(kind)
    return Planted(secrets=BILLING_SECRETS)


def _billing_job_scenario(kind: str) -> Planted:
    """The three scheduled billing jobs' own summary lines."""
    harness = _billable_harness()
    event = _invoice_event("evt_1", "invoice.paid", customer=webhooks.CUSTOMER)
    harness.service.receive_event(event_id=event["id"], event_type=event["type"], payload=event)
    if kind == "billing.drain":
        drain_events(harness.service)
    else:
        report_usage(harness.service, usage_date=BILLED_DAY)
    return Planted(secrets=BILLING_SECRETS)


def _billing_route_scenario(kind: str) -> Planted:
    """The public billing endpoints: the Stripe webhook, and a tenant's portal request."""
    match kind:
        case "billing.webhook_rejected":
            harness = _billing_harness()
            body = json.dumps(
                _invoice_event("evt_1", "invoice.paid", customer=webhooks.CUSTOMER)
            ).encode()
            # A forged signature over a real body: the rejection must say why without
            # quoting the body it refused to trust.
            harness.post_webhook(body, secret="whsec_not_the_configured_one")
        case "billing.portal_opened":
            _billing_harness().post_portal()
        case "billing.portal_rejected":
            _billing_harness().post_portal(tenant="nobody-at-all")
        case "billing.portal_unavailable":
            harness = _billing_harness()
            harness.store.given_tenant(webhooks.TENANT, stripe_customer_id=None)
            harness.post_portal()
        case _:  # pragma: no cover
            raise AssertionError(kind)
    return Planted(secrets=BILLING_SECRETS)


#: Every event that carries fields, and how to make the real code emit it with a lead in
#: scope. A discovered event missing from here fails the accounting test by name.
SCENARIOS: Final[Mapping[str, Scenario]] = {
    "lead.accepted": lambda: _pipeline_scenario("routed"),
    "assessment.completed": lambda: _pipeline_scenario("routed"),
    "lead.routed": lambda: _pipeline_scenario("routed"),
    "lead.suppressed": _suppressed_scenario,
    "lead.duplicate": lambda: _pipeline_scenario("duplicate"),
    "lead.dispatch_failed": lambda: _pipeline_scenario("dispatch_failed"),
    "tenant.quota_crossed": _quota_scenario,
    "http.ingest": lambda: _ingest_scenario("http.ingest"),
    "ingest.rejected": lambda: _ingest_scenario("ingest.rejected"),
    "ingest.rate_limited": lambda: _ingest_scenario("ingest.rate_limited"),
    "admin.login_failed": lambda: _admin_scenario("admin.login_failed"),
    "admin.session_rejected": lambda: _admin_scenario("admin.session_rejected"),
    "admin.config_changed": lambda: _admin_scenario("admin.config_changed"),
    "admin.lead_promoted": lambda: _admin_scenario("admin.lead_promoted"),
    "admin.page_failed": lambda: _admin_scenario("admin.page_failed"),
    # Fieldless until #36's review fixes added `method`; the AST check moved it here, which
    # is the mechanism working rather than a nuisance.
    "admin.csrf_rejected": lambda: _admin_scenario("admin.csrf_rejected"),
    "admin.rerun_completed": _rerun_scenario,
    "retention.purged": lambda: _retention_scenario("purged"),
    "retention.erased": lambda: _retention_scenario("erased"),
    "retention.run": _retention_run_scenario,
    "ingest.tenant_without_signing_secret": lambda: _credential_scenario(
        "ingest.tenant_without_signing_secret"
    ),
    "ingest.signing_secret_unavailable": lambda: _credential_scenario(
        "ingest.signing_secret_unavailable"
    ),
    "ingest.last_used_not_recorded": _last_used_scenario,
    "queue.undecodable_message": lambda: _worker_scenario("queue.undecodable_message"),
    "queue.qualify_failed": lambda: _worker_scenario("queue.qualify_failed"),
    "keyhash.unreadable_stored_hash": _keyhash_scenario,
    "ratelimit.limits_unavailable": _ratelimit_scenario,
    "secrets.tenant_hmac_created": lambda: _secrets_scenario("secrets.tenant_hmac_created"),
    "secrets.tenant_hmac_exists": lambda: _secrets_scenario("secrets.tenant_hmac_exists"),
    "secrets.tenant_hmac_rotated": lambda: _secrets_scenario("secrets.tenant_hmac_rotated"),
    "secrets.refresh_failed": _secrets_refresh_scenario,
    # #35's billing surface. The planted identity here is the *customer's billing contact*
    # rather than a lead, because that is whose data `stripe_events.payload` holds.
    "billing.webhook_received": lambda: _billing_scenario("billing.webhook_received"),
    "billing.event_ignored": lambda: _billing_scenario("billing.event_ignored"),
    "billing.event_unattributed": lambda: _billing_scenario("billing.event_unattributed"),
    "billing.event_failed": lambda: _billing_scenario("billing.event_failed"),
    "billing.invoice_settled": lambda: _billing_scenario("billing.invoice_settled"),
    "billing.invoice_settled_without_subscription": lambda: _billing_scenario(
        "billing.invoice_settled_without_subscription"
    ),
    "billing.dunning_started": lambda: _billing_scenario("billing.dunning_started"),
    "billing.dunning_continues": lambda: _billing_scenario("billing.dunning_continues"),
    "billing.dunning_sweep": lambda: _billing_scenario("billing.dunning_sweep"),
    "billing.tenant_activated": lambda: _billing_scenario("billing.tenant_activated"),
    "billing.tenant_suspended": lambda: _billing_scenario("billing.tenant_suspended"),
    "billing.subscription_status_unknown": lambda: _billing_scenario(
        "billing.subscription_status_unknown"
    ),
    "billing.customer_linked": lambda: _billing_scenario("billing.customer_linked"),
    "billing.usage_reported": lambda: _billing_scenario("billing.usage_reported"),
    "billing.usage_too_old": lambda: _billing_scenario("billing.usage_too_old"),
    "billing.usage_report_failed": lambda: _billing_scenario("billing.usage_report_failed"),
    "billing.drain": lambda: _billing_job_scenario("billing.drain"),
    "billing.usage_run": lambda: _billing_job_scenario("billing.usage_run"),
    "billing.webhook_rejected": lambda: _billing_route_scenario("billing.webhook_rejected"),
    "billing.portal_opened": lambda: _billing_route_scenario("billing.portal_opened"),
    "billing.portal_rejected": lambda: _billing_route_scenario("billing.portal_rejected"),
    "billing.portal_unavailable": lambda: _billing_route_scenario("billing.portal_unavailable"),
}


#: Events whose call sites pass no fields and whose message is a literal, so there is
#: nothing on the record that could carry a lead. Each entry says why it is allowed to be
#: here; the claim itself is checked against the AST by
#: :func:`test_a_fieldless_event_really_carries_no_fields`, so adding a field to one of
#: these fails the suite and asks for a scenario instead.
FIELDLESS_EVENTS: Final[Mapping[str, str]] = {
    "migrate.start": "the migration Lambda's bookends. No tenant is in scope at all.",
    "migrate.done": "the same.",
}


# ----------------------------------------------------------------------------- the sweep


def test_the_discovery_found_the_events_it_should_have() -> None:
    """Guards against the AST walk selecting nothing and every test below passing.

    The two named events are from opposite ends of the codebase and use the two different
    spellings the walk has to understand, so a regression in either arm fails here.
    """
    assert len(EMITTED) >= 15, sorted(EMITTED)
    assert "lead.routed" in EMITTED
    assert "migrate.start" in EMITTED, "the `extra={'event': …}` spelling was not discovered"


def test_every_emitted_event_is_accounted_for() -> None:
    """The mechanism. A new event is covered, declared fieldless, or this fails by name."""
    accounted = set(SCENARIOS) | set(FIELDLESS_EVENTS)
    missing = sorted(set(EMITTED) - accounted)

    assert not missing, (
        f"these events are emitted and nothing sweeps them for personal data: {missing}.\n"
        "Add a scenario to SCENARIOS that drives the real code path with a lead in scope, "
        "or — only if the call site passes no fields at all — an entry in FIELDLESS_EVENTS "
        "saying why that is true."
    )


def test_no_scenario_outlives_its_event() -> None:
    """The other direction: a scenario for an event nobody emits is dead weight."""
    stale = sorted((set(SCENARIOS) | set(FIELDLESS_EVENTS)) - set(EMITTED))

    assert not stale, f"these events are swept and no longer emitted: {stale}"


@pytest.mark.parametrize("event", sorted(FIELDLESS_EVENTS))
def test_a_fieldless_event_really_carries_no_fields(event: str) -> None:
    """The claim is checked, not taken.

    Being in :data:`FIELDLESS_EVENTS` is a statement about the call site — no fields, and a
    constant message — and the day somebody adds a field to one of these, that statement
    stops being true. Failing here is what sends them to write a scenario.
    """
    emission = EMITTED[event]

    assert not emission.fields, (
        f"{event} is declared fieldless and its call site in {emission.module} now passes "
        f"{sorted(emission.fields)}. Move it to SCENARIOS and sweep it."
    )
    assert emission.literal_message, (
        f"{event}'s message in {emission.module} is built rather than constant, so it can "
        "carry whatever was interpolated into it."
    )


@pytest.mark.parametrize("event", sorted(SCENARIOS))
def test_no_event_carries_personal_data(event: str) -> None:
    """Run the real code path that emits this event, and search everything it wrote.

    Against the raw output rather than the parsed records, so an address hiding in a key, in
    a nested object or inside an escaped traceback string is still caught.
    """
    with capture_json_logs() as logs:
        planted = SCENARIOS[event]()

    assert logs.text, f"{event}: nothing was logged, so this test proves nothing"
    assert logs.events(event), (
        f"{event}: the scenario ran and did not emit it. A scenario that emits the wrong "
        "event sweeps the wrong records."
    )
    _assert_clean(logs, planted, event)


def _assert_clean(logs: LogCapture, planted: Planted, event: str) -> None:
    """No planted string, and nothing address-shaped, anywhere in the output."""
    blob = logs.text
    for secret in planted.secrets:
        assert secret not in blob, f"{event} run leaked {secret!r} into the logs"
    # Nothing even *tried* to log an address: the formatter's net never had to fire. This is
    # the assertion that keeps the net from becoming a licence to be careless upstream.
    assert EMAIL_REDACTION not in blob, (
        f"{event} run: a call site tried to log an address and the formatter caught it. "
        "That is a bug at the call site, not a success."
    )
    for record in logs.records():
        leaked = [
            found
            for found in _ADDRESS_RE.findall(str(record))
            # A tenant's own configured sales inbox is not a lead's address and is
            # legitimately a routing destination; it is logged as a hash, so anything
            # matching here is a real find.
            if found
        ]
        assert not leaked, f"{event}: record {record.get('event')!r} carries {leaked}"


# ------------------------------------------------------------- the published contract


def _documented_events() -> frozenset[str]:
    """Event names appearing in ``docs/observability.md``'s tables, as backticked cells."""
    text = OBSERVABILITY_DOC.read_text(encoding="utf-8")
    # Anchored at the start of a line, so only the first cell of a row is read: the second
    # column holds the emitting module (`api.migrate`), which is the same shape and is not
    # an event name.
    return frozenset(re.findall(r"(?m)^\|\s*`([a-z][a-z_]*\.[a-z_]+)`\s*\|", text))


def test_every_emitted_event_is_in_the_published_contract() -> None:
    """``docs/observability.md`` is written for people outside this file.

    #29 writes alarms against those names and a runbook's saved Logs Insights query filters
    on them, so an event that exists and is not in the table is a gap in a published
    contract — and, in practice, the event nobody thought about when they wrote it.
    """
    undocumented = sorted(set(EMITTED) - _documented_events())

    assert not undocumented, (
        f"these events are emitted and absent from docs/observability.md: {undocumented}"
    )


def test_the_documentation_describes_no_event_that_does_not_exist() -> None:
    """A table naming an event nothing emits sends somebody to build an alarm on silence."""
    invented = sorted(_documented_events() - set(EMITTED))

    assert not invented, f"docs/observability.md names events nothing emits: {invented}"


def _iter_emissions() -> Iterator[Emission]:
    yield from EMITTED.values()


def test_no_event_field_is_named_after_an_address() -> None:
    """A cheap structural check on top of the behavioural sweep.

    It catches the mistake at the moment it is written rather than when a scenario happens
    to exercise it: a field called ``email``, ``contact`` or ``address`` is either a leak or
    a name that will be read as one by whoever inherits it.
    """
    banned = {"email", "contact_email", "address", "contact", "recipient", "sender"}
    for emission in _iter_emissions():
        offending = emission.fields & banned
        assert not offending, (
            f"{emission.event} in {emission.module} has field(s) {sorted(offending)}. "
            "Log contact_email_hash, never the address (CLAUDE.md invariant 5)."
        )
