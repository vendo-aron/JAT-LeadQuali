"""What a lead's payload is: the shape a form posts, and the shape ``leads.raw_payload`` holds.

This was ``api/schemas.py``'s :class:`LeadForm` and it moved here for the same reason
:mod:`leadquali.app.credentials` exists: an adapter needed it, and the layering rule in
``CLAUDE.md`` is one-directional — ``domain`` ← ``app`` ← ``adapters``/``api``.

The adapter in question is #36's ``PostgresAdminQueryStore.rerun_candidates``, which
rebuilds a :class:`~leadquali.prompts.lead.LeadSubmission` from a stored
``raw_payload`` so a historical lead can be re-assessed against a candidate rubric. It has
to parse that column **exactly** the way ingest parsed it on the way in: a re-run that read
old rows more leniently than the door reads new ones would be previewing a pipeline that
does not exist. The alternative — a second parser in the adapter — is the outcome that
actually costs something, because two descriptions of "what a stored payload looks like"
drift from each other inside a release and the one that drifts is the one nobody tests
against.

Seen that way the move is not a workaround for a test. **``LeadForm`` was never really an
HTTP concern.** It describes the *content* of a submission — which fields a form may send,
how long each may be, and that an unanticipated field is welcome while an unanticipated
document is not. The HTTP part of ingest is the envelope around it
(:class:`~leadquali.api.schemas.LeadIngestRequest`: the idempotency key, the honeypot, the
timing signal, ``extra="forbid"``), and that stays in ``api`` where it belongs.

``api/schemas.py`` imports these names and re-exports them, so #17's ingest path, its
tests and #22's golden-set loader are unchanged: there is one ``LeadForm`` class, reachable
by both spellings.

The caps here are the *schema's*, and they are deliberately far more generous than
:mod:`leadquali.prompts.lead`'s same-named constants. This layer decides what may be
**stored**; the renderer decides what is **shown to the model**, and it truncates rather
than rejects. A long genuine enquiry must not be answered with a 422.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from leadquali.prompts.lead import LeadSubmission

__all__ = [
    "MAX_EXTRA_FIELDS",
    "MAX_MESSAGE_CHARS",
    "MAX_SHORT_FIELD_CHARS",
    "LeadForm",
]

#: Longest a single short field may be. Generous against a real form, and far above
#: ``prompts.lead.MAX_SHORT_FIELD_CHARS``, which is what the *renderer* truncates to.
MAX_SHORT_FIELD_CHARS: Final[int] = 1_000

#: Longest a message may be. Same reasoning: a person writing at length about their problem
#: is the lead you want, and refusing them with a 422 would be an own goal. The real bound
#: on request size is the body-size limit applied before any of this runs.
MAX_MESSAGE_CHARS: Final[int] = 20_000

#: How many unrecognised fields survive into the submission. Bounded because the count is
#: attacker-chosen and everything downstream is per-field work.
MAX_EXTRA_FIELDS: Final[int] = 50


class LeadForm(BaseModel):
    """The form's fields. Known ones are typed; anything else is kept as a scalar.

    Also the parser for ``leads.raw_payload``, which is the same document: the ingest
    endpoint stores what this validated, so anything reading the column back — #36's
    re-run, a replay tool, a migration — goes through this class rather than trusting the
    JSON to still have the shape it had when it was written.
    """

    model_config = ConfigDict(extra="allow")

    full_name: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    email: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    company: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    role: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    phone: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    website: str | None = Field(default=None, max_length=MAX_SHORT_FIELD_CHARS)
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)

    @model_validator(mode="after")
    def _unknown_fields_are_scalars(self) -> LeadForm:
        """An unanticipated field is welcome; an unanticipated document is not.

        A nested object or array in a form field is not something a form produces, and
        accepting one would mean stringifying arbitrarily deep structure on the request
        path. Rejecting it keeps the work per request bounded by the body-size limit.
        """
        for name, value in (self.model_extra or {}).items():
            if isinstance(value, dict | list):
                raise ValueError(f"form field '{name[:64]}' must be a single value, not a list")
        return self

    def to_submission(self) -> LeadSubmission:
        """Map the wire form onto the renderer's input.

        Unknown fields become :attr:`~leadquali.prompts.lead.LeadSubmission.extra`,
        stringified and capped in count. The renderer sanitises, truncates and escapes
        every one of them, so nothing here needs to decide whether a value is safe — only
        whether it is bounded.
        """
        extras: dict[str, str | None] = {}
        for name, value in list((self.model_extra or {}).items())[:MAX_EXTRA_FIELDS]:
            extras[name] = None if value is None else str(value)
        return LeadSubmission(
            full_name=self.full_name,
            email=self.email,
            company=self.company,
            role=self.role,
            phone=self.phone,
            website=self.website,
            message=self.message,
            extra=extras,
        )
