"""The SAM template is configuration, so its invariants are asserted, not assumed.

No AWS account exists here, so nothing below proves the stack deploys. What it does prove
is that the template does not contain the specific mistakes that are expensive to discover
on deploy day: a visibility timeout shorter than the worker's timeout, a queue with no
dead letter, a plaintext secret, a handler path that no longer matches the code.

`cfn-lint` covers CloudFormation validity; these cover the things cfn-lint cannot know.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.cfn import APPLICATION_TEMPLATE_PATH, load_template
from tests.unit.cfn import parameters as _parameters
from tests.unit.cfn import resources as _resources


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return load_template(APPLICATION_TEMPLATE_PATH)


def test_the_template_exists_and_is_a_sam_template(template: dict[str, Any]) -> None:
    assert template["Transform"] == "AWS::Serverless-2016-10-31"


def test_visibility_timeout_exceeds_the_worker_timeout(template: dict[str, Any]) -> None:
    """The one number whose being wrong duplicates work under load.

    SQS redelivers a message whose visibility timeout expires while it is still being
    worked. Set this below the worker's timeout and every slow lead is qualified twice —
    two Claude calls, and but for the `(tenant_id, submission_id)` idempotency key, two
    emails to sales.
    """
    resources = _resources(template)
    visibility = resources["LeadQueue"]["Properties"]["VisibilityTimeout"]
    worker_default = template["Parameters"]["WorkerTimeoutSeconds"]["Default"]
    worker_max = template["Parameters"]["WorkerTimeoutSeconds"]["MaxValue"]

    assert visibility > worker_default
    assert visibility >= worker_max, (
        "visibility must exceed the worker timeout at its maximum too, or raising "
        "WorkerTimeoutSeconds silently introduces double delivery"
    )


def test_the_queue_has_a_dead_letter_queue_and_a_bounded_retry(
    template: dict[str, Any],
) -> None:
    resources = _resources(template)
    redrive = resources["LeadQueue"]["Properties"]["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {"Fn::GetAtt": "LeadDeadLetterQueue.Arn"}
    assert 1 < redrive["maxReceiveCount"] <= 5, (
        "each attempt costs a Claude call, so the console default of 10 is real money"
    )
    assert "LeadDeadLetterQueue" in resources


def test_the_dead_letter_queue_retains_long_enough_for_a_human(
    template: dict[str, Any],
) -> None:
    """A lead on the DLQ is one nobody has looked at yet."""
    retention = _resources(template)["LeadDeadLetterQueue"]["Properties"]["MessageRetentionPeriod"]
    assert retention == 1_209_600, "14 days, the maximum: a long weekend must not lose leads"


def test_the_worker_has_a_reserved_concurrency_cap(template: dict[str, Any]) -> None:
    """Each container holds a database connection; unbounded workers exhaust Postgres."""
    worker = _resources(template)["WorkerFunction"]["Properties"]
    assert worker["ReservedConcurrentExecutions"] == {"Fn::Ref": "WorkerReservedConcurrency"}


def test_the_worker_reports_partial_batch_failures(template: dict[str, Any]) -> None:
    """Without this, one poisoned message drags nine healthy leads back onto the queue."""
    event = _resources(template)["WorkerFunction"]["Properties"]["Events"]["Leads"]
    assert event["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


def test_no_secret_is_a_plaintext_parameter_or_environment_variable(
    template: dict[str, Any],
) -> None:
    """Secrets arrive as ARNs. A secret passed as a parameter is visible in the console.

    Checked structurally rather than by name: every parameter whose name suggests a secret
    must be an ARN reference, and none may carry a default value.
    """
    secretish = ("secret", "apikey", "api_key", "password", "token")
    for name, spec in template["Parameters"].items():
        lowered = name.lower()
        # A Number cannot carry a secret: FeedbackTokenTtlDays contains "token" and is a
        # duration. Narrowing by type keeps the heuristic broad without false positives.
        if spec.get("Type") == "Number":
            continue
        if any(word in lowered for word in secretish):
            assert lowered.endswith("arn"), f"{name} should name an ARN, not hold a value"
            assert "Default" not in spec, f"{name} must not have a default"

    for resource in _resources(template).values():
        env = resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
        for key, value in env.items():
            # A duration cannot carry a secret. FEEDBACK_TOKEN_TTL_DAYS contains "token"
            # and SECRETS_CACHE_TTL_SECONDS contains "secret"; both are numbers of
            # seconds or days. Same carve-out as the Number check on parameters above.
            if key.endswith(("_TTL_DAYS", "_TTL_SECONDS")):
                continue
            if any(word in key.lower() for word in secretish):
                assert key.endswith("_SECRET_ARN"), (
                    f"{key} looks like a secret carried in plaintext; pass an ARN instead"
                )
                assert isinstance(value, dict), f"{key} must be a reference, not a literal"


def test_every_handler_path_matches_a_real_module_attribute(
    template: dict[str, Any],
) -> None:
    """A renamed handler is a deploy that fails at the first invocation, not at deploy."""
    import importlib

    for resource in _resources(template).values():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        handler = resource["Properties"]["Handler"]
        module_name, _, attribute = handler.rpartition(".")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{handler} is not callable"


def test_the_ingest_route_is_the_logical_path_the_signature_covers(
    template: dict[str, Any],
) -> None:
    """The deploy-day bug this template most needed to avoid.

    #17 signs the logical path `/leads`, not the stage-prefixed one, so that renaming a
    stage cannot invalidate every signature the website produces. The route must therefore
    be exactly that literal.
    """
    from leadquali.api.main import HEALTH_PATH, INGEST_PATH

    events = _resources(template)["IngestFunction"]["Properties"]["Events"]
    assert events["Ingest"]["Properties"]["Path"] == INGEST_PATH
    assert events["Ingest"]["Properties"]["Method"] == "post"
    assert events["Health"]["Properties"]["Path"] == HEALTH_PATH


def test_the_feedback_base_url_includes_the_stage(template: dict[str, Any]) -> None:
    """#19's links are clicked from an email, so the base must be externally routable."""
    env = _resources(template)["WorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert "${Stage}" in env["FEEDBACK_BASE_URL"]["Fn::Sub"]


def test_the_escalation_destination_is_required(template: dict[str, Any]) -> None:
    """#14 makes it a required constructor argument; a default here would defeat that."""
    assert "Default" not in template["Parameters"]["EscalationDestination"]


def test_migrations_do_not_run_on_worker_cold_start(template: dict[str, Any]) -> None:
    """A worker that migrates on cold start races N containers for one DDL lock."""
    resources = _resources(template)
    assert "MigrationFunction" in resources
    assert resources["MigrationFunction"]["Properties"]["Handler"].endswith(
        "migrate.lambda_handler"
    )
    worker_env = resources["WorkerFunction"]["Properties"]["Environment"]["Variables"]
    assert not any("MIGRAT" in key.upper() for key in worker_env)


def test_the_api_access_log_carries_no_request_body_or_headers(
    template: dict[str, Any],
) -> None:
    """Invariant 5 applies to access logs: the body is a lead, the headers hold the key."""
    fmt = _resources(template)["LeadApi"]["Properties"]["AccessLogSetting"]["Format"]
    for forbidden in ("$input.body", "requestOverride", "authorization", "x-leadquali-key"):
        assert forbidden.lower() not in fmt.lower()


def test_the_stage_has_an_account_wide_throttle(template: dict[str, Any]) -> None:
    """The backstop behind #31's per-tenant limits.

    ``TenantRateLimiter`` counts inside one Python process, so under N warm containers a
    tenant can get up to N times its configured allowance. This throttle is what bounds the
    absolute worst case, in front of the runtime, where a flood costs no invocations and no
    database connections. It is deliberately the *only* API Gateway throttling in the
    stack: per-tenant usage plans were considered and rejected (docs/tenant-onboarding.md).
    """
    settings = _resources(template)["LeadApi"]["Properties"]["MethodSettings"]
    default = [entry for entry in settings if entry["ResourcePath"] == "/*"]
    assert default, "the stage needs a default throttle covering every route"
    for entry in default:
        assert entry["HttpMethod"] == "*"
        assert entry["ThrottlingRateLimit"] > 0
        assert entry["ThrottlingBurstLimit"] >= entry["ThrottlingRateLimit"]


def test_the_ingest_function_may_read_every_tenants_signing_secret(
    template: dict[str, Any],
) -> None:
    """Onboarding creates one Secrets Manager secret per customer, so the grant has to be
    by path: naming them individually would make adding a customer a stack update, which is
    the deploy that invariant 1 says onboarding must never be.

    The scope is asserted as well as the existence of the grant — one action, and one
    prefix that carries the stage — because a wildcard on ``secretsmanager:*`` here would
    let a compromised ingest function read the Anthropic key and the database password.
    """
    policies = _resources(template)["IngestFunction"]["Properties"]["Policies"]
    statements = [
        statement
        for policy in policies
        if isinstance(policy, dict) and "Statement" in policy
        for statement in policy["Statement"]
    ]
    granted = [s for s in statements if s.get("Action") == "secretsmanager:GetSecretValue"]
    assert len(granted) == 1, "expected exactly one path-scoped tenant-secret grant"

    resource = granted[0]["Resource"]
    assert "/tenant/" in str(resource)
    assert "${Stage}" in str(resource), "the grant must not cross environments"
    assert "secretsmanager:*" not in str(granted[0]["Action"])


# ------------------------------------------------------------------ retention (#37)


def test_the_retention_job_is_on_a_schedule(template: dict[str, Any]) -> None:
    """A retention policy nobody runs is a document, not a control.

    The acceptance criterion is "runs on schedule", and the only thing in this repository
    that can make that true is this event. Asserted as a reference to the parameter rather
    than to a literal expression, because the hour is an operational decision.
    """
    events = _resources(template)["RetentionFunction"]["Properties"]["Events"]

    assert [event["Type"] for event in events.values()] == ["Schedule"]
    assert events["Daily"]["Properties"]["Schedule"] == {"Fn::Ref": "RetentionSchedule"}
    assert events["Daily"]["Properties"]["Enabled"] is True


def test_the_retention_schedule_runs_before_the_backup_window(template: dict[str, Any]) -> None:
    """02:30 UTC, ahead of the database's 03:10 window in infra/network.yaml.

    The ordering is the point: deletions made at 02:30 are inside the snapshot taken at
    03:10, so the backup a restore would come from does not carry data the purge removed.
    The other order would mean every night's purge is undone for seven days in whichever
    backup somebody restores.
    """
    default = _parameters(template)["RetentionSchedule"]["Default"]

    assert default == "cron(30 2 * * ? *)"


def test_the_retention_function_can_read_the_database_secret_and_nothing_else(
    template: dict[str, Any],
) -> None:
    """The narrowest policy in the stack, on the function that can do the most damage.

    No Anthropic key, no SES, no feedback signing key. A retention job that could send mail
    or call a model has grown capabilities nobody asked it for, and it is the one function
    whose compromise means deleted customer data.
    """
    policies = _resources(template)["RetentionFunction"]["Properties"]["Policies"]
    secrets = [
        policy["AWSSecretsManagerGetSecretValuePolicy"]["SecretArn"]
        for policy in policies
        if isinstance(policy, dict) and "AWSSecretsManagerGetSecretValuePolicy" in policy
    ]

    assert secrets == [{"Fn::Ref": "DatabaseSecretArn"}]
    assert not any("SESCrudPolicy" in policy for policy in policies if isinstance(policy, dict))


def test_only_one_retention_run_can_be_in_flight(template: dict[str, Any]) -> None:
    """Not for correctness — every batch is SKIP LOCKED — but for the connection budget,
    and so that "how many of these are running?" has a one-word answer."""
    assert (
        _resources(template)["RetentionFunction"]["Properties"]["ReservedConcurrentExecutions"] == 1
    )


def test_the_retention_function_is_in_the_vpc_like_everything_that_talks_to_postgres(
    template: dict[str, Any],
) -> None:
    """There is no public database address, so a function outside the VPC cannot connect."""
    assert "VpcConfig" in _resources(template)["RetentionFunction"]["Properties"]
