"""The deterministic spam pre-filters: the first cost lever, and the cheapest one.

Every case here is decided without a token, so the assertions are about which reason was
recorded rather than about anything a model said. The reason matters as much as the
verdict: it is what goes on the suppression's routing event, and "we suppressed it because
the honeypot was filled" is an answer the business can audit, while "we suppressed it" is
not.
"""

from __future__ import annotations

import pytest

from leadquali.domain.spam import (
    DEFAULT_SPAM_POLICY,
    SpamPolicy,
    SpamReason,
    SpamVerdict,
    screen,
)
from leadquali.prompts.lead import LeadSubmission

GOOD = LeadSubmission(
    full_name="Ada Lovelace",
    email="ada@analytical-engines.co.uk",
    company="Analytical Engines",
    message="We need to qualify about 400 inbound leads a month. Can you help?",
)


def test_a_real_looking_submission_is_not_spam() -> None:
    verdict = screen(submission=GOOD, honeypot=None, elapsed_ms=9_000)
    assert verdict.is_spam is False
    assert verdict.reason is None


def test_missing_timing_information_is_not_by_itself_spam() -> None:
    """A form that does not report its render time must not have its leads suppressed."""
    assert screen(submission=GOOD, honeypot=None, elapsed_ms=None).is_spam is False


def test_a_filled_honeypot_is_spam() -> None:
    verdict = screen(submission=GOOD, honeypot="https://buy-followers.example", elapsed_ms=9_000)
    assert verdict.reason is SpamReason.HONEYPOT


def test_a_blank_honeypot_is_what_a_human_leaves_behind() -> None:
    for value in ("", "   ", None):
        assert screen(submission=GOOD, honeypot=value, elapsed_ms=9_000).is_spam is False


def test_a_submission_faster_than_a_human_can_type_is_spam() -> None:
    verdict = screen(submission=GOOD, honeypot=None, elapsed_ms=120)
    assert verdict.reason is SpamReason.TOO_FAST


def test_a_negative_elapsed_time_is_spam_not_an_error() -> None:
    """A client-reported duration is attacker-controlled; nonsense is a bot signal."""
    assert screen(submission=GOOD, honeypot=None, elapsed_ms=-5).reason is SpamReason.TOO_FAST


def test_the_timing_threshold_is_policy_not_a_constant() -> None:
    policy = SpamPolicy(min_elapsed_ms=0)
    assert screen(submission=GOOD, honeypot=None, elapsed_ms=1, policy=policy).is_spam is False


@pytest.mark.parametrize(
    "email",
    [
        "bot@mailinator.com",
        "bot@EXAMPLE.COM",
        "  bot@guerrillamail.com  ",
        "bot@host.test",
        "bot@whatever.invalid",
    ],
)
def test_obviously_fake_email_domains_are_spam(email: str) -> None:
    submission = GOOD.model_copy(update={"email": email})
    verdict = screen(submission=submission, honeypot=None, elapsed_ms=9_000)
    assert verdict.reason is SpamReason.FAKE_EMAIL_DOMAIN


@pytest.mark.parametrize("email", [None, "", "   ", "not-an-address", "a@", "@b.com", "a@b@c.com"])
def test_an_unusable_contact_address_is_spam(email: str | None) -> None:
    submission = GOOD.model_copy(update={"email": email})
    verdict = screen(submission=submission, honeypot=None, elapsed_ms=9_000)
    assert verdict.reason is SpamReason.UNUSABLE_EMAIL


def test_empty_free_text_is_spam_when_the_policy_requires_it() -> None:
    submission = GOOD.model_copy(update={"message": "   "})
    assert screen(submission=submission, honeypot=None, elapsed_ms=9_000).reason is (
        SpamReason.EMPTY_MESSAGE
    )


def test_empty_free_text_can_be_allowed_for_a_form_without_a_message_field() -> None:
    policy = SpamPolicy(require_message=False)
    submission = GOOD.model_copy(update={"message": None})
    verdict = screen(submission=submission, honeypot=None, elapsed_ms=9_000, policy=policy)
    assert verdict.is_spam is False


def test_the_honeypot_wins_over_every_other_reason() -> None:
    """Reason ordering is deliberate: the strongest bot signal is the one recorded."""
    submission = GOOD.model_copy(update={"email": "bot@mailinator.com", "message": ""})
    verdict = screen(submission=submission, honeypot="x", elapsed_ms=1)
    assert verdict.reason is SpamReason.HONEYPOT


def test_the_detail_line_never_carries_the_contact_address() -> None:
    """Invariant 5: a suppression reason is written to a routing event and to a log."""
    submission = GOOD.model_copy(update={"email": "ada@mailinator.com"})
    detail = screen(submission=submission, honeypot=None, elapsed_ms=9_000).detail
    assert "ada@" not in detail
    assert detail
    for other in (
        screen(submission=GOOD.model_copy(update={"email": "ada@nope"}), honeypot=None),
        screen(submission=GOOD, honeypot="ada@somewhere.com"),
    ):
        assert "ada" not in other.detail


def test_a_clean_verdict_has_no_detail() -> None:
    assert SpamVerdict.clean() == SpamVerdict(reason=None, detail="")


def test_the_default_policy_blocks_the_reserved_documentation_domains() -> None:
    """RFC 2606 names these as never-real, so a lead from one is never a customer."""
    assert "example.com" in DEFAULT_SPAM_POLICY.blocked_email_domains
    assert DEFAULT_SPAM_POLICY.min_elapsed_ms > 0
