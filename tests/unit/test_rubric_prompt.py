"""The rubric prompt: stable enough to cache, clean enough to trust.

Three properties are worth a test here, and they are all cheap to check offline:

* **Byte-stability.** The rubric is the cacheable head of every request we ever make. If
  its bytes move — a timestamp, a path, an environment value, a dict that iterates in hash
  order — the cache hit rate silently goes to zero and the only symptom is the bill. The
  cross-process check is the important one: ``PYTHONHASHSEED`` differs between processes,
  so a set or dict leaking into the text shows up there and nowhere else.
* **Purity.** Nothing tenant-specific and nothing about tier boundaries, the confidence
  gate or routing may appear in the shared block. The tenant half is invariant 1
  (onboarding is a config write); the policy half is invariant 2 — a model that can see the
  boundary it is measured against starts aiming for it.
* **Cacheability.** A prefix shorter than the model's minimum silently does not cache: no
  error, ``cache_creation_input_tokens`` just stays zero. The rubric has to clear that bar.

Nothing here asserts on model prose or calls the API — that is #11.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from leadquali.domain.models import DimensionScores, LeadAssessment
from leadquali.domain.tenant_config import DEFAULT_PROMPT_VERSION, TenantConfig
from leadquali.prompts import (
    BLOCK_SEPARATOR,
    CONSERVATIVE_CHARS_PER_TOKEN,
    MIN_CACHEABLE_PREFIX_TOKENS,
    PROMPT_VERSION,
    RUBRIC_FILENAME,
    PromptBlock,
    PromptVersionMismatchError,
    build_system_blocks,
    estimate_tokens,
    render_system_prompt,
    rubric_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

MINIMAL: dict[str, Any] = {
    "tenant_id": "acme",
    "name": "Acme Corp",
    "icp_description": "B2B SaaS companies with 50-500 employees in North America.",
    "routing_rules": {
        "hot": {"action": "email_sales", "destination": "hot@acme.test"},
        "warm": {"action": "email_sales", "destination": "sales@acme.test"},
        "cold": {"action": "email_sales", "destination": "nurture@acme.test"},
        "disqualified": {"action": "suppress"},
    },
}

OTHER: dict[str, Any] = {
    "tenant_id": "globex",
    "name": "Globex Industrial",
    "icp_description": "Mid-market manufacturers replacing a legacy MES in the EU.",
    "weights": {
        "authority": 2.5,
        "budget_signal": 1.0,
        "icp_fit": 0.5,
        "intent": 1.0,
        "urgency": 1.0,
    },
    "thresholds": {"hot": 90.0, "warm": 60.0, "cold": 20.0},
    "min_confidence": 0.8,
    "routing_rules": MINIMAL["routing_rules"],
}


def packaged_rubric() -> Path:
    """The rubric file as it sits in the source tree, comments and all."""
    return REPO_ROOT / "src" / "leadquali" / "prompts" / RUBRIC_FILENAME


def make_config(**overrides: Any) -> TenantConfig:
    """A valid config with only the named fields changed."""
    return TenantConfig.model_validate({**MINIMAL, **overrides})


@pytest.fixture
def fresh_rubric() -> str:
    """The rubric, loaded past the process-lifetime cache."""
    rubric_text.cache_clear()
    return rubric_text()


# --------------------------------------------------------------------------- stability


def test_two_loads_are_byte_identical(fresh_rubric: str) -> None:
    rubric_text.cache_clear()
    assert rubric_text().encode("utf-8") == fresh_rubric.encode("utf-8")


def test_the_environment_does_not_leak_into_the_rubric(
    fresh_rubric: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt that reads its environment is a prompt with a per-host cache entry."""
    monkeypatch.setenv("LEADQUALI_CANARY", "if-this-appears-the-prefix-is-not-stable")
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    rubric_text.cache_clear()
    assert rubric_text() == fresh_rubric


def test_the_rubric_is_byte_identical_in_a_separate_process(fresh_rubric: str) -> None:
    """``PYTHONHASHSEED`` differs per process, so set/dict iteration order shows up here."""
    src = str(REPO_ROOT / "src")
    program = (
        "import sys, hashlib;"
        f"sys.path.insert(0, {src!r});"
        "from leadquali.prompts import rubric_text;"
        "sys.stdout.write(hashlib.sha256(rubric_text().encode('utf-8')).hexdigest())"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no external input
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
    )
    import hashlib

    assert completed.stdout == hashlib.sha256(fresh_rubric.encode("utf-8")).hexdigest()


