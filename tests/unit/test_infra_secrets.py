"""Where the secrets live, who may read them, and what the templates must never carry.

Same shape as ``test_infra_template.py`` and ``test_infra_network.py`` (#26, #27): no AWS
account exists here, so none of this proves a deploy. It proves the mistakes that are
expensive to find on deploy day, and one that is expensive to find *ever* — a secret
value committed to the repository.

The least-privilege criterion in #28 is "IAM permits each function to read only its own
secrets", and that is checked in both directions: a function may not hold a grant for a
secret it has no environment variable for, and may not have an environment variable for
a secret it cannot read. One direction alone passes a template that over-grants.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from leadquali.app.feedback import MIN_TOKEN_SECRET_CHARS
from leadquali.config import DEFAULT_SECRETS_CACHE_TTL_SECONDS, MAX_SECRETS_CACHE_TTL_SECONDS
from tests.unit.cfn import (
    APPLICATION_TEMPLATE_PATH,
    NETWORK_TEMPLATE_PATH,
    load_template,
    parameters,
    resources,
)

#: Secrets this stack creates, and the resource that creates each.
CREATED_SECRETS = {
    "IngestCredentialsSecret": "IngestCredentialsSecretArn",
    "FeedbackTokenSecret": "FeedbackTokenSecretArn",
}

#: The application template's secret parameters, and where each one's value comes from.
#: ``None`` means "created outside CloudFormation" — see #43's runbook for the only one.
SECRET_SOURCES = {
    "IngestCredentialsSecretArn": "IngestCredentialsSecretArn",
    "FeedbackTokenSecretArn": "FeedbackTokenSecretArn",
    "DatabaseSecretArn": "DatabaseMasterUserSecretArn",
    "AnthropicApiKeySecretArn": None,
}


@pytest.fixture(scope="module")
def network() -> dict[str, Any]:
    return load_template(NETWORK_TEMPLATE_PATH)


@pytest.fixture(scope="module")
def application() -> dict[str, Any]:
    return load_template(APPLICATION_TEMPLATE_PATH)


def _functions(application: dict[str, Any]) -> dict[str, Any]:
    return {
        logical_id: resource["Properties"]
        for logical_id, resource in resources(application).items()
        if resource.get("Type") == "AWS::Serverless::Function"
    }


def _environment(properties: dict[str, Any]) -> dict[str, Any]:
    env = properties.get("Environment", {}).get("Variables", {})
    assert isinstance(env, dict)
    return env


def _readable_secret_parameters(properties: dict[str, Any]) -> set[str]:
    """The parameter names this function is granted ``GetSecretValue`` on."""
    granted: set[str] = set()
    for policy in properties.get("Policies", []):
        if isinstance(policy, dict) and "AWSSecretsManagerGetSecretValuePolicy" in policy:
            arn = policy["AWSSecretsManagerGetSecretValuePolicy"]["SecretArn"]
            granted.add(arn["Fn::Ref"])
    return granted


def _referenced_secret_parameters(properties: dict[str, Any]) -> set[str]:
    """The parameter names this function's ``*_SECRET_ARN`` variables point at."""
    return {
        value["Fn::Ref"]
        for key, value in _environment(properties).items()
        if key.endswith("_SECRET_ARN") and isinstance(value, dict)
    }


# ------------------------------------------------------- created, generated, referenced


def test_the_network_stack_creates_the_secrets_it_should_own(network: dict[str, Any]) -> None:
    """Stateful things live in the stack that is deployed once, not the one deployed daily."""
    created = {
        logical_id
        for logical_id, resource in resources(network).items()
        if resource.get("Type") == "AWS::SecretsManager::Secret"
    }
    assert created == set(CREATED_SECRETS)


def test_the_anthropic_key_is_referenced_and_never_created(
    network: dict[str, Any], application: dict[str, Any]
) -> None:
    """Its value comes from the Anthropic console, so no template can generate it."""
    assert "AnthropicApiKeySecretArn" in parameters(application)
    for resource in resources(network).values():
        if resource.get("Type") != "AWS::SecretsManager::Secret":
            continue
        rendered = str(resource["Properties"])
        assert "anthropic" not in rendered.lower(), (
            "the Anthropic key is created by hand per #43; a template that made one "
            "would create a second, empty secret nobody notices is unused"
        )


def test_every_secret_parameter_is_satisfied_by_a_network_output(
    network: dict[str, Any], application: dict[str, Any]
) -> None:
    """The two stacks are joined by outputs; a missing one is a deploy that cannot start."""
    declared = {name for name in parameters(application) if name.endswith("SecretArn")}
    assert declared == set(SECRET_SOURCES), "a new secret parameter needs a documented source"
    outputs = network["Outputs"]
    for parameter, output in SECRET_SOURCES.items():
        if output is None:
            continue
        assert output in outputs, f"{parameter} has no source in the network stack"


