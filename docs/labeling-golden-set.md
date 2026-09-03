# Labeling the golden set

**Audience:** whoever owns the ICP definition (default D3: `aron@vendoworks.com`, per
[`decisions/0001-open-product-questions.md`](decisions/0001-open-product-questions.md)).
**Time:** ~30 minutes a week, once leads are flowing. ~2 hours for the first sitting.
**File you are editing:** `tests/evals/golden_leads.jsonl`.

---

## 0. Read this before you quote a number

The file currently holds **15 synthetic cases and zero real ones.** They were written by
hand to exercise lead *shapes* — a hot lead, a job seeker, an injection attempt — and they
were labeled by the same person who invented them.

> **Any eval number computed against a synthetic set measures self-consistency, not
> correctness.** It tells you whether the model agrees with whoever wrote the seed. It does
> not tell you whether the rubric is right, because nothing in the file came from a real
> buyer.

This is not pessimism, it is the recorded answer to question 1 on
[#2](https://github.com/vendo-aron/JAT-LeadQuali/issues/2): **there is no historical lead
data with outcomes**, and the open follow-up (D0) is whether raw submissions exist without
outcomes. Until real leads are labeled, `run_eval.py` (#23) produces a smoke test of the
pipeline, not evidence about quality. The seed is deliberately small for the same reason:
padding it to fifty invented leads would produce a confident 90-something-percent accuracy
figure that measures nothing, and that number would then be quoted in a sales conversation.

Everything below is how the file stops being synthetic.

---

## 1. The weekly ritual

Thirty minutes, same slot every week. #19 made this possible: every routing email carries
signed one-click good-lead / bad-lead links, and clicking one writes a row to `feedback`.

1. **Pull last week's feedback.** Disagreements first — a `bad` verdict on a lead the model
   tiered `hot`, or a `good` verdict on one it tiered `cold` or `disqualified`.

   ```sql
   SELECT f.lead_id,
          f.verdict,
          f.rater,
          f.notes,
          a.tier            AS model_tier,
          a.total_score,
          a.confidence,
          a.escalation_reason,
          l.raw_payload
     FROM feedback f
     JOIN leads l        ON l.tenant_id = f.tenant_id AND l.id = f.lead_id
     LEFT JOIN assessments a ON a.tenant_id = f.tenant_id AND a.lead_id = f.lead_id
    WHERE f.tenant_id = :tenant_id
      AND f.created_at >= now() - interval '7 days'
    ORDER BY (f.verdict = 'bad' AND a.tier IN ('hot', 'warm')) DESC,
             (f.verdict = 'good' AND a.tier IN ('cold', 'disqualified')) DESC,
             f.created_at;
   ```

2. **Promote the disagreements** into `golden_leads.jsonl` (section 4). Two or three a week
   is a real dataset inside a quarter.
3. **Also promote anything the pipeline escalated** — `assessments.escalation_reason` is
   not null. A lead the system could not judge is a lead the rubric has nothing to say
   about yet, which is exactly what the golden set is for.
4. **Re-run `run_eval.py`** and read the summary line, which always states the
   synthetic/real split.

**A skipped week is a week of lost product value.** With no historical archive, this ritual
*is* the dataset. There is no back-fill: a rep's in-the-moment judgement in week 3 cannot be
reconstructed in month 6, because nobody remembers in November whether lead #412 was worth
the call.

### If the feedback table stays empty

That is the silent failure mode described in decision record 0001 §3. Nothing errors,
dashboards stay green, and you discover it in Phase 3 with nine labeled leads. If a week
passes with no `feedback` rows while leads are flowing, the problem is not this runbook —
it is that the routing email is landing somewhere nobody reads, which is open question 3
(CRM write-back). Escalate it as a product question, not as a labeling backlog.

---

## 2. Which leads to label

In order of value:

1. **Leads the agent got wrong.** A case the model already gets right teaches the eval
   nothing: it will keep getting it right and the accuracy number will not move when the
   prompt changes. A case it gets wrong is the only kind that can *detect* a regression or
   prove an improvement. A golden set of easy leads reports a high number and cannot
   distinguish a good rubric from a lucky one.
2. **Leads the pipeline escalated** (low confidence, refusal, parse error). Same argument:
   these are where the rubric has no opinion.
3. **Leads a human found genuinely ambiguous.** Mark them `hard_case: true` and record the
   ambiguity in the notes. If two reasonable people would disagree, that disagreement is
   the realistic ceiling on model accuracy, and you want it measured rather than assumed.
4. **The hard negatives** — the ones dense with buying vocabulary that are not buyers:
   job seekers, competitors doing recon, agencies pitching inbound. These are where a
   keyword-shaped rubric embarrasses itself in front of sales.
5. **Clean, obvious leads.** Worth a handful as anchors — they catch a rubric that has gone
   globally wrong — and no more. The seed already has them.

Do not sample randomly to fill a quota. A padded set is a lie about coverage.

---

## 3. How to assign a tier

**Do not look at the model's answer first.** Anchoring is the failure mode of this whole
exercise: once you have seen `hot / 82`, your own judgement collapses toward it, and you
will record agreement you did not independently reach. The golden set then measures the
model against itself and every number it produces is worthless — in exactly the way that is
impossible to notice afterwards.

So, in order:

1. Read **only the lead payload** — `leads.raw_payload`, or the form fields. Not the
   assessment, not the score, not the routing email.
2. Decide the tier the lead **should** have received, and write your one-line reason
   *before* revealing the model's answer. Note that this judgement does not require knowing
   what actually happened next — which is why labeling is possible at all with no outcome
   data.
3. *Then* look at the assessment. If you disagree, that is a golden case worth having. If
   you agree, it is worth much less (see section 2).
4. If you find you cannot decide, that is a legitimate and informative answer: label your
   best guess, set `hard_case: true`, and record the ambiguity in the notes. Do not force a
   confident tier onto an ambiguous lead — noise in the golden set is worse than a smaller
   golden set, which is the same reason #19's feedback has an `unsure` verdict.

### The four tiers, as judgements rather than score bands

Score thresholds are tenant configuration (invariant 1) and will move. Label the
*judgement*, not the arithmetic:

| Tier | The question it answers |
|---|---|
| `hot` | Would a rep be annoyed if this sat in a queue for a day? |
| `warm` | Worth a call this week, but nothing is on fire. |
| `cold` | Worth a nurture sequence; calling it today would waste the call. |
| `disqualified` | Nobody should spend time on this. Not the same as spam. |

`disqualified` is a judgement about value; `spam_or_test_submission` is a claim of fact
about the submission. A student writing a dissertation is disqualified and is *not* spam:
their row is still recorded and never suppressed (invariant 3).

### Two labelers, and why

#22 asks for inter-labeler agreement on an overlapping subset, because **that disagreement
rate is the realistic ceiling on model accuracy** — and with no outcome data it is the only
calibration signal that exists. A model cannot beat the rate at which humans agree with
each other about the answer.

To measure it: pick ~10 cases, have a second person label them independently (same rule —
they see the payload and neither the model's answer nor the first label), and append their
label to the same case's `labels` array. The loader reports agreement automatically; it
reports `unmeasured` today because no case has two labels, which is the honest answer and
better than a fabricated `1.0`.

When two labels disagree, set `expected_tier` to whichever tier the adjudication chose. The
validator requires it to be one of the tiers a labeler actually picked — adjudicating a
disagreement means choosing one of the human answers, not inventing a third.

---

## 4. Promoting a `feedback` row into a golden case

Promotion is an **append of one line**. The file is append-only by design so that two
people labeling in the same week do not conflict.

1. **Rewrite the payload — do not copy it.** Replace the person's name, the company name,
   the email address and the website with consistent pseudonyms, so the *semantics* survive
   (a VP is still a VP, a 300-person logistics firm is still a 300-person logistics firm)
   and the identity does not. This file lives in a git repository that may end up in a
   customer's hands.

   Email and website domains **must** be reserved-for-documentation domains — `example.com`,
   `example.org`, `example.net`, or anything ending `.example`, `.invalid`, `.test`,
   `.localhost`. The validator enforces this and fails the suite on anything else, which is
   what makes "no real PII" a check rather than a hope. The one exception is the consumer
   mailbox providers in `FREE_EMAIL_PROVIDERS` (`gmail.com`, `outlook.com`, …), allowed
   because "free-provider address with strong buying signals" is a lead shape the rubric has
   to get right and the domain *is* the signal under test. For those, invent an obviously
   fabricated local part — no regular expression can check that, so it is on you.

   Phone numbers: use the `555` reserved ranges. Job titles, headcounts, industries,
   timelines and money figures should be kept as they were: they are the signal.

2. **Set `provenance: "real"`** and `promoted_from` to where it came from — `feedback:<first
   8 characters of the lead id>` is enough. A `real` case with no origin is refused: an
   unattributable claim about the data is worse than no case.

3. **Write your own label under your own handle.** A `real` case labeled only by
   `seed_author` (the synthetic seed's handle) is refused. Growing the file is not the same
   as growing the evidence in it.

4. **Do not paste the rep's `feedback.notes` verbatim into a lead field.** It may name
   people. Summarise it in the label's `notes`, where it belongs.

5. **Consider raising `min_real_cases`** in the header to lock in what you now have. See
   section 6.

---

## 5. What to write in the notes

The notes field is free text and it is the most useful column in the file. In six months
someone will look at a number that seems wrong and this sentence is what tells them whether
the label or the model is at fault.

Write **why this tier and not the adjacent one**. Cite the lead's own words. Twenty
characters is the minimum the validator accepts and it is not the target.

Good:

> Textbook ICP and a real problem, but the lead explicitly says there is no deadline and
> never mentions money. Warm is the honest answer; a model that calls this hot is reading
> fit as intent.

> Hard because it is dense with exactly the vocabulary a hot lead uses — six-figure deals,
> mid-market revenue teams, pipeline — while asking for a job.

Useless:

> Looks warm. / Good fit. / Obvious disqualification.

If the case is a hard one, say what makes it hard and which way you nearly went. If you
disagreed with the model, say what you think it misread.

---

## 6. The counter that will fail the build

The header record at the top of `golden_leads.jsonl` declares the thresholds the file holds
itself to:

```json
{"$golden_set": {"schema_version": 1, "note": "…", "min_total_cases": 15,
 "min_real_cases": 0, "acceptance_target_total_cases": 50,
 "acceptance_target_real_cases": 50}}
```

- `min_total_cases` is a floor: deleting cases fails the suite.
- **`min_real_cases` is the mechanism that stops this issue being quietly abandoned.** It is
  `0` today because no real lead exists to label, so a wholly synthetic set loads green —
  which is correct. **Raising it is how you declare that collection has started.** From then
  on, a set that is still only synthetic fails the build with a message saying so. It is a
  counter rather than a date on purpose: a date in the code would either fire while you are
  on holiday or be commented out and forgotten, and this check must not be able to expire on
  its own.

A reasonable pattern once leads are flowing: after each weekly sitting, set `min_real_cases`
to the number of real cases you now have. It never has to come down, and if it ever does,
that is a visible, reviewable commit rather than a silent shrinkage of the only evidence
this product has.

The `acceptance_target_*` numbers are #22's acceptance criteria. They are **reported, never
enforced** — enforcing them would make the suite red from now until labeling is finished,
which trains everyone to ignore it. `run_eval.py` and the loader's summary print the gap.

---

## 7. The case schema

One JSON object per line. The first line is the `$golden_set` header; every other line is a
case. Unknown keys are refused, because a misspelled key would silently drop the
expectation it carried.

| Key | Required | Meaning |
|---|---|---|
| `case_id` | yes | Stable lowercase slug. Quoted in reviews; used as the pytest id. |
| `provenance` | yes | `"synthetic"` (hand-written) or `"real"` (a submission that arrived). |
| `expected_tier` | yes | The adjudicated tier. Must be one of the tiers a labeler chose. |
| `labels` | yes | Non-empty list of `{labeler, tier, labeled_at, notes}`. |
| `form` | one of | Inline form payload, as `POST /leads` would receive it (#17). |
| `injection_case_id` | one of | Id in `tests/fixtures/injection_corpus.json` (#12). |
| `expected_min_tier` | no | Floor of the accepted band. "Must not be binned below this." |
| `expected_max_tier` | no | Ceiling of the accepted band. |
| `expect_escalation` | no | A human must see this lead whatever tier it lands in. |
| `expected_dimension_ranges` | no | `{dimension: [low, high]}`, inside #7's per-dimension caps. |
| `expected_extracted` | no | Subset of `ExtractedFacts` fields the model should read off. |
| `hard_case` | no | Flagged as genuinely difficult. #22 wants ≥10 of these. |
| `promoted_from` | real only | Where it came from, e.g. `feedback:7f0d9c4e`. |
| `tags` | no | Short slugs for the shapes a case covers, e.g. `sparse`, `competitor`. |

A label is `{"labeler": "icp_owner", "tier": "warm", "labeled_at": "2026-09-14", "notes":
"…"}`. `labeler` is an **opaque lowercase handle, never an email address or a person's
name** — the same rule #15 puts on `feedback.rater`, for the same reason (invariant 5): the
golden set outlives the raw payload, and personal data may only live in `leads.raw_payload`.

### `expected_tier` versus the band

`expected_tier` is the exact-match answer, and exact-match tier accuracy is the headline
number. But for a sparse lead where a human would accept either `cold` or `warm` and only
`disqualified` is actually wrong, `expected_min_tier` / `expected_max_tier` record that.
Setting a band is not a way to hedge a label you have not thought about — it is how you
avoid claiming a precision the label does not have.

### Injection cases

Attack payloads are **referenced, not copied**. They live in
`tests/fixtures/injection_corpus.json`, which #12 wrote to be shared, and each carries an
advisory `expected_max_tier`. Your label may not exceed that ceiling: if an attack really
should score higher, change the advisory in that file and say why. Copying the payload here
would mean the two files drift the first time somebody adds an attack.

At least one injection case must have a **floor above `disqualified`** — a plausible
enquiry with a payload in one field. The attack must not raise a lead's tier; it must also
not destroy an otherwise genuine lead, which is the failure mode a blunt filter causes.

---

## 8. Checking your work

```bash
ruff check . && ruff format --check . && mypy && pytest
```

The validator runs in the default suite (`tests/evals/test_golden_leads.py`), so a malformed
or unlabelled case fails `pytest` with a message naming the line number, the case id and the
key to fix. To see the summary on its own:

```bash
python -c "from tests.evals import load_golden_set, describe_golden_set; \
print(describe_golden_set(load_golden_set()))"
```

---

## 9. What "done" looks like

From #22's acceptance criteria, all of which the loader reports as outstanding gaps until
they are met:

- ≥50 labeled leads (≥30 acceptable to start), **the great majority of them real**, with all
  four tiers represented and ≥10 hard cases.
- No real PII in the committed file — enforced.
- This guide, kept current, so a second person labels consistently.
- Inter-labeler agreement measured and recorded on an overlapping subset of ~10 cases.

The first three are work. The fourth is the one that tells you what the numbers from #23 are
worth.
