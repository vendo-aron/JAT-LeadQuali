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

from leadquali.app.feedback import DEFAULT_TOKEN_TTL_DAYS

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
    ingest_credentials: SecretStr | None = Field(
        default=None,
        description=(
            "Per-tenant ingest secrets as JSON: "
            '{"<tenant_id>": {"api_key_sha256": "<64 hex>", "signing_secret": "..."}}. '
            "Required by the public ingest API; see leadquali.api.signing."
        ),
    )

    aws_region: str | None = Field(
        default=None,
        description=(
            "AWS region for the SES client. Unset falls back to the ambient AWS chain, "
            "which is what a Lambda already has."
        ),
    )
    ses_sender: str | None = Field(
        default=None,
        description=(
            "The From identity for routing email, e.g. 'LeadQuali <leads@mail.example.com>'. "
            "The address must be a verified SES identity; see docs/runbooks/ses-setup.md."
        ),
    )
    ses_configuration_set: str | None = Field(
        default=None,
        description=(
            "SES configuration set routing bounce, complaint and delivery events. Optional "
            "in that a send succeeds without one, and required in practice: without it "
            "those events go nowhere."
        ),
    )
    feedback_base_url: str | None = Field(
        default=None,
        description=(
            "Public absolute origin the one-click feedback links point at, e.g. "
            "'https://api.example.com/prod'. Configuration rather than a request header: "
            "the worker composing the email has no request to derive a host from."
        ),
    )
    feedback_token_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Signing secret for feedback link tokens, 32+ characters. Distinct from the "
            "per-tenant ingest signing secrets: those are held by a customer's website, and "
            "this one authorises writes to the training data. See leadquali.app.feedback."
        ),
    )
    feedback_token_ttl_days: int = Field(
        default=DEFAULT_TOKEN_TTL_DAYS,
        gt=0,
        le=365,
        description="How long a feedback link stays usable, in days.",
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

    def require_ingest_credentials(self) -> str:
        """Return the ingest credential JSON, or raise if it was never configured.

        There is deliberately no "no credentials means no authentication" fallback: an
        unauthenticated public ingest endpoint is worse than one that will not start.
        """
        if self.ingest_credentials is None:
            raise RuntimeError(
                "INGEST_CREDENTIALS is not set. The ingest API refuses to run without "
                "per-tenant keys; export it in the environment (or add it to .env for "
                "local development)."
            )
        return self.ingest_credentials.get_secret_value()

    def require_ses_sender(self) -> str:
        """Return the SES sender identity, or raise if it was never configured.

        No default and no guess. A notifier that invented a sender would send from an
        unverified identity — every message rejected — or, if it happened to guess a
        verified one, from another environment's domain.
        """
        if self.ses_sender is None or not self.ses_sender.strip():
            raise RuntimeError(
                "SES_SENDER is not set. Export the verified sender identity from "
                "docs/runbooks/ses-setup.md (or add it to .env for local development)."
            )
        return self.ses_sender

    def require_feedback_base_url(self) -> str:
        """Return the public origin for feedback links, or raise if it was never configured.

        Without it the routing email would carry either no feedback links or relative ones,
        and the feedback table — the only source of the golden set — would never grow.
        """
        if self.feedback_base_url is None or not self.feedback_base_url.strip():
            raise RuntimeError(
                "FEEDBACK_BASE_URL is not set. Export the public origin the feedback "
                "endpoint is served on, e.g. https://api.example.com/prod."
            )
        return self.feedback_base_url

    def require_feedback_token_secret(self) -> str:
        """Return the feedback signing secret, or raise if it was never configured.

        There is deliberately no "unsigned links when no secret is set" fallback: that would
        be a public URL that writes whatever it is given into the training data.
        """
        if self.feedback_token_secret is None:
            raise RuntimeError(
                "FEEDBACK_TOKEN_SECRET is not set. Feedback links are signed capabilities "
                "and there is no unsigned mode; export 32+ characters of random material."
            )
        return self.feedback_token_secret.get_secret_value()

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
