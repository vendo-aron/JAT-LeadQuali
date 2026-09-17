"""Invariant 5, as code: the hash that may be logged and the net that catches the rest.

``CLAUDE.md``: *no PII in logs — log ``contact_email_hash``, never the address; never raw
payloads.* That is a rule about every call site in the codebase, present and future, and a
rule of that shape is kept by two things and not by one:

* :func:`contact_email_hash` — the *positive* half. There is exactly one definition of the
  hash in the system (``adapters.store_postgres`` re-exports this one) because a log line
  and a ``leads`` row that hash the same address differently cannot be joined, and a
  correlation identifier that does not correlate is worse than no identifier at all.
* :func:`redact_emails` — the *negative* half, applied by both formatters to every rendered
  message and every traceback on the way out. It is defence in depth, not permission to be
  careless: it catches the address that arrives inside an exception message from a
  dependency we do not control, which no amount of discipline at our own call sites can
  prevent.

The net has a limit worth stating plainly, because it bounds what the tests can promise:
it recognises a *pattern*, and the lead's free-text message is not a pattern. Nothing here
can stop ``LOGGER.info(submission.message)``. That is why ``tests/unit/
test_observability_pipeline.py`` runs a real lead through the real pipeline and asserts the
message text appears in no record — the only mechanism that actually holds.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: What an address is replaced by. Deliberately visible rather than silent: an operator who
#: sees this in a log line has learned that a call site tried to log an address, which is a
#: bug to fix and not a thing to be relieved about.
EMAIL_REDACTION: Final[str] = "[redacted-email]"

#: Addresses, matched loosely on purpose. Over-matching costs a redacted string in a log
#: line; under-matching costs a customer's contact in a log aggregator, so every ambiguous
#: case resolves towards redaction. Bounded quantifiers keep it linear on hostile input.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}"
)


def contact_email_hash(email: str | None) -> str | None:
    """SHA-256 of the normalised contact address, or ``None`` when there is no address.

    This is the identifier a log line carries in place of the address, and it is the same
    value stored in ``leads.contact_email_hash`` — the two exist to be joined.

    Normalisation is strip-and-lowercase, so ``Ada@Example.com`` and ``ada@example.com``
    hash alike, which is the entire point of storing it.

    Not reversible-proof: an address has little entropy and a determined attacker with a
    wordlist will invert this. It defends against casual disclosure — a support engineer
    reading logs, a log aggregator with broad access, an exported bundle — which is the
    threat that actually materialises.
    """
    if email is None:
        return None
    normalised = email.strip().lower()
    if not normalised:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def redact_emails(text: str) -> str:
    """Replace every address-shaped run in ``text`` with :data:`EMAIL_REDACTION`.

    Applied by the formatters to the rendered message and to the exception traceback, which
    is where an address arrives without anyone having decided to log one: a bounce message
    quoted by SES, a validation error from a library that echoes its input, a
    ``ValueError`` that interpolated the value it choked on.
    """
    return _EMAIL_RE.sub(EMAIL_REDACTION, text)


__all__ = ["EMAIL_REDACTION", "contact_email_hash", "redact_emails"]
