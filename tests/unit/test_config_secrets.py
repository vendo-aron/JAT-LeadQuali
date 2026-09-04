"""Secrets reach the application through ``Settings`` and through nothing else.

``CLAUDE.md``: *secrets come from the environment via ``leadquali.config.Settings``*. #28
adds a second place a secret can come from — Secrets Manager — and the requirement is
that no caller can tell. Every ``require_*`` here is exercised twice: once with a plain
environment variable (a laptop with a ``.env`` and no AWS at all) and once with an ARN.

The resolver is injected, so nothing in this file needs credentials, a network, or moto.
``tests/unit/test_secrets_manager.py`` is where the real client is exercised.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from leadquali.adapters.secrets_manager import SecretResolutionError
from leadquali.config import (
    DATABASE_DRIVER,
    DEFAULT_SECRETS_CACHE_TTL_SECONDS,
    Settings,
    build_database_url,
    set_secret_resolver,
)

DB_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:leadquali/prod/db-Ab12"
KEY_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:leadquali/prod/key-Cd34"
INGEST_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:leadquali/prod/in-Ef56"
FEEDBACK_SECRET_ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:leadquali/prod/fb-Gh78"

#: Long enough to pass `signing.MIN_SIGNING_SECRET_CHARS` and `feedback.MIN_TOKEN_SECRET_CHARS`.
A_SIGNING_SECRET = "s" * 40
ANOTHER_SECRET = "f" * 40

# Everything Settings reads that a developer might have exported. Cleared per test, or
# these assertions depend on the shell they run in.
CONFIGURED_VARIABLES = (
    "ENV",
    "LOG_LEVEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_SECRET_ARN",
    "DATABASE_URL",
    "DATABASE_SECRET_ARN",
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "INGEST_CREDENTIALS",
    "INGEST_CREDENTIALS_SECRET_ARN",
    "FEEDBACK_TOKEN_SECRET",
    "FEEDBACK_TOKEN_SECRET_ARN",
    "SECRETS_CACHE_TTL_SECONDS",
)


class FakeResolver:
    """A stand-in for the Secrets Manager adapter that records what it was asked for."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.asked: list[str] = []

    def resolve(self, secret_arn: str) -> str:
        self.asked.append(secret_arn)
        try:
            return self.values[secret_arn]
        except KeyError:
            raise SecretResolutionError(f"cannot read secret {secret_arn}: Fake") from None

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:
        parsed = json.loads(self.resolve(secret_arn))
        assert isinstance(parsed, dict)
        return {str(k): str(v) for k, v in parsed.items()}


class ForbiddenResolver:
    """Fails the test if anything reaches for AWS. This is the local-development guard."""

    def resolve(self, secret_arn: str) -> str:
        raise AssertionError(f"nothing should have resolved {secret_arn}")

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:
        raise AssertionError(f"nothing should have resolved {secret_arn}")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in CONFIGURED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    yield
    set_secret_resolver(None)


def _settings(resolver: Any = None, **overrides: Any) -> Settings:
    set_secret_resolver(resolver if resolver is not None else ForbiddenResolver())
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# --------------------------------------------------------------- local, no AWS at all


def test_local_development_needs_no_resolver() -> None:
    """A ``.env`` and no AWS: the resolver is never touched, so it need not exist."""
    settings = _settings(
        anthropic_api_key="sk-ant-local",
        database_url="postgresql+psycopg://u:p@localhost:5432/lq",
        ingest_credentials="{}",
        feedback_token_secret=A_SIGNING_SECRET,
    )

    assert settings.require_anthropic_api_key() == "sk-ant-local"
    assert settings.require_database_url() == "postgresql+psycopg://u:p@localhost:5432/lq"
    assert settings.require_ingest_credentials() == "{}"
    assert settings.require_feedback_token_secret() == A_SIGNING_SECRET


def test_an_unset_secret_still_raises_its_own_message() -> None:
    settings = _settings()
    for call, expected in (
        (settings.require_anthropic_api_key, "ANTHROPIC_API_KEY"),
        (settings.require_database_url, "DATABASE_URL"),
        (settings.require_ingest_credentials, "INGEST_CREDENTIALS"),
        (settings.require_feedback_token_secret, "FEEDBACK_TOKEN_SECRET"),
    ):
        with pytest.raises(RuntimeError, match=expected):
            call()


