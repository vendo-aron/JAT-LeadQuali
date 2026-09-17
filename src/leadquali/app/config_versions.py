"""Editing a tenant's rubric so that it can be reviewed, attributed and undone.

Invariant 1 makes the rubric configuration rather than code, which is what lets a customer
be onboarded without a deploy. The same property makes editing it the **highest-risk
action in the product**: a threshold typed wrong at 5pm mis-routes every lead that tenant
receives overnight, and there is no build, no review and no rollback in the way. This
module is the review and the rollback.

The flow is edit → preview → confirm, and never a single POST that saves
------------------------------------------------------------------------

:meth:`ConfigEditor.preview` validates the candidate document with
:class:`~leadquali.domain.tenant_config.TenantConfig` — *the* validator, #8's, not a second
one — and returns a **field-by-field diff** against what is stored. A raw text diff would
be technically correct and practically useless: the thing an operator needs to see is
"``thresholds.hot`` 80 → 85", not three lines of JSON that happen to have moved.

Only :meth:`ConfigEditor.apply` writes, and it writes through
:meth:`~leadquali.app.tenants.TenantService.update_config` — the one writer of
``tenants.icp_config`` — while appending a version row **in the same transaction**. A
saved config with no audit row must be impossible rather than merely unlikely, so the two
writes are enrolled in one :class:`UnitOfWorkPort` and a failure between them leaves
neither. ``tests/unit/test_config_versions.py`` drives exactly that case.

Full snapshots, not patches
---------------------------

Every version row carries the **whole** config as it stood after that change. A chain of
patches is one bad apply away from being unreplayable, and the entire point of this table
is that a bad rubric can be undone at 3am by somebody who is not the person who wrote it.
The storage cost is a few kilobytes per edit of a document that changes a handful of times
a year.

Revert is an append, never a delete
-----------------------------------

:meth:`ConfigEditor.revert` re-applies an old version's config *as a new version*. The
audit trail only answers "who changed this, and to what?" if nothing is ever removed from
it, and a revert is itself a change somebody made and should have to account for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from leadquali.app.ports import ClockPort
from leadquali.app.tenants import TenantRecord, TenantService
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError

__all__ = [
    "MAX_NOTE_CHARS",
    "MIGRATION_SUBJECT",
    "ChangeKind",
    "ConfigEditor",
    "ConfigPreview",
    "ConfigVersion",
    "ConfigVersionStorePort",
    "FieldChange",
    "UnitOfWorkPort",
    "UnknownConfigVersionError",
    "diff_configs",
]

#: Longest "why" accepted on a version row. A sentence or two; the audit trail wants a
#: reason, not an essay, and an unbounded text field on a form is an unbounded write.
MAX_NOTE_CHARS: Final[int] = 500

#: ``changed_by`` on the version rows the migration seeds from each tenant's current
#: config. Named so that "who made this change?" has an answer for version 1 too — and so
#: that the answer is visibly not a person.
MIGRATION_SUBJECT: Final[str] = "migration"


class UnknownConfigVersionError(LookupError):
    """No such version for this tenant."""


class ChangeKind(StrEnum):
    """What one field-level difference is."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One difference between two config documents, at one dotted path."""

    path: str
    """Dotted path into the document, e.g. ``thresholds.hot`` or ``routing_rules.hot.action``."""

    before: Any
    """The stored value, or ``None`` for an :attr:`ChangeKind.ADDED` field."""

    after: Any
    """The candidate value, or ``None`` for a :attr:`ChangeKind.REMOVED` field."""

    kind: ChangeKind


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    """One ``tenant_config_versions`` row: a whole config, and who put it there."""

    tenant_slug: str
    version: int
    """Monotonic per tenant, allocated inside the writing transaction from the table."""

    config: Mapping[str, Any]
    """The **full** config after this change, not a patch."""

    changed_by: str
    changed_at: datetime
    note: str | None


@dataclass(frozen=True, slots=True)
class ConfigPreview:
    """A validated candidate config and what it would change. Nothing has been written."""

    tenant_slug: str
    document: Mapping[str, Any]
    """The candidate, exactly as it will be stored if confirmed."""

    validated: TenantConfig
    """The parsed form. Held so a caller can show derived facts — the tier bands, the
    routing table — rather than re-parsing the document to do it."""

    current: Mapping[str, Any]
    """What is stored right now, and what the diff is against."""

    changes: tuple[FieldChange, ...]

    @property
    def changed(self) -> bool:
        """Whether confirming this preview would actually change anything."""
        return bool(self.changes)


