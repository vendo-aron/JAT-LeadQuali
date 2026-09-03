# JAT-LeadQuali

An AI lead qualification agent: takes a raw inbound web-form lead, decides whether it is worth a
salesperson's time, and routes it accordingly — without a human triaging every submission.

- **Stack:** Python 3.13 · FastAPI · Anthropic API (`claude-opus-5`)
- **Deploy target:** AWS Lambda + API Gateway (serverless)
- **Status:** Phase 1 — the qualifier runs from the command line

## Start here

📄 **[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)** — architecture, data model,
scoring design, evaluation strategy, delivery phases, and step-by-step Visual Studio 2026 setup.

## Score a lead from the command line

Phase 1 deliberately produces something you can judge before any infrastructure exists.
No web server, no database, no AWS.

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...             # Windows: setx, then restart the shell

python -m leadquali.cli score tests/fixtures/leads/hot_enterprise_buyer.json
```

It prints the tier and total score, the five dimension scores, the extracted facts and
what is missing, the model's reasoning and confidence, and the tokens, cache reads,
latency and cost for the call.

| Flag | Effect |
|---|---|
| `--tenant NAME` | Apply another tenant's rubric (default: `default`, from `tenants/`). |
| `--effort low\|medium\|high` | Model effort. Sweep it against the golden set before choosing (#24). |
| `--json` | Emit one machine-readable object instead of the report. The eval harness (#23) consumes this. |

Four sample leads live in `tests/fixtures/leads/` — an obvious hot lead, an obvious
disqualification, a sparse and ambiguous one, and a prompt-injection attempt. Run all four
and read the output with whoever owns the ICP definition. **That review is the Phase 1
gate**: rubric problems are cheap to fix here and expensive to fix after Phase 2.

Two behaviours are deliberate and worth knowing before you read the output:

- **A model failure exits `0` and prints an escalation.** A refusal, timeout or API error
  produces a lead routed to a human, because that is what production will do with it. Only
  a bad file or an unknown tenant exits non-zero.
- **The human report never prints the lead's email address.** It is meant to be pasted
  into a ticket. Use `--json` when you need the full record.

If `cache_read` is `0` on a second run of the same tenant, the cacheable prompt prefix has
moved — something volatile has leaked into it, and the cost model no longer holds.

## The release rule: no prompt or model change ships without an eval

Every change to the rubric, the prompt, the model id or the effort level **must have an
eval run attached to the pull request** — the run summary or the JSON result file from
`tests/evals/run_eval.py`. Not a description of one; the output. Without it the change is
a guess, and a rubric regression is invisible until a salesperson notices that good leads
stopped arriving, which is months later and unattributable.

```bash
# What will this cost? Free, calls nothing, needs no key.
python -m tests.evals.run_eval --estimate

# Run it. Both the flag and the key are required, and neither implies the other.
export ANTHROPIC_API_KEY=sk-ant-...
python -m tests.evals.run_eval --confirm-spend --effort medium
```

It prints, in this order: **recall on "should have been contacted"** (hot + warm — the
false-disqualification rate, the number that costs money), precision on `hot` (the number
sales feels), exact and adjacent tier accuracy, cost and p95 latency per lead, a confusion
matrix, per-case results with each label's notes beside the model's reasoning, and any
prompt-injection findings. It also writes a timestamped JSON file under
`tests/evals/results/` tagged with the model, prompt version, effort and git SHA, so two
runs can be diffed.

In CI it is `workflow_dispatch` only — see the header of `ci/github-actions-eval.yml` for
the one-line `git mv` that activates it. It never runs on `push`: the eval costs real
money per invocation.

> **Read this before quoting a number from it.** The golden set is currently **synthetic
> and self-labeled**, so every figure it produces measures self-consistency with whoever
> wrote the seed cases — not correctness. The report says so beside every metric, and
> [`docs/labeling-golden-set.md`](docs/labeling-golden-set.md) is how the set stops being
> synthetic: promote real leads from the `feedback` table, weekly.
