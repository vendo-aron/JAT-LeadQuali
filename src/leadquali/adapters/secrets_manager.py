"""AWS Secrets Manager, read once per container and cached with a bounded lifetime.

The only module in the system that imports ``boto3`` for Secrets Manager
(``CLAUDE.md``'s layering rule, enforced by ``tests/unit/test_layering.py``). Everything
above it — :class:`leadquali.config.Settings` and therefore every adapter — sees a plain
``str`` and cannot tell whether it came from Amazon or from a ``.env`` file.

Why a cache at all
------------------

A Lambda invocation that fetched its four secrets would add four HTTPS round trips (over
the NAT gateway, from a private subnet) to *every lead*, would be billed per call, and
would spend a slice of a rate limit that is shared with everything else in the account.
Secrets Manager throttles ``GetSecretValue`` per account, so the cost of getting this
wrong is not merely latency: a burst of leads becomes a burst of throttled cold starts.

Why the cache expires
---------------------

A cache with no expiry is a redeploy requirement. #28's acceptance criterion is that *a
secret rotated in the console is picked up by the worker within the cache TTL, with no
redeploy*, and a value pinned for the life of a container cannot satisfy that: Lambda
keeps a warm container for tens of minutes and, under steady traffic, indefinitely.

:data:`leadquali.config.DEFAULT_SECRETS_CACHE_TTL_SECONDS` is five minutes, and the
reasoning is in ``docs/runbooks/secrets-and-rotation.md``. In short: five minutes is
short enough that a *revocation* is meaningful — a leaked Anthropic key is out of use
five minutes after someone replaces it, not an hour after — and long enough that the API
call is amortised over hundreds of leads. The worst case is bounded and small: the whole
fleet is capped at 25 warm containers (#26's reserved concurrency) holding at most four
secrets each, so the ceiling is 25 x 4 / 300 s ≈ 0.33 calls per second, about 876k calls
a month, roughly $4.40 at $0.05 per 10,000 — noise beside the $87/month RDS Proxy the
same stack already pays for. It is a parameter (``SECRETS_CACHE_TTL_SECONDS``) rather
than a constant precisely because that arithmetic changes with the fleet.

Failure policy
--------------

Two different situations, deliberately handled differently:

* **Nothing cached.** Raise. There is no safe fallback — returning ``""`` would start an
  ingest endpoint with no credentials, or sign feedback links with an empty key. A cold
  start that cannot read its secrets must fail, loudly, with the ARN in the message.
* **Something cached, refresh failed.** Serve the value we have and retry sooner than a
  full TTL later. The likely cause is throttling or a momentary network fault, and the
  secret we hold is almost certainly still valid; taking the pipeline down over it would
  trade a small correctness risk for a large availability one.

Nothing here logs or renders a secret value. Errors are constructed from the ARN and the
error code only, because an exception message ends up in CloudWatch.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic as _monotonic
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

LOGGER: Final = logging.getLogger(__name__)

#: How long to keep serving a stale value after a failed refresh before trying again.
#: Short enough that a rotation still lands promptly once the fault clears, long enough
#: that a throttled account is not hammered once per invocation.
STALE_RETRY_SECONDS: Final[int] = 30

#: Retries inside the SDK, so a single throttle is absorbed before it becomes a cold-start
#: failure. ``standard`` mode backs off; three attempts is well inside a Lambda's budget.
_BOTO_CONFIG: Final[Config] = Config(retries={"max_attempts": 3, "mode": "standard"})

__all__ = [
    "STALE_RETRY_SECONDS",
    "SecretResolutionError",
    "SecretsManagerResolver",
]


class SecretResolutionError(RuntimeError):
    """A configured secret could not be read, or could not be used once read.

    A ``RuntimeError`` rather than a checked exception with a recovery path, because
    there is no recovery: every caller of this is wiring up a process that needs the
    secret to do its job. It names the ARN and never the value.
    """


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One resolved secret and the moment it stops being served without a refetch."""

    value: str
    expires_at: float


