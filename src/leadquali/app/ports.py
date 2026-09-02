"""Protocol interfaces for everything outside the domain.

Each port is the narrowest contract the application layer needs, stated where the
application layer lives. Adapters implement them structurally — no base class, no
registration — so ``domain`` and ``app`` never import an adapter, and swapping a Phase 1
file loader for the Phase 5 Postgres one is a wiring change at the entrypoint.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from leadquali.app.assessment_result import AssessmentOutcome
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


@runtime_checkable
class LeadAssessorPort(Protocol):
    """Turns one rendered lead into a judgment, or into the reason there isn't one.

    The whole point of this port is that ``app/qualify.py`` (#14) never sees the Anthropic
    SDK: it holds a ``LeadAssessorPort``, gets an
    :data:`~leadquali.app.assessment_result.AssessmentOutcome`, and routes on it. Swapping
    the model, the provider, or a recorded-response double for the eval harness is a wiring
    change at the entrypoint.

    Implementations **do not raise** for the failure modes of talking to a model. A refusal,
    a timeout, a rate limit, a 5xx and a schema violation all come back as
    :class:`~leadquali.app.assessment_result.AssessmentFailed` carrying the matching
    :class:`~leadquali.domain.models.EscalationReason`, because invariant 3 says every one
    of them has to reach a human rather than becoming a stack trace or, far worse, a low
    score. Only a programming error should ever escape.

    ``effort`` is deliberately absent from this signature: it is a property of the
    configured assessor, not of a lead, so #24 sweeps it by constructing assessors rather
    than by threading a parameter through the pipeline.
    """

    def assess(self, *, config: TenantConfig, rendered_lead: str) -> AssessmentOutcome:
        """Assess one lead against one tenant's profile.

        Args:
            config: the tenant whose ICP and prompt version this call is made under.
            rendered_lead: the lead as the user turn — already rendered and wrapped in
                untrusted-data delimiters by #12. Implementations send it verbatim as a
                user message and never as a system block: it is attacker-controlled text
                from a public form, and it must stay outside the cached prefix.

        Returns:
            :class:`~leadquali.app.assessment_result.AssessmentSucceeded` with a validated
            assessment and its metering, or
            :class:`~leadquali.app.assessment_result.AssessmentFailed` with the reason.
        """
        ...


__all__ = ["LeadAssessorPort", "TenantConfigPort"]
