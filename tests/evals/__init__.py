"""The eval corpus: labeled leads, and the loader that reads them.

The data (``golden_leads.jsonl``) and the loader (:mod:`tests.evals.golden_set`) live here
so that ``run_eval.py`` (#23) imports the corpus rather than re-parsing it, and so anything
outside this package — a notebook, a script, another language — can read the JSONL directly.
The public names are re-exported here for the short import ``from tests.evals import
load_golden_set``.

**Every seed case is synthetic.** Eval numbers computed against a synthetic set measure
self-consistency, not correctness. See :mod:`tests.evals.golden_set` and
``docs/labeling-golden-set.md``.
"""

from __future__ import annotations

from tests.evals.golden_set import (
    GOLDEN_LEADS_PATH,
    GoldenCase,
    GoldenSet,
    GoldenSetError,
    GoldenSetHeader,
    HumanLabel,
    Provenance,
    describe_golden_set,
    golden_case_ids,
    load_golden_set,
    parse_golden_set,
)

__all__ = [
    "GOLDEN_LEADS_PATH",
    "GoldenCase",
    "GoldenSet",
    "GoldenSetError",
    "GoldenSetHeader",
    "HumanLabel",
    "Provenance",
    "describe_golden_set",
    "golden_case_ids",
    "load_golden_set",
    "parse_golden_set",
]
