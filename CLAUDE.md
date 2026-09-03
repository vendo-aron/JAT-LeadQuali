# JAT-LeadQuali — working conventions

Source of truth for the design: [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md).
Work is tracked as GitHub issues under the epic (#1).

## Layout

`src/` layout, package `leadquali`. Layers, strictly one-directional
(`domain` ← `app` ← `adapters`/`api`):

| Package | Rule |
|---|---|
| `leadquali.domain` | Pure logic. No I/O, no network, no SDK imports. 100% unit tested. |
| `leadquali.app` | Orchestration + `ports.py` Protocols. Depends only on `domain` and ports. |
| `leadquali.adapters` | One file per external system. The only place third-party SDKs are imported. |
| `leadquali.api` | FastAPI app and Lambda entrypoints. Thin — no business logic. |

`anthropic` is imported in `adapters/llm_anthropic.py` and nowhere else. Same rule for
`boto3`, `sqlalchemy`, and `stripe` in their own adapters.

## Non-negotiable product invariants

1. **The rubric is tenant configuration, not code.** ICP text, weights, thresholds and
   routing rules live in `TenantConfig`. Onboarding a customer is a config write, never a
   deploy. Never hardcode a threshold or weight outside a config default.
2. **The model assesses, code routes.** The LLM returns a `LeadAssessment` only. Tier,
   total score and routing action are computed in Python. `tier`/`action` must never appear
   in the model's output schema.
3. **A lead is never silently dropped.** Low confidence, API failure, refusal, parse error,
   timeout — all escalate to a human. Only an explicit spam/test determination suppresses,
   and even that is recorded.
4. **`tenant_id` on every table and every repository method.** From the first migration.
5. **No PII in logs.** Log `contact_email_hash`, never the address. Never log raw payloads.

## Engineering rules

- Python 3.13. `from __future__ import annotations` at the top of every module.
- Test-driven: write the failing test first; a behaviour change lands with the test that
  proves it.
- `ruff check .`, `ruff format --check .`, `mypy`, `pytest` must all pass before commit.
- `mypy` runs `strict = true` over `src` and `tests`. No `Any` escapes without a comment
  saying why; no bare `# type: ignore` without a code.
- Prefer `StrEnum` over `(str, Enum)`.
- Tests that need Postgres are marked `@pytest.mark.integration`; tests that would call the
  Anthropic API are marked `@pytest.mark.live_api` and are excluded from the default suite.
- Never assert on model prose. Assert on tier, score ranges and extracted fields.
- Secrets come from the environment via `leadquali.config.Settings`. Never a literal.

## Commands

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```