def test_the_ingest_credentials_secret_is_created_without_a_value(
    network: dict[str, Any],
) -> None:
    """CloudFormation must have no ``SecretString`` here to overwrite.

    The value is a per-tenant map written by onboarding. A template carrying even a
    placeholder would wipe every tenant's credentials the first time somebody edited this
    resource's description, because CloudFormation applies the whole property set on an
    update.
    """
    properties = resources(network)["IngestCredentialsSecret"]["Properties"]
    assert "SecretString" not in properties
    assert "GenerateSecretString" not in properties


def test_the_feedback_secret_is_generated_long_enough_to_be_used(
    network: dict[str, Any],
) -> None:
    """Opaque random material with one holder: CloudFormation can mint this one."""
    generate = resources(network)["FeedbackTokenSecret"]["Properties"]["GenerateSecretString"]
    assert generate["PasswordLength"] >= MIN_TOKEN_SECRET_CHARS, (
        "app/feedback.load_token_secret rejects anything shorter, so a short generated "
        "value is a stack that deploys and then fails on the first routing email"
    )
    assert "SecretString" not in resources(network)["FeedbackTokenSecret"]["Properties"]


@pytest.mark.parametrize("logical_id", sorted(CREATED_SECRETS))
def test_a_created_secret_survives_the_stack(network: dict[str, Any], logical_id: str) -> None:
    """Deleting the ingest credentials deletes every customer's integration."""
    resource = resources(network)[logical_id]
    assert resource["DeletionPolicy"] == "Retain"
    assert resource["UpdateReplacePolicy"] == "Retain"


# ------------------------------------------------------------------------------- KMS


def test_the_secrets_have_their_own_customer_managed_key(network: dict[str, Any]) -> None:
    """Separate from the RDS key, and the point is which grants each one has to carry."""
    found = resources(network)
    assert found["SecretsKmsKey"]["Type"] == "AWS::KMS::Key"
    assert found["SecretsKmsKey"]["Properties"]["EnableKeyRotation"] is True
    assert found["SecretsKmsKey"]["DeletionPolicy"] == "Retain"

    for logical_id in CREATED_SECRETS:
        key = found[logical_id]["Properties"]["KmsKeyId"]
        assert key == {"Fn::Ref": "SecretsKmsKey"}, f"{logical_id} is not on the secrets key"
        assert key != {"Fn::Ref": "DatabaseKmsKey"}


def test_the_secrets_key_can_only_be_used_through_secrets_manager(
    network: dict[str, Any],
) -> None:
    """The statement that could never be written on the RDS key.

    It is what makes a second $1/month key worth having: the RDS key must be usable by
    RDS itself to encrypt volumes and snapshots, so this Deny would break the database.
    Here it means a role that somehow obtains ``kms:Decrypt`` still cannot use the key
    for anything but reading a secret.
    """
    statements = resources(network)["SecretsKmsKey"]["Properties"]["KeyPolicy"]["Statement"]
    denies = [s for s in statements if s["Effect"] == "Deny"]
    assert denies, "the key policy allows and forbids nothing"
    deny = denies[0]
    assert deny["Principal"] == "*"
    assert "kms:Decrypt" in deny["Action"]
    condition = deny["Condition"]["StringNotEquals"]["kms:ViaService"]
    assert condition == {"Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"}


def test_the_database_key_is_left_alone(network: dict[str, Any]) -> None:
    """#27 owns it; #28 must not have quietly repointed RDS at the secrets key."""
    database = resources(network)["Database"]["Properties"]
    assert database["KmsKeyId"] == {"Fn::GetAtt": "DatabaseKmsKey.Arn"}


@pytest.mark.parametrize(
    "logical_id", ["IngestCredentialsSecretPolicy", "FeedbackTokenSecretPolicy"]
)
def test_a_resource_policy_closes_the_account_boundary(
    network: dict[str, Any], logical_id: str
) -> None:
    """What an identity policy cannot say, because the reader writes that one.

    Deny rather than Allow: a resource policy that only allowed would be additive to IAM
    and would forbid nothing at all.
    """
    properties = resources(network)[logical_id]["Properties"]
    assert properties["BlockPublicPolicy"] is True
    statement = properties["ResourcePolicy"]["Statement"][0]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert statement["Condition"]["StringNotEquals"]["aws:PrincipalAccount"] == {
        "Fn::Ref": "AWS::AccountId"
    }


# ------------------------------------------------------------------- least privilege


def test_each_function_can_read_exactly_the_secrets_it_names(
    application: dict[str, Any],
) -> None:
    """#28's acceptance criterion, checked in both directions.

    Grants without a variable are over-privilege nobody notices; a variable without a
    grant is an AccessDenied on the first cold start.
    """
    checked = 0
    for logical_id, properties in _functions(application).items():
        granted = _readable_secret_parameters(properties)
        referenced = _referenced_secret_parameters(properties)
        assert granted == referenced, (
            f"{logical_id} is granted {sorted(granted)} and uses {sorted(referenced)}"
        )
        checked += 1
    assert checked == 3, "expected the ingest, worker and migration functions"


