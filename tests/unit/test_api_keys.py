"""The key format: what a generated key looks like, and what parsing refuses.

Parsing is the first gate on the ingest path and the only one that runs before any I/O,
so most of this file is about what it says ``None`` to. Every rejection here is a request
that never reached the database and never ran a KDF.
"""

from __future__ import annotations

import re

import pytest

from leadquali.app.api_keys import (
    KEY_ID_CHARS,
    MAX_SECRET_CHARS,
    MIN_SECRET_CHARS,
    ApiKeyParts,
    KeyEnvironment,
    generate_api_key,
    key_prefix,
    parse_api_key,
)


def test_a_generated_key_round_trips_through_parsing() -> None:
    minted = generate_api_key(KeyEnvironment.LIVE)
    parsed = parse_api_key(minted.text)
    assert parsed == minted


def test_a_generated_key_has_the_documented_shape() -> None:
    minted = generate_api_key(KeyEnvironment.LIVE)
    assert re.fullmatch(r"lq_live_[0-9a-f]{16}_[A-Za-z0-9_-]{43}", minted.text)
    assert len(minted.key_id) == KEY_ID_CHARS
    assert minted.prefix == f"lq_live_{minted.key_id}"
    assert minted.text.startswith(minted.prefix + "_")


def test_test_keys_say_so_in_the_key_itself() -> None:
    """A test key pasted into a production config has to be obvious to a human."""
    minted = generate_api_key(KeyEnvironment.TEST)
    assert minted.text.startswith("lq_test_")
    parsed = parse_api_key(minted.text)
    assert parsed is not None
    assert parsed.environment is KeyEnvironment.TEST


def test_two_keys_are_never_the_same() -> None:
    minted = [generate_api_key() for _ in range(50)]
    assert len({key.key_id for key in minted}) == 50
    assert len({key.secret for key in minted}) == 50


def test_the_default_environment_is_live() -> None:
    assert generate_api_key().environment is KeyEnvironment.LIVE


def test_key_prefix_is_everything_that_is_not_secret() -> None:
    assert key_prefix(KeyEnvironment.LIVE, "0" * 16) == "lq_live_" + "0" * 16


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "lq_live_" + "0" * 16,
        "lq_live__" + "a" * 43,
        "lq_live_" + "0" * 15 + "_" + "a" * 43,
        "lq_live_" + "0" * 17 + "_" + "a" * 43,
        "lq_live_" + "G" * 16 + "_" + "a" * 43,
        "lq_live_" + "0" * 16 + "_" + "a" * (MIN_SECRET_CHARS - 1),
        "lq_live_" + "0" * 16 + "_" + "a" * (MAX_SECRET_CHARS + 1),
        "lq_staging_" + "0" * 16 + "_" + "a" * 43,
        "xx_live_" + "0" * 16 + "_" + "a" * 43,
        "lq_live_" + "0" * 16 + "_" + "a" * 42 + "!",
        " lq_live_" + "0" * 16 + "_" + "a" * 43,
        "lq_live_" + "0" * 16 + "_" + "a" * 43 + "\n",
    ],
)
def test_a_malformed_key_parses_to_none_rather_than_raising(presented: str) -> None:
    """Parsing runs on every stranger's request; it must be total and it must be cheap."""
    assert parse_api_key(presented) is None


def test_a_key_tampered_with_in_one_character_is_a_different_secret() -> None:
    """Parsing cannot detect tampering — verification does. This pins that division."""
    minted = generate_api_key()
    flipped = minted.text[:-1] + ("A" if minted.text[-1] != "A" else "B")
    parsed = parse_api_key(flipped)
    assert parsed is not None
    assert parsed.key_id == minted.key_id
    assert parsed.secret != minted.secret


def test_the_parts_never_render_the_secret() -> None:
    minted = generate_api_key()
    rendered = repr(minted)
    assert minted.secret not in rendered
    assert "<redacted>" in rendered
    assert minted.key_id in rendered


def test_parts_are_immutable() -> None:
    minted = generate_api_key()
    with pytest.raises(AttributeError):
        minted.secret = "something else"  # type: ignore[misc]


def test_parts_can_be_rebuilt_from_their_pieces() -> None:
    """The CLI and the stores hold the pieces separately; this is the way back."""
    parts = ApiKeyParts(environment=KeyEnvironment.TEST, key_id="a" * 16, secret="b" * 43)
    assert parts.text == "lq_test_" + "a" * 16 + "_" + "b" * 43
    assert parse_api_key(parts.text) == parts