@runtime_checkable
class ConfigVersionStorePort(Protocol):
    """The append-only history of one tenant's rubric.

    There is deliberately no delete and no update: the table is the answer to "who changed
    this tenant's config, when, and to what", and a row that can be removed cannot answer
    it. Reverting appends.
    """

    def append(
        self,
        *,
        tenant_slug: str,
        config: Mapping[str, Any],
        changed_by: str,
        changed_at: datetime,
        note: str | None,
    ) -> ConfigVersion:
        """Append the next version for this tenant and return it.

        The version number is allocated **inside this call's transaction, from the table**
        — never from a counter held in Python, which two admin processes would hand out
        twice. Implementations rely on ``UNIQUE (tenant_id, version)`` to settle a race.

        Raises:
            UnknownTenantError: no such tenant.
        """
        ...

    def list_versions(
        self, *, tenant_slug: str, limit: int | None = None
    ) -> Sequence[ConfigVersion]:
        """This tenant's history, newest first."""
        ...

    def get_version(self, *, tenant_slug: str, version: int) -> ConfigVersion:
        """One version.

        Raises:
            UnknownConfigVersionError: this tenant has no such version.
        """
        ...


@runtime_checkable
class UnitOfWorkPort(Protocol):
    """Brackets several store calls into one transaction.

    A Protocol rather than a session, because ``app`` may not import SQLAlchemy. The
    Postgres implementation binds one session for the duration of the block and the stores
    inside it join that session instead of opening their own; see
    :mod:`leadquali.adapters.unit_of_work`.
    """

    def atomic(self) -> AbstractContextManager[None]:
        """Run the block in one transaction: it all commits, or none of it does."""
        ...


