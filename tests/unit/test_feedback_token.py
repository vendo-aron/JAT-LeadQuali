"""The feedback token: what it authorises, and everything it must refuse.

The token is the only thing standing between a public URL and the table the golden set is
built from, so these tests are written as attacks rather than as a happy path with a couple
of negatives bolted on: take a valid link and try to make it write something else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from leadquali.app.feedback import (
    MIN_TOKEN_SECRET_CHARS,
    TOKEN_VERSION,
    FeedbackClaim,
    TokenAccepted,
    TokenFailure,
    TokenRejected,
    Verdict,
    load_token_secret,
    mint_token,
    rater_id,
    tenant_key,
    verify_token,
)

SECRET = b"a" * MIN_TOKEN_SECRET_CHARS
OTHER_SECRET = b"b" * MIN_TOKEN_SECRET_CHARS
NOW = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
LEAD = "8f14e45f-ceea-467a-9575-1b1e0a4d3c11"
OTHER_LEAD = "11111111-2222-3333-4444-555555555555"
RATER = rater_id("sales@example.com")


def token_for(
    *,
    verdict: Verdict = Verdict.GOOD,
    tenant_id: str = "default",
    lead_id: str = LEAD,
    rater: str = RATER,
    expires_at: datetime | None = None,
    secret: bytes = SECRET,
) -> str:
    return mint_token(
        secret=secret,
        tenant_id=tenant_id,
        lead_id=lead_id,
        verdict=verdict,
        rater=rater,
        expires_at=expires_at if expires_at is not None else NOW + timedelta(days=30),
    )


def claim_of(token: str) -> FeedbackClaim:
    result = verify_token(secret=SECRET, token=token, now=NOW)
    assert isinstance(result, TokenAccepted)
    return result.claim


def failure_of(token: str, *, now: datetime = NOW, secret: bytes = SECRET) -> TokenFailure:
    result = verify_token(secret=secret, token=token, now=now)
    assert isinstance(result, TokenRejected), "this token was supposed to be refused"
    return result.failure


# ------------------------------------------------------------------------ the happy path


def test_a_minted_token_verifies_and_carries_its_whole_claim() -> None:
    claim = claim_of(token_for(verdict=Verdict.BAD))
    assert claim.tenant_id == "default"
    assert claim.lead_id == LEAD
    assert claim.verdict is Verdict.BAD
    assert claim.rater == RATER
    assert claim.expires_at == NOW + timedelta(days=30)


def test_the_token_is_one_url_safe_path_segment() -> None:
    """It goes in a path, in an email, on a phone. Nothing to percent-encode, no padding."""
    token = token_for()
    assert token.startswith(f"{TOKEN_VERSION}.")
    assert set(token) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    assert len(token) < 400, "a link this long gets broken across lines by mail clients"


def test_each_verdict_gets_a_different_token() -> None:
    assert token_for(verdict=Verdict.GOOD) != token_for(verdict=Verdict.BAD)


# --------------------------------------------------------------------------- forgery


def test_a_token_signed_with_another_key_is_refused() -> None:
    assert failure_of(token_for(secret=OTHER_SECRET)) is TokenFailure.BAD_SIGNATURE


def test_swapping_the_lead_id_inside_a_valid_token_is_refused() -> None:
    """The lead id is signed, so a leaked link cannot be aimed at somebody else's lead."""
    valid = token_for()
    other = token_for(lead_id=OTHER_LEAD)
    _, other_claim, _ = other.split(".")
    _, _, valid_mac = valid.split(".")
    assert failure_of(f"{TOKEN_VERSION}.{other_claim}.{valid_mac}") is TokenFailure.BAD_SIGNATURE


def test_swapping_the_verdict_inside_a_valid_token_is_refused() -> None:
    """One token, one verdict: a 'good lead' link cannot be turned into a 'bad lead' one."""
    good = token_for(verdict=Verdict.GOOD)
    bad = token_for(verdict=Verdict.BAD)
    _, bad_claim, _ = bad.split(".")
    _, _, good_mac = good.split(".")
    assert failure_of(f"{TOKEN_VERSION}.{bad_claim}.{good_mac}") is TokenFailure.BAD_SIGNATURE


def test_a_token_cannot_be_moved_sideways_onto_another_tenant() -> None:
    """Keys are derived per tenant, so one tenant's MAC does not authorise another's claim."""
    assert tenant_key(SECRET, "acme") != tenant_key(SECRET, "other")
    acme = token_for(tenant_id="acme")
    other = token_for(tenant_id="other")
    _, other_claim, _ = other.split(".")
    _, _, acme_mac = acme.split(".")
    assert failure_of(f"{TOKEN_VERSION}.{other_claim}.{acme_mac}") is TokenFailure.BAD_SIGNATURE


