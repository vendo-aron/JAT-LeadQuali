"""Revenue, until there is any: the adapter that honestly says it does not know.

Billing is #35 and does not exist yet, so nothing in this repository can say what a tenant
was charged. :class:`UnknownRevenue` implements
:class:`~leadquali.app.metering.RevenuePort` by returning ``None`` to every question, and
:class:`~leadquali.app.metering.MarginReport` renders that as ``unknown``.

The alternative — a stub returning a plausible figure, a list price, a placeholder — was
considered and is worse than having no margin report at all. A number in a margin column
gets quoted, put in a spreadsheet and used to price a plan, and by then nobody remembers
that its source was a constant somebody typed to get a test passing. An ``unknown`` cannot
be misread.

When #35 lands, its Stripe adapter implements the same Protocol and
:mod:`leadquali.usagectl` wires it in place of this one. Nothing else changes: the margin
arithmetic already handles a real number, and its tests already cover both branches.
"""

from __future__ import annotations

from decimal import Decimal

from leadquali.app.metering import BillingPeriod

__all__ = ["UnknownRevenue"]


class UnknownRevenue:
    """A :class:`~leadquali.app.metering.RevenuePort` that knows nothing, and says so."""

    def revenue_usd(self, *, tenant_id: str, period: BillingPeriod) -> Decimal | None:
        """Always ``None`` — revenue is not recorded anywhere in this system yet.

        The arguments are accepted and ignored, rather than the method being a no-argument
        one, because this is the shape #35's adapter has to fill: a change of wiring, not a
        change of every call site.
        """
        del tenant_id, period
        return None

    def __repr__(self) -> str:
        """Say what this is, so it is recognisable in a traceback or a repr of a service."""
        return "UnknownRevenue(revenue is not recorded until #35)"
