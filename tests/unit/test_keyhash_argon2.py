"""Argon2id hashing: the parameters, the memo, and the gate in front of the KDF.

These tests are deliberately frugal with real KDF calls — each one costs 19 MiB and a few
milliseconds, which is the whole reason the memo and the gate exist. Where a test is about
bookkeeping rather than about cryptography it counts ``kdf_calls`` instead of hashing more.
"""

from __future__ import annotations

import pytest

from leadquali.adapters.keyhash_argon2 import (
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    FAILURE_THRESHOLD,
    Argon2KeyHasher,
    KeyHashingError,
)

SECRET = "kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI"
KEY_ID = "3f1c9a02b7d45e68"


class FakeMonotonic:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(scope="module")
def stored_hash() -> str:
    """One real argon2 hash, shared by every test that only needs a valid one."""
    return Argon2KeyHasher().hash_secret(SECRET)


# ------------------------------------------------------------------------ parameters


def test_the_stored_hash_is_argon2id_at_the_documented_parameters(stored_hash: str) -> None:
    """A stored hash encodes its own cost, so this is checkable without the library."""
    assert stored_hash.startswith("$argon2id$")
    assert f"m={ARGON2_MEMORY_COST_KIB}" in stored_hash
    assert f"t={ARGON2_TIME_COST}" in stored_hash
    assert f"p={ARGON2_PARALLELISM}" in stored_hash


def test_the_same_secret_hashes_differently_every_time(stored_hash: str) -> None:
    """Salted, so two tenants with the same secret do not share a row value."""
    assert Argon2KeyHasher().hash_secret(SECRET) != stored_hash


def test_the_secret_is_not_recoverable_from_the_stored_hash(stored_hash: str) -> None:
    """The acceptance criterion, stated as bluntly as it can be stated."""
    assert SECRET not in stored_hash


# ----------------------------------------------------------------------- verification


def test_the_right_secret_verifies(stored_hash: str) -> None:
    assert Argon2KeyHasher().verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)


def test_a_wrong_secret_does_not_verify(stored_hash: str) -> None:
    hasher = Argon2KeyHasher()
    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET + "x", key_hash=stored_hash)
    assert not hasher.verify_secret(key_id=KEY_ID, secret="", key_hash=stored_hash)


def test_a_hash_from_another_key_does_not_verify(stored_hash: str) -> None:
    other = Argon2KeyHasher().hash_secret("a-completely-different-secret-value-4321")
    assert not Argon2KeyHasher().verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=other)


def test_an_unreadable_stored_hash_is_a_refusal_and_not_an_exception() -> None:
    """A corrupt row must not 500: a 500 would confirm that this key_id exists."""
    hasher = Argon2KeyHasher()
    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash="not-a-hash")


def test_hashing_rejects_a_negative_cache_size() -> None:
    with pytest.raises(ValueError, match="cache_size"):
        Argon2KeyHasher(cache_size=-1)


def test_hashing_rejects_a_zero_failure_threshold() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        Argon2KeyHasher(failure_threshold=0)


# ------------------------------------------------------------------------- the memo


def test_a_repeated_verification_returns_the_same_answer_without_the_kdf(
    stored_hash: str,
) -> None:
    """The claim the 200 ms budget rests on, asserted by counting work rather than time."""
    hasher = Argon2KeyHasher()
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    after_first = hasher.kdf_calls
    assert after_first == 1

    for _ in range(5):
        assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.kdf_calls == after_first


def test_the_memo_is_keyed_on_the_hash_as_well_as_the_secret(stored_hash: str) -> None:
    """A memo hit for one row must never satisfy another row's hash."""
    hasher = Argon2KeyHasher()
    other = hasher.hash_secret(SECRET)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    calls = hasher.kdf_calls
    assert hasher.verify_secret(key_id="a" * 16, secret=SECRET, key_hash=other)
    assert hasher.kdf_calls == calls + 1


def test_the_memo_is_bounded_and_evicts_the_oldest(stored_hash: str) -> None:
    hasher = Argon2KeyHasher(cache_size=1)
    other_secret = "another-secret-of-the-right-shape-1234567890"
    other = hasher.hash_secret(other_secret)

    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.verify_secret(key_id="b" * 16, secret=other_secret, key_hash=other)
    calls = hasher.kdf_calls
    # The first entry has been evicted, so this costs the KDF again.
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.kdf_calls == calls + 1


def test_a_disabled_memo_verifies_every_time(stored_hash: str) -> None:
    hasher = Argon2KeyHasher(cache_size=0)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.kdf_calls == 2


def test_failures_are_not_memoised(stored_hash: str) -> None:
    """A bounded cache of failures would be an eviction primitive for an attacker."""
    hasher = Argon2KeyHasher(failure_threshold=100)
    for _ in range(3):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == 3


# -------------------------------------------------------------------- the failure gate


def test_repeated_failures_stop_costing_the_kdf(stored_hash: str) -> None:
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)

    for _ in range(FAILURE_THRESHOLD):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == FAILURE_THRESHOLD

    for _ in range(20):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == FAILURE_THRESHOLD, "the gate should have skipped the KDF"


def test_a_gated_key_id_refuses_even_the_correct_secret_until_the_window_passes(
    stored_hash: str,
) -> None:
    """Honest about the cost: a customer being flooded is briefly refused too. The window
    is 60 seconds, and the alternative is paying 19 MiB per forged request."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)

    clock.advance(61.0)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)


def test_the_gate_is_per_key_id(stored_hash: str) -> None:
    """One customer's broken form must not lock out another customer."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD + 5):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    assert hasher.verify_secret(key_id="c" * 16, secret=SECRET, key_hash=stored_hash)


def test_a_correct_secret_resets_the_counter(stored_hash: str) -> None:
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock, cache_size=0)
    for _ in range(FAILURE_THRESHOLD - 1):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)

    calls = hasher.kdf_calls
    for _ in range(FAILURE_THRESHOLD - 1):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == calls + FAILURE_THRESHOLD - 1, "the counter did not reset"


def test_failures_older_than_the_window_do_not_accumulate(stored_hash: str) -> None:
    """Two failures a minute, forever, must never trip the gate."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD * 3):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
        clock.advance(61.0)
    assert hasher.kdf_calls == FAILURE_THRESHOLD * 3


def test_the_repr_carries_no_secret_material(stored_hash: str) -> None:
    hasher = Argon2KeyHasher()
    hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    rendered = repr(hasher)
    assert SECRET not in rendered
    assert stored_hash not in rendered


def test_hashing_error_is_the_modules_own_type() -> None:
    """Named so callers never have to import argon2's exception tree to handle it."""
    assert issubclass(KeyHashingError, RuntimeError)
