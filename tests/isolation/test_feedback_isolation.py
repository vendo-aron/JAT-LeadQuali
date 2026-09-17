"""Feedback isolation: a signed link authorises one verdict, on one lead, for one tenant.

The feedback endpoint is the only public surface that writes to the training set, and there
is no login behind it — a rep who has to authenticate does not click, and a feedback loop
nobody uses is worth nothing. What stands in for a session is a bearer capability whose
every field is signed, so this module attacks the fields:

* **The lead id.** A link for tenant A's lead, edited to name a different lead, is refused.
* **The tenant.** The same link edited to claim tenant B is refused, because the MAC key is
  derived per tenant (:func:`~leadquali.app.feedback.tenant_key`) — a token signed under
  A's derived key does not verify under B's even when the process secret is the same.
* **The verdict**, for completeness, because a leaked "good lead" link that could be turned
  into a "bad lead" one would poison the golden set just as effectively as a cross-tenant
  write.

Every refusal is asserted twice over: the token layer says no, **and** nothing reached the
store. The second half is the one that matters. A refusal that still called
``record_feedback`` would be a bug the status code cannot see, and the store is the thing
the golden set is built from.

The last section is about what is *not* isolated here and is honest about it: a genuine
link is a capability, so whoever holds it can use it. That is the design, it is bounded by
a signed expiry, and it is written down in ``docs/tenant-isolation.md`` rather than
implied away.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from typing import Final

import pytest
from fastapi.testclient import TestClient

# starlette's TestClient is built on httpx2 in this environment, and its Response is a
# different class from httpx 0.28's; take the type from the client that produces it.
from httpx2 import Response

from leadquali.api.feedback import CONFIRM_FIELD, FeedbackDeps, confirmation_code
from leadquali.api.main import create_app
from leadquali.app.feedback import (
    MIN_TOKEN_SECRET_CHARS,
    TOKEN_VERSION,
    TokenAccepted,
    TokenFailure,
    TokenRejected,
    UnknownLeadError,
    Verdict,
    mint_token,
    rater_id,
    tenant_key,
    verify_token,
)
from tests.fakes import FakeClock, InMemoryFeedbackStore
from tests.isolation.repositories import LEAD_A, LEAD_B, TENANT_A, TENANT_B

SECRET: Final[bytes] = b"f" * MIN_TOKEN_SECRET_CHARS
NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 9, 0, tzinfo=dt.UTC)
EXPIRY: Final[dt.datetime] = NOW + dt.timedelta(days=30)

RATER_A: Final[str] = rater_id("sales@alpha-instruments.invalid")
RATER_B: Final[str] = rater_id("neugeschaeft@zenith-freight.invalid")


def token_for(
    tenant_id: str,
    lead_id: str,
    *,
    verdict: Verdict = Verdict.GOOD,
    rater: str = RATER_A,
) -> str:
    """A genuine link for one tenant's lead."""
    return mint_token(
        secret=SECRET,
        tenant_id=tenant_id,
        lead_id=lead_id,
        verdict=verdict,
        rater=rater,
        expires_at=EXPIRY,
    )


# ------------------------------------------------------------------- tampering with one


