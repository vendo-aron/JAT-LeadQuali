# Tuning the rubric, and choosing an effort level

**Audience:** whoever owns the ICP definition and the prompt (default D3:
`aron@vendoworks.com`).
**Files you will touch:** `src/leadquali/prompts/rubric_vN.md`, `tenants/<tenant>.json`.
**Tools:** `tests/evals/sweep.py`, `tests/evals/diff_results.py`, `tests/evals/run_eval.py`.

This is the procedure for changing the rubric or the effort level *and being able to say
what the change did*. Labeling the golden set is a different job with its own runbook —
[`labeling-golden-set.md`](labeling-golden-set.md), from #62 — and this document does not
repeat it. Read §0 of that file before you read any number in this one.

---

## 0. The one thing to understand before tuning anything

The golden set is **fifteen synthetic cases, written and labeled by the same person, with
inter-labeler agreement unmeasured.** Two consequences, and they are not caveats, they are
the operating conditions:

> **Tuning a rubric against a self-labeled synthetic set optimises the model for agreement
> with the person who wrote the cases.** It does not optimise it for revenue. Every point
> of "accuracy" you win is a point of closer agreement with one person's guess about what a
> good lead looks like — and if that guess is wrong, tuning makes the product worse while
> the dashboard goes up.

> **Fifteen cases cannot resolve a difference smaller than one case.** One case is 6.7
> percentage points. A proportion measured over fifteen items takes only the values `k/15`,
> so a "three-point improvement" does not exist, and a one-case difference between two
> effort levels is a coin flip with a number printed beside it.

The tools enforce both. Every report carries the synthetic caveat next to every metric
block, and every difference is printed beside the smallest difference the set could have
detected. When nothing clears that floor the report says, verbatim:

```
This golden set cannot distinguish these effort levels.
```

That sentence is the honest output of this procedure today, and it is a result, not a
failure. Act on it by growing the golden set, not by picking the cheaper level anyway.

### What the current set can and cannot see

| Question | Answer at 15 cases |
|---|---|
| Smallest representable difference | **6.7pp** — one case |
| Stated minimum detectable difference | **13.3pp** — two cases (see below) |
| Cases needed to resolve 5pp | 20 |
| Cases needed to resolve 2pp | 50 — the acceptance target in #62 |
| p95 latency | The **slowest single call**. Nearest-rank p95 selects the maximum for any set under 20 cases, so "p95" is a max wearing a percentile's name |
| Inter-labeler agreement | Unmeasured. There is one labeler |

The **minimum detectable difference is a stated convention, not a significance test.** It
is `MIN_MEANINGFUL_CASES` (two) times the resolution, raised to the observed run-to-run
spread whenever `--repeat` measured one. Two cases is chosen because one case is the
resolution limit *and* is exactly what an unchanged prompt moves between two repeats — the
model samples, and a lead near a threshold lands either side of it. There is deliberately
no p-value anywhere in this toolchain: fifteen leads invented by one person have no
sampling model that would justify one, and an unjustifiable test that says `p < 0.05` is
more dangerous than no test at all. A stated floor and a measured spread are defensible,
and they are what the tools report.

---

## 1. Changing the rubric

The rubric is `src/leadquali/prompts/rubric_v1.md`, served as the cached prefix of every
call. **The text of a released version never changes.** A revision is a new file:

1. `cp src/leadquali/prompts/rubric_v1.md src/leadquali/prompts/rubric_v2.md` and edit the
   copy.
2. Bump `PROMPT_VERSION` in `src/leadquali/prompts/rubric.py` to `"rubric_v2"`.
3. Bump `prompt_version` in every `tenants/*.json` that should move to it. A tenant pinned
   to a version this build does not ship is refused at render time rather than silently
   served the new text (`rubric.py` checks it), so a tenant can be held back deliberately.