def diff_configs(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[FieldChange, ...]:
    """Compare two config documents field by field, deepest first, in path order.

    Nested objects are walked; anything else — a number, a string, ``None``, a list — is
    compared whole. Walking lists element by element would produce "``tags.0`` a → c" for
    an insertion at the front, which reads as five changes where a human made one. A
    rubric has no list-valued fields today, and "the list changed" is the honest reading if
    one appears.

    Args:
        before: The stored document.
        after: The candidate document.

    Returns:
        Every difference, ordered by dotted path so the rendering is stable between two
        previews of the same edit.
    """
    changes: list[FieldChange] = []
    _walk(before, after, prefix="", into=changes)
    return tuple(sorted(changes, key=lambda change: change.path))


def _walk(
    before: Mapping[str, Any], after: Mapping[str, Any], *, prefix: str, into: list[FieldChange]
) -> None:
    for key in sorted(set(before) | set(after)):
        path = f"{prefix}{key}"
        in_before, in_after = key in before, key in after
        old, new = before.get(key), after.get(key)
        if in_before and not in_after:
            into.append(FieldChange(path=path, before=old, after=None, kind=ChangeKind.REMOVED))
        elif in_after and not in_before:
            into.append(FieldChange(path=path, before=None, after=new, kind=ChangeKind.ADDED))
        elif isinstance(old, Mapping) and isinstance(new, Mapping):
            _walk(old, new, prefix=f"{path}.", into=into)
        elif old != new:
            into.append(FieldChange(path=path, before=old, after=new, kind=ChangeKind.CHANGED))


class ConfigEditor:
    """Preview, apply and revert one tenant's rubric, with an audit row for every change.

    Args:
        tenants: #31's control plane. The **only** writer of ``tenants.icp_config``; this
            class goes through it rather than around it, so the validation, the slug check
            and the "a bad paste cannot take a tenant down" property are stated once.
        versions: The append-only history.
        unit_of_work: What makes the config write and the version row one transaction.
        clock: Injected, so ``changed_at`` is deterministic in a test.
    """

    def __init__(
        self,
        *,
        tenants: TenantService,
        versions: ConfigVersionStorePort,
        unit_of_work: UnitOfWorkPort,
        clock: ClockPort,
    ) -> None:
        self._tenants = tenants
        self._versions = versions
        self._unit_of_work = unit_of_work
        self._clock = clock

    def preview(self, *, slug: str, document: Mapping[str, Any]) -> ConfigPreview:
        """Validate a candidate config and diff it against what is stored. Writes nothing.

        Args:
            slug: The tenant being edited.
            document: The candidate document, as parsed from the form.

        Returns:
            The validated candidate and the field-by-field diff.

        Raises:
            TenantConfigError: the document is invalid, or names a different tenant. The
                caller re-renders the form with this message **and the operator's text
                intact**: losing somebody's edit because they mistyped a threshold is how
                people stop using the tool.
            UnknownTenantError: no such tenant.
        """
        current = self._tenants.get_tenant(slug=slug)
        validated = self._validate(slug=slug, document=document)
        return ConfigPreview(
            tenant_slug=slug,
            document=dict(document),
            validated=validated,
            current=dict(current.config),
            changes=diff_configs(current.config, document),
        )

    def apply(
        self, *, slug: str, document: Mapping[str, Any], changed_by: str, note: str | None
    ) -> ConfigVersion:
        """Write the candidate config and its audit row, or write neither.

        Args:
            slug: The tenant being edited.
            document: The validated candidate. Re-validated here rather than trusted from
                the preview: the confirm arrives as a separate request and the document
                travels through the browser in between.
            changed_by: The staff subject from the session. Never blank — an audit row
                with no author is not an audit row.
            note: Why, in the operator's words. Optional, and truncated at
                :data:`MAX_NOTE_CHARS`.

        Returns:
            The version row that was appended.

        Raises:
            TenantConfigError: the document is invalid. Nothing was written.
            ValueError: ``changed_by`` is blank, or the document is identical to the
                stored one — a version row that records no change is noise in the only
                audit trail there is.
            UnknownTenantError: no such tenant.
        """
        author = changed_by.strip()
        if not author:
            raise ValueError("a config change needs a changed_by: an audit row needs an author")
        preview = self.preview(slug=slug, document=document)
        if not preview.changed:
            raise ValueError(
                f"this document is identical to tenant '{slug}''s stored config, so there "
                "is no change to record"
            )
        return self._write(slug=slug, document=preview.document, changed_by=author, note=note)

    def revert(
        self, *, slug: str, to_version: int, changed_by: str, note: str | None
    ) -> ConfigVersion:
        """Re-apply an earlier version's config **as a new version**.

        Never a delete and never a rewind: the history is the answer to "who changed this
        and to what", and a revert is itself a change somebody made.

        Args:
            slug: The tenant.
            to_version: The version whose config is being restored.
            changed_by: The staff subject doing the reverting.
            note: Why. Defaults to naming the version being restored, because "reverted to
                version 4" is the single most useful thing this row can say.

        Returns:
            The new version row.

        Raises:
            UnknownConfigVersionError: this tenant has no such version.
            ValueError: the named version is already what is stored.
        """
        restored = self._versions.get_version(tenant_slug=slug, version=to_version)
        return self.apply(
            slug=slug,
            document=restored.config,
            changed_by=changed_by,
            note=note if note else f"reverted to version {to_version}",
        )

    def history(self, *, slug: str, limit: int | None = None) -> Sequence[ConfigVersion]:
        """This tenant's config history, newest first."""
        return self._versions.list_versions(tenant_slug=slug, limit=limit)

    def current(self, *, slug: str) -> TenantRecord:
        """The tenant as stored, for a form to render from.

        Raises:
            UnknownTenantError: no such tenant.
        """
        return self._tenants.get_tenant(slug=slug)

    # ----------------------------------------------------------------------- internals

    def _write(
        self, *, slug: str, document: Mapping[str, Any], changed_by: str, note: str | None
    ) -> ConfigVersion:
        """Both writes, in one transaction, config first.

        Config first because :meth:`TenantService.update_config` is what raises
        :class:`UnknownTenantError` and re-runs the validation, and doing the cheap
        refusals before the append keeps the audit table free of rows for changes that
        never happened. Order does not decide atomicity — the unit of work does — but it
        decides which error an operator sees.
        """
        with self._unit_of_work.atomic():
            self._tenants.update_config(slug=slug, config=document)
            return self._versions.append(
                tenant_slug=slug,
                config=dict(document),
                changed_by=changed_by,
                changed_at=self._clock.now(),
                note=_clean_note(note),
            )

    def _validate(self, *, slug: str, document: Mapping[str, Any]) -> TenantConfig:
        """#8's validator, plus the check that the document names this tenant.

        The slug check duplicates :meth:`TenantService.update_config`'s, on purpose: the
        preview has to fail with the same message the confirm would, or an operator would
        get a clean preview and a refused save.
        """
        validated = TenantConfig.from_dict(dict(document))
        if validated.tenant_id != slug:
            raise TenantConfigError(
                f"this config names tenant '{validated.tenant_id}' but it is being edited "
                f"as '{slug}'. They have to be the same name — two names for one tenant is "
                "how a rubric ends up applied to the wrong customer's leads"
            )
        return validated


def _clean_note(note: str | None) -> str | None:
    """Strip a note and bound it, or report that there was not one."""
    if note is None:
        return None
    cleaned = " ".join(note.split())
    return cleaned[:MAX_NOTE_CHARS] if cleaned else None
