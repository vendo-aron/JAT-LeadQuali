"""The logging and metrics primitives, asserted on parsed JSON rather than on strings.

Every assertion here goes through ``json.loads`` of a real handler's output. Asserting on
a formatted string would pass on a line CloudWatch cannot parse, which is the only thing
that actually matters about this module: an alarm can only fire on a metric that was
emitted in a shape the platform recognises.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from leadquali.adapters.store_postgres import contact_email_hash as store_hash
from leadquali.config import Environment, Settings
from leadquali.observability import (
    EMAIL_REDACTION,
    LOG_FORMAT_HUMAN,
    LOG_FORMAT_JSON,
    Metric,
    MetricPayload,
    MetricSet,
    Unit,
    configure_logging,
    contact_email_hash,
    current_trace_id,
    ensure_trace_id,
    log_context,
    log_event,
    new_trace_id,
    redact_emails,
)
from leadquali.observability.metrics import METRIC_NAMESPACE, to_emf

LOGGER = logging.getLogger("leadquali.tests.observability")


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """Give every test the root logger back exactly as it found it.

    ``configure_logging`` takes the root logger over on purpose (see its docstring), and
    pytest's own capture handlers live there. Restoring is what keeps this file from
    silently switching off logging for the rest of the session.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def settings_for(env: Environment, level: str = "INFO") -> Settings:
    return Settings(env=env, log_level=level)  # type: ignore[arg-type]  # narrowed by validator


def records(buffer: io.StringIO) -> list[dict[str, Any]]:
    """Every emitted line, parsed. One JSON object per line is the contract."""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def configured(env: Environment = Environment.PROD, level: str = "INFO") -> io.StringIO:
    buffer = io.StringIO()
    configure_logging(settings_for(env, level), stream=buffer)
    return buffer


# ------------------------------------------------------------------ configure_logging


def test_json_lines_are_one_object_each() -> None:
    buffer = configured()
    LOGGER.info("first")
    LOGGER.info("second")

    assert [record["message"] for record in records(buffer)] == ["first", "second"]


def test_configure_logging_twice_does_not_duplicate_records() -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(settings_for(Environment.PROD), stream=first)
    configure_logging(settings_for(Environment.PROD), stream=second)

    LOGGER.info("only once")

    assert records(first) == []
    assert [record["message"] for record in records(second)] == ["only once"]
    assert len(logging.getLogger().handlers) == 1


def test_log_level_comes_from_settings() -> None:
    buffer = configured(level="WARNING")

    LOGGER.info("dropped")
    LOGGER.warning("kept")

    assert [record["message"] for record in records(buffer)] == ["kept"]


def test_local_environment_gets_human_readable_lines() -> None:
    buffer = io.StringIO()
    handler = configure_logging(settings_for(Environment.LOCAL), stream=buffer)

    log_event(LOGGER, "lead.routed", tenant_id="acme", tier="hot")

    line = buffer.getvalue().strip()
    assert handler.formatter is not None
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "lead.routed" in line
    assert "tenant_id=acme" in line
    assert "tier=hot" in line


def test_format_can_be_forced_independently_of_the_environment() -> None:
    buffer = io.StringIO()
    configure_logging(settings_for(Environment.LOCAL), stream=buffer, log_format=LOG_FORMAT_JSON)

    LOGGER.info("json please")

    assert records(buffer)[0]["message"] == "json please"


def test_human_format_is_the_local_default_and_json_the_deployed_one() -> None:
    assert LOG_FORMAT_HUMAN != LOG_FORMAT_JSON


# ------------------------------------------------------------------------ field set


def test_record_carries_the_standard_field_set() -> None:
    buffer = configured(env=Environment.STAGING)

    log_event(LOGGER, "lead.routed", tenant_id="acme", lead_id="lead-1", tier="hot")

    record = records(buffer)[0]
    assert record["event"] == "lead.routed"
    assert record["level"] == "INFO"
    assert record["logger"] == LOGGER.name
    assert record["service"] == "leadquali"
    assert record["env"] == "staging"
    assert record["message"] == "lead.routed"
    assert record["tenant_id"] == "acme"
    assert record["tier"] == "hot"
    assert record["timestamp"].endswith("Z")


