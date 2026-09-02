"""The Phase 1 JSON-file tenant config source, and the port it hides behind."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leadquali.adapters.tenant_config_json import (
    DEFAULT_TENANT_ID,
    JsonFileTenantConfigLoader,
    default_tenants_dir,
)
from leadquali.app.ports import TenantConfigPort
from leadquali.domain.models import Action, Tier
from leadquali.domain.tenant_config import (
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    TenantConfig,
    TenantConfigError,
    TenantNotFoundError,
)

VALID: dict[str, Any] = {
    "tenant_id": "acme",
    "name": "Acme Corp",
    "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
    "routing_rules": {
        "hot": {"action": "email_sales", "destination": "hot@acme.test"},
        "warm": {"action": "email_sales", "destination": "sales@acme.test"},
        "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
        "disqualified": {"action": "suppress"},
    },
}


@pytest.fixture
def tenants_dir(tmp_path: Path) -> Path:
    (tmp_path / "acme.json").write_text(json.dumps(VALID), encoding="utf-8")
    return tmp_path


def test_loader_satisfies_the_port(tenants_dir: Path) -> None:
    loader: TenantConfigPort = JsonFileTenantConfigLoader(tenants_dir)
    assert loader.get("acme").name == "Acme Corp"


def test_loader_returns_a_validated_config(tenants_dir: Path) -> None:
    cfg = JsonFileTenantConfigLoader(tenants_dir).get("acme")
    assert cfg.action_for(Tier.DISQUALIFIED) is Action.SUPPRESS
    assert cfg.thresholds == DEFAULT_THRESHOLDS


def test_repeated_loads_produce_an_identical_prompt_prefix(tenants_dir: Path) -> None:
    """The cache-hit property, proven through the real file path."""
    loader = JsonFileTenantConfigLoader(tenants_dir)
    assert loader.get("acme").icp_block().encode() == loader.get("acme").icp_block().encode()


def test_an_unknown_tenant_raises_not_found(tenants_dir: Path) -> None:
    with pytest.raises(TenantNotFoundError, match="nosuch"):
        JsonFileTenantConfigLoader(tenants_dir).get("nosuch")


def test_a_broken_config_names_the_tenant_and_the_file(tenants_dir: Path) -> None:
    broken = {**VALID, "thresholds": {"hot": 40.0, "warm": 55.0, "cold": 30.0}}
    (tenants_dir / "acme.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(TenantConfigError) as excinfo:
        JsonFileTenantConfigLoader(tenants_dir).get("acme")
    message = str(excinfo.value)
    assert "acme" in message
    assert "acme.json" in message


def test_malformed_json_is_reported_as_a_config_error(tenants_dir: Path) -> None:
    (tenants_dir / "acme.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TenantConfigError, match=r"acme\.json"):
        JsonFileTenantConfigLoader(tenants_dir).get("acme")


def test_the_file_must_agree_with_the_tenant_it_was_asked_for(tenants_dir: Path) -> None:
    (tenants_dir / "acme.json").write_text(
        json.dumps({**VALID, "tenant_id": "other"}), encoding="utf-8"
    )
    with pytest.raises(TenantConfigError, match="other"):
        JsonFileTenantConfigLoader(tenants_dir).get("acme")


@pytest.mark.parametrize("tenant_id", ["../secrets", "a/b", "", "."])
def test_a_tenant_id_that_is_not_a_slug_never_touches_the_filesystem(
    tenants_dir: Path, tenant_id: str
) -> None:
    with pytest.raises(TenantConfigError):
        JsonFileTenantConfigLoader(tenants_dir).get(tenant_id)


def test_available_tenants_lists_the_directory(tenants_dir: Path) -> None:
    (tenants_dir / "beta.json").write_text(
        json.dumps({**VALID, "tenant_id": "beta"}), encoding="utf-8"
    )
    (tenants_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    assert JsonFileTenantConfigLoader(tenants_dir).available_tenants() == ("acme", "beta")


def test_a_missing_directory_lists_nothing_and_raises_on_get(tmp_path: Path) -> None:
    loader = JsonFileTenantConfigLoader(tmp_path / "absent")
    assert loader.available_tenants() == ()
    with pytest.raises(TenantNotFoundError):
        loader.get("acme")


# ------------------------------------------------------------------ the shipped default


def test_the_shipped_default_tenant_is_valid_and_inherits_the_documented_defaults() -> None:
    cfg = JsonFileTenantConfigLoader(default_tenants_dir()).get(DEFAULT_TENANT_ID)
    assert cfg.tenant_id == DEFAULT_TENANT_ID
    assert cfg.thresholds == DEFAULT_THRESHOLDS
    assert cfg.weights == dict(DEFAULT_WEIGHTS)
    assert cfg.min_confidence == DEFAULT_MIN_CONFIDENCE
    assert set(cfg.routing_rules) == set(Tier)


def test_the_shipped_default_round_trips_through_json() -> None:
    cfg = JsonFileTenantConfigLoader(default_tenants_dir()).get(DEFAULT_TENANT_ID)
    assert TenantConfig.model_validate(json.loads(json.dumps(cfg.model_dump(mode="json")))) == cfg
