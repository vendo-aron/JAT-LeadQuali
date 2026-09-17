"""The one decision that accepts or refuses a presented ingest key.

This file exists because of a specific way security tests rot. The rules below — is this
key's row the claimed tenant's, is it revoked, has its rotation overlap closed, does its
secret verify, is the tenant active — used to be written twice: once in
``StaticCredentials``, which the offline suite exercises, and once in
``PostgresIngestCredentials``, which is what production actually runs and which skips
without Docker. Deleting the cross-tenant check from the Postgres copy left every unit test
green. So the rules were extracted into :func:`decide_credential`, and this is where they
are proved — with no database, no Docker and no skip.

The verifier here is a stub, and deliberately so: what is under test is the *ordering and
the outcomes*, not the KDF. ``test_keyhash_argon2.py`` owns the KDF, and it uses the real
one. The two tests that are about ordering count stub calls, which is the only honest way
to assert "this check happened before that one".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from leadquali.app.credentials import (
    ACTIVE_STATUS,
    TENANT_STATUSES,
    AuthFailure,
    CredentialAccepted,
    CredentialRejected,
    StoredApiKey,
    decide_credential,
)

TENANT = "acme-demo"
KEY_ID = "3f1c9a02b7d45e68"
SECRET = "kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI"
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class CountingVerifier:
    """A verifier that answers instantly and records that it was asked.

    Counting is the point: "the status is checked after the KDF" and "a revoked key costs
    no KDF" are both claims about *work done*, and the only way to assert work that did not
    happen is to count it.
    """

    def __init__(self, *, answer: bool = True) -> None:
        self.answer = answer
        self.calls = 0

    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        del key_id, key_hash
        self.calls += 1
        return self.answer and secret == SECRET


def a_key(
    *, key_id: str = KEY_ID, revoked: bool = False, expires_at: datetime | None = None
) -> StoredApiKey:
    return StoredApiKey(
        key_id=key_id, key_hash="$argon2id$stub$x", revoked=revoked, expires_at=expires_at
    )


def decide(
    *,
    claimed: str = TENANT,
    owner: str = TENANT,
    status: str = ACTIVE_STATUS,
    key: StoredApiKey | None = None,
    secret: str = SECRET,
    now: datetime = NOW,
    verifier: CountingVerifier | None = None,
) -> tuple[CredentialAccepted | CredentialRejected, CountingVerifier]:
    """Run the decision with one piece of it changed, and hand back the verifier too."""
    kdf = verifier if verifier is not None else CountingVerifier()
    decision = decide_credential(
        claimed_tenant_id=claimed,
        row_tenant_id=owner,
        tenant_status=status,
        key=key if key is not None else a_key(),
        presented_secret=secret,
        now=now,
        verifier=kdf,
    )
    return decision, kdf


def refusal(decision: CredentialAccepted | CredentialRejected) -> AuthFailure:
    assert isinstance(decision, CredentialRejected), f"expected a refusal, got {decision!r}"
    return decision.failure


# ------------------------------------------------------------------------- acceptance


def test_a_live_key_for_an_active_tenant_is_accepted() -> None:
    decision, kdf = decide()
    assert decision == CredentialAccepted(tenant_id=TENANT, key_id=KEY_ID)
    assert kdf.calls == 1


def test_the_accepted_result_carries_no_signing_secret() -> None:
    """It is not an ``IngestCredential``, and that is the point: this function does no I/O,
    so a half-filled credential with ``signing_secret=b""`` would be a request signed with
    the empty key by any caller that forgot the last step."""
    decision, _ = decide()
    assert isinstance(decision, CredentialAccepted)
    assert not hasattr(decision, "signing_secret")


def test_a_key_inside_its_rotation_overlap_is_accepted() -> None:
    decision, _ = decide(key=a_key(expires_at=NOW + timedelta(days=7)))
    assert isinstance(decision, CredentialAccepted)


# -------------------------------------------------------------------------- refusals


def test_a_key_presented_under_another_tenants_name_is_refused() -> None:
    """Invariant 4 at the door. Reported as an unknown tenant so that it cannot be told
    apart from a ``key_id`` that does not exist at all."""
    decision, kdf = decide(claimed="someone-else", owner=TENANT)
    assert refusal(decision) is AuthFailure.UNKNOWN_TENANT
    assert kdf.calls == 0, "a key for the wrong tenant must not cost a KDF"


def test_a_revoked_key_is_refused() -> None:
    decision, kdf = decide(key=a_key(revoked=True))
    assert refusal(decision) is AuthFailure.REVOKED_KEY
    assert kdf.calls == 0


def test_a_key_past_its_rotation_overlap_is_refused() -> None:
    decision, kdf = decide(key=a_key(expires_at=NOW - timedelta(seconds=1)))
    assert refusal(decision) is AuthFailure.REVOKED_KEY
    assert kdf.calls == 0


def test_a_key_expiring_exactly_now_is_refused() -> None:
    """The boundary, pinned: ``expires_at`` is the moment it stops working, not the last
    moment it works. A rotation deadline that let one more request through would be a
    deadline nobody could reason about."""
    decision, _ = decide(key=a_key(expires_at=NOW))
    assert refusal(decision) is AuthFailure.REVOKED_KEY


def test_a_wrong_secret_is_refused() -> None:
    decision, kdf = decide(secret="w" * 43)
    assert refusal(decision) is AuthFailure.BAD_KEY
    assert kdf.calls == 1


def test_a_verifier_that_says_no_is_refused_whatever_the_secret_looks_like() -> None:
    decision, _ = decide(verifier=CountingVerifier(answer=False))
    assert refusal(decision) is AuthFailure.BAD_KEY


@pytest.mark.parametrize("status", [s for s in TENANT_STATUSES if s != ACTIVE_STATUS])
def test_a_tenant_that_is_not_active_is_refused_with_its_own_reason(status: str) -> None:
    decision, _ = decide(status=status)
    assert refusal(decision) is AuthFailure.TENANT_SUSPENDED


def test_an_unrecognised_status_is_refused_rather_than_waved_through() -> None:
    """Fail closed. A status the database grew and this code has not heard of must not
    default to "active"."""
    decision, _ = decide(status="pending-something")
    assert refusal(decision) is AuthFailure.TENANT_SUSPENDED


# --------------------------------------------------------------- the order of the checks


def test_the_suspension_answer_is_only_reachable_with_the_right_secret() -> None:
    """The 403 leak, pinned.

    ``403 "tenant is not active"`` is the one answer this endpoint gives that differs from
    every other rejection. A ``key_id`` is public — it travels in the clear on every
    submission — so if the status were checked before the KDF, anyone who had seen a
    customer's key could discover whether that account had been suspended for non-payment
    without holding the secret, and an integrator with a genuinely wrong key would be told
    in writing that their key was fine.
    """
    with_wrong_secret, kdf = decide(status="suspended", secret="w" * 43)
    assert refusal(with_wrong_secret) is AuthFailure.BAD_KEY
    assert kdf.calls == 1

    with_right_secret, _ = decide(status="suspended", secret=SECRET)
    assert refusal(with_right_secret) is AuthFailure.TENANT_SUSPENDED


def test_a_revoked_key_on_a_suspended_tenant_reports_the_revocation() -> None:
    """Revocation is checked first and costs nothing, so a revoked key never reaches the
    one answer that says something about the account."""
    decision, kdf = decide(status="suspended", key=a_key(revoked=True))
    assert refusal(decision) is AuthFailure.REVOKED_KEY
    assert kdf.calls == 0


def test_the_kdf_runs_only_after_every_free_check_has_passed() -> None:
    """The claim the whole "argon2 on the request path" design rests on, asserted by
    counting work on a verifier that has never been asked anything before."""
    kdf = CountingVerifier()
    decide(verifier=kdf, claimed="someone-else")
    decide(verifier=kdf, key=a_key(revoked=True))
    decide(verifier=kdf, key=a_key(expires_at=NOW - timedelta(days=1)))
    assert kdf.calls == 0

    decide(verifier=kdf)
    assert kdf.calls == 1


def test_the_decision_asks_the_verifier_about_the_stored_hash_and_the_presented_secret() -> None:
    """The arguments matter: a verifier asked about the wrong pair would answer honestly
    and wrongly."""
    seen: list[tuple[str, str, str]] = []

    class Recording:
        def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
            seen.append((key_id, secret, key_hash))
            return True

    decide_credential(
        claimed_tenant_id=TENANT,
        row_tenant_id=TENANT,
        tenant_status=ACTIVE_STATUS,
        key=StoredApiKey(key_id=KEY_ID, key_hash="$argon2id$real$hash"),
        presented_secret=SECRET,
        now=NOW,
        verifier=Recording(),
    )
    assert seen == [(KEY_ID, SECRET, "$argon2id$real$hash")]
