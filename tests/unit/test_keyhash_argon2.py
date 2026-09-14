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
    FAILURE_WINDOW_SECONDS,
    GATED_RETRY_SECONDS,
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


def test_a_memoised_hash_still_refuses_the_wrong_secret(stored_hash: str) -> None:
    """The worst thing the memo could possibly do: "any secret matches a hash that has
    verified once". Asserted on the *same* hasher, straight after a successful
    verification, because that is the only state in which the bug exists.
    """
    hasher = Argon2KeyHasher()
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)

    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET + "x", key_hash=stored_hash)
    assert not hasher.verify_secret(key_id=KEY_ID, secret="", key_hash=stored_hash)
    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET[:-1], key_hash=stored_hash)
    # ...and the right one still does, so the refusals above are not the memo having been
    # emptied by the wrong guesses.
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)


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


# ----------------------------------------------------------------- the KDF throttle
#
# A ``key_id`` is public: it travels in the clear, in a header, on every submission a
# customer's form makes. So the question every test below is really asking is "can a
# stranger who has seen one request take this customer off the air?"


def test_repeated_failures_stop_costing_a_kdf_every_time(stored_hash: str) -> None:
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)

    for _ in range(FAILURE_THRESHOLD):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == FAILURE_THRESHOLD

    for _ in range(20):
        assert not hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == FAILURE_THRESHOLD, "the throttle should have skipped the KDF"


def test_a_throttled_key_id_costs_one_kdf_per_retry_interval(stored_hash: str) -> None:
    """Bounded, not stopped. The attacker's ceiling is one verification per six seconds,
    which is the same rate that tripped the throttle in the first place."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    spent = hasher.kdf_calls

    for _ in range(5):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == spent

    clock.advance(GATED_RETRY_SECONDS + 0.1)
    hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == spent + 1

    for _ in range(5):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
    assert hasher.kdf_calls == spent + 1


def test_a_stranger_cannot_lock_a_warm_container_out_of_a_customers_key(
    stored_hash: str,
) -> None:
    """The memo is checked before the throttle, so a key this process has already verified
    keeps working no matter how many wrong guesses arrive against its public ``key_id``."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)

    for _ in range(FAILURE_THRESHOLD * 5):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    assert all(
        hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash) for _ in range(12)
    )


def test_a_stranger_cannot_lock_a_cold_container_out_for_a_minute(stored_hash: str) -> None:
    """The case the memo cannot cover: a fresh container, an attacker who has already
    tripped the throttle, and the legitimate holder arriving with the right key.

    It must not be a minute-long outage for that customer. One retry interval — six
    seconds — is the whole delay, and after it the correct key works.
    """
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    # Immediately: deferred, because the attacker just spent the slot.
    assert not hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)

    clock.advance(GATED_RETRY_SECONDS + 0.1)
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash), (
        "a legitimate holder must be through after one retry, not after the whole window"
    )
    assert GATED_RETRY_SECONDS * 2 < FAILURE_WINDOW_SECONDS


def test_the_throttle_clears_entirely_once_the_window_passes(stored_hash: str) -> None:
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock, cache_size=0)
    for _ in range(FAILURE_THRESHOLD):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)

    clock.advance(FAILURE_WINDOW_SECONDS + 1)
    spent = hasher.kdf_calls
    assert hasher.verify_secret(key_id=KEY_ID, secret=SECRET, key_hash=stored_hash)
    assert hasher.kdf_calls == spent + 1


def test_the_throttle_is_per_key_id(stored_hash: str) -> None:
    """One customer's broken form must not slow another customer down."""
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
    """Two failures a minute, forever, must never trip the throttle."""
    clock = FakeMonotonic()
    hasher = Argon2KeyHasher(monotonic=clock)
    for _ in range(FAILURE_THRESHOLD * 3):
        hasher.verify_secret(key_id=KEY_ID, secret="wrong", key_hash=stored_hash)
        clock.advance(FAILURE_WINDOW_SECONDS + 1)
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
