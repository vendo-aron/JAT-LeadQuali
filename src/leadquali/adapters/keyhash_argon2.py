"""Argon2id hashing and verification of tenant API key secrets.

The only module in the system that imports ``argon2`` (``CLAUDE.md``'s layering rule,
enforced by ``tests/unit/test_layering.py``). Everything above it sees two functions —
"turn this secret into a stored string" and "does this secret match that string" — and
cannot tell which KDF is underneath. Swapping the parameters, or the algorithm, is a
change to this file and to nothing else.

Parameters
----------

OWASP's *Password Storage Cheat Sheet* minimum for Argon2id: ``m = 19 MiB, t = 2, p = 1``.
They are named as constants below rather than left to the library's defaults so that a
library upgrade cannot silently change what a stored hash costs to verify — and because a
stored hash encodes the parameters it was made with, so old rows keep verifying at their
own cost after these change.

Why a memory-hard KDF is affordable on a request path
-----------------------------------------------------

It normally is not: ~19 MiB and a few milliseconds per verification, on every lead, inside
a 200 ms budget, is a self-inflicted denial of service. Two things make it affordable here,
and both are properties of the key format (:mod:`leadquali.app.api_keys`) rather than of
this module:

1. **A key names its own row.** The lookup is by ``key_id``, an indexed read; the KDF runs
   only after a row has been found *and* found to be usable. A stranger who does not hold
   a real ``key_id`` can never make us run argon2 at all.
2. **The answer is memoised.** ``verify(hash, secret)`` is a pure function of two immutable
   inputs, so the first request from a warm container pays for the KDF and the rest do not.

What is *not* memoised is the row. Revocation and expiry are read fresh from Postgres on
every request, which is what makes "a revoked key stops working immediately" true rather
than "within the cache TTL".

Why failures are not memoised, and what replaces it
---------------------------------------------------

Caching failures would hand an attacker an eviction primitive: a flood of wrong secrets for
one real ``key_id`` would push every legitimate entry out of a bounded cache and put the
KDF back on the hot path for everybody. So failures are counted instead. After
:data:`FAILURE_THRESHOLD` consecutive failures for one ``key_id`` inside
:data:`FAILURE_WINDOW_SECONDS`, that ``key_id`` is refused without running the KDF until
the window passes. One correct secret resets the counter, so a customer with a stale form
and a customer being attacked both recover the moment the right key arrives.

Neither structure is authoritative and neither can grant access: the memo only skips work
it has already done, and the gate only ever refuses. A cold container, or one whose caches
were evicted, verifies from scratch and reaches the same answer.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

LOGGER: Final = logging.getLogger(__name__)

__all__ = [
    "ARGON2_HASH_LEN",
    "ARGON2_MEMORY_COST_KIB",
    "ARGON2_PARALLELISM",
    "ARGON2_SALT_LEN",
    "ARGON2_TIME_COST",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW_SECONDS",
    "MEMO_ENTRIES",
    "Argon2KeyHasher",
    "KeyHashingError",
]

# OWASP Password Storage Cheat Sheet (Argon2id minimum): m=19456 KiB, t=2, p=1.
ARGON2_TIME_COST: Final[int] = 2
ARGON2_MEMORY_COST_KIB: Final[int] = 19_456
"""19 MiB. Also the reason a container's memory footprint is bounded by concurrency: one
verification at a time per Lambda invocation."""
ARGON2_PARALLELISM: Final[int] = 1
ARGON2_HASH_LEN: Final[int] = 32
ARGON2_SALT_LEN: Final[int] = 16

MEMO_ENTRIES: Final[int] = 1_024
"""How many successful verifications are remembered. Bounded so a long-lived container
cannot grow without limit; 1024 is far more distinct keys than any one tenant has."""

FAILURE_THRESHOLD: Final[int] = 10
"""Consecutive failures for one ``key_id`` before the KDF stops being run for it."""

FAILURE_WINDOW_SECONDS: Final[float] = 60.0
"""How long a tripped gate stays tripped, and how long a failure counts for."""

FAILURE_ENTRIES: Final[int] = 4_096
"""Distinct ``key_id`` values the gate tracks. Bounded, because the values come from
strangers; evicting the least recently seen only ever loses a *refusal*."""


class KeyHashingError(RuntimeError):
    """A secret could not be hashed. Never carries the secret."""


class Argon2KeyHasher:
    """Hashes and verifies API key secrets with Argon2id.

    One instance per process. Not guarded by a lock: every mutation is a single dict
    operation under the GIL, and the worst a race can do is verify the same secret twice
    or forget one failure — neither of which can turn a "no" into a "yes".

    Args:
        cache_size: How many successful verifications to remember. ``0`` disables the
            memo entirely, which is what a test that wants to measure the raw KDF does.
        failure_threshold: Consecutive failures for one ``key_id`` before the gate trips.
        failure_window_seconds: How long the gate stays tripped.
        monotonic: The clock, injected so a test can move time without sleeping. Must be
            monotonic: a wall clock stepped backwards over NTP would extend a gate.
    """

    def __init__(
        self,
        *,
        cache_size: int = MEMO_ENTRIES,
        failure_threshold: int = FAILURE_THRESHOLD,
        failure_window_seconds: float = FAILURE_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_size < 0:
            raise ValueError(f"cache_size must not be negative, got {cache_size}")
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be positive, got {failure_threshold}")
        self._hasher = PasswordHasher(
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST_KIB,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            salt_len=ARGON2_SALT_LEN,
        )
        self._cache_size = cache_size
        self._failure_threshold = failure_threshold
        self._failure_window = failure_window_seconds
        self._monotonic = monotonic
        self._verified: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._kdf_calls = 0

    @property
    def kdf_calls(self) -> int:
        """How many times the KDF has actually run.

        Exposed because "the memo skips the KDF" is a claim about work done, and the only
        honest way to assert it is to count the work.
        """
        return self._kdf_calls

    def hash_secret(self, secret: str) -> str:
        """Return the at-rest form of one key secret: an encoded Argon2id string.

        The returned string carries its own salt and parameters, so rows hashed under
        older parameters keep verifying after the constants above change.

        Args:
            secret: The secret part of a freshly generated key.

        Returns:
            The encoded hash, e.g. ``$argon2id$v=19$m=19456,t=2,p=1$...``.

        Raises:
            KeyHashingError: the KDF failed. The message never contains the secret.
        """
        self._kdf_calls += 1
        try:
            return self._hasher.hash(secret)
        except HashingError as error:
            raise KeyHashingError(
                f"could not hash an API key secret: {type(error).__name__}"
            ) from None

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        """Whether ``secret`` is the secret behind ``key_hash``.

        Args:
            key_id: The clear-text handle of the row the hash came from. Used only for the
                per-key failure gate and for log lines; it is not secret.
            secret: The secret part of the presented key.
            key_hash: The encoded Argon2id string stored for that row.

        Returns:
            ``True`` on a match. ``False`` for a mismatch, for a ``key_id`` whose failure
            gate is currently tripped, and for a stored hash this library cannot read —
            all three are the same answer to the caller, which is the same 401 on the wire.
        """
        memo = (key_hash, hashlib.sha256(secret.encode("utf-8")).hexdigest())
        if self._remembered(memo):
            self._clear_failures(key_id)
            return True
        if self._gated(key_id):
            return False

        self._kdf_calls += 1
        try:
            self._hasher.verify(key_hash, secret)
        except VerificationError:
            # The ordinary wrong-key case, and by far the most common one.
            self._record_failure(key_id)
            return False
        except InvalidHashError:
            # A stored hash that is not an argon2 string at all: a bad migration or a
            # hand-edited row, not an attack. Logged loudly because no key will ever work
            # against this row until a human fixes it, and refused rather than raised
            # because a 500 here would tell a stranger that this key_id exists.
            LOGGER.error(
                "keyhash.unreadable_stored_hash",
                extra={"event": "keyhash.unreadable_stored_hash", "key_id": key_id},
            )
            self._record_failure(key_id)
            return False

        self._clear_failures(key_id)
        self._remember(memo)
        return True

    def __repr__(self) -> str:
        """Render the shape, never the contents."""
        gated = sum(1 for count, _ in self._failures.values() if count >= self._failure_threshold)
        return f"Argon2KeyHasher(memoised={len(self._verified)}, gated={gated})"

    # --------------------------------------------------------------------- the memo

    def _remembered(self, memo: tuple[str, str]) -> bool:
        if memo not in self._verified:
            return False
        self._verified.move_to_end(memo)
        return True

    def _remember(self, memo: tuple[str, str]) -> None:
        if self._cache_size == 0:
            return
        self._verified[memo] = None
        self._verified.move_to_end(memo)
        while len(self._verified) > self._cache_size:
            self._verified.popitem(last=False)

    # ---------------------------------------------------------------- the failure gate

    def _gated(self, key_id: str) -> bool:
        """Whether this ``key_id`` has burned its allowance inside the current window."""
        entry = self._failures.get(key_id)
        if entry is None:
            return False
        count, first_seen = entry
        if self._monotonic() - first_seen >= self._failure_window:
            del self._failures[key_id]
            return False
        return count >= self._failure_threshold

    def _record_failure(self, key_id: str) -> None:
        now = self._monotonic()
        entry = self._failures.get(key_id)
        if entry is None or now - entry[1] >= self._failure_window:
            self._failures[key_id] = (1, now)
        else:
            self._failures[key_id] = (entry[0] + 1, entry[1])
        self._failures.move_to_end(key_id)
        while len(self._failures) > FAILURE_ENTRIES:
            self._failures.popitem(last=False)

    def _clear_failures(self, key_id: str) -> None:
        self._failures.pop(key_id, None)
