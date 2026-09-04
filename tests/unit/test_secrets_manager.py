"""The Secrets Manager resolver: what it caches, for how long, and how it fails.

``moto`` stands in for Secrets Manager, so every one of these runs offline and none of
them needs credentials. What they are actually about is the three properties that decide
whether this thing is safe in a Lambda:

* a fetched secret is reused for the container's life, so a lead does not cost an API call;
* the reuse is bounded, so a rotation lands without a redeploy;
* a cold start that cannot read its secret dies, rather than continuing with nothing.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from leadquali.adapters.secrets_manager import (
    STALE_RETRY_SECONDS,
    SecretResolutionError,
    SecretsManagerResolver,
)

REGION = "eu-west-1"
TTL = 300


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingClient:
    """Wraps a real (moto) client and counts ``get_secret_value`` calls.

    Counting at the boundary rather than reading the resolver's private cache: the claim
    under test is "no second API call", and that is a statement about the client.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def get_secret_value(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner.get_secret_value(**kwargs)


class ExplodingClient:
    """A client whose every call raises, for the cold-start failure path."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def get_secret_value(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise self._error


class SwitchableClient:
    """A counting client that can be made to start failing part-way through a test."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.error: Exception | None = None
        self.calls = 0

    def get_secret_value(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self._inner.get_secret_value(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _create(client: Any, name: str, value: str) -> str:
    return str(client.create_secret(Name=name, SecretString=value)["ARN"])


@mock_aws
def test_resolves_a_secret_string(clock: FakeClock) -> None:
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(client, "anthropic", "sk-ant-not-a-real-key")

    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    assert resolver.resolve(arn) == "sk-ant-not-a-real-key"


@mock_aws
def test_the_cache_prevents_a_second_api_call(clock: FakeClock) -> None:
    """The whole point: one lead must not cost one Secrets Manager call."""
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "anthropic", "first")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    values = [resolver.resolve(arn) for _ in range(5)]

    assert values == ["first"] * 5
    assert client.calls == 1


@mock_aws
def test_the_cache_is_per_arn(clock: FakeClock) -> None:
    """Two secrets are two entries; a shared slot would serve one value for both."""
    inner = boto3.client("secretsmanager", region_name=REGION)
    first = _create(inner, "one", "value-one")
    second = _create(inner, "two", "value-two")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    assert resolver.resolve(first) == "value-one"
    assert resolver.resolve(second) == "value-two"
    assert resolver.resolve(first) == "value-one"
    assert client.calls == 2


@mock_aws
def test_a_rotated_secret_is_picked_up_once_the_ttl_expires(clock: FakeClock) -> None:
    """The acceptance criterion, in a test: rotate in the console, no redeploy."""
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "anthropic", "before-rotation")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    assert resolver.resolve(arn) == "before-rotation"
    inner.put_secret_value(SecretId=arn, SecretString="after-rotation")

    clock.advance(TTL - 1)
    assert resolver.resolve(arn) == "before-rotation", "still inside the TTL"
    assert client.calls == 1

    clock.advance(2)
    assert resolver.resolve(arn) == "after-rotation"
    assert client.calls == 2


@mock_aws
def test_a_zero_ttl_disables_the_cache(clock: FakeClock) -> None:
    """The configurable knob has to reach the degenerate end, or it is not a knob."""
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "anthropic", "value")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=0, monotonic=clock)

    resolver.resolve(arn)
    resolver.resolve(arn)

    assert client.calls == 2


@mock_aws
def test_invalidate_forces_a_refetch(clock: FakeClock) -> None:
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "anthropic", "before")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    assert resolver.resolve(arn) == "before"
    inner.put_secret_value(SecretId=arn, SecretString="after")
    resolver.invalidate(arn)

    assert resolver.resolve(arn) == "after"
    assert client.calls == 2


@mock_aws
def test_invalidate_with_no_argument_clears_everything(clock: FakeClock) -> None:
    inner = boto3.client("secretsmanager", region_name=REGION)
    first = _create(inner, "one", "a")
    second = _create(inner, "two", "b")
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)
    resolver.resolve(first)
    resolver.resolve(second)

    resolver.invalidate()
    resolver.resolve(first)
    resolver.resolve(second)

    assert client.calls == 4


@mock_aws
def test_a_missing_secret_fails_with_a_message_naming_the_arn(clock: FakeClock) -> None:
    """An operator reading the log must learn *which* secret, not merely that one failed."""
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = f"arn:aws:secretsmanager:{REGION}:123456789012:secret:leadquali/prod/absent-AbCdEf"
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    with pytest.raises(SecretResolutionError) as caught:
        resolver.resolve(arn)

    assert arn in str(caught.value)
    assert "ResourceNotFoundException" in str(caught.value)


