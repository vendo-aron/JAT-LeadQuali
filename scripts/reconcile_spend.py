#!/usr/bin/env python
"""Reconcile our computed Anthropic spend against the console's export.

    python scripts/reconcile_spend.py anthropic-september.csv --month 2026-09

Exits ``0`` when the totals agree within tolerance and ``1`` when they do not, so it can be
run from a cron on the first of the month and actually be noticed. The whole procedure —
where to export the CSV from, how often to run this, and what to do when it fails — is in
``docs/metering-and-billing.md``.

The logic lives in :mod:`leadquali.usagectl`; this file is only the entry point, for the
same reason ``scripts/seed.py`` is, so the part worth testing is importable and covered by
``mypy --strict`` like the rest of the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The package is not installed in a plain checkout — `alembic.ini` solves the same problem
# with `prepend_sys_path = src`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadquali.usagectl import main

if __name__ == "__main__":
    raise SystemExit(main(["reconcile", *sys.argv[1:]]))
