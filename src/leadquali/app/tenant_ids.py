"""The mapping from a tenant's slug to its database primary key.

A tenant has two identities and they are not interchangeable. The **slug** is external:
it is what a customer's form sends in ``X-LeadQuali-Tenant``, what names a config file,
and what an operator types. The **row id** is internal: a UUID, because every other table
references it and a stable fixed-width key is what the composite foreign keys of
``db_schema`` are built on.

:func:`tenant_id_for` is the only bridge between them, and it is a *derivation* rather
than a lookup — ``uuid5`` of a fixed namespace and the slug. That is what makes seeding
idempotent (re-running the seed updates the tenant it created last time instead of adding
a second one) and what gives the default tenant the same id in every developer's database,
so a fixture or a support query is portable between environments.

It lives in ``app`` rather than in an adapter because two layers need it and neither may
import the other: :class:`~leadquali.app.tenants.TenantService` computes the id it is
about to insert (the database cannot enforce ``id = uuid5(ns, slug)`` itself), and the
Postgres adapters resolve a port-level slug to the row they must filter on. A second
spelling of "which row is this tenant" is how a lead ends up filed under a tenant that
does not exist, so there is exactly one, here.

Standard library only.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from leadquali.domain.tenant_config import TENANT_ID_PATTERN

__all__ = ["TENANT_ID_NAMESPACE", "is_tenant_slug", "tenant_id_for"]

TENANT_ID_NAMESPACE: Final = uuid.UUID("c0ee1346-dad8-59ff-9326-afab90a0f177")
"""UUID5 namespace for tenant slugs.

Fixed forever. Changing it would re-point every tenant at a row that does not exist, so
it is a literal here and never derived from configuration.
"""

_SLUG_RE: Final[re.Pattern[str]] = re.compile(TENANT_ID_PATTERN)


def tenant_id_for(slug: str) -> uuid.UUID:
    """Return the stable ``tenants.id`` primary key for ``slug``.

    Args:
        slug: The tenant's external identity, matching
            :data:`~leadquali.domain.tenant_config.TENANT_ID_PATTERN`.

    Returns:
        ``uuid5(TENANT_ID_NAMESPACE, slug)``.
    """
    return uuid.uuid5(TENANT_ID_NAMESPACE, slug)


def is_tenant_slug(value: str) -> bool:
    """Whether ``value`` is a well-formed tenant slug.

    Used wherever a slug arrives from outside — a queue message, a request header, a CLI
    argument — so that a malformed one is refused before it reaches a query or a filename.
    """
    return bool(_SLUG_RE.match(value))