**Why the version must be bumped rather than the file edited in place.** Every result file
records the `prompt_version` the run used, and `diff_results.py` prints it as attribution
on any comparison. Edit `rubric_v1.md` in place and last Tuesday's result file still says
`rubric_v1` — so the diff shows two runs of "the same prompt" that were not the same
prompt, and the tool cannot warn you, because nothing in the artifacts records that
anything changed. The version string is the only thing that makes an old result file
comparable to a new one. Bumping it is not bookkeeping; it is the entire basis of the
comparison.

The same rule applies to what is *inside* the cached prefix. Anything volatile there — a
timestamp, a lead-specific string — breaks prompt caching, which changes the cost of every
run for reasons that have nothing to do with the rubric. See §4.

---

## 2. The loop

```bash
# 0. See the price before spending it. Needs no key, calls nothing.
python -m tests.evals.sweep --estimate

# 1. Measure the current state. Two repeats, so the noise floor is measured
#    rather than assumed.
python -m tests.evals.sweep --confirm-spend --repeat 2

# 2. Change one thing: the rubric text, a weight, a threshold, min_confidence.
#    One thing. Two changes and a moved number attributes to neither.

# 3. Re-measure at the level you actually ship.
python -m tests.evals.run_eval --confirm-spend --effort medium --repeat 2

# 4. Compare the two saved result files. Free: no key, no calls, no spend.
python -m tests.evals.diff_results \
    tests/evals/results/eval-<before>.json \
    tests/evals/results/eval-<after>.json
```

Step 4 is the point of the artifacts. "Did last Tuesday's prompt change make things worse?"
is a question about two files you have already paid for; re-running the eval to answer it
is paying twice for evidence you already own.

### Deciding

Read the diff in this order, and stop at the first line that answers you.

1. **Injection findings.** A `security_findings` entry is a defect, not a metric. An attack
   payload tiered `hot` fails the change regardless of what accuracy did. Exit code 4.
2. **Recall on contactable.** The number that costs money: leads a human said should have
   been called, that the pipeline surfaced. Every miss is a deal nobody called.
3. **`LOST` cases.** Named individually in the diff. Read the label's notes beside the
   model's reasoning — often the label is wrong, and the correct fix is to the golden set.
4. **Everything else,** and only if the verdict line says the difference cleared the noise
   floor. `within noise` means the measurement was not sensitive enough to answer the
   question. It does **not** mean the change was neutral.

`diff_results.py` exits `1` only on a regression larger than the floor, so it is safe to
gate a pull request on — the gate cannot fire on a coin flip.

---

## 3. The effort sweep

```bash
python -m tests.evals.sweep --estimate                     # the bill, per level and total
python -m tests.evals.sweep --confirm-spend --repeat 2     # three runs, one comparison
python -m tests.evals.sweep --confirm-spend --effort low --effort medium --baseline medium
```

The sweep runs `run_eval` once per level through the `assessor_factory` seam — the same
harness, three assessors — writes one result file per level, and compares those files. It
prints accuracy, cost per lead and p95 latency side by side, the delta of every metric
against the baseline level (`medium`, the incumbent), and a verdict.

It refuses to start without both `--confirm-spend` and `ANTHROPIC_API_KEY`, and the price
in the refusal is the sweep's, not one run's: a sweep is three times the cost, and the
number you are asked to confirm should be the number you pay.

**The sweep will not name a winner while the golden set holds no real cases.** Whatever the
numbers say, and even when a difference clears the floor, the recommendation is:

```
No effort level is recommended from this run.
```

This is deliberate and it is not a bug to be worked around. See §0. The gate opens when
`real_cases > 0` *and* some difference clears the noise floor; then it names the cheapest
level with no meaningful regression against the baseline, and says which metrics decided
it.

### The comparison table this issue asks for

