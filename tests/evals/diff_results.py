"""``python -m tests.evals.diff_results BEFORE.json AFTER.json`` — diff two saved runs.

"Did last Tuesday's prompt change make things worse?" is a question about two files that
have already been paid for. This command answers it without calling anything: no API key,
no network, no spend, no ``--confirm-spend``, because there is nothing to confirm.

What it prints is deliberately not just a pair of numbers. Against fifteen synthetic cases
a one-case difference is 6.7 points of nothing, so every delta is reported beside the
minimum difference the set could have detected, and anything at or below that floor is
labeled :data:`~tests.evals.compare.VERDICT_WITHIN_NOISE`. See
:mod:`tests.evals.compare` for why there is no significance test here.

Exit codes are meant for a pull-request check:

* :data:`EXIT_OK` — compared, and nothing moved further than the set can resolve.
* :data:`EXIT_MEANINGFUL_REGRESSION` — a metric got meaningfully worse. Not "a number went
  down": a number went down by more than this set moves on its own.
* :data:`EXIT_INPUT_ERROR` — the files could not be read or cannot honestly be compared.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from tests.evals.compare import (
    Comparison,
    ComparisonError,
    load_result,
    render_comparison,
)
from tests.evals.run_eval import EXIT_INPUT_ERROR, EXIT_OK

#: A headline metric fell by more than the noise floor. Non-zero so a CI check can gate on
#: it — and only ever raised above the floor, so the gate cannot fire on a coin flip.
EXIT_MEANINGFUL_REGRESSION: Final[int] = 1


def build_parser() -> argparse.ArgumentParser:
    """The command line. Two result files in, one comparison out."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.evals.diff_results",
        description=(
            "Compare two eval result files written by run_eval. Reads saved artifacts "
            "only: no API key, no network, no spend."
        ),
        epilog=(
            "Differences at or below the golden set's noise floor are reported as noise, "
            "not as findings. Read docs/rubric-tuning.md before acting on either."
        ),
    )
    parser.add_argument("baseline", type=Path, help="The earlier result file.")
    parser.add_argument("candidate", type=Path, help="The result file to judge against it.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the comparison as JSON on stdout instead of the text report.",
    )
    parser.add_argument(
        "--label-baseline", default=None, help="Name for the baseline run in the report."
    )
    parser.add_argument(
        "--label-candidate", default=None, help="Name for the candidate run in the report."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Compare two result files. Returns the process exit code."""
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        baseline = load_result(args.baseline, label=args.label_baseline)
        candidate = load_result(args.candidate, label=args.label_candidate)
    except (OSError, ComparisonError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    comparison = Comparison.of(baseline, candidate)
    if args.as_json:
        print(json.dumps(comparison.as_json(), indent=2))
    else:
        print(render_comparison(comparison), end="")
    return EXIT_MEANINGFUL_REGRESSION if comparison.regressions else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via the module entry point
    raise SystemExit(main())


__all__ = [
    "EXIT_INPUT_ERROR",
    "EXIT_MEANINGFUL_REGRESSION",
    "EXIT_OK",
    "build_parser",
    "main",
]
