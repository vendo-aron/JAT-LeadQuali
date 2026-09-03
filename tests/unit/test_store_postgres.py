"""The Postgres adapter's offline half.

Everything here is true without a database: the identity mapping between port-level ids and
the UUIDs the tables key on, the contact-email hash, the column values a successful and a
failed assessment produce, and three structural properties that a running database would
never reveal — that no method is reachable without a tenant scope, that importing the
module opens no connection, and that no SQL is assembled from strings.

The behavioural half — that Postgres accepts these rows, that the upsert is idempotent
under a real race — is ``tests/integration/test_store_postgres.py``, which needs a server.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import null
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from leadquali.adapters import store_postgres
from leadquali.adapters.db_schema import (
    ASSESSMENT_STATUSES,
    ESCALATION_REASONS,
    LEAD_STATUSES,
)
from leadquali.adapters.seed import tenant_id_for
from leadquali.adapters.store_postgres import (
    ASSESSMENT_STATUS_FAILED,
    ASSESSMENT_STATUS_OK,
    LEAD_STATUS_FAILED,
    LEAD_STATUS_QUALIFIED,
    LEAD_STATUS_ROUTED,
    UNKNOWN_MODEL_ID,
    UNKNOWN_PROMPT_VERSION,
    PostgresLeadStore,
    PostgresTenantConfigSource,
    assessment_values,
    contact_email_hash,
    engine_for,
    lead_uuid,
    session_factory,
    tenant_uuid,
)
from leadquali.app.ports import RoutingOutcome
from leadquali.domain.models import Action, EscalationReason, Tier
from tests.contract.lead_store_contract import (
    NOW,
    make_decision,
    make_failed,
    make_metering,
    make_submission,
    make_succeeded,
)

MODULE_PATH = Path(store_postgres.__file__)

#: A URL that is syntactically valid and points nowhere. ``create_engine`` does not
#: connect, so an engine can be built from it and inspected without a server.
NOWHERE_URL = "postgresql+psycopg://leadquali:leadquali@127.0.0.1:5439/nowhere"


def unbound_store() -> PostgresLeadStore:
    """A store whose sessions are never opened.

    Every argument check in the adapter happens before a session is asked for, which is
    what makes it testable this way — and is also the right order: rejecting a malformed
    tenant id should not cost a round trip.
    """
    return PostgresLeadStore(sessionmaker())


# ------------------------------------------------------------------------ identities


def test_a_tenant_slug_resolves_to_the_id_the_seed_script_writes() -> None:
    """One mapping, imported rather than restated. Two spellings of "which row is this
    tenant" is how a lead ends up filed under a tenant that does not exist."""
    assert tenant_uuid("default") == tenant_id_for("default")


def test_a_tenant_uuid_is_taken_as_the_row_id() -> None:
    row_id = uuid.uuid4()
    assert tenant_uuid(str(row_id)) == row_id


@pytest.mark.parametrize(
    "tenant_id",
    ["", "../../etc/passwd", "Default", "tenant id", "'; drop table leads; --", "x" * 64],
)
def test_a_tenant_id_that_is_neither_a_uuid_nor_a_slug_is_refused(tenant_id: str) -> None:
    """Tenant ids arrive from queue messages and request payloads, so they are checked
    before they reach a query rather than after."""
    with pytest.raises(ValueError, match="neither a UUID nor a valid slug"):
        tenant_uuid(tenant_id)


def test_a_lead_id_must_be_a_uuid() -> None:
    with pytest.raises(ValueError, match="not a UUID"):
        lead_uuid("lead-0001")


def test_the_email_hash_is_over_the_normalised_address() -> None:
    """``Ada@Example.com`` and ``ada@example.com`` are one person, and correlating their
    leads without ever logging the address is the whole reason the column exists."""
    expected = hashlib.sha256(b"ada@example.com").hexdigest()
    assert contact_email_hash("  Ada@Example.COM ") == expected
    assert contact_email_hash("ada@example.com") == expected


@pytest.mark.parametrize("email", [None, "", "   "])
def test_a_missing_address_hashes_to_nothing(email: str | None) -> None:
    """A form is a form: a submission with no address must still be storable."""
    assert contact_email_hash(email) is None


