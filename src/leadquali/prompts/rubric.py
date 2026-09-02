"""Loading the versioned rubric, and assembling the system prompt around it.

The system prompt is two blocks, in this order and never the other:

1. **The rubric** — :func:`rubric_text`, read from ``rubric_v1.md``. Byte-identical for
   every request, for every tenant, for the life of a version. This is the cacheable
   prefix, and *only* a byte-identical prefix caches: prompt caching is a prefix match, so
   a single differing byte anywhere in it invalidates everything from that byte onward.
2. **The tenant block** — :meth:`~leadquali.domain.tenant_config.TenantConfig.icp_block`.
   Varies per customer, so it goes after the cache breakpoint, where varying is free.

:func:`build_system_blocks` returns those blocks as data, tagged with which one carries the
breakpoint, so that #11 can render them into the SDK's ``system`` list and attach
``cache_control`` to the first without this module importing ``anthropic`` (``CLAUDE.md``:
the SDK lives in ``adapters/llm_anthropic.py`` and nowhere else).

Why the loaded text is not simply the file's bytes: an HTML comment header addressed to
maintainers is stripped, line endings are normalised to ``\\n`` and trailing whitespace is
dropped. The header keeps the versioning rule next to the thing it governs without
spending tokens on it every request; the normalisation is what stops the same file checked
out on Windows from being a different cache entry than the one on Lambda.

Cacheability has a floor. A prefix below the model's minimum silently does not cache — no
error, ``cache_creation_input_tokens`` simply stays zero — so
:data:`MIN_CACHEABLE_PREFIX_TOKENS` is asserted against in the test suite rather than
assumed.

Not here, deliberately:

* **The user turn.** Rendering the lead itself, inside untrusted-data delimiters, is #12.
  It appends to ``messages``, never to the system blocks, so it changes nothing this
  module produces and cannot move the cache prefix.
* **The API call**, token accounting and ``cache_control`` rendering: #11.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final

from leadquali.domain.tenant_config import TenantConfig

#: The rubric revision this build ships, recorded on every assessment it produces. Bumping
#: it means adding a new ``rubric_vN.md``; the text of a released version never changes.
PROMPT_VERSION: Final[str] = "rubric_v1"

#: Derived from the version rather than written twice, so the two cannot drift.
RUBRIC_FILENAME: Final[str] = f"{PROMPT_VERSION}.md"

_RUBRIC_PACKAGE: Final[str] = "leadquali.prompts"

#: Minimum prefix length that caches at all on ``claude-opus-5`` (plan §1), in input
#: tokens. Source: the ``claude-api`` skill, ``shared/prompt-caching.md`` § API reference.
#: The minimum is model-dependent and not monotonic across generations — 512 here, 1024 on
#: Opus 4.8 and Sonnet 5, 2048 on Opus 4.7, 4096 on Opus 4.6 and Haiku 4.5 — so a prompt
#: that caches today can stop caching purely because someone changed the model string.
#: Below the minimum there is no error and no warning; the cache simply never fills.
MIN_CACHEABLE_PREFIX_TOKENS: Final[int] = 512

#: Characters per token used by :func:`estimate_tokens`. English prose runs nearer four
#: for this tokenizer, so six deliberately *understates* the token count: an estimate that
#: clears the minimum above is a claim we can stand behind offline. An exact count needs
#: ``client.messages.count_tokens``, which is a billable API call and belongs to #11.
CONSERVATIVE_CHARS_PER_TOKEN: Final[int] = 6

#: What joins the blocks when they are flattened into one string for humans or for evals.
BLOCK_SEPARATOR: Final[str] = "\n\n"

_HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)


class PromptAssetError(RuntimeError):
    """A versioned prompt file is missing, unreadable, or empty.

    A packaging fault rather than a user error: the asset ships inside the wheel (see
    ``[tool.setuptools.package-data]``), so if it is absent at runtime the build is wrong
    and every request would otherwise go out with an empty rubric.
    """


class PromptVersionMismatchError(ValueError):
    """A tenant is pinned to a rubric revision this build does not contain.

    Serving ``rubric_v1`` while recording the tenant's pinned ``rubric_v2`` would quietly
    corrupt the one measurement the version string exists for — "did last Tuesday's prompt
    change make things worse?" — so this is loud, and it names the tenant.
    """


@dataclass(frozen=True, slots=True)
class PromptBlock:
    """One system-prompt block, in render order.

    ``cacheable`` marks where the cache breakpoint goes. It is advice about *stability*,
    not about the SDK: this layer does not know what ``cache_control`` is, and the adapter
    that does (#11) is free to spend its four breakpoints differently.
    """

    text: str
    cacheable: bool


def _canonicalise(text: str) -> str:
    """Collapse a prompt asset to one stable byte sequence, whatever wrote the file.

    CRLF becomes LF, trailing whitespace goes from every line, and leading and trailing
    blank lines go entirely. Mirrors the normalisation
    :mod:`leadquali.domain.tenant_config` applies to tenant text, and for the same reason:
    the plan has development happening on Windows and production on Lambda, and a checkout
    that differs by line ending is a prompt that differs by every byte after the first
    newline.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


@cache
def rubric_text() -> str:
    """The rubric instructions, canonicalised — the cacheable half of the system prompt.

    Cached for the life of the process: this is read once per Lambda container, and the
    identity of the returned string across calls is itself part of the guarantee that the
    prefix does not move.

    Raises:
        PromptAssetError: the file is missing, unreadable, or empty once stripped.
    """
    try:
        asset = resources.files(_RUBRIC_PACKAGE).joinpath(RUBRIC_FILENAME)
        raw = asset.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise PromptAssetError(
            f"prompt asset '{RUBRIC_FILENAME}' is not packaged with {_RUBRIC_PACKAGE}; "
            "check [tool.setuptools.package-data] and the build"
        ) from exc
    except OSError as exc:
        raise PromptAssetError(
            f"prompt asset '{RUBRIC_FILENAME}' could not be read: {exc}"
        ) from exc

    text = _canonicalise(_HTML_COMMENT.sub("", raw))
    if not text:
        raise PromptAssetError(
            f"prompt asset '{RUBRIC_FILENAME}' is empty once its maintainer header is "
            "stripped; a request built from it would carry no instructions at all"
        )
    return text


def build_system_blocks(config: TenantConfig) -> tuple[PromptBlock, PromptBlock]:
    """The system prompt for one tenant: stable rubric first, tenant block second.

    The order is the whole design. Everything before the breakpoint is shared by every
    request this service ever makes; everything after it is one customer's policy. Reverse
    them and the cacheable part sits behind bytes that change per tenant, which caches
    nothing while still paying the write premium.

    Args:
        config: the tenant whose ICP block forms the second, varying block.

    Returns:
        Exactly two blocks, in render order. The first is byte-identical for every tenant.

    Raises:
        PromptVersionMismatchError: the tenant is pinned to another rubric revision.
        PromptAssetError: the rubric file is missing or unreadable.
    """
    if config.prompt_version != PROMPT_VERSION:
        raise PromptVersionMismatchError(
            f"tenant '{config.tenant_id}' is pinned to prompt version "
            f"'{config.prompt_version}', but this build ships '{PROMPT_VERSION}'; deploy "
            f"the build that contains '{config.prompt_version}.md' or repin the tenant"
        )
    return (
        PromptBlock(text=rubric_text(), cacheable=True),
        PromptBlock(text=config.icp_block(), cacheable=False),
    )


def render_system_prompt(config: TenantConfig) -> str:
    """The same blocks flattened into one string, for evals, review and debugging.

    Not the request path. #11 sends :func:`build_system_blocks` as separate blocks, because
    a single joined string has nowhere to hang a cache breakpoint and would cost the full
    input price on every call.
    """
    return BLOCK_SEPARATOR.join(block.text for block in build_system_blocks(config))


def estimate_tokens(text: str) -> int:
    """A pessimistic offline lower bound on the token count of ``text``.

    Used to prove — without a network call — that the cacheable prefix clears
    :data:`MIN_CACHEABLE_PREFIX_TOKENS`. It divides by
    :data:`CONSERVATIVE_CHARS_PER_TOKEN`, which is above the real average for English
    prose, so the answer errs towards "too short to cache". That is the safe direction:
    the failure it guards against is silent.
    """
    return len(text) // CONSERVATIVE_CHARS_PER_TOKEN


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
