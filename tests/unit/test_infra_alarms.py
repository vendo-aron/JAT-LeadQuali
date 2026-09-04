"""What has to be true of the alarms for them to be worth having.

An alarm that never fires is worse than no alarm, because it looks like health. Two of
the tests below exist for exactly that failure: one cross-checks every metric name against
the code that emits it, so a typo fails the build rather than producing a permanently
green alarm; the other requires `TreatMissingData` to be set explicitly, because the
default turns a stopped pipeline into `INSUFFICIENT_DATA` rather than `ALARM`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from leadquali.observability import metrics as metric_names
from tests.unit.cfn import load_template

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "infra" / "alarms.yaml"

#: Namespaces whose metric names AWS owns, so this repository cannot validate them.
AWS_NAMESPACES = {"AWS/SQS", "AWS/Lambda", "AWS/RDS"}


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return load_template(TEMPLATE_PATH)


def _resources(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {name: body for name, body in template["Resources"].items() if body.get("Type") == kind}


def _alarms(template: dict[str, Any]) -> dict[str, Any]:
    return _resources(template, "AWS::CloudWatch::Alarm")


def _emitted_metric_names() -> set[str]:
    """Every metric name the application actually publishes."""
    return {
        value
        for name, value in vars(metric_names).items()
        if name.isupper() and isinstance(value, str) and not name.startswith(("DIM_", "METRIC_"))
    }


def test_there_are_alarms_to_check(template: dict[str, Any]) -> None:
    """Guards against a path typo turning this whole file into a no-op."""
    assert len(_alarms(template)) >= 10


def test_every_leadquali_metric_name_is_one_the_code_emits(template: dict[str, Any]) -> None:
    """The typo test. An alarm on `AssessmentFailure` (singular) is green forever."""
    emitted = _emitted_metric_names()
    assert "Assessments" in emitted, "sanity: the metrics module should define this"

    referenced: set[str] = set()
    for alarm in _alarms(template).values():
        properties = alarm["Properties"]
        if properties.get("Namespace") == metric_names.METRIC_NAMESPACE:
            referenced.add(properties["MetricName"])
        for query in properties.get("Metrics", []):
            stat = query.get("MetricStat")
            if stat and stat["Metric"].get("Namespace") == metric_names.METRIC_NAMESPACE:
                referenced.add(stat["Metric"]["MetricName"])

    assert referenced, "no application metrics referenced at all - are the alarms wired?"
    unknown = referenced - emitted
    assert not unknown, f"alarms reference metrics the code never emits: {sorted(unknown)}"


def test_every_dimension_name_is_one_the_code_emits(template: dict[str, Any]) -> None:
    known = {value for name, value in vars(metric_names).items() if name.startswith("DIM_")}
    for alarm in _alarms(template).values():
        for query in alarm["Properties"].get("Metrics", []):
            stat = query.get("MetricStat")
            if not stat or stat["Metric"].get("Namespace") != metric_names.METRIC_NAMESPACE:
                continue
            for dimension in stat["Metric"].get("Dimensions", []):
                assert dimension["Name"] in known, f"unknown dimension {dimension['Name']}"


def test_every_alarm_has_somewhere_to_go(template: dict[str, Any]) -> None:
    """An alarm with no action is a dashboard widget nobody sees at 3am."""
    for name, alarm in _alarms(template).items():
        actions = alarm["Properties"].get("AlarmActions")
        assert actions, f"{name} has no AlarmActions"


def test_the_topic_has_a_subscriber(template: dict[str, Any]) -> None:
    subscriptions = _resources(template, "AWS::SNS::Subscription")
    assert subscriptions, "a topic with no subscription notifies nobody"


def test_every_alarm_decides_what_missing_data_means(template: dict[str, Any]) -> None:
    """The default is `missing`, which reads as INSUFFICIENT_DATA - not as an alarm."""
    for name, alarm in _alarms(template).items():
        assert "TreatMissingData" in alarm["Properties"], (
            f"{name} leaves missing-data behaviour to the default; decide it explicitly"
        )


def test_a_silent_pipeline_alarms_rather_than_going_quiet(template: dict[str, Any]) -> None:
    """The one alarm where missing data *is* the outage.

    If nothing is assessed, no `Assessments` datapoint is published at all - so an alarm
    that treats missing data as anything but breaching sits at INSUFFICIENT_DATA through
    the entire incident.
    """
    alarm = _alarms(template)["NoLeadsAssessedAlarm"]["Properties"]
    assert alarm["TreatMissingData"] == "breaching"
    assert alarm["ComparisonOperator"].startswith("LessThan")


def test_the_dlq_alarm_treats_no_data_as_good_news(template: dict[str, Any]) -> None:
    """And the counterpart: for the DLQ, no data means no dead letters."""
    alarm = _alarms(template)["DeadLetterQueueDepthAlarm"]["Properties"]
    assert alarm["TreatMissingData"] == "notBreaching"
    assert alarm["Threshold"] == 0


@pytest.mark.parametrize(
    "alarm_name",
    [
        "AssessmentFailureRateAlarm",
        "HotShareDriftAlarm",
        "EnrichmentUnavailableAlarm",
    ],
)
def test_every_ratio_alarm_has_a_denominator_floor(
    template: dict[str, Any], alarm_name: str
) -> None:
    """A 5% failure rate over one lead is one lead.

    Without the floor these fire at 07:05 on the first bad lead of the day, and an alarm
    that cries wolf daily trains people to ignore it - which is worse than not having it.
    """
    queries = _alarms(template)[alarm_name]["Properties"]["Metrics"]
    expressions = [
        # An expression carrying a parameter is a `!Sub`, so it arrives as a dict.
        query["Expression"]["Fn::Sub"]
        if isinstance(query.get("Expression"), dict)
        else query["Expression"]
        for query in queries
        if "Expression" in query
    ]
    assert expressions, f"{alarm_name} is not a metric-math alarm"
    assert any("IF(" in expression for expression in expressions), (
        f"{alarm_name} divides without a denominator floor"
    )


def test_the_tier_drift_alarm_is_disabled_until_a_baseline_exists(
    template: dict[str, Any],
) -> None:
    """A 7-day band cannot exist on day one, so week one is a deliberate choice."""
    assert template["Resources"]["HotShareDriftAlarm"]["Condition"] == "TierBandReady"
    assert template["Parameters"]["TierDriftBandStartDate"]["Default"] == ""


def test_the_database_alarm_fires_below_the_connection_budget(
    template: dict[str, Any],
) -> None:
    """#27 computed 84 of 112. Alarming at max_connections leaves no room to diagnose."""
    assert template["Parameters"]["DatabaseConnectionBudget"]["Default"] == 84
    alarm = _alarms(template)["DatabaseConnectionsAlarm"]["Properties"]
    assert alarm["Namespace"] == "AWS/RDS"
    assert alarm["Threshold"] == {"Fn::Ref": "DatabaseConnectionBudget"}


def test_aws_namespaced_alarms_are_the_only_ones_this_repo_cannot_check(
    template: dict[str, Any],
) -> None:
    for name, alarm in _alarms(template).items():
        namespace = alarm["Properties"].get("Namespace")
        if namespace is None:
            continue
        assert namespace in AWS_NAMESPACES | {metric_names.METRIC_NAMESPACE}, (
            f"{name} uses an unrecognised namespace {namespace}"
        )


def test_the_dashboard_puts_the_latency_pair_together(template: dict[str, Any]) -> None:
    """The gap between the two is the diagnosis, so they belong on one widget."""
    body = template["Resources"]["Dashboard"]["Properties"]["DashboardBody"]["Fn::Sub"]
    assert "ModelLatencyMs" in body and "PipelineLatencyMs" in body
    model_at = body.index("ModelLatencyMs")
    pipeline_at = body.index("PipelineLatencyMs")
    assert abs(model_at - pipeline_at) < 200, "the two p99s should be one widget apart"


def test_the_alarm_email_must_look_like_an_email(template: dict[str, Any]) -> None:
    assert "AllowedPattern" in template["Parameters"]["AlarmEmail"]
