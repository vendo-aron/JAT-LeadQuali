"""Invariants of the network and data stack (#27).

No AWS account exists here, so none of this proves the stack deploys, that RDS accepts the
engine version, or that a Lambda in these subnets can actually resolve `api.anthropic.com`.
What it does prove is that the template still says the things the design depends on, and
those are exactly the things that are cheap to change by accident and expensive to
discover: a database that became publicly accessible, storage that stopped being
encrypted, a security group that grew a CIDR rule, a VPC whose DNS was turned off, and —
the one with no visible symptom — a worker concurrency cap raised past the number of
connections the database can serve.

The last one is the reason this file exists at all. The relationship

    (worker + ingest concurrency) x connections-per-container <= proxy backend budget

spans two templates and a Python module's engine settings. Nothing enforces it end to end
except the test below, so it is written as arithmetic over the parameters rather than as a
comparison against a remembered number.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.cfn import (
    APPLICATION_TEMPLATE_PATH,
    NETWORK_TEMPLATE_PATH,
    load_template,
    parameters,
    resources,
)

#: Connections a warm Lambda container holds open. One, and not a guess:
#: `store_postgres.engine_for` builds its engine with `pool_size=1` and `max_overflow=0`
#: precisely so that this factor is 1 and reserved concurrency *is* the connection budget.
#: `test_the_adapter_still_holds_one_connection_per_container` keeps that true.
CONNECTIONS_PER_CONTAINER = 1

DATABASE_PORT = 5432


@pytest.fixture(scope="module")
def network() -> dict[str, Any]:
    return load_template(NETWORK_TEMPLATE_PATH)


@pytest.fixture(scope="module")
def application() -> dict[str, Any]:
    return load_template(APPLICATION_TEMPLATE_PATH)


def _security_group_rules(
    template: dict[str, Any], group: str, direction: str
) -> list[dict[str, Any]]:
    """Every rule in one direction for one security group, inline and standalone.

    Collecting both forms matters: a rule declared inline on the group and a rule declared
    as its own `AWS::EC2::SecurityGroupIngress` resource have identical effect, so a check
    that only reads one of them can be defeated by moving a rule.

    Args:
        template: The loaded network template.
        group: The security group's logical id.
        direction: ``"Ingress"`` or ``"Egress"``.

    Returns:
        The matching rules, each annotated with the CloudFormation ``Condition`` guarding
        it (``None`` when unconditional).
    """
    inline_key = f"SecurityGroup{direction}"
    standalone_type = f"AWS::EC2::SecurityGroup{direction}"
    found: list[dict[str, Any]] = []

    for logical_id, resource in resources(template).items():
        properties = resource.get("Properties", {})
        if logical_id == group and resource.get("Type") == "AWS::EC2::SecurityGroup":
            for rule in properties.get(inline_key, []):
                found.append({**rule, "Condition": resource.get("Condition")})
        elif resource.get("Type") == standalone_type and properties.get("GroupId") == {
            "Fn::Ref": group
        }:
            found.append({**properties, "Condition": resource.get("Condition")})
    return found


# --------------------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------------------
def test_the_database_is_not_publicly_accessible(network: dict[str, Any]) -> None:
    """The acceptance criterion, and the reason the migration Lambda exists.

    `PubliclyAccessible: false` is not a preference here — with no public address there is
    no way to run a migration from a laptop, which is what makes "never open the database
    to the internet" a property of the stack rather than of somebody's discipline.
    """
    assert resources(network)["Database"]["Properties"]["PubliclyAccessible"] is False


def test_the_database_is_encrypted_at_rest_with_a_managed_key(
    network: dict[str, Any],
) -> None:
    """Leads are personal data (plan section 8); encryption is not optional for them."""
    properties = resources(network)["Database"]["Properties"]
    assert properties["StorageEncrypted"] is True
    assert properties["KmsKeyId"] == {"Fn::GetAtt": "DatabaseKmsKey.Arn"}
    key = resources(network)["DatabaseKmsKey"]
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["DeletionPolicy"] == "Retain", (
        "deleting the key destroys every snapshot taken with it, which would make the "
        "Snapshot deletion policy on the instance meaningless"
    )


def test_automated_backups_are_on_and_cannot_be_switched_off(
    network: dict[str, Any],
) -> None:
    """A retention of 0 disables automated backups; the parameter must not reach it."""
    retention = parameters(network)["DbBackupRetentionDays"]
    assert retention["MinValue"] >= 1
    assert retention["Default"] >= 7
    assert resources(network)["Database"]["Properties"]["BackupRetentionPeriod"] == {
        "Fn::Ref": "DbBackupRetentionDays"
    }


def test_the_database_survives_the_stack_being_deleted(network: dict[str, Any]) -> None:
    """The feedback loop in this database is the moat (plan section 4)."""
    database = resources(network)["Database"]
    assert database["DeletionPolicy"] == "Snapshot"
    assert database["UpdateReplacePolicy"] == "Snapshot"
    assert database["Properties"]["DeletionProtection"] is True


def test_the_master_password_is_never_in_the_template(network: dict[str, Any]) -> None:
    """RDS generates and rotates it in Secrets Manager; nothing here can hold a literal."""
    properties = resources(network)["Database"]["Properties"]
    assert properties["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in properties
    for name, spec in parameters(network).items():
        assert "password" not in name.lower(), f"{name} must not exist"
        assert spec.get("NoEcho") is not True, f"{name} carries a secret; use an ARN"


def test_the_parameter_group_family_matches_the_engine_version(
    network: dict[str, Any],
) -> None:
    """A mismatch is rejected by RDS at create time, twenty minutes into a deploy."""
    params = parameters(network)
    major = params["DbEngineVersion"]["Default"].split(".")[0]
    assert params["DbParameterGroupFamily"]["Default"] == f"postgres{major}"


# --------------------------------------------------------------------------------------
# The network
# --------------------------------------------------------------------------------------
def test_the_vpc_resolves_dns(network: dict[str, Any]) -> None:
    """#18's finding, asserted.

    A Lambda in a private subnet resolves names through the Amazon-provided resolver, and
    that resolver only exists when `EnableDnsSupport` is true. Turn it off and every
    lookup times out rather than failing: the enrichment circuit breaker opens, every lead
    is enriched with nothing, every lead is still delivered, and no alarm fires because
    nothing errored. That is the failure this assertion exists to prevent, and it is the
    reason it is an assertion and not a comment.
    """
    properties = resources(network)["Vpc"]["Properties"]
    assert properties["EnableDnsSupport"] is True
    assert properties["EnableDnsHostnames"] is True


def test_the_lambda_security_group_permits_dns_egress(network: dict[str, Any]) -> None:
    """The other half of the same finding: egress rules apply to DNS queries too.

    A group locked to 443 for "HTTPS only" resolves no names at all, and fails in exactly
    the silent way above.
    """
    egress = _security_group_rules(network, "LambdaSecurityGroup", "Egress")
    dns = [rule for rule in egress if rule.get("FromPort") == 53]
    protocols = {rule["IpProtocol"] for rule in dns}
    assert protocols == {"udp", "tcp"}, "DNS over TCP is needed for large responses"
    destinations = [rule.get("CidrIp") for rule in dns]
    assert {"Fn::Ref": "VpcCidr"} in destinations, (
        "the Amazon-provided resolver sits at the VPC base address plus two"
    )
    assert "169.254.169.253/32" in destinations, "the link-local resolver alias"


def test_the_private_subnets_span_at_least_two_availability_zones(
    network: dict[str, Any],
) -> None:
    """One AZ is not a deployment, it is a database with a single point of failure."""
    private = {
        logical_id: resource["Properties"]
        for logical_id, resource in resources(network).items()
        if resource.get("Type") == "AWS::EC2::Subnet" and logical_id.startswith("Private")
    }
    assert len(private) >= 2
    zones = [repr(properties["AvailabilityZone"]) for properties in private.values()]
    assert len(set(zones)) >= 2, f"all private subnets land in one AZ: {zones}"

    subnet_group = resources(network)["DatabaseSubnetGroup"]["Properties"]["SubnetIds"]
    assert {ref["Fn::Ref"] for ref in subnet_group} == set(private)


def test_the_private_subnets_have_no_route_to_an_internet_gateway(
    network: dict[str, Any],
) -> None:
    """Private means private: egress is via the NAT, and ingress does not exist."""
    private_route_tables = {
        resource["Properties"]["RouteTableId"]["Fn::Ref"]
        for logical_id, resource in resources(network).items()
        if resource.get("Type") == "AWS::EC2::SubnetRouteTableAssociation"
        and resource["Properties"]["SubnetId"]["Fn::Ref"].startswith("Private")
    }
    assert private_route_tables, "no private subnet is associated with a route table"

    for resource in resources(network).values():
        if resource.get("Type") != "AWS::EC2::Route":
            continue
        properties = resource["Properties"]
        if properties["RouteTableId"]["Fn::Ref"] not in private_route_tables:
            continue
        assert "GatewayId" not in properties, "a private subnet routes to the IGW"
        assert properties["NatGatewayId"] == {"Fn::Ref": "NatGateway"}


def test_the_egress_path_exists_for_the_public_apis_the_worker_needs(
    network: dict[str, Any],
) -> None:
    """Anthropic is a third-party public endpoint: without this the worker cannot work."""
    egress = _security_group_rules(network, "LambdaSecurityGroup", "Egress")
    https = [rule for rule in egress if rule.get("FromPort") == 443]
    assert [rule["CidrIp"] for rule in https] == ["0.0.0.0/0"]
    assert resources(network)["NatGateway"]["Properties"]["SubnetId"] == {"Fn::Ref": "NatSubnet"}


# --------------------------------------------------------------------------------------
# Security groups: identities, not addresses
# --------------------------------------------------------------------------------------
def test_no_security_group_lets_the_internet_reach_the_database(
    network: dict[str, Any],
) -> None:
    """Every rule into the database names a group, so none of them can name the world."""
    ingress = _security_group_rules(network, "DatabaseSecurityGroup", "Ingress")
    assert ingress, "the database is unreachable; that is not the invariant either"
    for rule in ingress:
        assert "CidrIp" not in rule, f"CIDR ingress to the database: {rule}"
        assert "CidrIpv6" not in rule, f"CIDR ingress to the database: {rule}"
        assert "SourceSecurityGroupId" in rule
        assert rule["FromPort"] == rule["ToPort"] == DATABASE_PORT


def test_no_security_group_anywhere_is_open_to_the_world_inbound(
    network: dict[str, Any],
) -> None:
    """Nothing in this VPC accepts an unsolicited connection from anywhere."""
    for logical_id, resource in resources(network).items():
        if resource.get("Type") == "AWS::EC2::SecurityGroup":
            rules = resource["Properties"].get("SecurityGroupIngress", [])
        elif resource.get("Type") == "AWS::EC2::SecurityGroupIngress":
            rules = [resource["Properties"]]
        else:
            continue
        for rule in rules:
            assert rule.get("CidrIp") not in ("0.0.0.0/0", "::/0"), logical_id
            assert rule.get("CidrIpv6") != "::/0", logical_id


def test_the_worker_cannot_reach_the_database_except_through_the_proxy(
    network: dict[str, Any],
) -> None:
    """Otherwise the proxy's connection cap is advice rather than a limit.

    Every rule that would let the worker's security group open a direct connection must be
    conditional on the proxy being switched off, so that turning the proxy back on removes
    the bypass instead of leaving it in place.
    """
    egress = _security_group_rules(network, "LambdaSecurityGroup", "Egress")
    for rule in egress:
        if rule.get("DestinationSecurityGroupId") == {"Fn::Ref": "DatabaseSecurityGroup"}:
            assert rule["Condition"] == "ProxyDisabled", (
                "the worker has an unconditional direct path to Postgres"
            )

    ingress = _security_group_rules(network, "DatabaseSecurityGroup", "Ingress")
    sources = {rule["SourceSecurityGroupId"]["Fn::Ref"]: rule["Condition"] for rule in ingress}
    assert sources["ProxySecurityGroup"] is None, "the pooled path must always exist"
    assert sources["MigrationSecurityGroup"] is None, (
        "migrations bypass the proxy deliberately and unconditionally"
    )
    assert set(sources) <= {
        "ProxySecurityGroup",
        "MigrationSecurityGroup",
        "LambdaSecurityGroup",
    }, f"an unexpected group reaches the database: {sorted(sources)}"
    if "LambdaSecurityGroup" in sources:
        assert sources["LambdaSecurityGroup"] == "ProxyDisabled"


def test_the_proxy_requires_tls_in_both_directions(network: dict[str, Any]) -> None:
    proxy = resources(network)["DatabaseProxy"]["Properties"]
    assert proxy["RequireTLS"] is True
    group = resources(network)["DatabaseParameterGroup"]["Properties"]["Parameters"]
    assert group["rds.force_ssl"] == "1"


def test_the_proxy_idle_timeout_outlives_the_client_pool_recycle(
    network: dict[str, Any],
) -> None:
    """The client must give up a connection before the proxy does, not the other way."""
    from leadquali.adapters.store_postgres import DEFAULT_POOL_RECYCLE_SECONDS

    idle = resources(network)["DatabaseProxy"]["Properties"]["IdleClientTimeout"]
    assert idle > DEFAULT_POOL_RECYCLE_SECONDS


# --------------------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------------------
def test_the_adapter_still_holds_one_connection_per_container() -> None:
    """The factor of 1 in the budget below is a property of the adapter, so check it.

    If someone raises `pool_size`, every number in this file is silently wrong by that
    factor, and nothing else in the codebase would notice.
    """
    from leadquali.adapters.store_postgres import DEFAULT_MAX_OVERFLOW, DEFAULT_POOL_SIZE

    assert DEFAULT_POOL_SIZE + DEFAULT_MAX_OVERFLOW == CONNECTIONS_PER_CONTAINER


def test_max_connections_is_pinned_rather_than_derived_from_instance_memory(
    network: dict[str, Any],
) -> None:
    """The budget depends on this number, so the template sets it instead of inferring it.

    Left to the RDS default of `LEAST({DBInstanceClassMemory/9531392}, 5000)` it is a
    number nobody can read off a template and everybody has to remember.
    """
    group = resources(network)["DatabaseParameterGroup"]["Properties"]["Parameters"]
    assert group["max_connections"] == {
        "Fn::FindInMap": ["InstanceCapacity", {"Fn::Ref": "DbInstanceClass"}, "MaxConnections"]
    }
    allowed = set(parameters(network)["DbInstanceClass"]["AllowedValues"])
    assert allowed <= set(network["Mappings"]["InstanceCapacity"]), (
        "an instance class with no recorded max_connections makes the budget unresolvable"
    )


def test_the_connection_budget_is_not_oversubscribed(
    network: dict[str, Any], application: dict[str, Any]
) -> None:
    """The relationship a future person will break, stated as arithmetic.

        (worker + ingest concurrency) x connections-per-container
            <= floor(max_connections x ProxyMaxConnectionsPercent / 100)

    and, on the database side, the proxy's budget plus the migration function's direct
    connection plus Postgres's own superuser reserve must still fit in max_connections.

    It is checked for *every* allowed instance class, not just the default, so that
    changing `DbInstanceClass` cannot quietly invalidate it in either direction.
    """
    net_params = parameters(network)
    app_params = parameters(application)
    capacity = network["Mappings"]["InstanceCapacity"]
    percent = net_params["ProxyMaxConnectionsPercent"]["Default"]

    group = resources(network)["DatabaseParameterGroup"]["Properties"]["Parameters"]
    superuser_reserved = int(group["superuser_reserved_connections"])
    migration_direct = resources(application)["MigrationFunction"]["Properties"][
        "ReservedConcurrentExecutions"
    ]

    peak_containers = (
        app_params["WorkerReservedConcurrency"]["MaxValue"]
        + app_params["IngestReservedConcurrency"]["MaxValue"]
    )
    peak_connections = peak_containers * CONNECTIONS_PER_CONTAINER

    for instance_class in net_params["DbInstanceClass"]["AllowedValues"]:
        max_connections = int(capacity[instance_class]["MaxConnections"])
        proxy_budget = max_connections * percent // 100

        assert peak_connections <= proxy_budget, (
            f"{instance_class}: the application stack may be deployed with "
            f"{peak_connections} concurrent containers, each holding "
            f"{CONNECTIONS_PER_CONTAINER} connection, against a proxy budget of "
            f"{proxy_budget} ({max_connections} x {percent}%). Raise DbInstanceClass or "
            f"lower the MaxValues."
        )
        assert proxy_budget + migration_direct + superuser_reserved <= max_connections, (
            f"{instance_class}: proxy budget {proxy_budget} plus {migration_direct} "
            f"migration connection plus {superuser_reserved} reserved for superusers "
            f"exceeds max_connections {max_connections}; an operator with psql would be "
            f"locked out of the incident they are trying to fix"
        )


def test_every_function_that_touches_postgres_has_a_concurrency_cap(
    application: dict[str, Any],
) -> None:
    """Capping the worker alone bounds half the connections.

    The ingest function reads Postgres too — the idempotency check in `api/main.py` and
    the feedback links in `api/feedback.py` — so an uncapped ingest is an uncapped
    connection count no matter what the worker is set to.
    """
    for logical_id, resource in resources(application).items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource["Properties"]
        env = properties.get("Environment", {}).get("Variables", {})
        if "DATABASE_URL_SECRET_ARN" not in env:
            continue
        assert "ReservedConcurrentExecutions" in properties, (
            f"{logical_id} connects to Postgres with no cap on how many of it exist"
        )


def test_every_function_that_touches_postgres_is_in_the_vpc(
    application: dict[str, Any],
) -> None:
    """With no public database endpoint, a function outside the VPC cannot connect at all.

    This is not a latency preference. `PubliclyAccessible: false` means a function without
    a `VpcConfig` fails on its first query, which for the ingest function is the first
    lead the site ever posts.
    """
    for logical_id, resource in resources(application).items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource["Properties"]
        env = properties.get("Environment", {}).get("Variables", {})
        if "DATABASE_URL_SECRET_ARN" not in env:
            continue
        vpc = properties.get("VpcConfig")
        assert vpc is not None, f"{logical_id} reads Postgres from outside the VPC"
        assert vpc["Fn::If"][0] == "InVpc"
        assert vpc["Fn::If"][1]["SubnetIds"] == {"Fn::Ref": "VpcSubnetIds"}


def test_migrations_run_one_at_a_time(application: dict[str, Any]) -> None:
    """Two concurrent alembic runs contend for the same DDL and advisory locks."""
    properties = resources(application)["MigrationFunction"]["Properties"]
    assert properties["ReservedConcurrentExecutions"] == 1
