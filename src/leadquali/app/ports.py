"""Protocol interfaces for everything outside the domain.

Each port is the narrowest contract the application layer needs, stated where the
application layer lives. Adapters implement them structurally — no base class, no
registration — so ``domain`` and ``app`` never import an adapter, and swapping a Phase 1
file loader for the Phase 5 Postgres one is a wiring change at the entrypoint.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from leadquali.domain.tenant_config import TenantConfig


@runtime_checkable
class TenantConfigPort(Protocol):
    """Source of validated tenant configuration.

    Phase 1 reads ``tenants/*.json``; P5.1 reads the ``tenants.icp_config`` jsonb column.
    Callers see no difference: either way they get a fully validated
    :class:`~leadquali.domain.tenant_config.TenantConfig` or an exception. There is no
    partially-valid config and no silent fallback to defaults — a tenant whose policy
    cannot be loaded must not have its leads routed by someone else's policy.
    """

    def get(self, tenant_id: str) -> TenantConfig:
        """Return the configuration for ``tenant_id``.

        Raises:
            TenantNotFoundError: no configuration exists for this tenant.
            TenantConfigError: a configuration exists but is invalid or unreadable.
        """
        ...
