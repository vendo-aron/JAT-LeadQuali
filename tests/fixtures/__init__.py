"""Shared test fixtures. The prompt-injection corpus lives here, in JSON, on purpose.

``injection_corpus.json`` is owned by #12 but written to be reused: the golden set in #22
needs exactly these payloads, and duplicating them would mean the two drift the first time
someone adds an attack. The data is plain JSON rather than Python so that #22 can read it
from an eval harness, a notebook or another language without importing #12's test package;
:func:`load_injection_corpus` is the convenience wrapper for the Python callers.

The corpus deliberately carries no expectation about *scores*. #12 asserts structure — the
payload stays inside its delimiters — because asserting on model behaviour needs an API key
and a golden set. Each case does carry an advisory ``expected_max_tier`` for #22 to use
when it has one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

#: The corpus file itself, for callers that would rather parse it their own way.
INJECTION_CORPUS_PATH: Final[Path] = Path(__file__).resolve().parent / "injection_corpus.json"


@dataclass(frozen=True, slots=True)
class InjectionCase:
    """One attacker-controlled web-form submission, plus what it is trying to do.

    ``fields`` maps onto ``LeadSubmission``'s named fields and ``extra`` onto its extras,
    so a case becomes a submission with ``LeadSubmission(**case.fields, extra=case.extra)``.
    ``canary`` is a plain-ASCII substring that must survive rendering: the renderer may
    escape, cap and mark the payload, but it may never silently swallow it.
    """

    id: str
    category: str
    description: str
    canary: str
    expected_max_tier: str
    fields: Mapping[str, str]
    extra: Mapping[str, str]

    @property
    def field_lengths(self) -> Mapping[str, int]:
        """Character count per field, for tests that reason about the caps."""
        return {name: len(value) for name, value in self.fields.items()}


def _expand(raw: Mapping[str, Any]) -> dict[str, str]:
    """Apply a case's optional ``repeat`` directive to its fields.

    A megabyte-scale payload is a legitimate test case and an illegitimate thing to keep in
    git, so the corpus stores the repeating unit and a count and the payload is built here.
    """
    fields = {str(name): str(value) for name, value in dict(raw.get("fields", {})).items()}
    repeat = raw.get("repeat")
    if repeat is not None:
        target = str(repeat["field"])
        fields[target] = fields.get(target, "") + str(repeat["unit"]) * int(repeat["times"])
    return fields


@cache
def load_injection_corpus() -> tuple[InjectionCase, ...]:
    """Every injection case, in file order. Cached: the largest case is ~1.5 MB."""
    document: Any = json.loads(INJECTION_CORPUS_PATH.read_text(encoding="utf-8"))
    cases: list[InjectionCase] = []
    for raw in document["cases"]:
        cases.append(
            InjectionCase(
                id=str(raw["id"]),
                category=str(raw["category"]),
                description=str(raw["description"]),
                canary=str(raw["canary"]),
                expected_max_tier=str(raw["expected_max_tier"]),
                fields=_expand(raw),
                extra={str(k): str(v) for k, v in dict(raw.get("extra", {})).items()},
            )
        )
    return tuple(cases)


__all__ = ["INJECTION_CORPUS_PATH", "InjectionCase", "load_injection_corpus"]
