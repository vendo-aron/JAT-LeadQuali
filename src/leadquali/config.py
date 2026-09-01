"""Application settings.

Every value comes from the environment. Nothing here carries a secret literal, and no
default points at production: an unconfigured process runs as ``local`` and fails loudly
the moment it needs a credential it was never given.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    """Deployment environment."""

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    """Process configuration, read from the environment (or a local ``.env``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Field(default=Environment.LOCAL, description="Deployment environment.")
    log_level: LogLevel = Field(default="INFO", description="Root log level.")
    anthropic_api_key: SecretStr | None = Field(
        default=None, description="Anthropic API key; required by the qualification worker."
    )
    database_url: str | None = Field(
        default=None, description="SQLAlchemy URL for Postgres; required by the store adapter."
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        """True only in the production environment."""
        return self.env is Environment.PROD

    def require_anthropic_api_key(self) -> str:
        """Return the Anthropic API key, or raise if it was never configured."""
        if self.anthropic_api_key is None:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it in the environment "
                "(or add it to .env for local development)."
            )
        return self.anthropic_api_key.get_secret_value()

    def require_database_url(self) -> str:
        """Return the database URL, or raise if it was never configured."""
        if self.database_url is None:
            raise RuntimeError(
                "DATABASE_URL is not set. Export it in the environment "
                "(or add it to .env for local development)."
            )
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings, read once."""
    return Settings()
