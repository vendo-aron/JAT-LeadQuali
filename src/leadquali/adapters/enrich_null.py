"""The enricher for a deployment that does not enrich.

``EnricherPort`` (#14) exists before its real implementation (#18) does, and a Protocol
with no implementation would make the pipeline unrunnable and its tests dependent on a
double. This is that implementation: it looks nothing up, returns
:meth:`~leadquali.app.enrichment.Enrichment.none`, and therefore adds nothing to the
prompt — a deployment wired to it sends byte-for-byte the user turn it would have sent if
enrichment had never been designed.

It is not a stub in a shipping path. "No enrichment configured" is a legitimate, complete
configuration: enrichment is an optimisation on scoring quality, and a tenant on a plan
without it still gets every lead assessed and routed. #18 swaps in the email adapter at the
entrypoint, and nothing else changes.
"""

from __future__ import annotations

from leadquali.app.enrichment import Enrichment
from leadquali.prompts.lead import LeadSubmission


class NullEnricher:
    """An :class:`~leadquali.app.ports.EnricherPort` that knows nothing and says so.

    It returns :meth:`~leadquali.app.enrichment.Enrichment.none` rather than
    :meth:`~leadquali.app.enrichment.Enrichment.unavailable`, and the difference is not
    pedantry: "unavailable" tells the model a check was attempted and failed, so it should
    record the gap in ``missing_information``. Nothing was attempted here, so there is no
    gap to report and no reason to spend tokens telling the model about a feature this
    deployment does not have.
    """

    def enrich(self, *, tenant_id: str, submission: LeadSubmission) -> Enrichment:
        """Return an empty enrichment. Never raises, never blocks, never looks anything up."""
        del tenant_id, submission
        return Enrichment.none()


__all__ = ["NullEnricher"]