# ------------------------------------------------------------------- resolved secrets


def test_each_require_helper_resolves_its_arn() -> None:
    resolver = FakeResolver(
        {
            KEY_SECRET_ARN: "sk-ant-from-aws",
            INGEST_SECRET_ARN: "{}",
            FEEDBACK_SECRET_ARN: A_SIGNING_SECRET,
        }
    )
    settings = _settings(
        resolver,
        anthropic_api_key_secret_arn=KEY_SECRET_ARN,
        ingest_credentials_secret_arn=INGEST_SECRET_ARN,
        feedback_token_secret_arn=FEEDBACK_SECRET_ARN,
    )

    assert settings.require_anthropic_api_key() == "sk-ant-from-aws"
    assert settings.require_ingest_credentials() == "{}"
    assert settings.require_feedback_token_secret() == A_SIGNING_SECRET


def test_a_resolved_secret_wins_over_a_plain_environment_variable() -> None:
    """Both set is a deployment mid-migration, or a stale ``.env`` in an image.

    Preferring the ARN is the safe direction: the environment variable is the thing that
    can be read off a Lambda's configuration page, so it must never be the one that wins.
    """
    resolver = FakeResolver({KEY_SECRET_ARN: "sk-ant-from-aws"})
    settings = _settings(
        resolver,
        anthropic_api_key="sk-ant-stale-env-value",
        anthropic_api_key_secret_arn=KEY_SECRET_ARN,
    )

    assert settings.require_anthropic_api_key() == "sk-ant-from-aws"


def test_a_cold_start_resolution_failure_is_fatal() -> None:
    """Not a fallback to the unset value, and not a fallback to the environment.

    An ARN that cannot be read is a broken deployment. Falling back would start an ingest
    endpoint whose credential map is whatever happened to be in the environment.
    """
    resolver = FakeResolver({})
    settings = _settings(
        resolver,
        anthropic_api_key="sk-ant-stale-env-value",
        anthropic_api_key_secret_arn=KEY_SECRET_ARN,
    )

    with pytest.raises(SecretResolutionError, match=KEY_SECRET_ARN):
        settings.require_anthropic_api_key()


def test_settings_reads_the_arns_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Lambda's view: four environment variables, none of them a value."""
    monkeypatch.setenv("ANTHROPIC_API_KEY_SECRET_ARN", KEY_SECRET_ARN)
    monkeypatch.setenv("DATABASE_SECRET_ARN", DB_SECRET_ARN)
    monkeypatch.setenv("INGEST_CREDENTIALS_SECRET_ARN", INGEST_SECRET_ARN)
    monkeypatch.setenv("FEEDBACK_TOKEN_SECRET_ARN", FEEDBACK_SECRET_ARN)

    settings = _settings()

    assert settings.anthropic_api_key_secret_arn == KEY_SECRET_ARN
    assert settings.database_secret_arn == DB_SECRET_ARN
    assert settings.ingest_credentials_secret_arn == INGEST_SECRET_ARN
    assert settings.feedback_token_secret_arn == FEEDBACK_SECRET_ARN


# ------------------------------------------------------------------- the database URL


def test_the_database_url_is_assembled_from_parts_and_requires_tls() -> None:
    """#27's endpoint, port and name plus RDS's own password secret.

    ``sslmode=require`` is not decoration: the instance sets ``rds.force_ssl=1`` and the
    proxy sets ``RequireTLS``, so a URL that negotiates plaintext is refused outright.
    """
    resolver = FakeResolver(
        {DB_SECRET_ARN: json.dumps({"username": "leadquali", "password": "swordfish"})}
    )
    settings = _settings(
        resolver,
        database_secret_arn=DB_SECRET_ARN,
        database_host="leadquali-prod.proxy-abc.eu-west-1.rds.amazonaws.com",
        database_port=5432,
        database_name="leadquali",
    )

    url = settings.require_database_url()

    assert url == (
        f"{DATABASE_DRIVER}://leadquali:swordfish@"
        "leadquali-prod.proxy-abc.eu-west-1.rds.amazonaws.com:5432/leadquali?sslmode=require"
    )