def test_nothing_is_interpolated_into_the_rubric(fresh_rubric: str) -> None:
    """No format placeholders survive: the text is data, not a template."""
    assert re.search(r"\{[A-Za-z_]+\}", fresh_rubric) is None
    assert "%s" not in fresh_rubric
    assert str(REPO_ROOT) not in fresh_rubric


def test_maintainer_notes_never_reach_the_model(fresh_rubric: str) -> None:
    """The versioning rule is documented in the file header, which is stripped at load."""
    header = packaged_rubric().read_text(encoding="utf-8")
    assert "VERSIONING RULE" in header, "the file header must document the versioning rule"
    assert "VERSIONING RULE" not in fresh_rubric
    assert "<!--" not in fresh_rubric
    assert "-->" not in fresh_rubric


def test_the_rubric_is_canonicalised(fresh_rubric: str) -> None:
    """CRLF from a Windows checkout would otherwise be a different cache entry."""
    assert "\r" not in fresh_rubric
    assert fresh_rubric == fresh_rubric.strip()
    assert not any(line != line.rstrip() for line in fresh_rubric.split("\n"))


# ------------------------------------------------------------------------------ purity


#: Policy vocabulary that belongs to the deterministic layer (invariant 2). A model that
#: can see the tier boundary it is measured against starts aiming for it.
FORBIDDEN_WORDS = (
    "hot",
    "warm",
    "cold",
    "tier",
    "tiers",
    "threshold",
    "thresholds",
    "route",
    "routed",
    "routing",
    "escalate",
    "escalation",
    "suppress",
    "suppressed",
    "disqualified",
    "disqualify",
    "notify",
    "inbox",
)


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_the_rubric_carries_no_policy_vocabulary(fresh_rubric: str, word: str) -> None:
    assert re.search(rf"\b{word}\b", fresh_rubric, flags=re.IGNORECASE) is None


def test_the_rubric_contains_no_numerals(fresh_rubric: str) -> None:
    """The blunt version of the rule above: no digit can hide a boundary.

    Score ranges live in the assessment schema's own field descriptions and are not
    restated here, so the rubric has no legitimate use for a numeral — which makes "no
    digits at all" a rule that cannot be got subtly wrong. 80, 55 and 30 are covered by
    construction.
    """
    offenders = sorted(set(re.findall(r"\d+", fresh_rubric)))
    assert offenders == [], f"numerals in the rubric: {offenders}"


def test_the_rubric_carries_no_tenant_specifics(fresh_rubric: str) -> None:
    """Everything customer-shaped arrives in the second block, from ``icp_block()``."""
    for tenant_word in ("Acme", "Globex", "JAT-LeadQuali", "tenant_profile", "@"):
        assert tenant_word not in fresh_rubric


def test_the_rubric_is_the_same_block_for_every_tenant() -> None:
    first, _ = build_system_blocks(make_config())
    second, _ = build_system_blocks(TenantConfig.model_validate(OTHER))
    assert first == second
    assert first.text == rubric_text()


def test_the_rubric_names_every_scored_dimension(fresh_rubric: str) -> None:
    """Adding a dimension to the schema without documenting it is a silent zero."""
    for dimension in DimensionScores.model_fields:
        assert re.search(rf"\b{dimension}\b", fresh_rubric), f"{dimension} is undocumented"


def test_the_rubric_covers_the_judgement_fields(fresh_rubric: str) -> None:
    for field in ("reasoning", "confidence", "missing_information", "spam_or_test_submission"):
        assert field in LeadAssessment.model_fields
        assert re.search(rf"\b{field}\b", fresh_rubric), f"{field} is unexplained"


def test_the_rubric_separates_spam_from_a_poor_lead(fresh_rubric: str) -> None:
    """The distinction that costs the most to get wrong is stated explicitly."""
    spam_section = fresh_rubric.split("spam_or_test_submission")[-1]
    assert "not spam" in spam_section.lower()


# -------------------------------------------------------------------------- assembly


