"""The shared store contract, run against the in-memory double.

``tests.fakes.InMemoryLeadStore`` is what #17, #19, #21 and #26 test their pipelines
against, so it is only useful for as long as it behaves like the database. Running the same
contract here and in ``tests/integration/test_store_postgres.py`` is what keeps the two
honest: a divergence fails one of the two suites instead of surfacing in production, where
the double is not the thing running.

No database, no network — this file is part of the default suite.
"""

from __future__ import annotations

import pytest

from leadquali.app.ports import LeadStorePort
from tests.contract.lead_store_contract import LeadStoreContract
from tests.fakes import InMemoryLeadStore


class TestInMemoryLeadStore(LeadStoreContract):
    """Every behaviour the port promises, checked against the fake."""

    @pytest.fixture
    def store(self) -> LeadStorePort:
        return InMemoryLeadStore()

    @pytest.fixture
    def tenants(self) -> tuple[str, str]:
        return ("tenant-a", "tenant-b")
