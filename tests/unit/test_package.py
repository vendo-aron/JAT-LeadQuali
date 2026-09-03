"""The package imports and carries a version."""

from __future__ import annotations

import leadquali


def test_package_exposes_version() -> None:
    assert leadquali.__version__
