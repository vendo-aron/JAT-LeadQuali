"""Invariant 2 guard rail: the model assesses, code routes.

``LeadAssessment`` is handed to Anthropic as a structured-output schema. If a routing
concept ever leaks into it — a tier, an action, a total score, a destination — then the
model, not Python, is deciding policy, and every downstream guarantee (deterministic
routing, offline-testable decisions, prompt-injection containment) is void.

This file is deliberately narrow and deliberately paranoid: it walks the *generated JSON
schema*, not the class, so a leak introduced through an inherited model, a nested model or
a field description is caught too.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from leadquali.domain.models import DimensionScores, ExtractedFacts, LeadAssessment

#: Concepts that belong to the deterministic layer and must never reach the model.
FORBIDDEN_NAMES = frozenset(
    {
        "tier",
        "action",
        "total_score",
        "destination",
        "route",
        "routing",
        "escalation_reason",
        "assigned_to",
    }
)

#: Substrings that must not occur anywhere in the schema text, in any casing — not as a
#: property name, not in a description, not in an enum value.
FORBIDDEN_SUBSTRINGS = ("tier", "total_score", "destination", "routing")


def _walk(node: Any, path: str = "$") -> list[tuple[str, str]]:
    """Yield ``(path, key)`` for every mapping key in a JSON-schema-shaped structure."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((path, str(key)))
            found.extend(_walk(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}[{index}]"))
    return found


def test_schema_has_no_routing_key_at_any_depth() -> None:
    schema = LeadAssessment.model_json_schema()
    offenders = [f"{path}.{key}" for path, key in _walk(schema) if key.lower() in FORBIDDEN_NAMES]
    assert offenders == [], f"routing concepts leaked into the model's schema: {offenders}"


def test_no_property_name_at_any_depth_is_a_routing_concept() -> None:
    schema = LeadAssessment.model_json_schema()
    offenders = [
        f"{path}.{key}"
        for path, key in _walk(schema)
        if path.endswith(".properties") and key.lower() in FORBIDDEN_NAMES
    ]
    assert offenders == []


@pytest.mark.parametrize("needle", FORBIDDEN_SUBSTRINGS)
def test_schema_text_never_mentions_a_routing_concept(needle: str) -> None:
    """Not even in a description — a hint is enough to make the model volunteer a tier."""
    schema_text = json.dumps(LeadAssessment.model_json_schema()).lower()
    assert needle not in schema_text


def test_schema_text_never_mentions_an_action_as_a_word() -> None:
    schema_text = json.dumps(LeadAssessment.model_json_schema()).lower()
    assert re.search(r"\bactions?\b", schema_text) is None


def test_assessment_rejects_a_smuggled_routing_field() -> None:
    """``extra="forbid"`` means a model that volunteers a tier fails validation loudly."""
    payload = json.loads(_valid_assessment().model_dump_json())
    payload["tier"] = "hot"
    with pytest.raises(ValueError, match="tier"):
        LeadAssessment.model_validate(payload)


def test_schema_is_closed_and_fully_required() -> None:
    """A structured-output schema Anthropic can be held to: no extras, no absent keys."""
    schema = LeadAssessment.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    for definition in schema.get("$defs", {}).values():
        assert definition["additionalProperties"] is False
        assert set(definition["required"]) == set(definition["properties"])


def _valid_assessment() -> LeadAssessment:
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=20, intent=15, authority=10, urgency=8, budget_signal=7
        ),
        extracted=ExtractedFacts(
            company_name="Acme",
            industry="logistics",
            company_size_estimate="50-200",
            role_seniority="director",
            stated_use_case="route planning",
            stated_timeline="this quarter",
        ),
        reasoning="Director at a mid-size logistics firm naming a concrete use case.",
        confidence=0.8,
        missing_information=["budget"],
        suggested_first_question="What does your current routing stack cost you?",
        spam_or_test_submission=False,
    )