def test_the_hash_is_not_the_address() -> None:
    """Invariant 5, stated as the property that matters: nothing recognisable survives."""
    digest = contact_email_hash("ada@example.com")
    assert digest is not None
    assert "ada" not in digest
    assert "example.com" not in digest
    assert len(digest) == 64


# ------------------------------------------------------------------- assessment rows


def test_a_successful_assessment_carries_every_model_output_column() -> None:
    values = assessment_values(
        outcome=make_succeeded(confidence=0.815),
        decision=make_decision(tier=Tier.HOT, total_score=88.25),
    )
    assert values["status"] == ASSESSMENT_STATUS_OK
    assert values["tier"] == "hot"
    assert values["total_score"] == Decimal("88.25")
    assert values["confidence"] == Decimal("0.815")
    assert values["dimension_scores"]["icp_fit"] == 24
    assert values["extracted"]["company_name"] == "Analytical Engines"
    assert values["reasoning"]
    assert values["missing_information"] == ["budget"]
    assert values["escalation_reason"] is None


def test_a_failed_assessment_carries_none_of_them_and_says_why() -> None:
    """The shape ``ck_assessments_output_present_iff_ok`` enforces: all present, or all
    absent with a reason. A half-written row would let the feedback loop average over
    assessments that never happened."""
    values = assessment_values(
        outcome=make_failed(reason=EscalationReason.API_ERROR, latency_ms=1_234),
        decision=make_decision(escalation_reason=EscalationReason.API_ERROR, total_score=0.0),
    )
    assert values["status"] == ASSESSMENT_STATUS_FAILED
    assert values["escalation_reason"] == "api_error"
    assert values["tier"] is None
    assert values["total_score"] is None
    assert values["reasoning"] is None
    assert values["confidence"] is None
    assert values["missing_information"] == []


def test_the_jsonb_columns_of_a_failure_are_sql_null_not_json_null() -> None:
    """The bug this test exists for: a Python ``None`` bound to a JSONB column is sent as
    the JSON value ``null``, which satisfies ``IS NOT NULL`` — so the row is rejected by
    the CHECK, and only a real database says so. ``null()`` is a SQL NULL."""
    values = assessment_values(
        outcome=make_failed(),
        decision=make_decision(escalation_reason=EscalationReason.API_ERROR),
    )
    assert values["dimension_scores"] is null()
    assert values["extracted"] is null()


def test_an_unbilled_failure_records_sentinels_and_the_time_it_wasted() -> None:
    """A call that never reached the model has no provenance to report, and the columns are
    NOT NULL. It still took time, and how long is the operator's first question."""
    values = assessment_values(
        outcome=make_failed(reason=EscalationReason.TIMEOUT, latency_ms=30_000),
        decision=make_decision(escalation_reason=EscalationReason.TIMEOUT),
    )
    assert values["model_id"] == UNKNOWN_MODEL_ID
    assert values["prompt_version"] == UNKNOWN_PROMPT_VERSION
    assert values["effort"] is None
    assert values["input_tokens"] == 0
    assert values["cost_usd"] == Decimal(0)
    assert values["latency_ms"] == 30_000


def test_a_billed_failure_is_metered_like_a_success() -> None:
    """A refusal is an HTTP 200 and it is charged for; so is a ``max_tokens`` truncation.
    Billing is a SUM over this table, and a failure left out of it is money nobody sees."""
    values = assessment_values(
        outcome=make_failed(
            reason=EscalationReason.MODEL_REFUSAL, metering=make_metering(latency_ms=1_500)
        ),
        decision=make_decision(escalation_reason=EscalationReason.MODEL_REFUSAL),
    )
    assert values["status"] == ASSESSMENT_STATUS_FAILED
    assert values["model_id"] == "claude-opus-5"
    assert values["prompt_version"] == "rubric_v1"
    assert values["effort"] == "medium"
    assert values["cost_usd"] == Decimal("0.012345")
    assert values["latency_ms"] == 1_500


def test_a_low_confidence_escalation_is_still_a_successful_assessment() -> None:
    values = assessment_values(
        outcome=make_succeeded(confidence=0.2),
        decision=make_decision(escalation_reason=EscalationReason.LOW_CONFIDENCE),
    )
    assert values["status"] == ASSESSMENT_STATUS_OK
    assert values["escalation_reason"] == "low_confidence"
    assert values["confidence"] == Decimal("0.200")


