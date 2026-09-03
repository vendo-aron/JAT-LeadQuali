"""Deterministic spam pre-filters. Free, offline, and the first cost lever in plan §8.

Every check here is arithmetic and string comparison on data the form already sent, so a
bot submission costs nothing: it is caught before the queue, before the worker and before
a single token is spent. That is the whole point — the model is the expensive resource and
the cheapest way to protect it is to never hand it something obviously worthless.

Two things this module is deliberately *not*:

* **Not a judgement.** "This lead looks weak" is the model's job. Everything here is a
  mechanical signal a real submitter cannot trip: a hidden field that only a form-filling
  script fills, a submission faster than a human can type, an address at a domain that is
  reserved as fake by RFC 2606 or sold as disposable by the minute.
* **Not a drop.** A caught submission is still persisted and still recorded, with the
  reason (invariant 3). Suppression is the one place code may decide a lead stops here,
  and even then the row exists and says why.

Thresholds and the blocked-domain list are :class:`SpamPolicy` values rather than module
constants used in place, so a tenant whose form has no message field is a configuration
change and not a code change. Pure logic: no I/O, no clock, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from leadquali.prompts.lead import LeadSubmission

#: Below this many milliseconds between rendering the form and posting it, the submitter
#: is not a person. Humans take seconds to read a label; scripts post in tens of
#: milliseconds. Deliberately generous — a false "too fast" suppresses a real lead.
DEFAULT_MIN_ELAPSED_MS: Final[int] = 2_000

#: Domains that can never belong to a customer: RFC 2606/6761 reserved names, the
#: documentation domains, and the disposable-inbox services bots reach for by default.
DEFAULT_BLOCKED_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "example.com",
        "example.net",
        "example.org",
        "test.com",
        "email.com",
        "mailinator.com",
        "guerrillamail.com",
        "sharklasers.com",
        "10minutemail.com",
        "yopmail.com",
        "trashmail.com",
        "tempmail.com",
        "temp-mail.org",
        "getnada.com",
        "dispostable.com",
        "fakeinbox.com",
        "throwawaymail.com",
        "maildrop.cc",
        "spam4.me",
        "discard.email",
        "mytemp.email",
    }
)

#: Reserved top-level names that resolve nowhere, so an address under one is fabricated.
DEFAULT_BLOCKED_EMAIL_TLDS: Final[frozenset[str]] = frozenset(
    {"test", "invalid", "example", "localhost", "local"}
)


class SpamReason(StrEnum):
    """Why a submission was suppressed before it cost anything.

    Recorded on the suppression's routing event and grouped in metrics: a spike in
    :attr:`HONEYPOT` is a bot campaign, a spike in :attr:`UNUSABLE_EMAIL` is usually a
    broken form on the customer's site, and the two need different people told.
    """

    HONEYPOT = "honeypot"
    """A field no human can see was filled in."""

    TOO_FAST = "too_fast"
    """The form was posted faster than a person could have completed it."""

    FAKE_EMAIL_DOMAIN = "fake_email_domain"
    """The address is at a reserved, documentation or disposable-inbox domain."""

    UNUSABLE_EMAIL = "unusable_email"
    """There is no address, or what arrived cannot be one."""

    EMPTY_MESSAGE = "empty_message"
    """The free-text field the form requires came through empty."""


@dataclass(frozen=True, slots=True)
class SpamVerdict:
    """The outcome of screening one submission.

    :attr:`detail` is one short line, PII-free by construction (invariant 5): it names the
    rule that fired and, for a blocked domain, the public domain from the list — never the
    address, never the submitter's own words.
    """

    reason: SpamReason | None
    detail: str = ""

    @property
    def is_spam(self) -> bool:
        """Whether this submission stops here."""
        return self.reason is not None

    @classmethod
    def clean(cls) -> SpamVerdict:
        """No pre-filter fired; the lead continues to the queue."""
        return cls(reason=None, detail="")

    @classmethod
    def caught(cls, reason: SpamReason, detail: str) -> SpamVerdict:
        """This submission is suppressed, for ``reason``."""
        return cls(reason=reason, detail=detail)


@dataclass(frozen=True, slots=True)
class SpamPolicy:
    """What "obviously not a lead" means for one deployment.

    A value rather than constants read at the point of use, so raising the timing floor or
    allowing message-less submissions from a one-field form is a wiring change. The
    defaults are the conservative ones: they suppress only what cannot be a customer.
    """

    min_elapsed_ms: int = DEFAULT_MIN_ELAPSED_MS
    """Minimum plausible time from form render to submit. ``0`` disables the check."""

    blocked_email_domains: frozenset[str] = field(default=DEFAULT_BLOCKED_EMAIL_DOMAINS)
    blocked_email_tlds: frozenset[str] = field(default=DEFAULT_BLOCKED_EMAIL_TLDS)

    require_message: bool = True
    """Whether an empty free-text field suppresses the submission.

    ``True`` for a form with a message box, which is the normal case and where an empty one
    means a script filled the fields it recognised. Set it ``False`` for a form that has no
    such field at all, where every real lead would otherwise be suppressed.
    """


#: The policy used when a caller does not supply one.
DEFAULT_SPAM_POLICY: Final[SpamPolicy] = SpamPolicy()


def screen(
    *,
    submission: LeadSubmission,
    honeypot: str | None = None,
    elapsed_ms: int | None = None,
    policy: SpamPolicy = DEFAULT_SPAM_POLICY,
) -> SpamVerdict:
    """Screen one submission against the free filters, strongest signal first.

    Args:
        submission: the lead as it arrived, already schema-validated.
        honeypot: the value of the hidden field a human never sees. Anything non-blank
            means a script filled the form.
        elapsed_ms: milliseconds the client reports between rendering the form and posting
            it. ``None`` when the form does not report it, which is not itself suspicious.
        policy: the thresholds and lists to apply.

    Returns:
        A :class:`SpamVerdict`. The order of the checks is the order of the reasons'
        strength, so a submission that trips several is recorded under the least
        deniable one.
    """
    if honeypot is not None and honeypot.strip():
        return SpamVerdict.caught(
            SpamReason.HONEYPOT, "hidden honeypot field was filled in; no human sees it"
        )

    if elapsed_ms is not None and policy.min_elapsed_ms > 0 and elapsed_ms < policy.min_elapsed_ms:
        return SpamVerdict.caught(
            SpamReason.TOO_FAST,
            f"submitted {elapsed_ms}ms after render, under the {policy.min_elapsed_ms}ms floor",
        )

    domain = _email_domain(submission.email)
    if domain is None:
        return SpamVerdict.caught(
            SpamReason.UNUSABLE_EMAIL, "contact address is missing or not a usable address"
        )
    if domain in policy.blocked_email_domains:
        return SpamVerdict.caught(
            SpamReason.FAKE_EMAIL_DOMAIN, f"email domain '{domain}' is on the blocked list"
        )
    tld = domain.rsplit(".", 1)[-1]
    if tld in policy.blocked_email_tlds:
        return SpamVerdict.caught(
            SpamReason.FAKE_EMAIL_DOMAIN, f"email domain ends in reserved TLD '.{tld}'"
        )

    if policy.require_message and not (submission.message or "").strip():
        return SpamVerdict.caught(
            SpamReason.EMPTY_MESSAGE, "the form's free-text field arrived empty"
        )

    return SpamVerdict.clean()


def _email_domain(email: str | None) -> str | None:
    """The lowercased domain of a syntactically usable address, or ``None``.

    Not a validator — RFC 5321 addresses are stranger than anything worth rejecting on —
    just enough structure to tell an address from a string a bot typed: exactly one ``@``,
    something either side of it, and a dotted domain with no whitespace.
    """
    if email is None:
        return None
    candidate = email.strip().lower()
    if not candidate or any(character.isspace() for character in candidate):
        return None
    local, separator, domain = candidate.partition("@")
    if not separator or not local or "@" in domain:
        return None
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return domain