def test_the_assembled_url_wins_over_a_plain_database_url() -> None:
    resolver = FakeResolver({DB_SECRET_ARN: json.dumps({"username": "u", "password": "p"})})
    settings = _settings(
        resolver,
        database_url="postgresql+psycopg://stale:stale@localhost:5432/stale",
        database_secret_arn=DB_SECRET_ARN,
        database_host="db.internal",
        database_name="leadquali",
    )

    assert "stale" not in settings.require_database_url()


def test_a_generated_password_is_percent_encoded() -> None:
    """RDS generates punctuation; an unescaped ``@`` or ``/`` silently reshapes the URL."""
    resolver = FakeResolver(
        {DB_SECRET_ARN: json.dumps({"username": "lead/quali", "password": "p@ss:w/rd?x#y"})}
    )
    settings = _settings(
        resolver,
        database_secret_arn=DB_SECRET_ARN,
        database_host="db.internal",
        database_name="leadquali",
    )

    url = settings.require_database_url()

    assert "p@ss" not in url
    assert "lead%2Fquali:p%40ss%3Aw%2Frd%3Fx%23y@db.internal:5432/leadquali" in url


def test_the_assembled_url_round_trips_through_sqlalchemy() -> None:
    """The check that matters: the URL a driver parses back must hold the same password."""
    from sqlalchemy.engine import make_url

    password = "p@ss:w/rd?x#y &+%"
    url = make_url(
        build_database_url(
            host="db.internal",
            port=5432,
            database="leadquali",
            username="lead quali",
            password=password,
        )
    )

    assert url.password == password
    assert url.username == "lead quali"
    assert url.host == "db.internal"
    assert url.database == "leadquali"
    assert url.query["sslmode"] == "require"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"database_name": "leadquali"}, "DATABASE_HOST"),
        ({"database_host": "db.internal"}, "DATABASE_NAME"),
    ],
)
def test_a_database_secret_without_its_endpoint_fails_by_name(
    overrides: dict[str, Any], expected: str
) -> None:
    """Half-configured is the deploy mistake: the ARN wired up, the endpoint forgotten."""
    resolver = FakeResolver({DB_SECRET_ARN: json.dumps({"username": "u", "password": "p"})})
    settings = _settings(resolver, database_secret_arn=DB_SECRET_ARN, **overrides)

    with pytest.raises(RuntimeError, match=expected):
        settings.require_database_url()


@pytest.mark.parametrize(
    "body", ['{"username": "u"}', '{"password": "p"}', '{"username": "", "password": "p"}']
)
def test_a_database_secret_missing_a_field_names_the_arn(body: str) -> None:
    resolver = FakeResolver({DB_SECRET_ARN: body})
    settings = _settings(
        resolver,
        database_secret_arn=DB_SECRET_ARN,
        database_host="db.internal",
        database_name="leadquali",
    )

    with pytest.raises(RuntimeError, match=DB_SECRET_ARN):
        settings.require_database_url()


def test_the_database_password_never_appears_in_an_error() -> None:
    resolver = FakeResolver({DB_SECRET_ARN: json.dumps({"password": "swordfish"})})
    settings = _settings(
        resolver,
        database_secret_arn=DB_SECRET_ARN,
        database_host="db.internal",
        database_name="leadquali",
    )

    with pytest.raises(RuntimeError) as caught:
        settings.require_database_url()

    assert "swordfish" not in str(caught.value)


# ------------------------------------------- #60: the feedback secret stands on its own


def _ingest_map(signing_secret: str) -> str:
    return json.dumps(
        {"acme": {"api_key_sha256": "a" * 64, "signing_secret": signing_secret}},
    )


