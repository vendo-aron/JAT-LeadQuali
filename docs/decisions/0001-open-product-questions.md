# 0001 — The four open product questions

**Status:** 1 of 4 answered · 3 open
**Opened:** 2026-09-01 · **Last updated:** 2026-09-01
**Decision owner:** aron@vendoworks.com (business decisions — engineering cannot make these)
**Tracks:** [#2](https://github.com/vendo-aron/JAT-LeadQuali/issues/2) · parent epic [#1](https://github.com/vendo-aron/JAT-LeadQuali/issues/1) · plan [§12](../IMPLEMENTATION_PLAN.md)

These four questions change what gets built. Answering one costs a sentence; guessing one wrong
costs a phase. This record exists so each answer takes a minute, and so no answer is needed to
*start* — every open question below carries a default that work proceeds under, plus the price of
reversing that default later.

**To answer:** reply on #2 with one sentence per question. Then update the row below, and fold the
answer into plan §12 and the decision table on #1 (deliberately not edited from here — parallel
work is in flight on those files).

## Decision record

| Date | Question | Decision | Owner | Needed by | Consequence |
|---|---|---|---|---|---|
| 2026-09-01 | Historical lead data with outcomes? | **No — none exists** | aron@vendoworks.com | — (answered) | #22 stays in Phase 3; #10 rubric is authored, not calibrated; #19 feedback links become the only ground truth this product will ever have |
| — | Expected lead volume, now and in 12 months? | **OPEN** — proceeding under default D1 | aron@vendoworks.com | Phase 4 kickoff (#26); immediately if >5,000/day, which reopens plan §8 before Phase 1 | Sets whether SQS/RDS Proxy (#26, #27) are built or deferred, and whether the §8 cost model survives |
| — | CRM write-back in v1? | **OPEN** — proceeding under default D2 | aron@vendoworks.com | Phase 2 kickoff (#19), ideally before the first migration (#15) | Decides whether the feedback loop — the only ground truth — actually closes |
| — | Named owner of the ICP definition? | **OPEN** — proceeding under default D3 | aron@vendoworks.com | Phase 1, before #10 starts (earliest of the three) | Sets the ceiling on rubric quality, with no data available to correct a vague ICP |
| — | *(follow-up)* Do raw historical submissions exist, outcomes aside? | **OPEN** — proceeding under default D0 | aron@vendoworks.com | Phase 3 (#22) | If yes, #22 can be labeled before launch instead of growing from zero live traffic |

---

## 1. Historical lead data with outcomes — ANSWERED 2026-09-01: there is none

**Recorded:** no archive of past leads with closed/won, contacted-and-dead or ignored outcomes
exists anywhere in the business.

**Consequences, now locked in:**

- **The golden set (#22) stays in Phase 3.** It cannot move into Phase 1, because there is nothing
  to move. It grows from live traffic via the `feedback` table, so it is thin until Phase 4 has
  been running for weeks. Treat the first #23 numbers as directional, not authoritative.
- **The rubric (#10) is authored, not calibrated.** Weights and tier thresholds in #8 are a
  considered guess by a human, not a fit to observed outcomes. The plan called calibration "the
  single biggest quality improvement available" — it is off the table.
- **#19 is the only ground truth the product will ever have.** The one-click good/bad links in the
  routing email are not a nice-to-have; they are the entire labeling pipeline. If those links go
  unclicked, #22, #23 and #24 have nothing to work with and the resale story has no quality
  evidence behind it. This is what makes question 3 (CRM write-back) urgent rather than cosmetic.
- **The #13 phase gate and the #30 shadow period are doing real work.** Human review of CLI output
  before Phase 2, and shadow-mode comparison before cutover, are the *earliest* quality signals
  that exist. They are not formalities to rush past.

**Follow-up still open (D0).** *No outcomes* is not *no leads*. Labeling a lead with the tier it
**should** have received is a judgement call that does not require knowing what actually happened.
So: do raw form submissions exist anywhere — an inbox, a spreadsheet, a form-provider export? If
yes, #22 can be assembled and labeled before launch, which is far better than starting at zero;
you only lose the ability to check labels against what really closed.

> **Default D0 — assume nothing exists.** #22 starts collecting from the first live lead in #30.
> **Cost to reverse:** none. A found export can be labeled and added to `golden_leads.jsonl` at any
> time; the file is append-only by design.

---

## 2. Volume — OPEN

**The question, answerable in one sentence:** *How many leads per day does the form take today, and
what do you expect in twelve months?* (Two numbers. Order of magnitude is enough — "about 20 now,
maybe 100 next year" is a complete answer.)

### Why it blocks engineering

The whole async architecture in plan §3 exists to absorb volume the browser cannot wait on. Below a
certain rate, half of it is machinery guarding against a load that will never arrive; above another
rate, the economics in §8 stop being true.

| Answer | What changes |
|---|---|
| **Under ~50/day** | #26 loses SQS, the DLQ and the second Lambda. #27 loses RDS Proxy. #29's DLQ-depth alarm changes shape. #17's response contract may change. ~1 day off Phase 4 and a recurring AWS line item removed. |
| **~50–5,000/day** | Nothing changes. The plan as written is correct in this band; it is the band it was designed for. |
| **Over ~5,000/day** | Plan §8's cost model is wrong by two orders of magnitude and must be rewritten before Phase 1. #24's effort sweep stops being tuning and becomes a margin lever, moving ahead of Phase 4. #27's instance size and concurrency caps are both wrong. #3's Anthropic spend limits and rate-limit tier need raising. #33's per-lead margin becomes the central number in the resale story. |

### The thresholds, concretely

**Below ~50 leads/day (~1,500/month).** Peak concurrency is a lead every few minutes; SQS is
absorbing a burst that does not exist. The simpler architecture, stated precisely:

- API Gateway → **one** ingest Lambda. It verifies the HMAC signature, schema-validates, runs the
  deterministic spam pre-filters, persists the raw lead (`status=received`), and returns 202.
- Instead of enqueueing to SQS, it **asynchronously invokes itself** (Lambda event invocation) in
  worker mode. Async invocation carries built-in retries and an **on-failure destination**, so the
  never-drop-a-lead invariant survives without a queue — the destination is the DLQ, and #29's
  alarm points at it instead. This preserves invariant 3 of the epic; qualifying synchronously
  inside the request and making the visitor's browser wait several seconds does not, because a
  gateway timeout would lose the lead outright. Synchronous inline qualification is acceptable
  *only* if the form posts by `fetch()` with a spinner and the lead is already persisted before the
  model is called.
- **No RDS Proxy.** Cap the worker's reserved concurrency at 5, open one connection per container,
  connect directly to `db.t4g.micro`. Revisit when sustained concurrency approaches 50.
- The `ports.py` seam (#14) is unchanged either way, so adding the queue later is an infrastructure
  change and a handler split — not a rewrite of the pipeline.

**Above ~5,000 leads/day (~150,000/month).** Plan §8 quotes ~$0.02–0.03 per lead and **~$25/month
at 1,000 leads/month**. At 5,000/day that same arithmetic gives **~$3,000–4,500/month** in model
spend alone. Everything downstream of "$25/month is a rounding error" stops being true: the
pre-filter/caching/effort ladder in §8 becomes the difference between a viable and an unviable
resale price, #24 moves before Phase 4, #27's `db.t4g.micro` and concurrency caps are both
undersized, and Anthropic per-minute rate limits (not cost) become the binding constraint — which
makes SQS backpressure mandatory rather than merely prudent.

> **Default D1 — assume 50–500 leads/day and build the queued architecture exactly as planned.**
> Additionally, set the Anthropic spend limit (#3) and the CloudWatch daily-spend alarm (#29) at
> **3× the §8 figure (~$75/month)**, so a wrong volume assumption surfaces as an alarm within a day
> rather than as an invoice at month end.
>
> **Why this is the conservative direction:** the failure modes are asymmetric. Building the queue
> at 20 leads/day costs about a day of Phase 4 work and a small recurring AWS bill, and deleting it
> later is deleting infrastructure. *Adding* a queue after cutover means changing the ingest
> contract from synchronous to 202 while live traffic flows, and re-proving the never-drop
> invariant under load — with real leads at risk while you do it.
>
> **Cost to reverse:** downward (it turns out to be 20/day) ≈ 1 day, no data risk. Upward (it turns
> out to be 10,000/day) ≈ the §8 rewrite plus an RDS resize, done under load.

---

## 3. CRM write-back in v1 — OPEN

**The question, answerable in one sentence:** *Do the reps work leads inside a CRM — and if so,
which one — such that an email to a shared sales inbox will in practice go unread?*

### Why it blocks engineering

Email-only is the chosen v1 routing action, and #19 hangs the feedback links off that email. Since
question 1 was answered *no historical data*, those links are the **only** source of labels this
product will ever have. So this is not a routing-convenience question; it is the question of
whether the feedback loop closes at all.

If reps live in a CRM and the routing email lands in an inbox nobody reads:

- #19's links are never clicked → the `feedback` table stays empty.
- #22 has no golden set to grow → #23 has nothing to measure → #24 tunes against noise.
- The epic's definition of done ("the `feedback` table is growing the golden set weekly") is
  unreachable, and the resale story has no quality evidence.
- The failure is **silent**. Nothing errors. Dashboards stay green. You find out in Phase 3 when
  the eval harness has nine labeled leads.

If the answer is yes, the changes are: file a CRM-adapter issue and schedule it **in Phase 2
alongside #19, not Phase 5**; add a nullable `crm_record_id` to the leads table in the **first**
migration (#15); store per-tenant CRM credentials in #28's Secrets Manager; and #31 grows a
per-tenant CRM config block.

### What a CRM adapter actually costs, given the ports/adapters split

Plan §6 exists for exactly this: the pipeline (#14) dispatches through a `Notifier` Protocol, so a
CRM write-back is **one new file in `adapters/`**, a config field, and a credential — no change to
`domain/`, no change to `qualify.py`. Estimate **1–2 days of code** to create-or-update a
contact/deal and write score, tier, reasoning and the feedback links onto it as properties.

The expensive parts are not code:

- **External wait time.** An OAuth app registration or API credential in the customer's CRM, plus
  field mapping onto their schema. This behaves like SES (#20): start it early or it blocks.
- **Reading feedback back out.** Writing *into* a CRM is easy; capturing the rep's good/bad
  judgement *from* it means a custom field plus a webhook or a poll. Budget for that separately —
  it is the half that actually feeds #22.
- **Idempotency.** `crm_record_id` must exist from the first migration. Adding a column later is a
  routine migration; changing idempotency semantics on a table that already has rows is a data
  repair.

> **Default D2 — ship email-only for v1, exactly as planned, with two hedges that are free now:**
> (a) #19's feedback links are signed, standalone URLs that work from anywhere they are pasted —
> email, Slack, or a CRM note — so a manual bridge exists on day one; (b) `ports.Notifier` stays a
> Protocol and the dispatch step takes a **list** of notifiers, so a CRM becomes an addition rather
> than a substitution. Add nullable `crm_record_id` to #15's first migration regardless: an unused
> column is free, and a later migration on live data is not.
>
> **Cost to reverse:** the adapter itself stays cheap (1–2 days) whenever it arrives. What does not
> come back cheaply is calendar time. Every week of live traffic with an ignored routing email is a
> week of unlabeled leads. Raw leads are stored, so a human can label a backlog retroactively — but
> that costs paid labeling hours and produces worse labels than a rep's in-the-moment judgement,
> because nobody remembers in November whether lead #412 was worth the call.

---

## 4. Named owner of the ICP definition — OPEN

**The question, answerable in one sentence:** *Which named person owns the ICP definition and can
book roughly six hours in Phase 1 to author it and review the first scored output?*

"Sales owns it" is not an answer. A ticket is not an answer. This is a person with time on a
calendar.

### Why it blocks engineering

- **#10 (rubric prompt v1) lists #2 as a dependency in the epic's own table.** It cannot start
  without this. It is the earliest of the three open questions to bite — Phase 1, not Phase 4.
- **#8 (`TenantConfig`)** needs the dimension weights and the tier thresholds. Those are numbers
  someone has to choose and defend; engineering can supply the plan's defaults (hot ≥ 80, warm
  55–79, cold 30–54, disqualified < 30) but not justify them for this business.
- **#13's phase gate is a review with the ICP owner.** No owner, no gate — and the gate is the
  point of Phase 1 producing something judgeable before any infrastructure exists.
- **#22 labeling and #24 tuning** are the same judgement, ongoing: "is this false-hot acceptable?"
  is an ICP call, not an engineering one, forever.

### The threshold, concretely

Because question 1 was answered *no historical data*, **the rubric's ceiling is the ICP's
articulation quality**. With calibration data, a vague ICP gets corrected by the numbers; without
it, a vague ICP is simply what the product believes, indefinitely. That is why this needs a person
rather than a process.

The commitment, so it can be booked rather than agreed to in the abstract:

- **~3 hours, once, before #10:** segments in and out, hard disqualifiers, buying signals worth
  points, deal-size floor, and the two or three real past customers the ICP is drawn from.
- **~2 hours at the #13 gate:** read ~20 scored leads and say where the scoring is wrong.
- **~30 minutes weekly from Phase 4:** label the week's feedback for #22.

> **Default D3 — aron@vendoworks.com is the ICP owner until someone else is named.** #10 proceeds
> with a rubric authored from existing sales and marketing collateral plus recalled won deals,
> shipped as `rubric_v1` and flagged in the issue as provisional. Plan defaults stand for weights
> and thresholds.
>
> **Cost to reverse:** cheap in the artifact, expensive in the ground truth. The rubric is
> configuration (#8), so rewriting it is a config change, not a deploy. But every lead assessed
> under a wrong ICP generates feedback labels *against the wrong standard*, so #22 inherits the
> error — and #23's metrics are not comparable across rubric versions whose underlying ICP changed,
> which resets the eval baseline. Naming the right person in week one is the cheapest correction
> available; naming them in Phase 3 costs the golden set.