| Metric | `low` | `medium` | `high` |
|---|---|---|---|
| Exact tier accuracy | not measured | not measured | not measured |
| Precision on `hot` | not measured | not measured | not measured |
| Recall on contactable | not measured | not measured | not measured |
| Cost per lead | not measured | not measured | not measured |
| p95 latency | not measured | not measured | not measured |
| `cache_read_tokens` > 0 | not measured | not measured | not measured |

**These cells are empty because no live sweep has been run.** There is no Anthropic API key
in the environment this tooling was built in, and the sweep deliberately has no offline
mode: a number produced without calling the model would measure nothing and would end up
quoted as though it had. The measurement — and the "pick the cheapest effort that holds
accuracy" decision it feeds — is the owner's to execute, **once the golden set has real
cases in it**, per #62. Running the sweep against the synthetic seed first is still worth
doing as a smoke test of the pipeline and a real cost figure; it is not worth doing as a
basis for choosing an effort level.

Fill the table in by running the sweep and pasting the numbers, and record the sweep's
`sweep-*.json` path beside it so the row can be traced to an artifact.

---

## 4. Before drawing any cost conclusion: check the cache

`cache_read_tokens` must be **greater than zero** across a run. Every result file records
it per case (`record.metering.cache_read_tokens`). If it is zero, prompt caching is not
working: something volatile has leaked into the cached prefix, every call is paying full
input price, and every cost number in the sweep is wrong in the same direction.

Fix that before comparing costs between effort levels. A cost difference measured with a
broken cache is a measurement of the bug, not of the effort level.

---

## 5. Setting `min_confidence` from data

`min_confidence` (default `0.6` in `tenants/default.json`) is the threshold below which a
lead escalates to a human instead of being routed on the model's judgement. It trades two
costs against each other:

- **Too high:** everything escalates, sales drowns, and the product is a mailing list.
- **Too low:** mislabels route silently, and invariant 3 — no lead is ever silently dropped
  — is satisfied only on paper.

Pick it from the recorded numbers rather than by feel. Every result file carries each
case's `confidence` beside its `status`. The threshold you want is the one where most cases
that landed `MISS` or `LOST` sit *below* it and most cases that landed `ok` sit above it.

**At fifteen cases you cannot do this yet.** A threshold fitted to fifteen synthetic cases
is fitted to noise, and it will move the first time a real lead arrives. Leave
`min_confidence` at its default until the golden set has real cases, then read the
distribution off a real run.

---

## 6. Recording the outcome

When a revision ships:

1. The new `rubric_vN.md`, with `PROMPT_VERSION` and the tenants' `prompt_version` bumped.
2. The before and after result files, and the `diff_results` output, attached to the pull
   request. The numbers in the PR body must carry the synthetic caveat with them — the
   reports print it beside every metric block precisely so it cannot be cropped out.
3. The chosen effort level in the default `TenantConfig`, **with the sweep artifact that
   justifies it**. An effort level in config with no result file behind it is a guess, and
   `medium` is currently exactly that: a documented starting point (plan §5), not a
   measurement.
4. The measured cost per lead into §8 of the implementation plan — from a run with a
   working cache (§4).

---

## 7. Reference

| Command | Spends? | Purpose |
|---|---|---|
| `python -m tests.evals.run_eval --estimate` | no | Price one run |
| `python -m tests.evals.run_eval --confirm-spend` | **yes** | One run, one result file |
| `python -m tests.evals.sweep --estimate` | no | Price the whole sweep, per level and total |
| `python -m tests.evals.sweep --confirm-spend` | **yes** | Every level, one comparison |
| `python -m tests.evals.diff_results A.json B.json` | no | Compare two saved runs |
| `python -m tests.evals.diff_results A.json B.json --json` | no | The same, machine-readable |

Exit codes: `0` fine, `1` a regression beyond the noise floor (`diff_results` only), `2` an
input problem, `3` the run was never authorised, `4` an injection finding.

Related: [`labeling-golden-set.md`](labeling-golden-set.md) (#62) for growing the set,
[`observability.md`](observability.md) for the metrics the same fields produce in
production.
