"""The ingest API key format: how one is minted, and how one is taken apart again.

A key is **self-identifying**. It carries, in the clear, the handle of the row that stores
its hash::

    lq_live_3f1c9a02b7d45e68_kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI
    ^^ ^^^^ ^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    |  |    key_id            secret
    |  environment
    scheme

Each part earns its place:

* **scheme** — ``lq``. A key found in a log, a paste buffer or a customer's source tree is
  identifiable as ours, which is what makes automated secret scanning possible at all.
* **environment** — ``live`` or ``test``, as a literal. A test key pasted into a production
  config is then obvious to a human reading the config, rather than a 401 nobody can
  explain. :func:`parse_api_key` does not judge the environment; the verifier does, by
  looking up a row that only exists in one of them.
* **key_id** — 16 lowercase hex characters, 64 bits from :func:`secrets.token_bytes`. It is
  **not a secret**: it is stored in the clear, uniquely indexed, and it is how a presented
  key finds its row in one indexed read. That single property is what makes an argon2 hash
  affordable on a request path — see the "Why argon2 is affordable here" section of
  :mod:`leadquali.api.signing`.
* **secret** — 256 bits from :func:`secrets.token_urlsafe`. Only its argon2 hash is stored,
  and it is the only part of the key that is ever compared against a hash.

The service generates keys and a caller may never supply one. Every argument above about
what a key costs an attacker rests on the key_id being uniformly random and the secret
being 256 bits, and a customer-chosen key would make both claims false.

Standard library only, so the signing module — which #30's browser-side form signer also
imports — stays free of third-party code.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "KEY_ID_CHARS",
    "MAX_SECRET_CHARS",
    "MIN_SECRET_CHARS",
    "SCHEME",
    "SECRET_BYTES",
    "ApiKeyParts",
    "KeyEnvironment",
    "generate_api_key",
    "key_prefix",
    "parse_api_key",
]

SCHEME: Final[str] = "lq"
"""The literal every key starts with, so ours are recognisable in a secret scan."""

KEY_ID_BYTES: Final[int] = 8
KEY_ID_CHARS: Final[int] = KEY_ID_BYTES * 2
"""64 bits, hex-encoded. Wide enough that guessing an existing handle is hopeless — which
is the assumption behind letting a lookup by key_id be the thing that gates the KDF."""

SECRET_BYTES: Final[int] = 32
"""256 bits of the part that is actually secret."""

MIN_SECRET_CHARS: Final[int] = 43
"""``token_urlsafe(32)`` is 43 characters. Shorter is not a key we ever minted."""

MAX_SECRET_CHARS: Final[int] = 86
"""Twice that, so a future longer secret still parses, and a header cannot be used as
free storage. Anything outside the range is refused before a byte of I/O happens."""


class KeyEnvironment(StrEnum):
    """Which deployment a key is meant for, written into the key itself."""

    LIVE = "live"
    TEST = "test"


_ENVIRONMENTS: Final[str] = "|".join(sorted(member.value for member in KeyEnvironment))

_KEY_RE: Final[re.Pattern[str]] = re.compile(
    rf"\A{SCHEME}_(?P<env>{_ENVIRONMENTS})_(?P<key_id>[0-9a-f]{{{KEY_ID_CHARS}}})"
    rf"_(?P<secret>[A-Za-z0-9_-]{{{MIN_SECRET_CHARS},{MAX_SECRET_CHARS}}})\Z"
)


@dataclass(frozen=True, slots=True)
class ApiKeyParts:
    """One API key, taken apart. Two of the three fields are safe to log; one is not."""

    environment: KeyEnvironment
    key_id: str
    """The clear-text lookup handle. Stored, indexed, logged."""

    secret: str
    """The part that is hashed. Never stored, never logged, never in a repr."""

    @property
    def prefix(self) -> str:
        """``lq_live_<key_id>`` — what a key listing shows so a human can tell keys apart."""
        return key_prefix(self.environment, self.key_id)

    @property
    def text(self) -> str:
        """The whole key, as the customer's form must send it."""
        return f"{self.prefix}_{self.secret}"

    def __repr__(self) -> str:
        """Render everything except the secret: a repr ends up in tracebacks, and those
        end up in logs."""
        return (
            f"ApiKeyParts(environment={self.environment.value!r}, "
            f"key_id={self.key_id!r}, secret='<redacted>')"
        )


def key_prefix(environment: KeyEnvironment, key_id: str) -> str:
    """The display form of a key: everything about it that is not secret."""
    return f"{SCHEME}_{environment.value}_{key_id}"


def generate_api_key(environment: KeyEnvironment = KeyEnvironment.LIVE) -> ApiKeyParts:
    """Mint a new key.

    Args:
        environment: ``live`` or ``test``. Written into the key, and into the stored
            ``key_prefix``, so the two can never disagree.

    Returns:
        The parts of a brand new key. Its :attr:`~ApiKeyParts.text` is the only copy of
        the secret that will ever exist — show it once and store only its hash.
    """
    return ApiKeyParts(
        environment=environment,
        key_id=secrets.token_bytes(KEY_ID_BYTES).hex(),
        secret=secrets.token_urlsafe(SECRET_BYTES),
    )


def parse_api_key(presented: str) -> ApiKeyParts | None:
    """Take a presented key apart, or return ``None`` if it is not one of ours.

    This is deliberately total and cheap: it does no I/O and raises nothing, because it
    runs on every request including the ones from strangers. ``None`` is the answer for
    every malformed input — a truncated key, a key for an environment we do not issue, a
    key with a key_id that is not hex — and the caller turns all of them into the same
    rejection.

    Args:
        presented: The raw header value, exactly as it arrived.

    Returns:
        The parsed parts, or ``None``.
    """
    match = _KEY_RE.match(presented)
    if match is None:
        return None
    return ApiKeyParts(
        environment=KeyEnvironment(match.group("env")),
        key_id=match.group("key_id"),
        secret=match.group("secret"),
    )
