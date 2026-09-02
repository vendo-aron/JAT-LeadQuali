"""Settings are environment-driven, secret-safe, and never default to production."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from leadquali.config import Environment, Settings, get_settings

# Variables Settings reads. Tests that assert a value is *absent* must clear these, or
# they pass or fail depending on the developer's shell — and DATABASE_URL is routinely
# exported for the Postgres integration suite (see docs/local-database.md).
CONFIGURED_VARIABLES = ("ENV", "LOG_LEVEL", "ANTHROPIC_API_KEY", "DATABASE_URL")


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def _with_a_clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CONFIGURED_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_local_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_a_clean_environment(monkeypatch)
    settings = _settings(_env_file=None)
    assert settings.env is Environment.LOCAL
    assert settings.log_level == "INFO"
    assert settings.anthropic_api_key is None
    assert settings.database_url is None
    assert settings.is_production is False


def test_reads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/lq")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("ENV", "prod")

    settings = _settings(_env_file=None)

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert settings.database_url == "postgresql+psycopg://u:p@localhost:5432/lq"
    assert settings.log_level == "DEBUG"
    assert settings.env is Environment.PROD
    assert settings.is_production is True


def test_api_key_is_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_a_clean_environment(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    settings = _settings(_env_file=None)
    assert "sk-ant-super-secret" not in repr(settings)
    assert "sk-ant-super-secret" not in str(settings.model_dump())


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "chatty")
    with pytest.raises(ValidationError):
        _settings(_env_file=None)


def test_invalid_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "staging-ish")
    with pytest.raises(ValidationError):
        _settings(_env_file=None)


def test_require_helpers_raise_with_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_a_clean_environment(monkeypatch)
    settings = _settings(_env_file=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        settings.require_anthropic_api_key()
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        settings.require_database_url()


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("ENV", "dev")
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
