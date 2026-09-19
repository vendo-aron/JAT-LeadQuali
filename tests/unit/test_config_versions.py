"""The config editor: validate, diff, write both rows or neither, revert by appending.

Every property here runs without Postgres. That is deliberate and it is the lesson from
#31's and #33's reviews: a money- or policy-critical guarantee asserted only by a
Docker-gated test is a guarantee that a mutation can break while the suite stays green.
:class:`~tests.fakes.FakeUnitOfWork` models a transaction honestly — it snapshots every
participant on entry and restores them on any exception — so "a failure between the two
writes leaves neither" is a real assertion about :class:`ConfigEditor`'s control flow. That
the *Postgres* unit of work really is one transaction is asserted separately, against
SQLite, in ``tests/unit/test_unit_of_work.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from leadquali.app.config_versions import (
    ChangeKind,
    ConfigEditor,
    ConfigVersion,
    diff_configs,
)
from leadquali.app.tenants import TenantService, UnknownTenantError
from leadquali.domain.tenant_config import TenantConfigError
from tests.fakes import (
    ExplodingConfigVersionStore,
    FakeClock,
    FakeSecretHasher,
    FakeTenantSecrets,
    FakeUnitOfWork,
    InMemoryConfigVersionStore,
    InMemoryTenantAdminStore,
)

SLUG = "acme"
STAFF = "ada"


def config_document(slug: str = SLUG, **overrides: Any) -> dict[str, Any]:
    """A minimal valid rubric, so a test can change one field and mean it."""
    document: dict[str, Any] = {
        "tenant_id": slug,
        "name": slug.title(),
        "icp_description": "Mid-market logistics companies with a revenue team.",
        "weights": {
            "icp_fit": 1.0,
            "intent": 1.0,
            "authority": 1.0,
            "budget_signal": 1.0,
            "urgency": 1.0,
        },
        "thresholds": {"hot": 80.0, "warm": 55.0, "cold": 30.0},
        "min_confidence": 0.6,
        "routing_rules": {
            "hot": {"action": "email_sales", "destination": "hot@example.com"},
            "warm": {"action": "email_sales", "destination": "warm@example.com"},
            "cold": {"action": "email_sales", "destination": "cold@example.com"},
            "disqualified": {"action": "suppress", "destination": None},
        },
        "prompt_version": "v1",
    }
    document.update(overrides)
    return document


def build(
    *, versions: InMemoryConfigVersionStore | ExplodingConfigVersionStore | None = None
) -> tuple[ConfigEditor, InMemoryTenantAdminStore, Any]:
    store = InMemoryTenantAdminStore()
    tenants = TenantService(
        store=store,
        hasher=FakeSecretHasher(),
        secrets=FakeTenantSecrets(),
        clock=FakeClock(),
    )
    tenants.create_tenant(slug=SLUG, name="Acme", config=config_document())
    resolved = versions if versions is not None else InMemoryConfigVersionStore()
    resolved.seed(tenant_slug=SLUG, config=config_document(), changed_by="migration")
    editor = ConfigEditor(
        tenants=tenants,
        versions=resolved,
        unit_of_work=FakeUnitOfWork(store, resolved),
        clock=FakeClock(),
    )
    return editor, store, resolved


# ------------------------------------------------------------------------------- diff


def test_the_diff_is_field_by_field_and_names_the_path() -> None:
    changes = diff_configs(
        config_document(), config_document(thresholds={"hot": 85.0, "warm": 55.0, "cold": 30.0})
    )

    assert [(change.path, change.before, change.after) for change in changes] == [
        ("thresholds.hot", 80.0, 85.0)
    ]


def test_the_diff_reports_added_and_removed_fields_distinctly() -> None:
    before = {"a": 1, "b": {"c": 2}}
    after = {"a": 1, "b": {"d": 3}}

    changes = {change.path: change.kind for change in diff_configs(before, after)}

    assert changes == {"b.c": ChangeKind.REMOVED, "b.d": ChangeKind.ADDED}


def test_an_identical_config_has_an_empty_diff() -> None:
    assert diff_configs(config_document(), config_document()) == ()


def test_a_list_is_compared_whole_rather_than_element_by_element() -> None:
    """A rubric has no lists today; if one appears, "the list changed" is the honest read."""
    changes = diff_configs({"tags": ["a", "b"]}, {"tags": ["a", "c"]})

    assert len(changes) == 1
    assert changes[0].path == "tags"
    assert changes[0].kind is ChangeKind.CHANGED


# ---------------------------------------------------------------------------- preview


def test_preview_validates_and_diffs_without_writing_anything() -> None:
    editor, store, versions = build()

    preview = editor.preview(slug=SLUG, document=config_document(min_confidence=0.75))

    assert preview.changes[0].path == "min_confidence"
    assert store.tenants[SLUG].config["min_confidence"] == 0.6
    assert len(versions.versions(SLUG)) == 1


def test_preview_refuses_an_invalid_config() -> None:
    editor, _, _ = build()

    with pytest.raises(TenantConfigError):
        editor.preview(slug=SLUG, document=config_document(min_confidence=7.0))


def test_preview_refuses_a_config_that_renames_the_tenant() -> None:
    """Two names for one tenant is how a rubric ends up applied to the wrong customer."""
    editor, _, _ = build()

    with pytest.raises(TenantConfigError):
        editor.preview(slug=SLUG, document=config_document(slug="someone-else"))


def test_preview_of_an_unknown_tenant_raises() -> None:
    editor, _, _ = build()

    with pytest.raises(UnknownTenantError):
        editor.preview(slug="nobody", document=config_document("nobody"))


# ------------------------------------------------------------------------------ apply


def test_applying_writes_exactly_one_config_and_exactly_one_version() -> None:
    editor, store, versions = build()

    applied = editor.apply(
        slug=SLUG, document=config_document(min_confidence=0.75), changed_by=STAFF, note="tighter"
    )

    assert store.tenants[SLUG].config["min_confidence"] == 0.75
    assert isinstance(applied, ConfigVersion)
    assert applied.version == 2
    assert [version.version for version in versions.versions(SLUG)] == [1, 2]
    assert versions.versions(SLUG)[-1].changed_by == STAFF
    assert versions.versions(SLUG)[-1].note == "tighter"


def test_the_version_row_holds_the_whole_config_and_not_a_patch() -> None:
    """A chain of patches is one bad apply away from being unreplayable."""
    editor, _, versions = build()

    editor.apply(
        slug=SLUG, document=config_document(min_confidence=0.75), changed_by=STAFF, note=None
    )

    assert versions.versions(SLUG)[-1].config == config_document(min_confidence=0.75)


def test_an_invalid_config_writes_neither_row() -> None:
    editor, store, versions = build()

    with pytest.raises(TenantConfigError):
        editor.apply(
            slug=SLUG, document=config_document(min_confidence=7.0), changed_by=STAFF, note=None
        )

    assert store.tenants[SLUG].config["min_confidence"] == 0.6
    assert len(versions.versions(SLUG)) == 1


def test_a_failing_version_insert_leaves_the_config_unchanged() -> None:
    """The one that matters: a saved config with no audit row must be impossible."""
    editor, store, versions = build(versions=ExplodingConfigVersionStore())

    with pytest.raises(RuntimeError):
        editor.apply(
            slug=SLUG, document=config_document(min_confidence=0.75), changed_by=STAFF, note=None
        )

    assert store.tenants[SLUG].config["min_confidence"] == 0.6
    assert versions.appends == 1, "the version insert is supposed to have been attempted"


def test_applying_an_unchanged_config_is_refused_rather_than_recorded() -> None:
    """A version row that records no change is noise in the only audit trail there is."""
    editor, _, versions = build()

    with pytest.raises(ValueError, match="no change"):
        editor.apply(slug=SLUG, document=config_document(), changed_by=STAFF, note=None)

    assert len(versions.versions(SLUG)) == 1


def test_applying_requires_a_staff_subject() -> None:
    editor, _, _ = build()

    with pytest.raises(ValueError, match="changed_by"):
        editor.apply(
            slug=SLUG, document=config_document(min_confidence=0.75), changed_by="  ", note=None
        )


# ----------------------------------------------------------------------------- revert


def test_revert_restores_the_earlier_config_and_appends_a_new_version() -> None:
    editor, store, versions = build()
    editor.apply(
        slug=SLUG, document=config_document(min_confidence=0.75), changed_by=STAFF, note=None
    )

    reverted = editor.revert(slug=SLUG, to_version=1, changed_by="bob", note="rolled back")

    assert store.tenants[SLUG].config["min_confidence"] == 0.6
    assert reverted.version == 3, "a revert is a new version, never a deletion"
    assert [version.version for version in versions.versions(SLUG)] == [1, 2, 3]
    assert versions.versions(SLUG)[-1].config == config_document()


def test_reverting_to_the_current_config_is_refused() -> None:
    editor, _, versions = build()

    with pytest.raises(ValueError, match="no change"):
        editor.revert(slug=SLUG, to_version=1, changed_by=STAFF, note=None)

    assert len(versions.versions(SLUG)) == 1


def test_reverting_to_a_version_that_does_not_exist_raises() -> None:
    editor, _, _ = build()

    with pytest.raises(LookupError):
        editor.revert(slug=SLUG, to_version=99, changed_by=STAFF, note=None)


# ---------------------------------------------------------------------------- history


def test_history_is_newest_first_and_carries_attribution() -> None:
    editor, _, _ = build()
    editor.apply(
        slug=SLUG, document=config_document(min_confidence=0.75), changed_by=STAFF, note="why"
    )

    history = editor.history(slug=SLUG)

    assert [version.version for version in history] == [2, 1]
    assert history[0].changed_by == STAFF
    assert history[-1].changed_by == "migration"
    assert all(isinstance(version.changed_at, datetime) for version in history)
    assert all(version.changed_at.tzinfo is UTC for version in history)