def test_the_worker_cannot_read_the_ingest_credentials(application: dict[str, Any]) -> None:
    """The signing secrets are the customers' half of the system; the worker has no part.

    Named separately from the symmetry check above because this is the specific pair the
    blast radius argument is about: a compromised worker must not be able to mint
    signatures for a customer's website.
    """
    worker = _functions(application)["WorkerFunction"]
    assert "IngestCredentialsSecretArn" not in _readable_secret_parameters(worker)


def test_the_ingest_function_cannot_read_the_anthropic_key(
    application: dict[str, Any],
) -> None:
    """It never calls the model, so a key it can read is a key it can leak."""
    ingest = _functions(application)["IngestFunction"]
    assert "AnthropicApiKeySecretArn" not in _readable_secret_parameters(ingest)


def test_the_migration_function_reads_only_the_database_secret(
    application: dict[str, Any],
) -> None:
    migration = _functions(application)["MigrationFunction"]
    assert _readable_secret_parameters(migration) == {"DatabaseSecretArn"}


def test_every_function_that_reads_a_secret_may_also_decrypt_it(
    application: dict[str, Any],
) -> None:
    """The permission that is easy to miss: with a CMK, GetSecretValue alone is denied."""
    for logical_id, properties in _functions(application).items():
        if not _readable_secret_parameters(properties):
            continue
        assert {"Fn::Ref": "DecryptSecretsPolicy"} in properties["Policies"], (
            f"{logical_id} can fetch a secret it cannot decrypt"
        )


def test_the_decrypt_grant_is_bound_to_secrets_manager(application: dict[str, Any]) -> None:
    """``Resource: "*"`` is only defensible because of this condition."""
    statement = resources(application)["DecryptSecretsPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ][0]
    assert statement["Action"] == "kms:Decrypt"
    assert statement["Condition"]["StringEquals"]["kms:ViaService"] == {
        "Fn::Sub": "secretsmanager.${AWS::Region}.amazonaws.com"
    }


# ------------------------------------------------------ the assembled URL and the TTL


def test_no_function_is_handed_a_database_url(application: dict[str, Any]) -> None:
    """The URL is assembled at cold start; a DATABASE_URL variable would be a password."""
    for logical_id, properties in _functions(application).items():
        assert "DATABASE_URL" not in _environment(properties), logical_id


def test_a_function_with_the_database_secret_gets_the_parts_to_assemble_a_url(
    application: dict[str, Any],
) -> None:
    """Host and name are not optional: the secret alone does not say what to connect to."""
    checked = 0
    for logical_id, properties in _functions(application).items():
        env = _environment(properties)
        if "DATABASE_SECRET_ARN" not in env:
            continue
        for variable in ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_NAME"):
            assert variable in env, f"{logical_id} cannot assemble a URL without {variable}"
        checked += 1
    assert checked == 3, "all three functions talk to Postgres"


def test_the_cache_ttl_is_configurable_and_bounded(application: dict[str, Any]) -> None:
    """A parameter, because the arithmetic behind the default changes with the fleet."""
    spec = parameters(application)["SecretsCacheTtlSeconds"]
    assert spec["Type"] == "Number"
    assert spec["Default"] == DEFAULT_SECRETS_CACHE_TTL_SECONDS, (
        "the template and leadquali.config must agree on the rotation delay"
    )
    assert spec["MinValue"] == 0
    assert 0 < spec["MaxValue"] <= MAX_SECRETS_CACHE_TTL_SECONDS


def test_every_function_that_resolves_a_secret_is_told_the_ttl(
    application: dict[str, Any],
) -> None:
    """A function left on the code default would ignore an operator turning the knob."""
    for logical_id, properties in _functions(application).items():
        if not _referenced_secret_parameters(properties):
            continue
        env = _environment(properties)
        assert env.get("SECRETS_CACHE_TTL_SECONDS") == {"Fn::Ref": "SecretsCacheTtlSeconds"}, (
            f"{logical_id} does not receive the configured TTL"
        )


# ---------------------------------------------------------------- nothing in the repo


@pytest.mark.parametrize("path", [APPLICATION_TEMPLATE_PATH, NETWORK_TEMPLATE_PATH])
def test_no_template_carries_anything_that_looks_like_a_secret(path: Any) -> None:
    """The sweep in #28's acceptance criteria, run on every commit rather than once.

    Deliberately crude and deliberately narrow: it catches the specific shapes that would
    end up here — an Anthropic key, an AWS secret access key, a password assignment.
    """
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "sk-ant" not in lowered
    assert "aws_secret_access_key" not in lowered
    assert not re.search(r"(?m)^\s+SecretString:", text), (
        f"{path.name} assigns a literal secret value. Every secret here is either "
        "generated by Secrets Manager, written by an operator, or created by RDS; there "
        "is no case where a template should carry one"
    )
