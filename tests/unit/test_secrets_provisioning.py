"""Provisioning a tenant's HMAC signing secret, against ``moto``.

Three properties, and the middle one is the one that would cost a customer an outage:

* a created secret holds real random material, is tagged with its tenant, and is encrypted
  with the configured key;
* creating it **again** returns the existing ARN and does not touch the value — onboarding
  has to be safe to retry, and the value is what every form the customer has deployed signs
  with;
* rotation replaces the value, which is deliberately breaking and deliberately separate.

``moto`` stands in for Secrets Manager, so none of this needs credentials or a network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from leadquali.adapters.secrets_manager import (
    SecretProvisioningError,
    TenantSecretsProvisioner,
)
from leadquali.api.signing import MIN_SIGNING_SECRET_CHARS

REGION = "eu-west-1"
SLUG = "acme-demo"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials only moto will ever see, and a region that is nobody's default."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def client(aws_credentials: None) -> Iterator[Any]:
    """A moto-backed Secrets Manager client."""
    del aws_credentials
    with mock_aws():
        yield boto3.client("secretsmanager", region_name=REGION)


@pytest.fixture
def provisioner(client: Any) -> TenantSecretsProvisioner:
    return TenantSecretsProvisioner(client, environment="prod")


# ------------------------------------------------------------------------- creation


def test_the_secret_name_says_which_environment_and_which_tenant(
    provisioner: TenantSecretsProvisioner,
) -> None:
    """Derived rather than stored, so an operator can find a customer's secret in the
    console from the customer's name, and one IAM wildcard covers an environment."""
    assert provisioner.secret_name(SLUG) == f"leadquali/prod/tenant/{SLUG}/hmac"


def test_creating_a_secret_returns_its_arn(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    assert arn.startswith("arn:aws:secretsmanager:")
    assert provisioner.secret_name(SLUG) in client.describe_secret(SecretId=arn)["Name"]


def test_the_generated_secret_is_long_enough_to_be_accepted(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    """``api.signing.load_credentials`` refuses anything shorter, so a short generated
    value would be an onboarding that succeeds and a tenant that can never authenticate."""
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    value = client.get_secret_value(SecretId=arn)["SecretString"]
    assert len(value) >= MIN_SIGNING_SECRET_CHARS


def test_two_tenants_never_share_a_signing_secret(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    """The whole point of per-tenant signing: a leak at one customer's website cannot be
    used to forge leads for another."""
    first = client.get_secret_value(SecretId=provisioner.create_tenant_hmac_secret("one"))
    second = client.get_secret_value(SecretId=provisioner.create_tenant_hmac_secret("two"))
    assert first["SecretString"] != second["SecretString"]


def test_the_secret_is_tagged_with_its_tenant(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    """So cost allocation, and "what belongs to this customer?" during an offboarding,
    are answerable without parsing names."""
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    tags = {tag["Key"]: tag["Value"] for tag in client.describe_secret(SecretId=arn)["Tags"]}
    assert tags["leadquali:tenant"] == SLUG


def test_the_configured_kms_key_is_used(client: Any) -> None:
    """#28's customer-managed key, not the account's AWS-managed one: the blast radius
    argument in ``infra/network.yaml`` depends on which key a secret is under."""
    provisioner = TenantSecretsProvisioner(
        client, environment="prod", kms_key_id="alias/leadquali-secrets"
    )
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    assert client.describe_secret(SecretId=arn)["KmsKeyId"] == "alias/leadquali-secrets"


def test_environments_do_not_collide(client: Any) -> None:
    """The same tenant in staging and in production is two secrets, never one."""
    staging = TenantSecretsProvisioner(client, environment="staging")
    production = TenantSecretsProvisioner(client, environment="prod")
    assert staging.create_tenant_hmac_secret(SLUG) != production.create_tenant_hmac_secret(SLUG)


# ---------------------------------------------------------------------- idempotency


def test_creating_twice_returns_the_same_arn(
    provisioner: TenantSecretsProvisioner,
) -> None:
    first = provisioner.create_tenant_hmac_secret(SLUG)
    assert provisioner.create_tenant_hmac_secret(SLUG) == first


def test_creating_twice_does_not_replace_the_value(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    """The one that matters. Onboarding is retried — a failed insert, a mistyped command,
    a half-finished runbook — and a retry that silently minted new signing material would
    break every form the customer had already deployed, with no error anywhere."""
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    original = client.get_secret_value(SecretId=arn)["SecretString"]

    provisioner.create_tenant_hmac_secret(SLUG)

    assert client.get_secret_value(SecretId=arn)["SecretString"] == original


# ------------------------------------------------------------------------- rotation


def test_rotation_replaces_the_value_at_the_same_arn(
    provisioner: TenantSecretsProvisioner, client: Any
) -> None:
    """Breaking for that tenant's forms, on purpose: there is no dual-secret overlap for
    HMAC in v1, which is why the runbook says to coordinate it. The ARN does not change,
    so nothing stored against the tenant row has to be rewritten."""
    arn = provisioner.create_tenant_hmac_secret(SLUG)
    before = client.get_secret_value(SecretId=arn)["SecretString"]

    provisioner.rotate_tenant_hmac_secret(arn)

    after = client.get_secret_value(SecretId=arn)["SecretString"]
    assert after != before
    assert len(after) >= MIN_SIGNING_SECRET_CHARS


def test_rotating_a_secret_that_does_not_exist_is_a_clean_error(
    provisioner: TenantSecretsProvisioner,
) -> None:
    with pytest.raises(SecretProvisioningError, match="cannot rotate"):
        provisioner.rotate_tenant_hmac_secret("leadquali/prod/tenant/ghost/hmac")


# ---------------------------------------------------------------------------- errors


class ExplodingClient:
    """A client whose every call raises, for the "nothing half-done" paths."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def create_secret(self, **kwargs: Any) -> Any:
        raise self._error

    def describe_secret(self, **kwargs: Any) -> Any:
        raise self._error

    def put_secret_value(self, **kwargs: Any) -> Any:
        raise self._error


def test_a_failure_to_create_is_one_error_type_naming_the_secret() -> None:
    """Callers must not have to know botocore's exception tree, and an exception message
    ends up in CloudWatch — so it names the secret and never the value."""
    provisioner = TenantSecretsProvisioner(
        ExplodingClient(RuntimeError("no credentials")), environment="prod"
    )
    with pytest.raises(SecretProvisioningError) as caught:
        provisioner.create_tenant_hmac_secret(SLUG)

    message = str(caught.value)
    assert provisioner.secret_name(SLUG) in message
    assert "RuntimeError" in message


def test_a_failure_to_rotate_is_the_same_error_type() -> None:
    provisioner = TenantSecretsProvisioner(
        ExplodingClient(RuntimeError("boom")), environment="prod"
    )
    with pytest.raises(SecretProvisioningError, match="cannot rotate"):
        provisioner.rotate_tenant_hmac_secret("arn:aws:secretsmanager:eu-west-1:0:secret:x")


def test_the_repr_renders_no_secret_material(provisioner: TenantSecretsProvisioner) -> None:
    provisioner.create_tenant_hmac_secret(SLUG)
    assert repr(provisioner) == "TenantSecretsProvisioner(environment='prod')"
