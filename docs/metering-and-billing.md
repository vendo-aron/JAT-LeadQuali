# Metering and billing

What we count, what we charge for, what we don't, and how we check our own numbers against
the bill we get from Anthropic. Issue #33.

The first half is written for a customer conversation — every rule in it is one we would be
happy to read out on a call. The second half is the operator's runbook.

---

## What a customer is charged for

Three numbers are recorded for every tenant, every day. They are different on purpose.

| | What it counts | Charged? |
|---|---|---|
| **Leads received** | Every submission your form sent us, spam included | No |
| **Leads assessed** | Every submission we ran through the qualification model | — |
| **Billable leads** | Every assessment that actually reached the model | **Yes** |

**You are charged for billable leads.** That is the third number, and it is the only one on
an invoice.

### Spam we filter out is free

Before anything reaches the model, every submission goes through a deterministic
pre-filter: obvious bot signatures, honeypot fields, test submissions. Anything it catches
is recorded — you can still see it — and is **not charged, and does not count against your
plan's monthly allowance**.

Three reasons, and the third is the one that matters most:

1. You cannot control who posts to a public form on your own website.
2. Charging for it would mean charging you for our filter doing its job.
3. It would give *us* a reason not to improve the filter. A pricing rule that rewards us
   for being worse at something is a bad rule however small the amounts are.

### A failed assessment is charged

If the model is asked and something goes wrong — it refuses, it times out after the request
was accepted, the response cannot be parsed — that lead is still billable. Two reasons:

1. We were charged for it. The tokens were spent on your lead.
2. **You still got the lead.** A failed assessment never drops a submission: it goes
   straight to a human with a note saying the system could not judge it. That is a product
   guarantee, not a best effort, and it is worth what a qualified lead is worth.

The number of failures is reported alongside the billable count, every day, so "how much of
what we were billed for actually failed?" is a question you can answer without asking us.

### A day is a UTC day

Usage is grouped into calendar days in UTC, for every customer, wherever you are. If each
customer's usage day started at their own local midnight, one nightly billing run would
count some customers twice at each month boundary and miss others. One definition is worth
more than a locally convenient one.

A day is not final until it is over. Figures for the day in progress are visible, and they
are labelled *partial*; nothing is ever invoiced from a partial day.

---

## Soft quotas

A plan can carry a monthly allowance (`monthly_lead_quota`) and an alert threshold
(`quota_alert_fraction`, 80% by default). Both are optional. A customer with no allowance
configured is unlimited, which is the default.

**Going over an allowance never stops anything.** No lead is refused, no assessment is
skipped, nothing is silently downgraded. Crossing the threshold produces:

- a `warning` or `exceeded` status in `usagectl quota`,
- a log event (`tenant.quota_crossed`) and a CloudWatch metric.

That is the whole mechanism. Going over your plan is a conversation and an invoice line; it
is not a technical event, and a system that turned it into one would be choosing to throw
away leads that a customer is willing to pay for. The status levels are:

| Level | Meaning |
|---|---|
| `ok` | Below the alert threshold |
| `warning` | At or past the threshold, up to and including the allowance |
| `exceeded` | Past the allowance — every lead is still being qualified |

Exactly at the allowance is `warning`, not `exceeded`: the thousandth lead of a
thousand-lead plan is included in the plan.

---

## Margin

`usagectl margin` reports revenue minus cost for one tenant and period.

**Revenue is `unknown` until #35 (Stripe) exists.** It is not estimated, defaulted or
filled in with a list price — a fabricated revenue number in a margin report gets quoted
and then priced from, and by that point nobody remembers where it came from.

Cost has two parts:

- **Inference** — summed from the per-assessment token cost. Real, per tenant, measured.
- **Infrastructure** — an **allocation, not a measurement**.

### Read this before quoting a per-tenant infrastructure cost

The monthly infrastructure bill (`docs/infrastructure-cost.md`, ≈ $139.60) is allocated
across tenants pro rata by billable leads. That is a convention chosen because it is simple
and explicable; it is not a measurement of what a customer costs us, and it cannot be.

Most of that bill is **fixed**. The RDS Proxy alone is $87.60/month and is spent whether
there is one tenant or fifty. So:

- a single tenant's "infrastructure cost" **falls as customers are added**, without
  anything about that tenant changing;
- with one customer, the allocation charges them the entire platform;
- a per-tenant margin computed this way is **only meaningful next to the fleet total**.

Every report that prints the number prints that caveat with it. Keep it that way.

---

## Operator runbook

All commands are `python -m leadquali.usagectl`. They need `DATABASE_URL` set (see
`docs/local-database.md`); none of them needs AWS or an Anthropic key.

### Daily rollup

```bash
python -m leadquali.usagectl rollup acme-demo
```