def test_scores_land_on_the_scale_of_their_numeric_columns() -> None:
    """``Numeric`` is what keeps SUM and AVG over the column exact; going through the
    binary float would put the imprecision straight back."""
    values = assessment_values(
        outcome=make_succeeded(confidence=0.1),
        decision=make_decision(total_score=62.5),
    )
    assert values["total_score"] == Decimal("62.50")
    assert values["total_score"].as_tuple().exponent == -2
    assert values["confidence"].as_tuple().exponent == -3


def test_every_status_this_adapter_writes_is_one_the_database_accepts() -> None:
    """The CHECK vocabularies are literals in the DDL; this is what stops the adapter and
    the schema from drifting into a constraint violation nobody sees until a lead fails."""
    assert {ASSESSMENT_STATUS_OK, ASSESSMENT_STATUS_FAILED} <= set(ASSESSMENT_STATUSES)
    assert {LEAD_STATUS_QUALIFIED, LEAD_STATUS_FAILED, LEAD_STATUS_ROUTED} <= set(LEAD_STATUSES)
    assert {reason.value for reason in EscalationReason} <= set(ESCALATION_REASONS)


@pytest.mark.parametrize("reason", list(EscalationReason))
def test_every_escalation_reason_the_domain_defines_can_be_recorded(
    reason: EscalationReason,
) -> None:
    if reason is EscalationReason.LOW_CONFIDENCE:
        values = assessment_values(
            outcome=make_succeeded(), decision=make_decision(escalation_reason=reason)
        )
    else:
        values = assessment_values(
            outcome=make_failed(reason=reason), decision=make_decision(escalation_reason=reason)
        )
    assert values["escalation_reason"] == reason.value


# ------------------------------------------------------ guards that need no database


@pytest.mark.parametrize(
    ("action", "outcome"),
    [
        (Action.EMAIL_SALES, RoutingOutcome.SUPPRESSED),
        (Action.ESCALATE_HUMAN, RoutingOutcome.SUPPRESSED),
        (Action.SUPPRESS, RoutingOutcome.DISPATCHED),
        (Action.SUPPRESS, RoutingOutcome.FAILED),
    ],
)
def test_an_action_and_outcome_that_disagree_about_suppression_are_refused(
    action: Action, outcome: RoutingOutcome
) -> None:
    """``routing_events`` has no outcome column, so suppression is encoded *as* the action.
    A row where the two disagree would be read back wrongly by ``already_routed``, so it is
    refused before any session is opened."""
    with pytest.raises(ValueError, match="disagree"):
        unbound_store().record_routing_event(
            tenant_id="tenant-a",
            lead_id=str(uuid.uuid4()),
            action=action,
            destination=None,
            outcome=outcome,
            provider_message_id=None,
            occurred_at=NOW,
            detail="nonsense",
        )


@pytest.mark.parametrize(
    ("action", "outcome"),
    [
        (Action.EMAIL_SALES, RoutingOutcome.DISPATCHED),
        (Action.ESCALATE_HUMAN, RoutingOutcome.FAILED),
        (Action.SUPPRESS, RoutingOutcome.SUPPRESSED),
    ],
)
def test_the_combinations_the_pipeline_actually_produces_are_accepted(
    action: Action, outcome: RoutingOutcome
) -> None:
    """The guard above must not reject anything ``app/qualify.py`` can emit: it gets past
    the check and only then fails on the unbound session."""
    with pytest.raises(Exception) as caught:
        unbound_store().record_routing_event(
            tenant_id="tenant-a",
            lead_id=str(uuid.uuid4()),
            action=action,
            destination="sales@example.invalid" if action is not Action.SUPPRESS else None,
            outcome=outcome,
            provider_message_id=None,
            occurred_at=NOW,
            detail="fine",
        )
    assert "disagree" not in str(caught.value)


def test_a_blank_submission_id_is_refused_without_a_round_trip() -> None:
    with pytest.raises(ValueError, match="submission_id"):
        unbound_store().upsert_lead(
            tenant_id="tenant-a",
            submission_id="",
            submission=make_submission(),
            source="web_form",
            received_at=NOW,
        )


# ------------------------------------------------------------- structural properties


