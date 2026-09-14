"""Offline tests for the default-tenant seed script.

The database half of seeding is covered in ``tests/integration/test_seed.py``. Everything
here is about the part that runs before a connection is opened: reading the tenant config
file, deciding it is usable, and failing legibly when it is not.

Validation is ``TenantConfig``'s and nothing else's (#31 tightened this; the script used to
do a shallow key-presence check because #8 was not on the branch yet). So the tests below
are about what the *seed script* adds on top: a legible error for a missing file, for a
file that is not JSON, and for a document the rubric model rejects — and the fact that an
accepted document is stored byte-for-byte as it was read.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from leadquali.adapters.seed import (
    DEFAULT_TENANT_SLUG,
    SeedError,
    load_tenant_document,
    main,
    tenant_id_for,
)

# Shaped exactly like tenants/default.json, and complete: every tier is routed, because
# TenantConfig refuses a config that leaves one without a rule.
A_TENANT_DOCUMENT: dict[str, Any] = {
    "tenant_id": "default",
    "name": "JAT-LeadQuali (internal)",
    "prompt_version": "rubric_v1",
    "icp_description": "B2B software companies with inbound web-form volume.",
    "weights": {
        "authority": 1.0,
        "budget_signal": 1.0,
        "icp_fit": 1.0,
        "intent": 1.0,
        "urgency": 1.0,
    },
    "thresholds": {"hot": 80.0, "warm": 55.0, "cold": 30.0},
    "min_confidence": 0.6,
    "routing_rules": {
        "hot": {"action": "email_sales", "destination": "sales@example.invalid"},
        "warm": {"action": "email_sales", "destination": "sales@example.invalid"},
        "cold": {"action": "email_sales", "destination": "sales@example.invalid"},
        "disqualified": {"action": "suppress"},
    },
}


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "default.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_default_tenant_id_is_stable_across_environments() -> None:
    """Seeding twice must update one tenant, not create a second.

    A random primary key would make the script append-only, and would give the default
    tenant a different id in every developer's database — so no fixture, support query or
    runbook could name it.
    """
    assert tenant_id_for(DEFAULT_TENANT_SLUG) == tenant_id_for(DEFAULT_TENANT_SLUG)
    assert tenant_id_for(DEFAULT_TENANT_SLUG) == uuid.UUID("2342f768-d5e0-59ad-8460-132c716d43b3")


def test_different_slugs_get_different_tenant_ids() -> None:
    assert tenant_id_for("default") != tenant_id_for("acme")


def test_a_well_formed_document_loads_unchanged(tmp_path: Path) -> None:
    """The document is stored verbatim, so #16 can hand the column to TenantConfig whole."""
    assert load_tenant_document(_write(tmp_path, A_TENANT_DOCUMENT)) == A_TENANT_DOCUMENT


def test_a_missing_file_names_the_path_and_the_way_out(tmp_path: Path) -> None:
    """The likeliest failure on this branch: #8's file is simply not in the checkout."""
    missing = tmp_path / "nope.json"

    with pytest.raises(SeedError) as caught:
        load_tenant_document(missing)

    message = str(caught.value)
    assert str(missing) in message
    assert "--config" in message


def test_a_file_that_is_not_json_is_reported_as_such(tmp_path: Path) -> None:
    path = tmp_path / "default.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SeedError, match="not valid JSON"):
        load_tenant_document(path)


def test_a_json_document_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="must contain a JSON object"):
        load_tenant_document(_write(tmp_path, ["not", "a", "config"]))


@pytest.mark.parametrize("missing_key", ["tenant_id", "name", "icp_description", "routing_rules"])
def test_a_document_missing_a_required_rubric_key_is_rejected(
    tmp_path: Path, missing_key: str
) -> None:
    """Invariant 1: a tenant without a rubric is a tenant every config load rejects, so the
    seed script must not be the thing that puts one in the database.

    These four have no defaults and cannot be guessed — a destination in particular, where
    a placeholder would mean a whole tier's leads going nowhere with no error at all."""
    document = {k: v for k, v in A_TENANT_DOCUMENT.items() if k != missing_key}

    with pytest.raises(SeedError) as caught:
        load_tenant_document(_write(tmp_path, document))

    assert missing_key in str(caught.value)