Recomputes every **closed** day of the current month for that tenant and replaces the rows
in `usage_daily`. Run it once a day, after midnight UTC.

It is **idempotent**: a day is recomputed from `leads` and `assessments` and written as a
whole row, never incremented. Consequences worth knowing:

- Running it twice changes nothing but the `computed_at` stamp.
- If the job was broken for a week, the next run repairs the whole week — there is no
  separate backfill.
- If a lead was redelivered late and landed in a day already rolled up, re-running that day
  corrects it. This is the normal repair, not an exceptional one.
- To rebuild history after a migration or an import:
  `usagectl rollup acme-demo --from 2026-01-01 --to 2026-08-31`.

### Reading usage

```bash
python -m leadquali.usagectl usage acme-demo --month 2026-09
python -m leadquali.usagectl usage acme-demo --month 2026-09 --json
python -m leadquali.usagectl usage acme-demo --daily
python -m leadquali.usagectl quota acme-demo
python -m leadquali.usagectl margin acme-demo --month 2026-09
```

Reads come from `usage_daily` and never scan `assessments`, so a billing read costs the
same in month one and in year three. Add `--include-today` to see the day in progress; it
is marked `(partial)`, and an invoice must not come from it.

In `--json`, **every money figure is a string** (`"0.054000"`, not `0.054`). A JSON number
is a double by the time anything has parsed it, and a billing figure that has been through
binary floating point can no longer be reconciled with one that has not.

### Putting a tenant on a plan

```bash
python -m leadquali.usagectl set-quota acme-demo --quota 2000
python -m leadquali.usagectl set-quota acme-demo --quota 2000 --alert-fraction 0.9
python -m leadquali.usagectl set-quota acme-demo --unlimited
```

A plan is a property of the tenant, not of a month, so this command takes no period. Every
tenant starts unlimited and `--unlimited` puts them back. A quota of zero is refused: that
is a suspension, and `tenantctl suspend` is the command for it.

### Reconciling against the Anthropic invoice

**This is a monthly manual task and it is the owner's.** Nothing in this repository can
reach the Anthropic console.

1. On the first working day of the month, open the Anthropic Console → **Usage** (or
   **Billing → Usage**) for the workspace this deployment uses.
2. Export the previous month's usage as CSV, grouped **by day**. The tool accepts the
   console's usual column names (`date`/`usage_date`, `input_tokens`/
   `uncached_input_tokens`, `output_tokens`, `cache_read_tokens`/
   `cache_read_input_tokens`, `cache_creation_tokens`/`cache_creation_input_tokens`,
   `cost_usd`/`amount_usd`) and sums rows that share a date, so a per-model export is
   fine. If the columns do not match, it fails and prints the columns it did find.
3. Run:

   ```bash
   python scripts/reconcile_spend.py anthropic-2026-09.csv --month 2026-09
   ```

   It sums our `usage_daily` **across all tenants** — Anthropic bills the workspace, not
   the customer — and prints a per-day and total variance in dollars and percent.

4. **Exit 0** means the totals agree within 2% (`RECONCILIATION_TOLERANCE`). **Exit 1**
   means they do not, and no customer should be invoiced from our figures until the
   difference is explained. Run it from a cron so a silent drift cannot accumulate.

#### What legitimately causes drift

- **Our rate card is a snapshot.** `CLAUDE_OPUS_5_PRICES` in
  `src/leadquali/adapters/llm_anthropic.py` was written down on a date and Anthropic's
  published prices change. This is the first thing to check, and the fix is to update that
  constant and re-run the rollup for the affected range.
- **The console rounds.** We keep six decimal places per call.
- **A call we gave up on may still have been billed.** A client-side timeout does not stop
  the server from having served the request.
- **Cache-write pricing depends on the TTL requested.** We only write 5-minute entries
  today; a change there moves the number.
- **Anything else is a bug**, and it is a bug in the direction of money.

If the variance is outside tolerance and none of the above explains it, do not widen the
tolerance. `--tolerance` exists for a deliberate, documented one-off (a mid-month rate
change, say), not for making the alarm quieter.

---

## Where the numbers live

| | |
|---|---|
| The rule, and the arithmetic | `src/leadquali/app/metering.py` |
| The SQL | `src/leadquali/adapters/metering_postgres.py` |
| The rollup table | `usage_daily`, migration `a3f5c2b81d47` |
| The commands | `src/leadquali/usagectl.py`, `scripts/reconcile_spend.py` |
| The rate card | `CLAUDE_OPUS_5_PRICES` in `src/leadquali/adapters/llm_anthropic.py` |
| Infrastructure cost | `docs/infrastructure-cost.md` |

When #35 reports usage to Stripe, it must read `usage_daily` through
`MeteringService.usage_for_period` rather than recomputing from `assessments`. Two
independent definitions of "billable" is how an invoice and a usage report start disagreeing
with each other in front of a customer.