def port_methods(cls: type) -> dict[str, Any]:
    """Public instance methods, excluding the ``from_*`` constructors."""
    return {
        name: member
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_") and not name.startswith("from_")
    }


def test_no_store_method_is_reachable_without_a_tenant() -> None:
    """Invariant 4, checked as a property of the class rather than of the queries.

    Not one method — including a convenience getter someone adds next year, since this
    enumerates them rather than listing them — may be callable without naming the tenant,
    because #32's isolation suite attacks exactly the method that forgot.
    """
    methods = port_methods(PostgresLeadStore)
    assert set(methods) == {
        "upsert_lead",
        "already_routed",
        "record_assessment",
        "record_routing_event",
    }
    for name, method in methods.items():
        parameters = inspect.signature(method).parameters
        assert "tenant_id" in parameters, f"{name} has no tenant scope"
        assert parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_config_source_is_also_tenant_scoped() -> None:
    methods = port_methods(PostgresTenantConfigSource)
    assert set(methods) == {"get"}
    assert "tenant_id" in inspect.signature(methods["get"]).parameters


def _calls_executed_at_import(module: ast.Module) -> list[ast.Call]:
    """Every call that runs when the module is imported.

    Function bodies are skipped — that is where the engine is *supposed* to be built, on
    first use — while class bodies and module-level statements are not, because those do
    run at import.
    """
    calls: list[ast.Call] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(child, ast.Call):
                calls.append(child)
            visit(child)

    visit(module)
    return calls


def test_importing_the_module_creates_no_engine() -> None:
    """A Lambda imports this at cold start, possibly before configuration is resolved. An
    engine built at module scope would open a socket there — or fail there, in a place
    where the traceback has no request to attach itself to."""
    for call in _calls_executed_at_import(ast.parse(MODULE_PATH.read_text(encoding="utf-8"))):
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        assert name not in {"create_engine", "sessionmaker"}, (
            f"line {call.lineno}: an engine is created at import time"
        )


def test_no_sql_is_assembled_from_strings() -> None:
    """Acceptance criterion: no SQL string interpolation of user input anywhere. Every
    statement is a Core construct, so ids and payloads travel as bound parameters; the one
    textual fragment in the module is a constant with no input in it."""
    module = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    textual = {"text", "literal_column", "column", "table"}
    found = 0
    for call in (n for n in ast.walk(module) if isinstance(n, ast.Call)):
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        if name not in textual:
            continue
        found += 1
        first = call.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"line {call.lineno}: {name}() is given something other than a literal string"
        )
    assert found == 1, "expected exactly the (xmax = 0) fragment; a new one needs a look"


def test_the_engine_is_shaped_for_a_lambda_container() -> None:
    """One connection per container, pre-pinged, recycled before any proxy idle timeout —
    which also makes reserved concurrency in #27 the connection budget, with no second
    number to keep in step."""
    engine = engine_for(NOWHERE_URL)
    assert isinstance(engine.pool, QueuePool)
    assert engine.pool.size() == 1
    # Private attributes because SQLAlchemy exposes no public reader for either, and both
    # are load-bearing enough to be worth asserting on.
    assert engine.pool._max_overflow == 0
    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == store_postgres.DEFAULT_POOL_RECYCLE_SECONDS


def test_one_engine_and_one_session_factory_per_url() -> None:
    """A warm container reuses its connection; a second engine would mean a second
    connection doing nothing but holding a backend slot."""
    assert engine_for(NOWHERE_URL) is engine_for(NOWHERE_URL)
    factory = session_factory(NOWHERE_URL)
    assert session_factory(NOWHERE_URL) is factory
    assert factory.kw["bind"] is engine_for(NOWHERE_URL)


def test_a_store_can_be_built_from_a_url_without_connecting() -> None:
    store = PostgresLeadStore.from_url(NOWHERE_URL)
    assert isinstance(store, PostgresLeadStore)
    assert isinstance(PostgresTenantConfigSource.from_url(NOWHERE_URL), PostgresTenantConfigSource)


def test_the_sessions_the_store_is_given_are_the_ones_it_uses() -> None:
    """The factory is a constructor argument, not a global: the entrypoint wires one per
    container and the integration tests bind one to a transaction they roll back."""
    factory: sessionmaker[Session] = sessionmaker()
    assert PostgresLeadStore(factory)._sessions is factory