def test_the_stable_block_comes_first_and_carries_the_breakpoint() -> None:
    blocks = build_system_blocks(make_config())
    assert [block.cacheable for block in blocks] == [True, False]
    assert blocks[0].text == rubric_text()
    assert blocks[1].text == make_config().icp_block()


def test_at_most_one_breakpoint_is_requested() -> None:
    """The API allows four; asking for one keeps three free for #11's message blocks."""
    blocks = build_system_blocks(make_config())
    assert sum(block.cacheable for block in blocks) == 1


def test_blocks_are_values() -> None:
    block = PromptBlock(text="x", cacheable=True)
    with pytest.raises(AttributeError):
        block.text = "y"  # type: ignore[misc]


def test_two_tenants_share_an_identical_prefix_and_differ_only_after_it() -> None:
    one = render_system_prompt(make_config())
    two = render_system_prompt(TenantConfig.model_validate(OTHER))
    prefix = rubric_text() + BLOCK_SEPARATOR

    assert one.startswith(prefix)
    assert two.startswith(prefix)
    assert one != two
    # The divergence starts exactly at the end of the shared prefix, not one byte earlier.
    shared = len(prefix)
    assert one[:shared] == two[:shared]
    assert one[shared:] != two[shared:]


def test_the_tenant_block_carries_no_policy_numbers() -> None:
    """#8's promise, restated where it matters: the assembled prompt hides the boundary."""
    cfg = TenantConfig.model_validate(OTHER)
    rendered = render_system_prompt(cfg)
    for hidden in ("90", "60", "20", "0.8"):
        assert hidden not in rendered


def test_a_tenant_pinned_to_another_revision_fails_loudly() -> None:
    """Recording ``rubric_v2`` beside text that is ``rubric_v1`` corrupts the one
    measurement the version string exists for."""
    cfg = make_config(prompt_version="rubric_v2")
    with pytest.raises(PromptVersionMismatchError) as excinfo:
        build_system_blocks(cfg)
    message = str(excinfo.value)
    assert "acme" in message
    assert "rubric_v2" in message
    assert PROMPT_VERSION in message


# ----------------------------------------------------------------------- cacheability


def test_the_rubric_clears_the_minimum_cacheable_prefix(fresh_rubric: str) -> None:
    """Below the minimum, ``cache_control`` is a no-op with no error at all.

    The minimum for ``claude-opus-5`` is 512 input tokens (per the ``claude-api`` skill,
    ``shared/prompt-caching.md`` § API reference — the minimum is model-dependent and *not*
    monotonic across generations: 512 here, 1024 on Opus 4.8 / Sonnet 5, 2048 on Opus 4.7,
    4096 on Opus 4.6 / Haiku 4.5). Exact counts need ``messages.count_tokens``, which is an
    API call; ``estimate_tokens`` is a deliberately pessimistic offline proxy, so clearing
    the bar here means clearing it for real.
    """
    assert estimate_tokens(fresh_rubric) >= MIN_CACHEABLE_PREFIX_TOKENS


def test_the_estimate_understates_the_true_token_count() -> None:
    """The proxy must err towards "not cacheable", never towards a false green test."""
    assert CONSERVATIVE_CHARS_PER_TOKEN >= 5  # English prose runs nearer four
    assert estimate_tokens("x" * (CONSERVATIVE_CHARS_PER_TOKEN * 10)) == 10
    assert estimate_tokens("") == 0


def test_the_tenant_block_is_not_counted_towards_the_cacheable_prefix() -> None:
    """The prefix that must clear the minimum is the rubric alone: it is the only part
    that is byte-identical across tenants, and therefore the only part worth caching."""
    blocks = build_system_blocks(make_config())
    assert estimate_tokens(blocks[0].text) >= MIN_CACHEABLE_PREFIX_TOKENS


# --------------------------------------------------------------------------- versioning


def test_the_version_is_consistent_everywhere() -> None:
    assert PROMPT_VERSION == "rubric_v1"
    assert PROMPT_VERSION == DEFAULT_PROMPT_VERSION
    assert make_config().prompt_version == PROMPT_VERSION
    assert f"{PROMPT_VERSION}.md" == RUBRIC_FILENAME


def test_the_versioned_file_is_the_one_that_ships() -> None:
    assert packaged_rubric().is_file()