def test_plain_library_records_still_produce_the_core_fields() -> None:
    """A uvicorn or botocore line has no ``event``; it must still be one JSON object."""
    buffer = configured()

    logging.getLogger("uvicorn.error").warning("listening on %s", "0.0.0.0:8000")

    record = records(buffer)[0]
    assert record["message"] == "listening on 0.0.0.0:8000"
    assert record["logger"] == "uvicorn.error"
    assert "event" not in record


def test_none_valued_fields_are_dropped_rather_than_emitted_as_null() -> None:
    buffer = configured()

    log_event(LOGGER, "lead.routed", tenant_id="acme", escalation_reason=None)

    record = records(buffer)[0]
    assert "escalation_reason" not in record


# --------------------------------------------------------------------------- context


def test_new_trace_id_is_32_lowercase_hex() -> None:
    trace_id = new_trace_id()
    assert len(trace_id) == 32
    assert trace_id == trace_id.lower()
    assert int(trace_id, 16) >= 0
    assert new_trace_id() != trace_id


def test_bound_context_rides_on_every_record_inside_the_block() -> None:
    buffer = configured()

    with log_context(trace_id="trace-1", tenant_id="acme"):
        LOGGER.info("inside")
    LOGGER.info("outside")

    inside, outside = records(buffer)
    assert inside["trace_id"] == "trace-1"
    assert inside["tenant_id"] == "acme"
    assert "trace_id" not in outside


def test_nested_context_extends_and_then_restores() -> None:
    with log_context(trace_id="trace-1"):
        with log_context(tenant_id="acme"):
            assert current_trace_id() == "trace-1"
        assert current_trace_id() == "trace-1"
    assert current_trace_id() is None


def test_explicit_event_fields_win_over_the_bound_context() -> None:
    buffer = configured()

    with log_context(trace_id="trace-1", tenant_id="acme"):
        log_event(LOGGER, "lead.routed", tenant_id="other")

    assert records(buffer)[0]["tenant_id"] == "other"


def test_ensure_trace_id_prefers_the_candidate_then_the_context_then_a_new_one() -> None:
    assert ensure_trace_id("given") == "given"
    with log_context(trace_id="bound"):
        assert ensure_trace_id(None) == "bound"
        assert ensure_trace_id("  ") == "bound"
    generated = ensure_trace_id(None)
    assert len(generated) == 32


# ------------------------------------------------------------------------------ PII


def test_contact_email_hash_is_the_hash_the_store_writes() -> None:
    """One hash, or a log line cannot be joined to a row (that is the whole point)."""
    assert contact_email_hash("Ada@Example.com ") == store_hash("ada@example.com")
    assert contact_email_hash(None) is None
    assert contact_email_hash("   ") is None


def test_redaction_replaces_addresses_and_keeps_the_rest() -> None:
    assert redact_emails("wrote to ada@example.com twice") == (f"wrote to {EMAIL_REDACTION} twice")
    assert redact_emails("no address here") == "no address here"


def test_an_address_in_a_log_message_never_reaches_the_line() -> None:
    buffer = configured()

    LOGGER.warning("bounce for ada.lovelace@analytical-engines.test")

    record = records(buffer)[0]
    assert "ada.lovelace@analytical-engines.test" not in json.dumps(record)
    assert EMAIL_REDACTION in record["message"]


def test_an_address_in_an_event_field_never_reaches_the_line() -> None:
    buffer = configured()

    log_event(LOGGER, "lead.routed", destination="ada@example.test")

    assert "ada@example.test" not in buffer.getvalue()


def test_an_address_in_a_traceback_never_reaches_the_line() -> None:
    buffer = configured()

    try:
        raise RuntimeError("SES rejected ada@example.test")
    except RuntimeError:
        LOGGER.exception("dispatch failed")

    record = records(buffer)[0]
    serialised = json.dumps(record)
    assert "ada@example.test" not in serialised
    assert record["exception"]["type"] == "RuntimeError"
    assert EMAIL_REDACTION in record["exception"]["message"]
    assert "RuntimeError" in record["exception"]["stack"]


