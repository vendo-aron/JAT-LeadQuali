# Deletion requests: the runbook

Somebody has asked for their personal data to be deleted. This is what to do, in order, and
what to send back. Issue #37.

Once a DPA is signed this stops being a courtesy and becomes a contractual obligation with a
clock on it, so the two halves that matter are **doing it** and **being able to prove you
did**. The commands below produce both.

Related: [`docs/data-retention-policy.md`](data-retention-policy.md) (what is held and for
how long), [`docs/dpa-draft.md`](dpa-draft.md) (the contractual wording).

---

## Who the request comes from, and to whom

Almost every request will arrive **through the customer**, not from the individual. That is
the right shape: for an inbound lead, our customer is the **controller** and LeadQuali is the
**processor**. The person's relationship is with the company whose form they filled in.

So the normal flow is: the individual asks our customer, our customer asks us, we act on
their instruction and give them evidence they can pass on.

**If an individual contacts us directly**, do not act on it as if it were a controller's
instruction. Answer within one working day saying (a) we process their data on behalf of the
company whose form they used, (b) we have told that company, and (c) they can also ask the
company directly. Then tell the customer. Acting unilaterally on a stranger's email is how
you delete the wrong person's data on the word of somebody who is not them.

> **For legal review:** whether we may act directly on a data subject's request, and what we
> must do when a controller does not respond, is a question about the DPA's wording and about
> the applicable law. The paragraph above is our intended operating position, not advice.

---

## Step 0 — verify the requester

You are about to permanently destroy data on somebody's say-so. Verification is the whole
control; there is no undo.

- **From a customer**: the request must come from a named contact on their account, through
  a channel you already use with them (their ticketing system, or a reply within an existing
  thread). A new email address claiming to be their DPO is not verification.
- **From an individual**: do not act. See above.
- **Record how you verified it.** The `--requested-by` argument below is not decoration; it
  is the answer to "on whose authority was this data destroyed?" when it turns out to have
  been a mistake. Use a ticket reference: `SUP-4471`, `acme/2026-09-17/dsr-3`.

---

## Step 1 — find out what is held. This deletes nothing.

```bash
python -m leadquali.retentionctl find acme-demo --email someone@example.com
```

```
tenant:              acme-demo
subject (SHA-256):   94900108ee10dc3dd29dbb03f53140ba968f4f185b6af0a18eb2624240065e88

  leads:             2
    found by hash:   1
    found by scan:   1
  assessments:       2
  routing events:    2
  feedback:          1
  golden promotions: 0
```

Two nets are cast, and the difference between them is worth understanding:

- **found by hash** — leads whose `contact_email_hash` is this person. The indexed lookup,
  and the ordinary case.
- **found by scan** — leads where the address appears *somewhere else in the payload*: a
  "please cc my colleague" note, a second contact box, a message that quotes the sender. This
  is a substring match over the whole JSON document for one tenant, with no index. It is slow
  by design and that is the right trade.

A non-zero **found by scan** is worth reading rather than filing. It means that tenant's form
collects addresses in a field nobody modelled, which is a conversation to have with them.

`find` is always read-only. Run it first, every time.

---

## Step 2 — carry it out

```bash
python -m leadquali.retentionctl erase acme-demo \
    --email someone@example.com \
    --requested-by SUP-4471 \
    --apply \
    --receipt ~/receipts/SUP-4471.txt
```

Without `--apply` this is a dry run: it prints exactly what `find` prints and deletes
nothing.

What happens, in one transaction: the lead rows go, and the composite `ON DELETE CASCADE`
takes their assessments, routing events, feedback and golden promotions with them. The
`erasure_log` row is written in the **same** transaction, so there is never a deletion nobody
can prove or a proof of one that did not happen. If it fails, nothing happened and you re-run.

Erasure is **per tenant**. The same person can be a lead of two of our customers, and each
controller may only erase their own copy. Run the command once per tenant, with a separate
instruction from each.

---

## Step 3 — what you send back

The **receipt** is the artifact. It is printed, and written to `--receipt` if you gave one:

