"""Every case in the injection corpus, held to the same structural guarantees.

These tests answer one question for eighteen different attacks: *can this submission stop
being data?* They never ask what the model scores it — that needs an API key and belongs
to the golden set (#22), which reuses this same corpus file.

The guarantees are deliberately few and absolute, because a containment property that
holds "usually" is not containment:

* nothing but the framework's own nonce-tagged delimiters may begin a line;
* the nonce never appears inside the payload;
* the payload region contains no unescaped ``<``;
* the rendered turn is bounded however large the submission;
* the cacheable system prefix is byte-identical whatever arrives;
* and nothing is *silently* lost — content either survives or is recorded as truncated.
"""

from __future__ import annotations

from typing import Any

import pytest

from leadquali.domain.tenant_config import TenantConfig
from leadquali.prompts import build_system_blocks
from leadquali.prompts.lead import (
    LEAD_BLOCK_TAG,
    LEAD_MESSAGE_BLOCK_TAG,
    MAX_MESSAGE_CHARS,
    LeadSubmission,
    RenderedLead,
    block_delimiters,
    render_lead_detailed,
)
from tests.fixtures import InjectionCase, load_injection_corpus

NONCE = "0123456789abcdef0123456789abcdef"

CORPUS = load_injection_corpus()

TENANT: dict[str, Any] = {
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


def _render(case: InjectionCase) -> RenderedLead:
    submission = LeadSubmission(**dict(case.fields), extra=dict(case.extra))
    return render_lead_detailed(submission, nonce=NONCE)


def _markers() -> tuple[str, ...]:
    lead = block_delimiters(NONCE, LEAD_BLOCK_TAG)
    message = block_delimiters(NONCE, LEAD_MESSAGE_BLOCK_TAG)
    return (*lead, *message)


def _payload_region(text: str) -> str:
    lead_open, lead_close = block_delimiters(NONCE, LEAD_BLOCK_TAG)
    start = text.index("\n" + lead_open + "\n")
    end = text.index("\n" + lead_close + "\n", start)
    region = text[start : end + len(lead_close) + 2]
    for marker in _markers():
        region = region.replace(marker, "")
    return region


def test_the_corpus_is_broad_enough_to_be_worth_running() -> None:
    """A corpus of one attack proves nothing; guard against it shrinking by accident."""
    assert len(CORPUS) >= 12
    assert len({case.category for case in CORPUS}) >= 8
    assert len({case.id for case in CORPUS}) == len(CORPUS)


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
def test_no_case_can_open_or_close_a_block(case: InjectionCase) -> None:
    rendered = _render(case)
    opening = [line for line in rendered.text.splitlines() if line.startswith("<")]
    assert set(opening) <= set(_markers())
    lead_open, lead_close = block_delimiters(NONCE, LEAD_BLOCK_TAG)
    assert rendered.text.splitlines().count(lead_open) == 1
    assert rendered.text.splitlines().count(lead_close) == 1


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
def test_no_case_gets_an_unescaped_angle_bracket_or_the_nonce(case: InjectionCase) -> None:
    payload = _payload_region(_render(case).text)
    assert "<" not in payload
    assert NONCE not in payload


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
def test_no_case_can_blow_the_token_budget(case: InjectionCase) -> None:
    """Including the ~1.5 MB submission, which is why every field is capped."""
    rendered = _render(case)
    assert len(rendered.text) < MAX_MESSAGE_CHARS * 2


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
def test_nothing_is_lost_without_being_recorded(case: InjectionCase) -> None:
    """Invariant 3 at the first step: content survives, or its loss is on the record."""
    rendered = _render(case)
    if case.canary in rendered.text:
        return
    assert rendered.truncated_fields or rendered.dropped_extra_fields, (
        f"{case.id}: canary vanished with nothing recorded"
    )


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
def test_no_case_disturbs_the_cacheable_prefix(case: InjectionCase) -> None:
    config = TenantConfig.from_dict(TENANT)
    baseline = build_system_blocks(config)[0].text
    _render(case)
    assert build_system_blocks(config)[0].text == baseline
    assert case.canary not in baseline
