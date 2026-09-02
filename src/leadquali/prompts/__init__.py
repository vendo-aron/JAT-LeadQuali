"""Versioned prompt assets and the system-prompt assembly that uses them.

``rubric_vN.md`` files are the instructional text; :mod:`leadquali.prompts.rubric` loads
one and assembles it with a tenant's block into the two-block system prompt. A released
version's text is never edited — assessments reference it by name.
"""

from leadquali.prompts.rubric import (
    BLOCK_SEPARATOR,
    CONSERVATIVE_CHARS_PER_TOKEN,
    MIN_CACHEABLE_PREFIX_TOKENS,
    PROMPT_VERSION,
    RUBRIC_FILENAME,
    PromptAssetError,
    PromptBlock,
    PromptVersionMismatchError,
    build_system_blocks,
    estimate_tokens,
    render_system_prompt,
    rubric_text,
)

__all__ = [
    "BLOCK_SEPARATOR",
    "CONSERVATIVE_CHARS_PER_TOKEN",
    "MIN_CACHEABLE_PREFIX_TOKENS",
    "PROMPT_VERSION",
    "RUBRIC_FILENAME",
    "PromptAssetError",
    "PromptBlock",
    "PromptVersionMismatchError",
    "build_system_blocks",
    "estimate_tokens",
    "render_system_prompt",
    "rubric_text",
]
