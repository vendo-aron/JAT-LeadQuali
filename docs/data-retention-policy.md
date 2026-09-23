# Data retention policy

How long LeadQuali keeps a lead's personal data, what survives after it is gone, and the
job that enforces it. Issue #37.

This is written for a security reviewer and for whoever answers a customer's questionnaire.
Every number here is a default in the code, not an aspiration: `tenants.raw_retention_days`
and `tenants.assessment_retention_days` are columns, a daily job reads them, and
`tests/unit/test_retention.py` fails if the job stops honouring them.

Related: [`docs/deletion-requests.md`](deletion-requests.md) (an individual asking for their
data to be erased), [`docs/security-overview.md`](security-overview.md) (encryption, access,
backups), [`docs/dpa-draft.md`](dpa-draft.md) (the contractual version of all of this).

---

## What personal data we hold, and where

Four columns in the whole schema may contain personal data. They are listed in
`db_schema.PERSONAL_DATA_COLUMNS`, and `tests/unit/test_db_schema.py` classifies **every**
column in the database against that list, so a fifth cannot appear without somebody
deciding it should.

| Column | What it is | Retention |
|---|---|---|
| `leads.raw_payload` | The submission itself: name, address, phone, company, and whatever the person typed. The only verbatim copy anywhere. | **Tier 1** |
| `assessments.reasoning` | The model's prose about the lead. It routinely quotes the lead back. | Addresses redacted at the end of tier 1; the row goes at **tier 2** |
| `feedback.notes` | A sales rep's free text about the lead. Can name the person they spoke to. | **Tier 2** |
| `golden_promotions.note` | A staff rationale for promoting one lead into the eval set. | **Tier 2** |
| `stripe_events.payload` | A verified Stripe webhook body, stored verbatim (#35). An invoice object carries the billing contact's name, email and postal address. | **Not a tier — see [Billing records](#billing-records-a-different-subject-and-the-opposite-constraint)** |

The last row is a different kind of record from the four above it and must not be read as a
fifth lead column: its data subject is the **customer's own billing contact**, not somebody
who filled in a form.

Two columns are **pseudonyms** rather than personal data in the ordinary sense, and they are
treated separately below: `leads.contact_email_hash` and `erasure_log.subject_hash`.

Everything else — scores, tiers, token counts, costs, timestamps, tenant configuration,
routing destinations — contains nothing a lead submitted.

---

## The three tiers

### Tier 1 — the raw lead payload. Default **90 days**.

At the end of tier 1 the payload is replaced with a tombstone:

```json
{"redacted": true, "redacted_at": "2026-12-16T02:30:11.482000+00:00"}
```

Not `NULL`, and the difference matters to anybody reading the table: a null payload is
indistinguishable from a row some future writer got wrong, while a tombstone says *we did
this, on purpose, on this date*. It is also what the admin lead page renders instead of an
empty panel.

In the same pass, **addresses are redacted out of `assessments.reasoning`** for every lead
whose payload was tombstoned. This is not optional tidying. The model writes things like
*"the enquiry came from ada@example.com and mentions a Q4 deadline"*, so without it a lead's
address would sit in the assessment record for the remaining 21 months while the policy
document claimed it had been deleted on day 90.

Ninety days is the shortest window that still covers what the payload is actually needed for
after qualification: a sales conversation that started from the form, a customer asking
"what exactly did this person send us?", and a rubric complaint investigated a quarter later.

### Tier 2 — the assessment record. Default **730 days** (two years).

At the end of tier 2 the whole `leads` row is deleted. The composite
`(tenant_id, lead_id)` foreign keys are `ON DELETE CASCADE`, so its assessments, routing
events, feedback and golden promotions go with it, in the server, in one statement.

Two years because that is the window over which the rubric is tuned and conversion is
measured, and because it is long enough to settle a billing dispute about a period the
customer has already closed.

### Tier 3 — aggregates. **Indefinite.**

`usage_daily` (issue #33) and the CloudWatch metrics are counts and sums per tenant per day:
how many leads there were, not who they were. They are not personal data, nothing in the
retention job touches them, and they are what makes "our conversion rate in 2026" answerable
after every lead from 2026 has been deleted.

**Tier 1 expiring without tier 2 expiring is the entire point of the design.** A lead whose
payload has gone still has its score, its tier and its routing history, so last quarter's
analysis survives the person's data being destroyed.

---

## `contact_email_hash` is a pseudonym, not anonymisation

`leads.contact_email_hash` survives the tier-1 purge. It is SHA-256 of the address,
lowercased and stripped.

**It is not anonymous data, and we do not claim it is.** An email address has very little
entropy: anybody holding a wordlist and a list of candidate addresses can confirm whether a
given person is in our database by computing one hash. Under GDPR that makes it
pseudonymised personal data, still in scope, not data outside the Regulation.

It is kept for one reason, and it is a reason that benefits the data subject: **it is what
makes a deletion request answerable at all.** Without it, "do you hold anything about
this person?" after day 90 would mean a substring scan of every JSONB payload the tenant has
ever received — slower, less reliable, and no less revealing. The hash is also what lets a
log line be joined to a lead without the log ever carrying an address (invariant 5).

It is dropped with the lead row at the end of tier 2. There is no separate, longer life for
it.

The same applies to `erasure_log.subject_hash`, which outlives the lead by design: see
[`docs/deletion-requests.md`](deletion-requests.md).

---

## What redaction cannot do, stated plainly

The tier-1 redaction of `assessments.reasoning` removes **address-shaped text**. It knows
about non-ASCII and internationalised domains — `anna@müller-logistik.de` and
`olga@почта.рф` are redacted, and `tests/unit/test_observability.py` pins that — because the
pattern it uses is the same one the log formatter uses, and #37 widened it after finding it
was ASCII-only.

It does **not** remove:

- a person's **name** in the model's prose ("the VP of RevOps was clear about the timeline");
- a **phone number** or a postal address written into free text;
- a **company name**, which is often as identifying as a person's name for a one-person
  business;
- anything in `feedback.notes` or `golden_promotions.note`, which are deliberately left
  alone — they are a human's own words and the product's only training signal, and rewriting
  them would corrupt the feedback loop for text that is not reliably identifying anyway.

No regular expression can do those things, and a policy that claimed otherwise would be
worse than one that does not. **Tier 2 ending is what closes them**, which is the reason
tier 2 has an end at all rather than being "kept while useful".

If a customer needs those closed sooner, the lever is their `assessment_retention_days`, not
a cleverer redactor.

---

## Per-tenant overrides

Both windows are columns on `tenants`, so a customer who has negotiated something different
has it recorded where an auditor can read it:

```sql
SELECT slug, raw_retention_days, assessment_retention_days FROM tenants;
```

```bash
python -m leadquali.retentionctl policy acme-demo
python -m leadquali.retentionctl policy acme-demo --raw-days 30 --apply
```

The database enforces two rules, and it enforces them rather than the application, because a
row written by `psql` during an incident has to be as well-formed as one written by the
service:

- **both windows are positive.** Zero is not a policy, it is an outage: the next run would
  redact the lead being qualified at that moment.
- **`raw_retention_days <= assessment_retention_days`.** The tier split only means something
  in one direction. The other way round, the purge would delete the lead — payload and all —
  while the payload's own window still had months to run.

Changing a window is **not retroactive in the reassuring direction**: shortening it means
the next nightly run destroys everything that is now expired. There is no undo, and no
confirmation prompt beyond `--apply`. Shorten a window on a live tenant deliberately, and
run `retentionctl purge <slug>` (without `--apply`) first to see what it will take.

---

## The purge job

**Schedule.** An EventBridge rule fires `leadquali-<stage>-retention` daily at **02:30 UTC**
(`RetentionSchedule` in `infra/template.yaml`). That is before the database's 03:10 backup
window, deliberately: a night's deletions are then inside the snapshot that follows, rather
than being taken and then restored from backups for the next seven days.

**By hand.** `python -m leadquali.retentionctl purge [slug] [--apply]`. Dry run is the
default — neither `--apply` nor `--dry-run` reports counts and writes nothing.

**Batched.** Every destructive statement takes at most `--batch-size` rows (default 500,
maximum 10 000), ordered oldest first, with `FOR UPDATE SKIP LOCKED`. The job loops until a
pass does nothing. Three consequences worth knowing: the first run against a year of backlog
is a sequence of short transactions rather than one that holds locks for minutes; a run that
is cut off half way has deleted the most overdue data and the next run picks up where it
stopped; and a scheduled run and an operator running the same command during an incident do
not block each other.

**Idempotent.** The tier-1 statement excludes rows that are already tombstones, so the second
run reports zero and `redacted_at` is never rewritten — which is what keeps the date on a
tombstone an honest statement about when the data actually went.

**Loud on failure.** An exception propagates, the Lambda invocation fails, and #29's error
alarm fires. A retention job that swallowed an error would report success while personal
data stayed past its window.

**What it logs.** `retention.purged`, once per tenant per run and only when something was
destroyed: counts, and the tenant. No lead id, no cutoff that could be joined back to one, no
payload. A run that changed nothing emits nothing, because a daily no-op line trains
everybody to ignore the event.

---

## Backups

Deleting a row from the database does not delete it from the backups taken before the
deletion. RDS automated backups are retained for **7 days** by default
(`DbBackupRetentionDays` in `infra/network.yaml`), so the honest statement is:

> Personal data is removed from the live database within 24 hours of its retention window
> ending, and from the last automated backup within a further 7 days. Manual snapshots, if
> any are taken, are retained until deleted by hand.

This is the standard position and it is what a reviewer expects to hear; what they are
checking is whether you say it. The mitigation is that backups are encrypted with the same
customer-managed KMS key as the instance and are reachable only by the AWS account — see
[`docs/security-overview.md`](security-overview.md).

**A restore reintroduces deleted data.** If a database is restored from a snapshot older than
a purge, that purge has to be re-run. Do that as the last step of any restore:

```bash
python -m leadquali.retentionctl purge --apply
```

---

## Billing records: a different subject, and the opposite constraint

`stripe_events.payload` (#35) holds a verified Stripe webhook body, stored verbatim because
a payload pruned to the fields this build models is missing the ones an incident will want.
A Stripe invoice object carries the billing contact's name, email address and postal
address, so the column is personal data and is declared as such in
`db_schema.PERSONAL_DATA_COLUMNS`.

**It is not purged by the retention job, and that is a decision rather than an omission.**
`app.retention.COLUMN_DISPOSITION` says so in the code, and this is why:

- **The data subject is different.** Every other column above is about an inbound lead,
  whose controller is our customer. This one is about our *own* customer's
  accounts-payable person. We are the controller of it, not a processor.
- **The lawful basis is different**, and so is the shape of the constraint. An invoice
  record is a financial record. In most jurisdictions a company is *required* to keep them
  for a period measured in years — a statutory **minimum**, where everything else in this
  document is a policy **maximum**. Applying the 90-day lead window to it would destroy
  evidence we are obliged to hold.
- **It is therefore very likely out of scope for an erasure request**, on the usual
  analysis that the processing is necessary for compliance with a legal obligation. See
  [`docs/deletion-requests.md`](deletion-requests.md).

**What is true today:** `stripe_events` rows are retained indefinitely. No code path deletes
them, `retentionctl` does not touch them, and the tenant foreign key is `ON DELETE SET NULL`
so that closing a customer's account does not destroy their billing history either.

**What a lawyer must decide**, and it is item 10 on
[`docs/dpa-draft.md`](dpa-draft.md)'s list:

1. the **statutory minimum period** for our invoice records, in the jurisdiction we invoice
   from;
2. whether we should delete them once that period has elapsed, or keep them — "indefinitely"
   is the current behaviour and is not automatically the right answer, because a minimum is
   not a licence;
3. whether an erasure request from a billing contact reaches this column at all.

**Do not copy 90 or 730 into this.** When the period is set, it becomes a third window, a
`PurgedRecords` member and a step in the purge — the structure is already there and is held
together by a test that fails if a column that can hold personal data has no written
disposition.

> #35's [`docs/billing-integration.md`](billing-integration.md) says the retention job "must
> cover `stripe_events.payload` as it covers `leads.raw_payload`". That was written before
> anybody had worked out that the two constraints point in opposite directions. This section
> is the answer, and that line has been corrected to point here.

**What Stripe itself receives** is in
[`docs/billing-integration.md`](billing-integration.md) and is not restated here. The one
sentence worth repeating anywhere a customer can read it: **Stripe never receives lead
data.**

## Verifying this

```bash
pytest tests/unit/test_retention.py tests/unit/test_retention_postgres.py   # no database
docker compose up -d && export DATABASE_URL=...                            # see docs/local-database.md
pytest tests/integration/test_retention_postgres.py -m integration
```

The offline half carries the weight on purpose. It proves the tiers, the batching, the
idempotence, the redaction of the model's prose and the erasure receipt without a server,
because a property asserted only by a test that skips is a property nobody is checking.