def test_truncating_the_mac_is_refused() -> None:
    version, claim, mac = token_for().split(".")
    assert failure_of(f"{version}.{claim}.{mac[:-4]}") is TokenFailure.BAD_SIGNATURE


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "fb1.onlytwo",
        "fb2.abc.def",
        "fb1..abc",
        "fb1.abc.",
        "fb1.!!!!.abc",
        "fb1." + "A" * 2000 + ".abc",
    ],
    ids=[
        "empty",
        "no-parts",
        "two-parts",
        "wrong-version",
        "empty-claim",
        "empty-mac",
        "undecodable",
        "oversized",
    ],
)
def test_a_token_that_is_not_one_is_refused_as_malformed(token: str) -> None:
    assert failure_of(token) is TokenFailure.MALFORMED


def test_an_unknown_verdict_is_malformed_not_a_signature_failure() -> None:
    """A verdict outside the schema's CHECK constraint never reaches the database."""
    from leadquali.app.feedback import _b64encode

    claim = "\n".join(
        (
            "LEADQUALI-FEEDBACK-HMAC-SHA256",
            TOKEN_VERSION,
            "default",
            LEAD,
            "excellent",
            RATER,
            str(int((NOW + timedelta(days=1)).timestamp())),
        )
    )
    assert (
        failure_of(f"{TOKEN_VERSION}.{_b64encode(claim.encode())}.zzzz") is TokenFailure.MALFORMED
    )


# ---------------------------------------------------------------------------- expiry


def test_an_expired_token_is_refused() -> None:
    token = token_for(expires_at=NOW + timedelta(minutes=1))
    assert failure_of(token, now=NOW + timedelta(minutes=2)) is TokenFailure.EXPIRED


def test_expiry_is_exclusive_at_the_instant_it_names() -> None:
    expires = NOW + timedelta(minutes=1)
    assert failure_of(token_for(expires_at=expires), now=expires) is TokenFailure.EXPIRED
    assert isinstance(
        verify_token(
            secret=SECRET, token=token_for(expires_at=expires), now=expires - timedelta(seconds=1)
        ),
        TokenAccepted,
    )


def test_a_forged_token_never_reports_expiry_first() -> None:
    """Signature before expiry: a forged claim must not become an oracle over lead ids."""
    forged = token_for(secret=OTHER_SECRET, expires_at=NOW - timedelta(days=1))
    assert failure_of(forged) is TokenFailure.BAD_SIGNATURE


# ------------------------------------------------------------------ wiring mistakes


def test_a_short_secret_is_refused_at_mint_time() -> None:
    with pytest.raises(ValueError, match="at least"):
        token_for(secret=b"too-short")


def test_a_naive_expiry_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        mint_token(
            secret=SECRET,
            tenant_id="default",
            lead_id=LEAD,
            verdict=Verdict.GOOD,
            rater=RATER,
            expires_at=datetime(2026, 10, 1),
        )


@pytest.mark.parametrize("blank", ["tenant_id", "lead_id", "rater"])
def test_a_blank_identity_field_is_refused(blank: str) -> None:
    fields: dict[str, str] = {"tenant_id": "default", "lead_id": LEAD, "rater": RATER}
    fields[blank] = ""
    with pytest.raises(ValueError, match="non-empty"):
        mint_token(secret=SECRET, verdict=Verdict.GOOD, expires_at=NOW, **fields)


def test_the_configured_secret_is_validated_when_it_is_loaded() -> None:
    assert load_token_secret("x" * MIN_TOKEN_SECRET_CHARS) == b"x" * MIN_TOKEN_SECRET_CHARS
    with pytest.raises(ValueError, match="at least"):
        load_token_secret("short")


# -------------------------------------------------------------------------- rater ids


def test_a_rater_id_is_stable_and_carries_no_address() -> None:
    """Invariant 5, and the reason a second click updates rather than inserts."""
    assert rater_id("Sales@Example.com ") == rater_id("sales@example.com")
    assert "example.com" not in rater_id("sales@example.com")
    assert "@" not in rater_id("sales@example.com")


def test_different_destinations_get_different_rater_ids() -> None:
    assert rater_id("sales@example.com") != rater_id("escalations@example.com")


def test_a_blank_destination_has_no_rater_id() -> None:
    with pytest.raises(ValueError, match="blank"):
        rater_id("   ")


def test_the_opposite_verdict_is_what_a_change_of_mind_wants() -> None:
    assert Verdict.GOOD.opposite is Verdict.BAD
    assert Verdict.BAD.opposite is Verdict.GOOD
    assert Verdict.UNSURE.opposite is Verdict.UNSURE
