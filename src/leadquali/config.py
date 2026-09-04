"""Application settings.

Every value comes from the environment. Nothing here carries a secret literal, and no
default points at production: an unconfigured process runs as ``local`` and fails loudly
the moment it needs a credential it was never given.

Two sources, one code path (#28)
--------------------------------

In AWS the secrets are not in the environment; the *ARNs* of their Secrets Manager
entries are, and the values are fetched at cold start and cached (see
:mod:`leadquali.adapters.secrets_manager`). On a laptop there is a ``.env`` file and no
AWS at all. Rather than scatter ``if settings.is_production`` through the callers, that
choice is made once, per secret, inside the matching ``require_*`` method:

* ``<NAME>_SECRET_ARN`` set — resolve it. A failure here is fatal, never a fallback.
* otherwise ``<NAME>`` set — use it.
* otherwise raise, with the same message it always raised.

So ``AnthropicLeadAssessor``, the SES notifier and the Postgres store are unchanged, and
a developer with a ``.env`` never constructs a ``boto3`` client — the adapter is imported
lazily, inside the method that needs it, so importing this module does not pull ``boto3``
into the graph.

The database URL is the one secret that is *assembled* rather than stored. RDS manages
and rotates the master password in its own secret (#27's ``ManageMasterUserPassword``);
storing a second secret containing a full URL with that password in it would duplicate
the thing RDS is already rotating, and the duplicate would go stale silently. So the
host, port and database name arrive as plain configuration from #27's stack outputs, the
username and password come from the RDS-managed secret, and :func:`build_database_url`
puts them together — with ``sslmode=require``, because the instance sets
``rds.force_ssl=1`` and the proxy sets ``RequireTLS`` and will refuse anything else.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterator, Mapping
from enum import StrEnum
from functools import lru_cache
from typing import Final, Literal, Protocol
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from leadquali.app.feedback import DEFAULT_TOKEN_TTL_DAYS

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

#: How long a secret fetched from Secrets Manager is served before it is fetched again.
#:
#: Five minutes. The upper bound is set by the rotation story: #28 requires that a secret
#: rotated in the console reaches the worker *without a redeploy*, and the TTL is exactly
#: how long that takes, so it also bounds how long a leaked credential stays in use after
#: someone replaces it. The lower bound is cost and rate limit: at #26's reserved
#: concurrency the whole fleet is at most 25 warm containers holding four secrets each,
#: so five minutes caps the system at roughly a third of a call per second and a few
#: dollars a month, while a per-invocation fetch would put four round trips over the NAT
#: gateway on the path of every lead. See ``docs/runbooks/secrets-and-rotation.md``.
DEFAULT_SECRETS_CACHE_TTL_SECONDS: Final[int] = 300

#: A day. Past this the cache is a redeploy requirement wearing a TTL's clothes.
MAX_SECRETS_CACHE_TTL_SECONDS: Final[int] = 86_400

#: The SQLAlchemy driver the store adapter is built for; see ``adapters/store_postgres``.
DATABASE_DRIVER: Final[str] = "postgresql+psycopg"

#: Not negotiable, and not a preference. ``rds.force_ssl=1`` on the instance and
#: ``RequireTLS: true`` on the proxy (#27) both refuse a plaintext session, so a URL
#: without this does not connect at all.
DATABASE_SSLMODE: Final[str] = "require"

#: Default Postgres port, so a deployment only has to state the interesting parts.
DEFAULT_DATABASE_PORT: Final[int] = 5432


class SecretResolver(Protocol):
    """What :class:`Settings` needs from a secret store.

    A Protocol rather than the concrete adapter so that this module never imports
    ``boto3`` — the layering rule — and so a test can inject a dictionary.
    """

    def resolve(self, secret_arn: str) -> str:
        """Return the secret's current string value."""
        ...

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:
        """Return the secret's current value parsed as a JSON object of strings."""
        ...


_RESOLVER: SecretResolver | None = None


def set_secret_resolver(resolver: SecretResolver | None) -> None:
    """Install the process-wide secret resolver, or clear it.

    Process-wide because the cache has to be: a resolver per :class:`Settings` instance
    would fetch every secret again on every construction, which is the exact cost the
    cache exists to avoid. Cleared with ``None``, which is what a test does in teardown.

    Args:
        resolver: The resolver to use, or ``None`` to fall back to the real
            Secrets Manager adapter, built on first use.
    """
    global _RESOLVER
    _RESOLVER = resolver