```
LeadQuali erasure receipt
  tenant:              acme-demo
  subject (SHA-256):   94900108ee10dc3dd29dbb03f53140ba968f4f185b6af0a18eb2624240065e88
  completed at:        2026-09-17T09:00:00+00:00
  requested by:        SUP-4471

  leads deleted:       2
    found by hash:     1
    found by scan:     1
  assessments:         2
  routing events:      2
  feedback:            1
  golden promotions:   0

No email address appears in this receipt. The hash above is SHA-256 of the
address lowercased and stripped, and can be recomputed to check this is
the right subject.
```

**The receipt never contains the address.** It gets logged, filed and forwarded, and an
address in it would be a disclosure made by the very act of proving a deletion. The
controller can recompute the SHA-256 from the address they already hold and satisfy
themselves it is about the right person:

```bash
printf '%s' "someone@example.com" | tr 'A-Z' 'a-z' | sha256sum
```

Send the controller the receipt. Do not send them the lead ids: they identify rows in our
database and mean nothing to them.

**The durable half is the `erasure_log` row**, which is in the database, is not editable by
the CLI, and survives the customer's account being closed (its foreign key to `tenants` is
`RESTRICT`, unlike every other table hanging off a customer). The receipt is a rendering of
that row. If an auditor asks for evidence rather than a printout:

```sql
SELECT completed_at, subject_hash, leads_deleted, assessments_deleted,
       routing_events_deleted, feedback_deleted, golden_promotions_deleted,
       matched_by_hash, matched_by_payload_scan, requested_by
FROM erasure_log
WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'acme-demo')
ORDER BY completed_at DESC;
```

There is also a log line, `retention.erased`, carrying the same counts and the same hash. It
is the third copy and the least durable; use it to answer "when did we do this?" from a log
search.

---

## The SLA

**One calendar month from receipt of a verified request**, which is what GDPR Article 12(3)
allows a controller. Our customer's clock started before ours did, so in practice:

| | Target |
|---|---|
| Acknowledge the customer's instruction | 1 working day |
| Run `find` and report what is held | 3 working days |
| Complete the erasure and send the receipt | **5 working days** |

Five days rather than a month because the work is one command and because our customer needs
the remainder of their own month. The technical work takes seconds; the time is verification
and somebody being available.

> **For legal review:** the one-month figure, whether it can be extended, and what our
> contractual undertaking to a customer should actually say are for whoever signs the DPA.
> The five-day internal target is an operational choice and can be changed without legal
> input; the month cannot.

---

## The cases that come up

### The same person appears under two addresses

`ada@example.com` and `ada.lovelace@example.com` are two subjects as far as the hash is
concerned, because the hash is of the normalised address and nothing else. There is no
"same person" concept anywhere in the system, and inventing one — matching on name, or on
the local part, or on the domain — would be guessing about somebody's identity in order to
delete their data.

**Ask the controller for every address, and run the command once per address.** Each run
produces its own receipt and its own audit row. Send all of them.

If the controller asks you to find the others: the payload scan in step 1 will surface leads
where a *given* address appears anywhere, so if they name the second address you will find it
there too. It cannot find an address nobody has named.

Gmail-style dots and `+tag` suffixes are **not** normalised away. `ada.lovelace@gmail.com`
and `adalovelace@gmail.com` reach the same mailbox and hash differently. That is deliberate —
the normalisation rules are per provider and guessing them wrong deletes somebody else's
lead — but it means "the address they gave you" may not be "the address in the form". Ask.

### The retention job got there first, and we hold nothing

This is a good outcome and it is still a request you have to answer. **Run the command
anyway, with `--apply`.**

An erasure that finds nothing writes an `erasure_log` row with zero counts and returns a
receipt saying so. That is the point: *"we checked on 17 September 2026 and held no data
about this subject"* is only evidence if it was written down at the time. A reply that just
says "nothing to do" is an assertion; a receipt with a date on it is a record.

The correct wording to the controller is **"no data held"**, not "already deleted" — you do
not know whether this person ever filled in their form, and you should not speculate.

### The lead's payload has been redacted but the row is still there

Between day 90 and day 730 a lead exists with a tombstoned payload and a surviving
`contact_email_hash`. `find` will report it, and `erase` will delete it. That is correct: the
row is still pseudonymised personal data (see the retention policy), and the request covers
it.

