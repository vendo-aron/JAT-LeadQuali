"""Suite-wide collection rules.

``live_api`` tests make billable Anthropic calls against a real key. They are real tests —
they are meant to be runnable — but they must never be part of the default suite, and CI
has no key. Rather than depend on every caller remembering ``-m "not live_api"``, they are
skipped unless the run explicitly selects markers itself.
"""

from __future__ import annotations

import pytest

LIVE_API = "live_api"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live_api`` tests unless the run asked for a marker expression of its own."""
    if config.getoption("-m"):
        return
    skip_live = pytest.mark.skip(
        reason="live_api: billable Anthropic API call; select it with -m live_api"
    )
    for item in items:
        if LIVE_API in item.keywords:
            item.add_marker(skip_live)
