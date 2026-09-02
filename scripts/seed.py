#!/usr/bin/env python
"""Seed the default internal tenant. Run after ``alembic upgrade head``.

    python scripts/seed.py [--config tenants/default.json] [--database-url URL]

The logic lives in :mod:`leadquali.adapters.seed`; this file is only the entry point, so
that the part worth testing is importable and covered by ``mypy --strict`` like the rest of
the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The package is not installed in a plain checkout — `alembic.ini` solves the same problem
# with `prepend_sys_path = src`, and `python scripts/seed.py` has to work the way
# docs/local-database.md says it does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadquali.adapters.seed import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