def test_the_feedback_secret_may_not_be_an_ingest_signing_secret() -> None:
    """#60, enforced rather than documented.

    The two secrets authorise opposite things: an ingest signing secret is *given to a
    customer's website*, and the feedback secret authorises writes to the training data.
    Reusing one for the other hands every customer the ability to mint feedback links for
    every tenant.
    """
    settings = _settings(
        ingest_credentials=_ingest_map(A_SIGNING_SECRET),
        feedback_token_secret=A_SIGNING_SECRET,
    )

    with pytest.raises(RuntimeError, match="acme"):
        settings.require_feedback_token_secret()


def test_the_check_holds_when_both_secrets_come_from_secrets_manager() -> None:
    """The production shape: two ARNs whose *values* collide."""
    resolver = FakeResolver(
        {
            INGEST_SECRET_ARN: _ingest_map(A_SIGNING_SECRET),
            FEEDBACK_SECRET_ARN: A_SIGNING_SECRET,
        }
    )
    settings = _settings(
        resolver,
        ingest_credentials_secret_arn=INGEST_SECRET_ARN,
        feedback_token_secret_arn=FEEDBACK_SECRET_ARN,
    )

    with pytest.raises(RuntimeError, match="acme"):
        settings.require_feedback_token_secret()


def test_distinct_secrets_are_accepted() -> None:
    settings = _settings(
        ingest_credentials=_ingest_map(A_SIGNING_SECRET),
        feedback_token_secret=ANOTHER_SECRET,
    )

    assert settings.require_feedback_token_secret() == ANOTHER_SECRET


def test_the_collision_error_does_not_quote_the_secret() -> None:
    settings = _settings(
        ingest_credentials=_ingest_map(A_SIGNING_SECRET),
        feedback_token_secret=A_SIGNING_SECRET,
    )

    with pytest.raises(RuntimeError) as caught:
        settings.require_feedback_token_secret()

    assert A_SIGNING_SECRET not in str(caught.value)


def test_unparseable_ingest_credentials_do_not_break_the_feedback_secret() -> None:
    """The distinctness check is a guard, not a second validator.

    ``api/signing.load_credentials`` is what rejects a malformed credential map, with a
    message about the malformation. If this check raised its own error first, an operator
    would be sent looking for a secret collision that does not exist.
    """
    settings = _settings(
        ingest_credentials="not json at all",
        feedback_token_secret=A_SIGNING_SECRET,
    )

    assert settings.require_feedback_token_secret() == A_SIGNING_SECRET


def test_the_worker_shape_needs_no_ingest_credentials_to_get_its_feedback_secret() -> None:
    """The worker signs feedback links and has no ingest credentials at all.

    If the check demanded them it would put an ARN the worker must not be able to read
    into the worker's environment — the opposite of the least-privilege criterion.
    """
    resolver = FakeResolver({FEEDBACK_SECRET_ARN: A_SIGNING_SECRET})
    settings = _settings(resolver, feedback_token_secret_arn=FEEDBACK_SECRET_ARN)

    assert settings.require_feedback_token_secret() == A_SIGNING_SECRET
    assert resolver.asked == [FEEDBACK_SECRET_ARN]


# ------------------------------------------------------------------------------ the TTL


def test_the_cache_ttl_defaults_and_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings().secrets_cache_ttl_seconds == DEFAULT_SECRETS_CACHE_TTL_SECONDS
    monkeypatch.setenv("SECRETS_CACHE_TTL_SECONDS", "60")
    assert _settings().secrets_cache_ttl_seconds == 60


@pytest.mark.parametrize("value", ["-1", "86401"])
def test_an_absurd_cache_ttl_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A negative TTL is meaningless and a day-long one is a redeploy requirement."""
    from pydantic import ValidationError

    monkeypatch.setenv("SECRETS_CACHE_TTL_SECONDS", value)
    with pytest.raises(ValidationError):
        _settings()


def test_the_default_ttl_bounds_rotation_pickup_to_minutes() -> None:
    """The number is a judgement call; that it is in this range is not.

    Long enough to amortise the API call over hundreds of leads, short enough that
    "rotated in the console, picked up with no redeploy" is measured in minutes.
    """
    assert 60 <= DEFAULT_SECRETS_CACHE_TTL_SECONDS <= 900