### They ask for a copy of their data instead of deletion

That is an access request, not an erasure request, and this runbook does not cover it. The
data is visible in the staff admin (`/admin/leads/<id>`), which renders the payload to a
human by design. **Do not export it from there and send it without the controller's
instruction** — same reasoning as step 0.

### The requester is a billing contact rather than a lead

Someone at a **customer company** — the person named on the invoices — asks us to delete
their data. This is a different subject with a different lawful basis, and the answer is very
likely **not** the same.

What we hold about them:

- their name and email on the `tenants` row, as the account contact;
- **`stripe_events.payload`** — every verified Stripe webhook body, stored verbatim. A Stripe
  invoice object carries the billing contact's name, email and postal address, so this is the
  substantial one;
- whatever Stripe itself holds as the processor of their payment data, which is theirs to
  answer about and is described in [`docs/billing-integration.md`](billing-integration.md).

**`retentionctl find` and `retentionctl erase` do not look at any of this.** They search
`leads` and delete leads; they have no code path that reaches `stripe_events` or `tenants`.
Running them for a billing contact will correctly report "no data held" **about that person
as a lead**, which is a true statement about a different question and must not be sent as if
it answered theirs.

**Do not delete a billing record on request.** `stripe_events.payload` is a financial record.
The usual analysis is that keeping it is necessary for compliance with a legal obligation, so
the right to erasure does not reach it — and a statutory minimum retention period, measured
in years, normally *requires* us to keep it. See
[`docs/data-retention-policy.md`](data-retention-policy.md).

The safe interim answer, which does not need a lawyer to send:

> We have removed you from marketing and operational contact. The invoice records naming you
> are retained because we are required to retain them; we will confirm the exact legal basis
> and the retention period in writing.

Then escalate. Do not improvise the basis or the period in the reply.

> **For legal review, and it is the most important item in this document:** the statutory
> retention period for our invoice records in the jurisdiction we invoice from, and whether
> an erasure request reaches `stripe_events.payload` at all. Items 10 and 11 on
> [`docs/dpa-draft.md`](dpa-draft.md)'s list.

Deleting the `tenants` row itself is **closing the account**, which is a commercial act and
not a data-subject request. Nothing in this codebase deletes a tenant row, and the database
refuses it while any lead, usage report or erasure record still references it.

---

## What this cannot reach

Say these plainly rather than being asked.

- **Backups.** A restore from a snapshot taken before an erasure reintroduces the data.
  Automated backups are kept 7 days. Re-run the purge and any outstanding erasures as the
  last step of any restore. There is no way to selectively edit a snapshot and anybody who
  tells you otherwise is selling something.
- **The model provider.** The lead's free text was sent to Anthropic's API for assessment.
  What we can say about that is in [`docs/dpa-draft.md`](dpa-draft.md); what we cannot do is
  issue a deletion instruction into their systems from this command.
- **Email already sent.** A routing email went to the customer's sales inbox with the lead's
  details in it. That mailbox is the customer's, not ours, and it is theirs to clear.
  `routing_events` records that we sent it, and that row is deleted; the message is not.
- **The golden set.** If the lead was promoted into `tests/evals/golden_leads.jsonl`, that
  file is in git and the erasure does not touch it. The line is pseudonymised by #22's
  `strip_pii` before it is committed — the address is rewritten to an `.invalid` domain and
  free text is scrubbed — but it is a rewrite, not a deletion, and a determined reader who
  already knows the lead could recognise it. `golden_promotions` records which leads were
  promoted, so after an erasure that table will tell you which case ids to review.
  **If a request covers a promoted lead, say so to the controller and remove the line from
  the file in a separate commit.** There is no command for this and there should not be:
  editing a committed training set is a reviewed change.
- **Logs.** No lead's address or free text is in the logs at all — that is invariant 5,
  enforced by `tests/unit/test_pii_log_sweep.py` across every event the system emits. What is
  there is the `contact_email_hash`, in lines about a lead that no longer exists. Log groups
  expire on their own retention; there is no per-subject deletion from CloudWatch Logs and
  none is needed, because there is nothing there to delete.