@pytest.mark.parametrize(
    "optional_key", ["weights", "thresholds", "min_confidence", "prompt_version"]
)
def test_the_rubric_numerics_fall_back_to_the_documented_defaults(
    tmp_path: Path, optional_key: str
) -> None:
    """The other half of the same rule: onboarding is three fields plus a routing table,
    and an unopinionated tenant inherits the house policy rather than being refused."""
    document = {k: v for k, v in A_TENANT_DOCUMENT.items() if k != optional_key}
    assert load_tenant_document(_write(tmp_path, document)) == document


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_name_is_rejected(tmp_path: Path, blank: str) -> None:
    with pytest.raises(SeedError, match="name"):
        load_tenant_document(_write(tmp_path, {**A_TENANT_DOCUMENT, "name": blank}))


def test_a_rubric_key_of_the_wrong_kind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SeedError, match="weights"):
        load_tenant_document(_write(tmp_path, {**A_TENANT_DOCUMENT, "weights": [1, 2, 3]}))


def test_a_config_that_would_break_scoring_is_rejected(tmp_path: Path) -> None:
    """The tightening #31 made: the seed script validates with ``TenantConfig`` and nothing
    else, so a config the worker would refuse at 3am is refused here instead.

    Overlapping tier bounds are the example because the old shallow check — "thresholds is
    an object" — waved this through, and the result is every lead in the gap mis-routed."""
    broken = {**A_TENANT_DOCUMENT, "thresholds": {"hot": 40.0, "warm": 55.0, "cold": 30.0}}
    with pytest.raises(SeedError, match="thresholds overlap"):
        load_tenant_document(_write(tmp_path, broken))


def test_a_weight_for_a_dimension_the_model_does_not_score_is_rejected(tmp_path: Path) -> None:
    """The other half of the same tightening: a typo in a weight name used to be stored."""
    weights = {**A_TENANT_DOCUMENT["weights"], "vibes": 2.0}
    with pytest.raises(SeedError, match="unknown dimension vibes"):
        load_tenant_document(_write(tmp_path, {**A_TENANT_DOCUMENT, "weights": weights}))


def test_every_shipped_tenant_file_is_seedable() -> None:
    """The files in ``tenants/`` are what an operator seeds from; a broken one is an
    onboarding that fails on the customer's first lead rather than on the command."""
    tenants_dir = Path(__file__).resolve().parents[2] / "tenants"
    shipped = sorted(tenants_dir.glob("*.json"))
    assert shipped, "expected at least the default tenant's config to ship"
    for path in shipped:
        assert load_tenant_document(path)["tenant_id"] == path.stem


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.5, "high", True])
def test_a_min_confidence_that_is_not_a_probability_is_rejected(
    tmp_path: Path, bad_confidence: object
) -> None:
    """``True`` is in here on purpose: bool is an int in Python, so a naive numeric check
    would accept it and store a gate of 1.0 that nothing could ever pass."""
    with pytest.raises(SeedError, match="min_confidence"):
        load_tenant_document(
            _write(tmp_path, {**A_TENANT_DOCUMENT, "min_confidence": bad_confidence})
        )


def test_the_cli_fails_with_a_readable_message_and_never_opens_a_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing config must not surface as a stack trace, and must be caught before the
    script tries to connect — so this passes with no database anywhere in sight."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    missing = tmp_path / "nope.json"

    exit_code = main(["--config", str(missing)])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert stderr.startswith("seed: ")
    assert str(missing) in stderr


def test_the_cli_reports_a_missing_database_url_rather_than_guessing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is exactly one way to point a process at a database, and an unset variable is
    an error rather than a default that migrates or seeds the wrong one."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # so a stray .env in the repo cannot supply one
    path = _write(tmp_path, A_TENANT_DOCUMENT)

    exit_code = main(["--config", str(path)])

    assert exit_code == 1
    assert "DATABASE_URL is not set" in capsys.readouterr().err