def test_human_format_redacts_too() -> None:
    buffer = io.StringIO()
    configure_logging(settings_for(Environment.LOCAL), stream=buffer)

    LOGGER.warning("bounce for ada@example.test")

    assert "ada@example.test" not in buffer.getvalue()


# --------------------------------------------------------------------------- metrics


def assessment_payload() -> MetricPayload:
    return MetricPayload(
        dimensions={"TenantId": "acme", "Tier": "hot"},
        metric_sets=(
            MetricSet(dimensions=("TenantId", "Tier"), metrics=(Metric("Assessments", 1),)),
            MetricSet(
                dimensions=("TenantId",),
                metrics=(
                    Metric("CostUsd", Decimal("0.0213"), Unit.NONE),
                    Metric("ModelLatencyMs", 1234, Unit.MILLISECONDS),
                ),
            ),
        ),
    )


def test_emf_document_has_the_shape_cloudwatch_extracts() -> None:
    document = to_emf(assessment_payload(), timestamp_ms=1_700_000_000_000)

    aws = document["_aws"]
    assert aws["Timestamp"] == 1_700_000_000_000
    directives = aws["CloudWatchMetrics"]
    assert [directive["Namespace"] for directive in directives] == [METRIC_NAMESPACE] * 2
    assert directives[0]["Dimensions"] == [["TenantId", "Tier"]]
    assert directives[0]["Metrics"] == [{"Name": "Assessments", "Unit": "Count"}]
    assert {"Name": "CostUsd", "Unit": "None"} in directives[1]["Metrics"]

    # Dimension values and metric values all sit at the root.
    assert document["TenantId"] == "acme"
    assert document["Tier"] == "hot"
    assert document["Assessments"] == 1
    assert document["CostUsd"] == pytest.approx(0.0213)


def test_emf_refuses_a_dimension_it_has_no_value_for() -> None:
    payload = MetricPayload(
        dimensions={"TenantId": "acme"},
        metric_sets=(MetricSet(dimensions=("TenantId", "Tier"), metrics=(Metric("X", 1),)),),
    )
    with pytest.raises(ValueError, match="Tier"):
        to_emf(payload, timestamp_ms=0)


def test_emf_refuses_a_blank_dimension_value() -> None:
    payload = MetricPayload(
        dimensions={"TenantId": ""},
        metric_sets=(MetricSet(dimensions=("TenantId",), metrics=(Metric("X", 1),)),),
    )
    with pytest.raises(ValueError, match="TenantId"):
        to_emf(payload, timestamp_ms=0)


def test_metrics_ride_on_the_log_line_itself() -> None:
    buffer = configured()

    log_event(LOGGER, "assessment.completed", metrics=assessment_payload(), tenant_id="acme")

    record = records(buffer)[0]
    assert record["event"] == "assessment.completed"
    assert record["_aws"]["CloudWatchMetrics"][0]["Namespace"] == METRIC_NAMESPACE
    assert record["Assessments"] == 1
    assert record["CostUsd"] == pytest.approx(0.0213)
    assert record["_aws"]["Timestamp"] > 0


def test_metric_values_are_json_numbers_not_strings() -> None:
    buffer = configured()

    log_event(LOGGER, "assessment.completed", metrics=assessment_payload())

    raw = buffer.getvalue()
    assert '"CostUsd": 0.0213' in raw
    assert '"CostUsd": "0.0213"' not in raw


def test_human_format_renders_metrics_readably_and_emits_no_emf() -> None:
    buffer = io.StringIO()
    configure_logging(settings_for(Environment.LOCAL), stream=buffer)

    log_event(LOGGER, "assessment.completed", metrics=assessment_payload())

    line = buffer.getvalue()
    assert "_aws" not in line
    assert "Assessments=1" in line