def build_database_url(
    *,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> str:
    """Assemble a SQLAlchemy URL from its parts, TLS included.

    Every component is percent-encoded with nothing treated as safe, because RDS
    generates passwords containing punctuation and an unescaped ``@``, ``/`` or ``:``
    does not fail — it silently reparses into a different host, database or port.

    Args:
        host: The endpoint from #27 (the RDS Proxy endpoint when the proxy is enabled).
        port: The database port.
        database: The database name.
        username: The master username from the RDS-managed secret.
        password: The master password from the RDS-managed secret.

    Returns:
        A URL of the form
        ``postgresql+psycopg://user:pass@host:port/db?sslmode=require``.
    """
    user = quote(username, safe="")
    secret = quote(password, safe="")
    name = quote(database, safe="")
    return f"{DATABASE_DRIVER}://{user}:{secret}@{host}:{port}/{name}?sslmode={DATABASE_SSLMODE}"


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

    # ------------------------------------------------------------------ Secrets Manager
    #
    # ARNs, never values. Setting one takes precedence over the matching plain variable:
    # the environment variable is the half of the configuration that is visible on a
    # Lambda's console page, so it must never be the half that wins.
    anthropic_api_key_secret_arn: str | None = Field(
        default=None,
        description=(
            "Secrets Manager ARN holding the Anthropic API key. Created by hand per "
            "#43's runbook, never by CloudFormation, because the value comes from the "
            "Anthropic console."
        ),
    )
    ingest_credentials_secret_arn: str | None = Field(
        default=None,
        description=(
            "Secrets Manager ARN holding the ingest credential JSON. The secret itself "
            "is created empty by infra/network.yaml and written by tenant onboarding."
        ),
    )
    feedback_token_secret_arn: str | None = Field(
        default=None,
        description=(
            "Secrets Manager ARN holding the feedback-link signing secret, generated by "
            "infra/network.yaml. Must never be an ingest signing secret (#60), which "
            "require_feedback_token_secret enforces."
        ),
    )
    database_secret_arn: str | None = Field(
        default=None,
        description=(
            "Secrets Manager ARN of the RDS-managed master user secret (#27's "
            "DatabaseMasterUserSecretArn): {'username': ..., 'password': ...}. The URL "
            "is assembled from it and database_host/port/name, never stored."
        ),
    )
    database_host: str | None = Field(
        default=None,
        description=(
            "Database endpoint from #27 — the RDS Proxy endpoint when the proxy is on. "
            "Configuration, not a secret: it resolves only inside the VPC."
        ),
    )
    database_port: int = Field(
        default=DEFAULT_DATABASE_PORT,
        gt=0,
        le=65535,
        description="Database port; only interesting if #27's instance is moved off 5432.",
    )
    database_name: str | None = Field(
        default=None, description="Database name; 'leadquali' in every deployed stack."
    )
    secrets_cache_ttl_seconds: int = Field(
        default=DEFAULT_SECRETS_CACHE_TTL_SECONDS,
        ge=0,
        le=MAX_SECRETS_CACHE_TTL_SECONDS,
        description=(
            "How long a resolved secret is reused before it is fetched again. This is "
            "the delay between rotating a secret and the fleet using it. Zero disables "
            "the cache, which is correct only for a test."
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

    def secret_resolver(self) -> SecretResolver:
        """The process-wide secret resolver, built on first use.

        The import is inside the method on purpose. ``adapters.secrets_manager`` imports
        ``boto3``; doing it at module scope would mean every process that reads a setting
        — a CLI run, a unit test, a developer's shell — pays to import the AWS SDK, and
        it would make this module's own import graph depend on a layer above it.

        Returns:
            The resolver installed by :func:`set_secret_resolver`, or a
            :class:`~leadquali.adapters.secrets_manager.SecretsManagerResolver` built
            against this process's TTL and region.
        """
        global _RESOLVER
        if _RESOLVER is None:
            from leadquali.adapters.secrets_manager import SecretsManagerResolver

            _RESOLVER = SecretsManagerResolver.from_env(
                ttl_seconds=self.secrets_cache_ttl_seconds,
                region_name=self.aws_region,
            )
        return _RESOLVER

    def _secret(self, *, secret_arn: str | None, literal: SecretStr | None, unset: str) -> str:
        """One secret, from Secrets Manager if an ARN is configured and locally if not.

        Args:
            secret_arn: The ARN to resolve, or ``None``.
            literal: The plain configured value, or ``None``.
            unset: The message to raise when neither is configured.

        Returns:
            The secret's value.

        Raises:
            SecretResolutionError: the ARN is set and could not be read. Deliberately not
                caught: falling back to ``literal`` here would mean a broken deployment
                quietly running on whatever stale value an image happened to bake in.
            RuntimeError: neither source is configured.
        """
        if secret_arn:
            return self.secret_resolver().resolve(secret_arn)
        if literal is not None:
            return literal.get_secret_value()
        raise RuntimeError(unset)

    def require_anthropic_api_key(self) -> str:
        """Return the Anthropic API key, or raise if it was never configured."""
        return self._secret(
            secret_arn=self.anthropic_api_key_secret_arn,
            literal=self.anthropic_api_key,
            unset=(
                "ANTHROPIC_API_KEY is not set. Export it in the environment "
                "(or add it to .env for local development), or set "
                "ANTHROPIC_API_KEY_SECRET_ARN to a Secrets Manager ARN."
            ),
        )

    def require_ingest_credentials(self) -> str:
        """Return the ingest credential JSON, or raise if it was never configured.

        There is deliberately no "no credentials means no authentication" fallback: an
        unauthenticated public ingest endpoint is worse than one that will not start.
        """
        return self._secret(
            secret_arn=self.ingest_credentials_secret_arn,
            literal=self.ingest_credentials,
            unset=(
                "INGEST_CREDENTIALS is not set. The ingest API refuses to run without "
                "per-tenant keys; export it in the environment (or add it to .env for "
                "local development), or set INGEST_CREDENTIALS_SECRET_ARN."
            ),
        )

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

        Raises:
            RuntimeError: the secret is unset, or is the same value as one of the ingest
                signing secrets (#60). The second check is here rather than in a document
                because the two secrets authorise opposite directions of trust: an ingest
                signing secret is *given to a customer's website*, while this one
                authorises writes to the training data. Reusing one as the other hands
                every customer the ability to mint feedback verdicts for every tenant.
        """
        secret = self._secret(
            secret_arn=self.feedback_token_secret_arn,
            literal=self.feedback_token_secret,
            unset=(
                "FEEDBACK_TOKEN_SECRET is not set. Feedback links are signed capabilities "
                "and there is no unsigned mode; export 32+ characters of random material, "
                "or set FEEDBACK_TOKEN_SECRET_ARN."
            ),
        )
        self._reject_reused_ingest_secret(secret)
        return secret

    def require_database_url(self) -> str:
        """Return the database URL, assembled from parts in AWS and given whole locally.

        Returns:
            A SQLAlchemy URL. When :attr:`database_secret_arn` is set it is built by
            :func:`build_database_url` from #27's endpoint and RDS's own credential
            secret, and therefore always carries ``sslmode=require``.

        Raises:
            RuntimeError: nothing is configured, or the ARN is set without the endpoint
                and database name it has to be combined with, or the RDS secret does not
                carry a username and password. The message names the ARN, never the
                password.
            SecretResolutionError: the secret could not be read.
        """
        if self.database_secret_arn:
            return self._assemble_database_url(self.database_secret_arn)
        if self.database_url is None:
            raise RuntimeError(
                "DATABASE_URL is not set. Export it in the environment "
                "(or add it to .env for local development), or set DATABASE_SECRET_ARN "
                "together with DATABASE_HOST and DATABASE_NAME."
            )
        return self.database_url

    def _assemble_database_url(self, secret_arn: str) -> str:
        """Build the URL from #27's outputs plus the RDS-managed credential secret."""
        missing = [
            name
            for name, value in (
                ("DATABASE_HOST", self.database_host),
                ("DATABASE_NAME", self.database_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"DATABASE_SECRET_ARN is set but {' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not. The URL is assembled from "
                "the secret's username and password plus the endpoint from "
                "infra/network.yaml's outputs; the secret alone does not say which "
                "database to connect to."
            )
        credentials = self.secret_resolver().resolve_mapping(secret_arn)
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not username or not password:
            raise RuntimeError(
                f"secret {secret_arn} does not carry both 'username' and 'password'. "
                "This must be the RDS-managed master user secret from "
                "infra/network.yaml (DatabaseMasterUserSecretArn)."
            )
        assert self.database_host is not None  # narrowed by the `missing` check above
        assert self.database_name is not None
        return build_database_url(
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            username=username,
            password=password,
        )

    def _reject_reused_ingest_secret(self, feedback_secret: str) -> None:
        """Raise if ``feedback_secret`` is also one tenant's ingest signing secret.

        Only runs where both are configured — the worker holds the feedback secret and no
        ingest credentials at all, and demanding them would mean giving the worker read
        access to a secret it has no business reading.
        """
        for tenant_id, signing_secret in self._ingest_signing_secrets():
            if hmac.compare_digest(feedback_secret.encode(), signing_secret.encode()):
                raise RuntimeError(
                    f"the feedback token secret is the same value as tenant "
                    f"'{tenant_id}''s ingest signing secret. They authorise opposite "
                    "things and must be distinct (#60): generate fresh material for "
                    "FEEDBACK_TOKEN_SECRET."
                )

    def _ingest_signing_secrets(self) -> Iterator[tuple[str, str]]:
        """Yield ``(tenant_id, signing_secret)`` for each configured ingest credential.

        Deliberately forgiving about shape. ``api.signing.load_credentials`` is what
        validates the credential map, with messages about the malformation; if this
        method raised its own error first, an operator would be sent looking for a secret
        collision that does not exist.
        """
        if self.ingest_credentials_secret_arn:
            raw = self.secret_resolver().resolve(self.ingest_credentials_secret_arn)
        elif self.ingest_credentials is not None:
            raw = self.ingest_credentials.get_secret_value()
        else:
            return
        try:
            parsed = json.loads(raw)
        except ValueError:
            return
        if not isinstance(parsed, dict):
            return
        for tenant_id, entry in parsed.items():
            if isinstance(entry, dict):
                secret = entry.get("signing_secret")
                if isinstance(secret, str) and secret:
                    yield str(tenant_id), secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings, read once."""
    return Settings()
