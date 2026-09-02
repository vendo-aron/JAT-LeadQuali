"""Phase 1 tenant configuration source: one JSON file per tenant.

The only external system here is the filesystem. It exists so Phase 1 can onboard a tenant
by writing ``tenants/<id>.json`` — which is the whole point of invariant 1 — while P5.1
swaps in the Postgres-backed loader behind the same
:class:`~leadquali.app.ports.TenantConfigPort` without touching a caller.

Reads are deliberately uncached: a config file is a few hundred bytes, and picking up an
operator's edit without a restart is worth far more than the microseconds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from leadquali.domain.tenant_config import (
    TENANT_ID_PATTERN,
    TenantConfig,
    TenantConfigError,
    TenantNotFoundError,
)

#: The tenant the single-tenant Phase 1 deployment runs as.
DEFAULT_TENANT_ID: Final[str] = "default"

_TENANT_ID_RE: Final[re.Pattern[str]] = re.compile(TENANT_ID_PATTERN)


def default_tenants_dir() -> Path:
    """The repository's ``tenants/`` directory, holding the shipped default config.

    Resolved relative to this file, so it works from a source checkout — which is where
    Phase 1 runs. It is a convenience for local runs and tests, not a deployment
    mechanism: an installed or Lambda-packaged build passes the directory explicitly, and
    from P5.1 the configs come from Postgres instead.
    """
    return Path(__file__).resolve().parents[3] / "tenants"


class JsonFileTenantConfigLoader:
    """Loads tenant configuration from ``<directory>/<tenant_id>.json``.

    Implements :class:`~leadquali.app.ports.TenantConfigPort`.
    """

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """Where this loader looks for tenant config files."""
        return self._directory

    def get(self, tenant_id: str) -> TenantConfig:
        """Read, parse and validate one tenant's configuration.

        The tenant id is checked against the slug pattern *before* it is joined onto a
        path: ids reach this method from request payloads and queue messages, and
        ``../../etc/passwd`` must be a config error, not a file read.
        """
        path = self._path_for(tenant_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise TenantNotFoundError(f"tenant '{tenant_id}': no configuration at {path}") from exc
        except OSError as exc:
            raise TenantConfigError(f"tenant '{tenant_id}': cannot read {path} — {exc}") from exc

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TenantConfigError(
                f"tenant '{tenant_id}': {path.name} is not valid JSON — {exc}"
            ) from exc

        try:
            config = TenantConfig.from_dict(document)
        except TenantConfigError as exc:
            raise TenantConfigError(f"{exc} (in {path.name})") from exc

        if config.tenant_id != tenant_id:
            raise TenantConfigError(
                f"tenant '{tenant_id}': {path.name} declares tenant_id "
                f"'{config.tenant_id}'; the file name and the config must agree"
            )
        return config

    def available_tenants(self) -> tuple[str, ...]:
        """Tenant ids with a config file present, sorted. Empty if the directory is absent."""
        return tuple(sorted(self._tenant_ids()))

    def _tenant_ids(self) -> Iterator[str]:
        if not self._directory.is_dir():
            return
        for path in self._directory.glob("*.json"):
            if _TENANT_ID_RE.match(path.stem):
                yield path.stem

    def _path_for(self, tenant_id: str) -> Path:
        if not _TENANT_ID_RE.match(tenant_id):
            raise TenantConfigError(
                f"tenant id {tenant_id!r} is not a valid slug "
                f"(expected {TENANT_ID_PATTERN}); refusing to build a path from it"
            )
        return self._directory / f"{tenant_id}.json"