def _decode(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")


def _encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def edited(token: str, *, line: int, value: str) -> str:
    """The same token with one line of its signed claim replaced and the MAC left alone.

    The claim travels base64url-encoded and the MAC is computed over the *encoded* form, so
    this is exactly what an attacker holding a link can do: decode, edit, re-encode, and
    present the original signature. Lines are the seven of
    :meth:`~leadquali.app.feedback.FeedbackClaim.signing_string`: algorithm, version,
    tenant, lead, verdict, rater, expiry.
    """
    version, encoded, mac = token.split(".")
    lines = _decode(encoded).split("\n")
    lines[line] = value
    return f"{version}.{_encode(chr(10).join(lines))}.{mac}"


def resigned_with(token: str, *, as_tenant: str) -> str:
    """The same claim, re-signed with ``as_tenant``'s derived key.

    Models the worst case the per-tenant derivation exists for: one tenant's derived key
    has leaked — through a log, a crash dump, a support session — and is used to mint a
    link for somebody else's lead. The construction is
    :func:`~leadquali.app.feedback.tenant_key` plus HMAC-SHA256 over ``fb1.<claim>``,
    written out here rather than imported from a private helper so that a change to the
    scheme fails this test instead of silently agreeing with itself.
    """
    version, encoded, _ = token.split(".")
    material = f"{version}.{encoded}".encode()
    digest = hmac.new(tenant_key(SECRET, as_tenant), material, hashlib.sha256).digest()
    return f"{version}.{encoded}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def test_a_genuine_link_verifies_for_the_tenant_it_was_minted_for() -> None:
    """The positive control. Everything below is a refusal, and refusals prove nothing
    unless the same code path accepts something."""
    result = verify_token(secret=SECRET, token=token_for(TENANT_A, LEAD_A), now=NOW)

    assert isinstance(result, TokenAccepted)
    assert result.claim.tenant_id == TENANT_A
    assert result.claim.lead_id == LEAD_A
    assert result.claim.verdict is Verdict.GOOD


def test_a_link_for_one_lead_cannot_be_edited_to_name_another() -> None:
    """The lead id is signed, so it is not a parameter the holder gets to choose.

    ``lead_id`` is a UUID that appears in no other public surface, which is precisely why
    the alternative design — a bare path parameter — would be the one way to write
    arbitrary feedback against leads you have never seen.
    """
    forged = edited(token_for(TENANT_A, LEAD_A), line=3, value=LEAD_B)
    result = verify_token(secret=SECRET, token=forged, now=NOW)

    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.BAD_SIGNATURE


def test_a_link_for_one_tenant_cannot_be_edited_to_claim_another() -> None:
    """The tenant is signed *and* selects the key, so this fails twice over."""
    forged = edited(token_for(TENANT_A, LEAD_A), line=2, value=TENANT_B)
    result = verify_token(secret=SECRET, token=forged, now=NOW)

    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.BAD_SIGNATURE


def test_a_link_cannot_be_edited_to_flip_its_verdict() -> None:
    """One token, one verdict. A leaked 'good lead' link cannot become a 'bad lead' one."""
    forged = edited(token_for(TENANT_A, LEAD_A, verdict=Verdict.GOOD), line=4, value="bad")
    result = verify_token(secret=SECRET, token=forged, now=NOW)

    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.BAD_SIGNATURE


def test_one_tenants_derived_key_cannot_sign_another_tenants_claim() -> None:
    """The reason keys are derived per tenant rather than shared.

    A claim naming tenant B, signed with tenant A's derived key, is refused: verification
    derives the key from the tenant *in the claim*, so the attacker's key is never the one
    the comparison is made against.
    """
    claim_for_b = token_for(TENANT_B, LEAD_B)
    forged = resigned_with(claim_for_b, as_tenant=TENANT_A)

    assert forged != claim_for_b, "the re-signing helper produced the original token"
    result = verify_token(secret=SECRET, token=forged, now=NOW)
    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.BAD_SIGNATURE


def test_the_derived_keys_are_actually_different() -> None:
    """The premise of the test above, which would otherwise pass on a broken derivation."""
    assert tenant_key(SECRET, TENANT_A) != tenant_key(SECRET, TENANT_B)
    assert len(tenant_key(SECRET, TENANT_A)) == hashlib.sha256().digest_size


def test_two_tenants_links_for_the_same_lead_id_are_different_tokens() -> None:
    """Same lead id, same verdict, same rater, same expiry — and a different signature.

    Lead ids are UUIDs and will not collide, but the guarantee must not *rest* on that: it
    rests on the tenant being inside the signed material.
    """
    left = token_for(TENANT_A, LEAD_A, rater=RATER_A)
    right = token_for(TENANT_B, LEAD_A, rater=RATER_A)

    assert left != right
    assert left.split(".")[2] != right.split(".")[2]
    assert verify_token(secret=SECRET, token=left, now=NOW) != verify_token(
        secret=SECRET, token=right, now=NOW
    )


def test_the_signature_is_checked_before_the_expiry() -> None:
    """Order matters, and it is an isolation property rather than a tidiness one.

    If expiry were checked first, a forged claim naming a real lead id would answer "this
    link just expired" — an oracle over another tenant's lead ids, readable by anybody.
    """
    stale = mint_token(
        secret=SECRET,
        tenant_id=TENANT_A,
        lead_id=LEAD_A,
        verdict=Verdict.GOOD,
        rater=RATER_A,
        expires_at=NOW - dt.timedelta(days=1),
    )
    forged = edited(stale, line=3, value=LEAD_B)
    result = verify_token(secret=SECRET, token=forged, now=NOW)

    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.BAD_SIGNATURE, (
        "an unsigned claim must never be read far enough to learn whether it expired"
    )


# ------------------------------------------------------------------- nothing is written


class Harness:
    """The real feedback routes over an in-memory store that knows both tenants' leads."""

    def __init__(self) -> None:
        self.store = InMemoryFeedbackStore(known_leads=[(TENANT_A, LEAD_A), (TENANT_B, LEAD_B)])
        self.deps = FeedbackDeps(
            store=self.store, token_secret=SECRET, clock=FakeClock(start=NOW, step_ms=0)
        )
        self.client = TestClient(create_app(feedback_deps=self.deps))

    def confirm(self, token: str) -> Response:
        """POST the token with a valid confirmation code, as the rendered page would."""
        return self.client.post(
            f"/feedback/{token}",
            data={CONFIRM_FIELD: confirmation_code(secret=SECRET, token=token)},
        )


@pytest.mark.parametrize(
    ("name", "line", "value"),
    [
        ("another tenant's lead", 3, LEAD_B),
        ("another tenant", 2, TENANT_B),
        ("the other verdict", 4, "bad"),
    ],
)
def test_a_tampered_link_writes_nothing_at_all(name: str, line: int, value: str) -> None:
    """The assertion that matters: the store was never called.

    A 400 with a row written would be a poisoned golden set and a green test suite. The
    page is checked too, because a rejection that told the visitor which field was wrong
    would hand them the oracle the signature exists to close.
    """
    harness = Harness()
    forged = edited(token_for(TENANT_A, LEAD_A), line=line, value=value)

    response = harness.confirm(forged)

    assert response.status_code == 400, name
    assert harness.store.calls == 0, f"{name}: the store was reached"
    assert harness.store.rows == {}
    assert LEAD_A not in response.text and LEAD_B not in response.text
    assert TENANT_A not in response.text and TENANT_B not in response.text


def test_a_genuine_link_writes_exactly_one_row_under_its_own_tenant() -> None:
    """The positive control for the section, and the shape of a correct write."""
    harness = Harness()
    response = harness.confirm(token_for(TENANT_B, LEAD_B, rater=RATER_B))

    assert response.status_code == 200
    assert len(harness.store.rows) == 1
    row = next(iter(harness.store.rows.values()))
    assert (row.tenant_id, row.lead_id, row.rater) == (TENANT_B, LEAD_B, RATER_B)


def test_a_verdict_for_one_tenant_leaves_the_others_rows_alone() -> None:
    """Both tenants write real feedback; neither row is visible from the other's key."""
    harness = Harness()
    harness.confirm(token_for(TENANT_A, LEAD_A, rater=RATER_A, verdict=Verdict.GOOD))
    harness.confirm(token_for(TENANT_B, LEAD_B, rater=RATER_B, verdict=Verdict.BAD))

    assert set(harness.store.rows) == {
        (TENANT_A, LEAD_A, RATER_A),
        (TENANT_B, LEAD_B, RATER_B),
    }
    assert harness.store.rows[(TENANT_A, LEAD_A, RATER_A)].verdict is Verdict.GOOD
    assert harness.store.rows[(TENANT_B, LEAD_B, RATER_B)].verdict is Verdict.BAD


def test_the_store_refuses_a_verdict_against_a_lead_the_tenant_does_not_have() -> None:
    """The layer below the token, for the case where a token is somehow not the defence.

    The in-memory store mirrors the composite ``(tenant_id, lead_id)`` foreign key, and the
    Postgres one gets the same treatment from ``test_repository_isolation.py``. Both of
    them have to refuse, because "the token was valid" and "the lead is yours" are separate
    questions and only the database can answer the second.
    """
    store = InMemoryFeedbackStore(known_leads=[(TENANT_A, LEAD_A), (TENANT_B, LEAD_B)])

    with pytest.raises(UnknownLeadError):
        store.record_feedback(
            tenant_id=TENANT_B,
            lead_id=LEAD_A,
            rater=RATER_B,
            verdict=Verdict.BAD,
            notes=None,
            recorded_at=NOW,
        )
    assert store.rows == {}


# ------------------------------------------------------- what this deliberately does not do


def test_a_rater_id_is_not_tenant_scoped_and_the_row_key_is() -> None:
    """An honest limit, pinned so it cannot change without somebody noticing.

    :func:`~leadquali.app.feedback.rater_id` hashes the destination alone, so two tenants
    whose leads happen to route to the same shared inbox produce the same opaque rater id.
    That is not a leak — the id is one-way, it is never shown across a tenant boundary, and
    the row it keys is ``(tenant_id, lead_id, rater)``, so the two tenants' verdicts are
    still different rows. It is recorded here and in docs/tenant-isolation.md because a
    future per-rep identity scheme (#29) must not turn it into one.
    """
    shared = "leads@shared-agency.invalid"
    assert rater_id(shared) == rater_id(shared)

    store = InMemoryFeedbackStore()
    for tenant, lead in ((TENANT_A, LEAD_A), (TENANT_B, LEAD_B)):
        store.record_feedback(
            tenant_id=tenant,
            lead_id=lead,
            rater=rater_id(shared),
            verdict=Verdict.GOOD,
            notes=None,
            recorded_at=NOW,
        )

    assert len(store.rows) == 2, "one rater id must not collapse two tenants onto one row"
    assert {row.tenant_id for row in store.rows.values()} == {TENANT_A, TENANT_B}


def test_a_genuine_link_is_a_bearer_capability_and_that_is_the_design() -> None:
    """Whoever holds a valid link can use it, once, for the verdict it was minted for.

    Stated as a test so that the limit is in the suite rather than only in the prose. The
    bound on it is the signed expiry and the fact that the capability is exactly one
    verdict on exactly one lead — not a session, not a login, and nothing that can be
    pointed at a different tenant.
    """
    harness = Harness()
    token = token_for(TENANT_A, LEAD_A, rater=RATER_A)

    first = harness.confirm(token)
    second = harness.confirm(token)

    assert first.status_code == 200 and second.status_code == 200
    assert len(harness.store.rows) == 1, "a second click is an update, never a second row"

    expired = verify_token(secret=SECRET, token=token, now=EXPIRY + dt.timedelta(seconds=1))
    assert isinstance(expired, TokenRejected)
    assert expired.failure is TokenFailure.EXPIRED


def test_the_token_version_is_part_of_the_signed_material() -> None:
    """A future ``fb2`` with different fields cannot be read as an ``fb1`` claim."""
    token = token_for(TENANT_A, LEAD_A)
    assert token.startswith(f"{TOKEN_VERSION}.")
    forged = edited(token, line=1, value="fb2")
    result = verify_token(secret=SECRET, token=forged, now=NOW)
    assert isinstance(result, TokenRejected)
    assert result.failure is TokenFailure.MALFORMED
