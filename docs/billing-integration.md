# Billing integration (Stripe)

How money moves through this system, why each piece is where it is, and the two procedures
— the test-mode lifecycle and the production cutover — that a person has to run by hand.

`docs/metering-and-billing.md` (#33) is the other half of this document: it defines what a
billable lead *is*. This one is about what we do with that number.

> **Not verified against a live Stripe account.** Everything below was built and tested
> offline: there is no Stripe API key and no Stripe CLI in the development environment. The
> parameters we send are asserted against a fake client, the signature construction is
> asserted byte-for-byte against the installed SDK's own implementation, and the lifecycle
> is driven through the real handlers with hand-built event fixtures. What *cannot* be
> proved here is that Stripe accepts them. The procedure in
> [Test-mode lifecycle](#test-mode-lifecycle-owner-verified) is the owner's to run.

## The shape of it

```
Stripe ──webhook──▶ POST /webhooks/stripe ──▶ stripe_events (pending) ──▶ 200
                     verify signature                    │
                     insert, do nothing on conflict      │  every minute
                                                         ▼
                                        billing_jobs.process_events_handler
                                          └─▶ tenants.status / dunning_until

usage_daily (#33) ──▶ billing_jobs.report_usage_handler ──▶ Stripe meter event
   leads_billable        daily, yesterday only              └─▶ usage_reports

tenants.dunning_until ──▶ billing_jobs.sweep_dunning_handler ──▶ tenants.status
                             daily
```

| Module | What it owns |
|---|---|
| `app/billing.py` | Every decision. No SDK, no SQL. |
| `adapters/billing_stripe.py` | The **only** module that imports `stripe`. |
| `adapters/store_billing.py` | `stripe_events`, `usage_reports`, the tenant columns. |
| `api/stripe_signing.py` | Webhook signature verification — standard library only. |
| `api/webhooks.py` | `POST /webhooks/stripe`, `POST /billing/portal`. |
| `api/billing_jobs.py` | The three scheduled Lambda entrypoints. |

## Meter events, not usage records

Stripe has changed its metered-billing API once already: usage records posted against a
subscription item, and then meter events aggregated server-side. **In the installed SDK
(`stripe` 15.6.1) there is no choice left to make.** `stripe.UsageRecord` does not exist and
neither does `SubscriptionItem.create_usage_record`; `stripe.billing.MeterEvent` and
`client.v1.billing.meter_events` do. `tests/contract/test_billing_stripe_contract.py`
asserts both halves of that, so the day a successor appears, a test tells us.

The choice is behind `BillingPort.report_usage` anyway. That is not hedging — it is the
direct lesson of Stripe having moved this once: switching is one file's diff.

## Idempotency, and exactly how far each half goes

Double-reporting overbills a customer. The issue says that is worse than under-reporting,
and it is right: an under-reported day can be reported tomorrow, while an over-reported one
has already reached an invoice. So there are two mechanisms and they are independent.

1. **`usage_reports`, unique on `(tenant_id, usage_date)`.** This is the durable guarantee.
   A day already present is skipped before anything is sent, and the insert is
   `ON CONFLICT DO NOTHING` so two overlapping runs cannot both record it.
2. **A deterministic `identifier` on the meter event**, `uuid5(namespace, "<tenant>:<date>")`.
   This is a backstop, and the SDK is explicit about its limit: uniqueness is enforced
   *"within a rolling period of at least 24 hours"*, to address *"issues arising from
   accidental retries"*. It will stop a job that ran twice in an hour. It will **not** stop
   a day being billed again a week later — so restoring the database from a backup that
   predates a billing run, or a downgrade of #35's migration, really can double-bill, and
   the migration's `downgrade()` says so where somebody will be standing when they need it.

The quantity sent is `leads_billable` from #33's rollup: **distinct leads** with at least one
assessment attempt that cost input tokens. Not `leads_ingested` (which counts spam we filter
and do not charge for) and not `leads_assessed` (which counts a redelivered lead once per
attempt, so a dispatch failure of *ours* would bill the customer three times).
`tests/unit/test_billing.py` asserts the reported number equals the rollup's `leads_billable`
for the same period, read back through `MeteringService.usage_for_period` — two different
code paths compared against each other, which is the only thing that keeps them from
drifting.

**An open day is never reported.** `usage_for_period` defaults to closed days only, and the
service relies on that *default* rather than passing the argument, so the default cannot be
weakened in one place without being weakened everywhere.

**Backfill has a hard edge.** Stripe's meter events take a timestamp "within the past 35
calendar days". `report_usage_for_day` refuses an older day (`UsageReportOutcome.TOO_OLD`)
rather than sending one Stripe will reject and recording it as done.

## Webhook verification

Verified with the standard library, in `api/stripe_signing.py`, and the `stripe` SDK is kept
out of that path entirely. Three reasons, all in the module docstring: the signature covers
the raw bytes and must be checked *before* parsing; a malformed request from a stranger then
costs an HMAC and no third-party code; and it is testable with fixtures and no network.

The construction was read out of the SDK's own `_webhook.py`, not from documentation, and
`tests/contract/test_billing_stripe_contract.py` proves our implementation computes
byte-for-byte what the SDK computes, in both directions.

Two places we are deliberately **stricter** than Stripe's own SDK:

* **Bytes, never text.** The SDK decodes the body to `str` and re-encodes it to compute the
  MAC. That raises `UnicodeDecodeError` for a body that is not valid UTF-8 — on the
  rejection path of a public endpoint. Here the body stays `bytes` end to end, so such a
  body is simply a signature that does not match.
* **The tolerance is two-sided.** The SDK's check is `timestamp < now - tolerance`; a
  timestamp a year in the future passes it. Ours rejects both directions, exactly as
  `api/signing.py` does.

Multiple `v1` values are all tried. That is not defensive coding — it is how Stripe rotates
an endpoint secret without downtime: while two secrets are live it signs with both, and an
endpoint that read only the first would reject every webhook for the length of the overlap.

**Every rejection is the same 400 with the same body.** Missing header, malformed header,
wrong signature, stale timestamp, body that is not an event. A different answer per failure
would tell whoever is probing which part of their forgery was wrong.

## The lifecycle

| Event | Effect |
|---|---|
| `customer.subscription.created` / `.updated`, status `active`/`trialing`/`past_due` | tenant `active`, `dunning_until` cleared |
| `customer.subscription.updated`, status `canceled`/`unpaid`/`incomplete*`/`paused` | tenant `suspended` |
| `customer.subscription.deleted` | subscription cleared, tenant `suspended` |
| `invoice.payment_failed` | `dunning_until = now + 7 days`; **tenant stays active** |
| `invoice.payment_succeeded` / `invoice.paid` | `dunning_until` cleared; a suspended tenant becomes `active` again |
| anything else | stored, logged, marked processed |

`past_due` keeps a tenant serving on purpose: that is what the grace period is *for*. `unpaid`
does not — by the time Stripe says `unpaid`, its own retries are over.

> **`DUNNING_GRACE_DAYS = 7` is a commercial decision and it is currently the
> orchestrator's, not the owner's.** Seven days covers Stripe's default smart-retry cycle
> and is short enough that a customer who has stopped paying is not served free for a month.
> **The owner should confirm it.** It is one named constant in `app/billing.py`.

A second `invoice.payment_failed` during a running grace period does **not** restart it.
Stripe retries an invoice several times and each retry sends another event; restarting the
clock each time would make the window unbounded, which is the same as never suspending.

## Suspension never drops a lead

Three properties, all tested in `tests/unit/test_billing_suspension.py`:

1. **A suspended tenant's ingest returns 403**, not a silent 202 — the existing error from
   #31, reached through the existing `tenants.status` column. There is no second suspension
   path. Since #31's review fixes, the status check runs *after* the argon2 verification, so
   a 403 is only reachable by a caller who has proved it holds the secret; a wrong secret
   against a suspended tenant still comes back as a plain bad key.
2. **A lead already on the queue is still assessed and still delivered.** The qualification
   pipeline does not read `tenants.status`, and the test that says so is structural as well
   as behavioural — it is there to survive somebody later adding a "helpful" billing check
   to the worker.
3. **Nothing in billing can discard a lead.** No billing module imports a lead store, a
   queue, the ingest service or the pipeline.

Invariant 3 has no billing exception. "The customer stopped paying" is a reason to refuse
the *next* submission at the door, with an error the sender can see. It is never a reason to
drop a lead already accepted.

## The customer portal

`POST /billing/portal`, authenticated with the same signed-request scheme as ingest — the
tenant already holds an API key and a signing secret, and a second credential for one
endpoint would be a second thing to rotate and to get wrong. The path is inside the signed
string, so an ingest signature cannot be replayed here.

**The return URL is configuration (`STRIPE_PORTAL_RETURN_URL`), never request input.**
Reading it off the body would turn an authenticated endpoint into a redirector to any page
the caller names, wearing Stripe's domain on the way there.

**Known limitation: a *suspended* tenant cannot open the portal.** The endpoint reuses
ingest's credential semantics rather than weakening them for a billing convenience, so a
suspended tenant gets the same 403. In practice self-service recovery still works, because a
tenant stays `active` for the whole seven-day grace period — which is the window where it
matters — and Stripe's own dunning email carries a hosted invoice link that works regardless.
If the owner wants a suspended tenant to reach the portal from here, that needs an explicit
"allow suspended" mode on the credential source, which is a security change and should be
its own issue rather than a quiet exception.

## Personal data

`stripe_events.payload` is stored **verbatim** — it is the evidence of what Stripe actually
said, and a payload pruned to the fields this build models is missing exactly the ones an
incident will want. A Stripe invoice object carries the *billing contact's* name, email and
address: a customer's accounts-payable person, not a lead, but personal data all the same.

It is therefore the **second** column in the schema classified as able to hold personal data,
alongside `leads.raw_payload`. `tests/unit/test_db_schema.py` pins that set at exactly two.

**Its retention answer is not the lead one**, and the difference matters. An earlier version
of this paragraph said #37's job must purge it "as it covers `leads.raw_payload`"; that was
written before anybody had noticed that the two constraints point in opposite directions. A
lead payload has a policy *maximum* of 90 days. An invoice record is a financial record with
a statutory *minimum* measured in years, about a different data subject under a different
lawful basis — so applying the lead window to it would destroy evidence we are required to
keep. It is retained indefinitely today and no code path deletes it. See
[`docs/data-retention-policy.md`](data-retention-policy.md) for the worked-out position and
for the three questions a lawyer has to settle.

`stripe_events.last_error` holds an exception class and one short line — never a traceback
and never a payload — because that column is read by operators and reaches CloudWatch.

## Configuration

| Setting | Secret? | Notes |
|---|---|---|
| `STRIPE_API_KEY` / `STRIPE_API_KEY_SECRET_ARN` | yes | `sk_test_…` / `sk_live_…` |
| `STRIPE_WEBHOOK_SECRET` / `STRIPE_WEBHOOK_SECRET_ARN` | yes | `whsec_…`; refused at load if it is not |
| `STRIPE_PRICE_ID` | no | the recurring price; also the SAM switch for the billing functions |
| `STRIPE_METER_EVENT_NAME` | no | default `leadquali_billable_leads` |
| `STRIPE_API_VERSION` | no | pinned to `2026-08-26.dahlia`, the version SDK 15.6.1 is generated against |
| `STRIPE_PORTAL_RETURN_URL` | no | where the portal sends a tenant back to |

> **#34 (`docs/runbooks/stripe-setup.md`) is on a parallel branch and not here.** When both
> land, reconcile these setting names and the meter's payload keys with it. In particular the
> meter must be created with Stripe's **default** payload keys — `stripe_customer_id` for the
> customer and `value` for the quantity — because that is what
> `adapters/billing_stripe.py` sends (`METER_CUSTOMER_KEY`, `METER_VALUE_KEY`). A mismatch is
> not an error Stripe reports; it is usage that aggregates into nothing and is discovered on
> an invoice.

## Operating it

```bash
# Re-run a day the schedule missed. Safe at any time: a day already in usage_reports is
# skipped rather than sent again.
aws lambda invoke --function-name leadquali-prod-billing-usage \
  --payload '{"usage_date":"2026-09-05"}' /dev/stdout

# What is stuck?
psql -c "select event_id, event_type, attempts, last_error, received_at
         from stripe_events where status = 'failed' order by received_at;"

# What did we bill this customer, and does it match the rollup?
psql -c "select r.usage_date, r.quantity, d.leads_billable
         from usage_reports r
         join usage_daily d using (tenant_id, usage_date)
         where r.tenant_id = (select id from tenants where slug = 'acme-demo')
         order by r.usage_date;"
```

A `failed` row is never retried automatically. Five one-minute attempts absorb five minutes
of transient trouble; past that the failure is not transient, and continuing to retry would
hide it. Fix the cause, then set the row back to `pending`.

## Deliberately not built here

**`RevenuePort` is still `UnknownRevenue`.** #33's margin report asks "what was this tenant
charged for this period?", and `adapters/revenue_none.py` answers `None` — which renders as
`unknown` rather than as a plausible-looking number somebody would put in a spreadsheet.
Answering it properly means listing and interpreting Stripe invoices: recognising revenue
across a period boundary, deciding what a credit note or a proration does to a month, and
what an unpaid invoice counts as. Those are commercial definitions, not code, and none of
them is in this issue's scope. The adapter is a `BillingPort`, not a `RevenuePort`, and
wiring the second one is a follow-up with the owner's revenue-recognition rules in hand.

## Test-mode lifecycle (owner-verified)

The acceptance criterion *"a full lifecycle runs in test mode: subscribe → usage reported →
invoice paid → plan changed → cancelled → tenant suspended"* **cannot be run in this
environment** — no Stripe key, no Stripe CLI, no network to Stripe. It is written out here
as a procedure for the owner. Everything in it has an offline counterpart in the test suite;
what this proves that the suite cannot is that Stripe accepts what we send.

Prerequisites: a Stripe **test-mode** account, the `stripe` CLI logged in, a deployed stack
(or `uvicorn leadquali.api.main:app --port 8000` with the settings above).

```bash
# 0. A metered price and its meter. Note the event_name: it must equal STRIPE_METER_EVENT_NAME.
stripe billing meters create \
  --display-name "Billable leads" \
  --event-name leadquali_billable_leads \
  --default-aggregation-formula sum \
  --customer-mapping-type by_id \
  --customer-mapping-event-payload-key stripe_customer_id \
  --value-settings-event-payload-key value
# Create a recurring usage-based price against that meter in the dashboard, and export its
# id as STRIPE_PRICE_ID.

# 1. Forward webhooks to the running endpoint. Export the whsec_ it prints as
#    STRIPE_WEBHOOK_SECRET and restart the process.
stripe listen --forward-to localhost:8000/webhooks/stripe

# 2. Subscribe. In another shell:
stripe customers create --name "Acme Ltd" --email ap@acme.example -d "metadata[tenant_id]=acme-demo"
stripe subscriptions create --customer cus_XXX -d "items[0][price]=price_XXX"
psql -c "update tenants set stripe_customer_id='cus_XXX' where slug='acme-demo';"
# Within a minute: customer.subscription.created is processed and the tenant is active.
psql -c "select status, stripe_subscription_id from tenants where slug='acme-demo';"

# 3. Usage. Roll up a closed day, then report it.
python -m leadquali.usagectl rollup acme-demo --from 2026-09-07 --to 2026-09-07
aws lambda invoke --function-name leadquali-prod-billing-usage \
  --payload '{"usage_date":"2026-09-07"}' /dev/stdout
stripe billing meter-event-summaries list --meter mtr_XXX --customer cus_XXX \
  --start-time ... --end-time ...
# ASSERT: the summary's aggregated value equals usage_daily.leads_billable for that day.

# 4. Idempotency, both halves.
aws lambda invoke --function-name leadquali-prod-billing-usage \
  --payload '{"usage_date":"2026-09-07"}' /dev/stdout
# ASSERT: outcome "already_reported", and the meter summary is unchanged.
stripe events resend evt_XXX
# ASSERT: 200, one row in stripe_events, and the tenant unchanged.

# 5. Invoice paid, then failed, then paid again.
stripe trigger invoice.payment_succeeded
stripe trigger invoice.payment_failed
psql -c "select status, dunning_until from tenants where slug='acme-demo';"
# ASSERT: status still 'active', dunning_until = now + 7 days.

# 6. Plan change.
stripe subscriptions update sub_XXX -d "items[0][id]=si_XXX" -d "items[0][price]=price_YYY"
# ASSERT: customer.subscription.updated processed, tenant still active.

# 7. Cancel, and the suspension.
stripe subscriptions cancel sub_XXX
psql -c "select status from tenants where slug='acme-demo';"
# ASSERT: 'suspended' within a minute.

# 8. Reversal.
stripe trigger invoice.payment_succeeded
# ASSERT: 'active' again, dunning_until NULL.

# 9. The signature, from the outside.
curl -i -X POST localhost:8000/webhooks/stripe -d '{"id":"evt_x","type":"invoice.paid"}'
# ASSERT: 400, and nothing in stripe_events.
```

Steps 4, 5, 7 and 8 have offline equivalents in `tests/unit/test_billing.py` and
`tests/unit/test_api_webhooks.py`; step 9 is `tests/unit/test_stripe_signing.py` in full.
Steps 0, 2, 3 and 6 are the ones only Stripe can settle.