class SecretsManagerResolver:
    """Reads secrets from Secrets Manager, caching each one for ``ttl_seconds``.

    One instance per process. It is not thread-safe in the sense of holding a lock: the
    worst a race can do is fetch the same secret twice and keep the later answer, which
    is cheaper than serialising every lead behind a mutex. Lambda's execution model gives
    one invocation per container at a time anyway.

    Args:
        client: A ``boto3`` Secrets Manager client, or anything with the same
            ``get_secret_value`` shape. Injected so tests can count calls and fail on
            demand; :meth:`from_env` builds the real one.
        ttl_seconds: How long a fetched value is served before it is fetched again. ``0``
            disables the cache entirely.
        monotonic: The clock, injected so a test can move time without sleeping. Must be
            monotonic: a wall clock that steps backwards over NTP would extend a TTL.

    Raises:
        ValueError: ``ttl_seconds`` is negative.
    """

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: int,
        monotonic: Callable[[], float] = _monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must not be negative, got {ttl_seconds}")
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._cache: dict[str, _CacheEntry] = {}

    @classmethod
    def from_env(
        cls,
        *,
        ttl_seconds: int,
        region_name: str | None = None,
    ) -> SecretsManagerResolver:
        """Build a resolver against the ambient AWS credential chain.

        The client is created here rather than at import time, for the same reason the
        SQS and SES adapters do it: importing a module must not create a network-capable
        object, and a Lambda cold start should pay for the client once.

        Args:
            ttl_seconds: Cache lifetime, from ``SECRETS_CACHE_TTL_SECONDS``.
            region_name: Explicit region, or ``None`` to use the ambient one — which is
                what a Lambda already has.

        Returns:
            A resolver ready to read secrets.
        """
        return cls(
            boto3.client("secretsmanager", region_name=region_name, config=_BOTO_CONFIG),
            ttl_seconds=ttl_seconds,
        )

    @property
    def ttl_seconds(self) -> int:
        """How long a fetched secret is served before it is fetched again."""
        return self._ttl_seconds

    def resolve(self, secret_arn: str) -> str:
        """Return the current value of ``secret_arn``, from cache when it is fresh.

        Args:
            secret_arn: The secret's ARN (or any identifier Secrets Manager accepts).

        Returns:
            The secret's ``SecretString`` at the ``AWSCURRENT`` stage.

        Raises:
            SecretResolutionError: the secret is missing, unreadable, or holds only
                binary. The message names the ARN and never the value.
        """
        now = self._monotonic()
        cached = self._cache.get(secret_arn)
        if cached is not None and now < cached.expires_at:
            return cached.value

        try:
            value = self._fetch(secret_arn)
        except SecretResolutionError:
            if cached is None:
                # Cold start with nothing to fall back on. There is no safe answer.
                raise
            LOGGER.warning(
                "secrets.refresh_failed",
                extra={"event": "secrets.refresh_failed", "secret_arn": secret_arn},
            )
            self._cache[secret_arn] = _CacheEntry(
                value=cached.value,
                expires_at=now + min(STALE_RETRY_SECONDS, self._ttl_seconds),
            )
            return cached.value

        self._cache[secret_arn] = _CacheEntry(value=value, expires_at=now + self._ttl_seconds)
        return value

    def resolve_mapping(self, secret_arn: str) -> Mapping[str, str]:
        """Return a JSON secret as a string-to-string mapping.

        The shape RDS writes for a managed master user password
        (``{"username": ..., "password": ...}``) and the shape the ingest credential map
        is stored in. Shares :meth:`resolve`'s cache, so reading two fields costs one
        call.

        Args:
            secret_arn: The secret's ARN.

        Returns:
            The parsed object.

        Raises:
            SecretResolutionError: the secret is unreadable, is not a JSON object, or has
                a non-string value. The message names the ARN and never the value — a
                parse error from ``json`` quotes the input, so it is deliberately not
                repeated here.
        """
        raw = self.resolve(secret_arn)
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise SecretResolutionError(
                f"secret {secret_arn} is not valid JSON; expected an object of strings"
            ) from None
        if not isinstance(parsed, dict):
            raise SecretResolutionError(
                f"secret {secret_arn} must be a JSON object, got {type(parsed).__name__}"
            )
        mapping: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(value, str):
                raise SecretResolutionError(
                    f"secret {secret_arn}: field '{key}' must be a string, "
                    f"got {type(value).__name__}"
                )
            mapping[str(key)] = value
        return mapping

    def invalidate(self, secret_arn: str | None = None) -> None:
        """Drop one cached secret, or all of them.

        The manual half of the rotation story: an operator who has just rotated a secret
        and does not want to wait out the TTL can restart the process, and a caller that
        has seen an authentication failure can force a refetch rather than retry with a
        value it already knows is stale.

        Args:
            secret_arn: The secret to forget, or ``None`` to clear the whole cache.
        """
        if secret_arn is None:
            self._cache.clear()
        else:
            self._cache.pop(secret_arn, None)

    def __repr__(self) -> str:
        """Render the shape, never the contents: a repr ends up in tracebacks."""
        return f"SecretsManagerResolver(ttl_seconds={self._ttl_seconds}, cached={len(self._cache)})"

    def _fetch(self, secret_arn: str) -> str:
        """One ``GetSecretValue`` call, with every failure turned into one error type."""
        try:
            response = self._client.get_secret_value(SecretId=secret_arn)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "Unknown")
            raise SecretResolutionError(
                f"cannot read secret {secret_arn}: {code}. Check that the secret exists "
                f"and that this function's role may GetSecretValue and kms:Decrypt it."
            ) from None
        except BotoCoreError as error:
            raise SecretResolutionError(
                f"cannot read secret {secret_arn}: {type(error).__name__}"
            ) from None
        except Exception as error:
            # Credentials errors, endpoint resolution and anything else the SDK invents
            # arrive here. They are all "this process cannot read its secret", and the
            # caller must not have to know botocore's exception tree to say so. The
            # original type is named; the original message is not, because it can quote
            # the request.
            raise SecretResolutionError(
                f"cannot read secret {secret_arn}: {type(error).__name__}"
            ) from None
        value = response.get("SecretString")
        if not isinstance(value, str):
            raise SecretResolutionError(
                f"secret {secret_arn} has no SecretString; a binary secret cannot be used "
                f"as configuration"
            )
        return value