@mock_aws
def test_a_binary_only_secret_is_refused_by_name(clock: FakeClock) -> None:
    """Every secret this system holds is text; a binary one is a configuration mistake."""
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = str(client.create_secret(Name="binary", SecretBinary=b"\x00\x01")["ARN"])
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    with pytest.raises(SecretResolutionError) as caught:
        resolver.resolve(arn)

    assert arn in str(caught.value)
    assert "SecretString" in str(caught.value)


def test_a_cold_start_failure_is_fatal_and_caches_nothing(clock: FakeClock) -> None:
    """No cached value means no fallback: raise, do not return an empty secret.

    A resolver that swallowed this would hand the caller ``""`` and the process would
    carry on with an unauthenticated ingest endpoint or an unsigned feedback link.
    """
    client = ExplodingClient(RuntimeError("no credentials"))
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    for _ in range(2):
        with pytest.raises(SecretResolutionError):
            resolver.resolve("arn:aws:secretsmanager:eu-west-1:1:secret:x")

    assert client.calls == 2, "a failure must not be cached as a value"


@mock_aws
def test_a_refresh_failure_serves_the_last_known_value(clock: FakeClock) -> None:
    """Throttling at refresh time must not take the pipeline down.

    The distinction is deliberate: with nothing cached there is no safe answer and the
    call raises, but with a value already in hand the honest choice is to keep using it
    and retry sooner than a full TTL later. Secrets Manager throttles per account, so the
    failure mode this guards against is a burst of cold starts, not a broken secret.
    """
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "anthropic", "known-good")
    client = SwitchableClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)
    assert resolver.resolve(arn) == "known-good"

    client.error = RuntimeError("Throttling")
    clock.advance(TTL + 1)

    assert resolver.resolve(arn) == "known-good"
    assert client.calls == 2

    # ... and the retry is sooner than a whole TTL later, but not on every call.
    assert resolver.resolve(arn) == "known-good"
    assert client.calls == 2
    clock.advance(STALE_RETRY_SECONDS + 1)
    assert resolver.resolve(arn) == "known-good"
    assert client.calls == 3


@mock_aws
def test_resolve_mapping_parses_a_json_secret(clock: FakeClock) -> None:
    """The shape RDS writes for a managed master user password."""
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(client, "db", '{"username": "leadquali", "password": "p@ss/word"}')
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    assert resolver.resolve_mapping(arn) == {"username": "leadquali", "password": "p@ss/word"}


@mock_aws
def test_resolve_mapping_shares_the_cache_with_resolve(clock: FakeClock) -> None:
    inner = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(inner, "db", '{"username": "u", "password": "p"}')
    client = CountingClient(inner)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    resolver.resolve_mapping(arn)
    resolver.resolve_mapping(arn)
    resolver.resolve(arn)

    assert client.calls == 1


@mock_aws
@pytest.mark.parametrize("body", ["not json at all", "[1, 2]", '{"port": 5432}'])
def test_resolve_mapping_rejects_a_shape_it_cannot_use(clock: FakeClock, body: str) -> None:
    """A JSON secret is a contract; a list, a scalar or a non-string value breaks it."""
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(client, "db", body)
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    with pytest.raises(SecretResolutionError) as caught:
        resolver.resolve_mapping(arn)

    assert arn in str(caught.value)


@mock_aws
def test_the_secret_value_never_appears_in_an_error(clock: FakeClock) -> None:
    """Errors end up in logs; a message quoting the secret would put it there."""
    client = boto3.client("secretsmanager", region_name=REGION)
    arn = _create(client, "db", "sk-ant-super-secret-value")
    resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)

    with pytest.raises(SecretResolutionError) as caught:
        resolver.resolve_mapping(arn)

    assert "sk-ant-super-secret-value" not in str(caught.value)


def test_a_negative_ttl_is_refused() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        SecretsManagerResolver(ExplodingClient(RuntimeError()), ttl_seconds=-1)


def test_the_resolver_never_renders_its_cache(clock: FakeClock) -> None:
    """``repr`` lands in tracebacks; a cache of secrets must not be printable."""
    with mock_aws():
        client = boto3.client("secretsmanager", region_name=REGION)
        arn = _create(client, "anthropic", "sk-ant-secret")
        resolver = SecretsManagerResolver(client, ttl_seconds=TTL, monotonic=clock)
        resolver.resolve(arn)

        assert "sk-ant-secret" not in repr(resolver)
