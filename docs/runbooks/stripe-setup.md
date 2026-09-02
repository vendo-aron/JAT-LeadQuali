# Runbook — Stripe: account, products, prices, tax and webhooks

**Phase 5.4 · Issue [#34](https://github.com/vendo-aron/JAT-LeadQuali/issues/34) · Manual dashboard + CLI work, no application code**
**Owner:** aron@vendoworks.com · **Created:** 2026-09-01 · **Status:** not started

---

> ## Start this before Phase 5.5 — it has a real external wait
>
> Stripe **business verification is performed by Stripe, not by us, and can take one to several
> business days.** Nothing on our side makes it go faster. Until it clears, the account cannot
> take a live-mode charge and payouts are disabled.
>
> **Begin Phase 1 of this runbook the day Phase 5 starts**, not the day
> [#35](https://github.com/vendo-aron/JAT-LeadQuali/issues/35) starts.
>
> **What #35 cannot be verified without:**
>
> | #35 needs | Comes from | Blocking? |
> |---|---|---|
> | Test-mode secret key (`sk_test_…`) | Phase 5 | **Hard block** — no code path runs without it |
> | Test-mode price IDs (base + metered) | Phase 3 | **Hard block** — subscriptions are created by price ID |
> | Meter ID / usage-reporting API shape | Phase 4 | **Hard block** — decides which function the adapter calls |
> | Webhook signing secret (`whsec_…`) | Phase 6 | **Hard block** — signature verification is the first line of the handler |
> | Registered webhook endpoint + event list | Phase 6 | **Hard block** — no events, no lifecycle to test |
> | Dunning / payment-failure policy answer | Phase 10 | **Hard block** — it changes what the handler code does |
> | Customer portal configured | Phase 9 | Soft — portal-session endpoint can be written, not proven |
> | Live-mode keys, live price IDs, verified account | Phases 1, 12 | **Not needed for #35.** Only for go-live |
>
> **The parallelism that matters:** *test mode works the instant the account exists*, before any
> verification completes. Test-mode products, prices, meters, keys, webhooks and the Stripe CLI are
> all fully functional during the wait. So the sequence is:
>
> 1. Day 0 — create the account, submit verification, then immediately do Phases 2–11 in test mode.
> 2. Days 0–N — **#35 is built and tested end-to-end against test mode while verification is pending.**
>    Its entire acceptance criteria list is satisfiable in test mode.
> 3. Day N — verification clears; do Phase 12 (live mode) only when actually going live.
>
> Verification blocks *taking real money*. It does not block *writing or proving the billing code*.
> Do not let anyone wait on it.

---

## 0. Scope and honesty statement

**No Stripe account exists in this environment, and no Stripe key is available here.** This
document is the deliverable for #34. Every step below is written to be executed by a human with
dashboard access against a real Stripe account; none of it has been run here. Where a value must be
recorded (a price ID, a meter ID, a secret ARN), this runbook carries a fill-in table — the person
who runs the steps fills it in and commits the change, because those IDs are what #35 references.

**What this runbook provisions vs. what #35 owns:**

| This runbook (#34) | Issue #35 |
|---|---|
| Creates the Stripe account, products, prices, meters | Writes `adapters/billing_stripe.py` behind a `BillingPort` |
| Registers the webhook endpoint and picks the event list | Implements `POST /webhooks/stripe` — the code that endpoint points at |
| Obtains the signing secret and stores it in Secrets Manager (#28) | Reads that secret and verifies every signature |
| Configures portal, tax, dunning retry schedule | Calls the portal-session API, reacts to dunning events |
| Records the decisions | Encodes the decisions |

The webhook endpoint URL is registered here **before** the code exists. That is fine and
intentional: Stripe will happily hold an endpoint that 404s, and the Stripe CLI (Phase 7) lets #35
be developed against a local listener with no public URL at all.

**Cross-references:** cost basis from
[#33](https://github.com/vendo-aron/JAT-LeadQuali/issues/33) and plan §8; secret storage from
[#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28); infrastructure cost from
[#27](https://github.com/vendo-aron/JAT-LeadQuali/issues/27); consumer is
[#35](https://github.com/vendo-aron/JAT-LeadQuali/issues/35).

### A standing rule about Stripe's API, read this before Phase 4

Stripe's billing API has changed shape more than once, and this document is written from a fixed
point in time. **Do not treat any API-shape claim in this runbook as authoritative.** Where the
shape matters — principally usage-based billing — the runbook tells you *what to look up and
where*, and gives you a table to record what you actually found. Verify against
<https://docs.stripe.com/billing> and the API reference at <https://docs.stripe.com/api> for the
API version your account is pinned to (Developers → API version in the dashboard), and against
`stripe <command> --help` for CLI flags. A runbook that confidently states last year's API is worse
than one that tells you to check.

---

## Decisions the owner must make (not engineering calls)

These four cannot be decided by whoever writes #35. Each needs an answer and a date.

| # | Decision | Owner | Needed by | Recorded answer |
|---|---|---|---|---|
| D1 | Pricing model and price points (Phase 2) | aron@vendoworks.com | Before Phase 3 | *(fill in)* |
| D2 | Tax registrations — which jurisdictions we register in (Phase 8) | aron@vendoworks.com, with an accountant | Before first **live** charge; not a #35 blocker | *(fill in)* |
| D3 | Dunning policy — what the product does when payment fails (Phase 10) | aron@vendoworks.com | **Before #35 starts** — it changes the code | *(fill in)* |
| D4 | Is pre-filtered spam billable? (from #33) | aron@vendoworks.com | Before Phase 4 | *(fill in)* |

---

## Phase 1 — Account creation and business verification

Start here on day one. Everything else can proceed while this is pending.

1. Create the account at <https://dashboard.stripe.com/register> using a **company email address**
   on the company domain, not a personal one. The account email is an identity that will outlive
   whoever creates it; a personal address means a migration later.
2. Set the account name and country. **Country cannot be changed after creation** — a Stripe account
   is bound to the legal entity's country. Getting this wrong means opening a new account.
3. Complete **business verification**. Stripe asks for, in roughly this order:
   - legal entity type and registered legal name, exactly as registered;
   - registered business address;
   - business tax ID / company registration number;
   - the representative's details: full name, date of birth, home address, and often the last digits
     of a national ID number;
   - beneficial-owner details where the entity type requires them;
   - a **bank account for payouts** in the account's currency;
   - a description of what the business sells, and the business website or product URL.
   Stripe may follow up asking for a document scan (certificate of incorporation, ID, proof of
   address). Respond quickly — each round trip adds a day.
4. **Settings → Business → Public details:** set the **statement descriptor**, support email and
   support URL. The descriptor is the text on the customer's card statement. If it does not
   obviously read as this product, cardholders will not recognise it and will file chargebacks
   instead of emailing support. Use something recognisable, e.g. `JAT LEADQUALI`.
5. **Enable two-factor authentication for every user with dashboard access**, without exception. The
   Stripe dashboard can move money and read customer PII.
6. Set team roles (Settings → Team). Developers building #35 need Developer access, not Administrator.

**Verification for Phase 1**

- [ ] Dashboard loads and the account name is correct.
- [ ] Verification status is visible at **Settings → Business** — record its current state below.
- [ ] Every dashboard user shows 2FA enabled.
- [ ] `stripe --version` works locally and `stripe login` links to this account (see Phase 7).
- [ ] The **test-mode toggle is on** and test mode is usable *right now*, regardless of verification state.

| Recorded | Value |
|---|---|
| Account ID (`acct_…`) | *(fill in)* |
| Verification submitted on | *(fill in)* |
| Verification cleared on | *(fill in)* |
| Payouts enabled | *(yes/no + date)* |
| Statement descriptor | *(fill in)* |

> If verification is still pending, **stop worrying about it and continue to Phase 2.** Come back
> and fill in the two dates when Stripe emails.

---

## Phase 2 — Decide the pricing model, with the margin arithmetic behind it

Decide before creating any object. The choice determines every Product and Price below, and
changing it later means new price objects and migrating live subscriptions.

### The economics we are pricing against

From plan §8, the **direct model cost is roughly $0.02–0.03 per lead** on `claude-opus-5`
(~2k input tokens of which ~1.5k is a cached rubric prefix, ~800 output + thinking tokens) — about
**$25/month at 1,000 leads/month**. #33 computes this per tenant from stored `cost_usd`, so the real
number is measurable, not estimated, once tenants exist.

That is **not** the cost floor. The floor also includes the recurring infrastructure from #27 —
the NAT gateway or VPC endpoints, RDS, and the rest of the always-on AWS footprint — which is
charged per environment, not per lead, and which a small tenant does not come close to covering
through usage alone.

| Cost component | Shape | Source | Figure |
|---|---|---|---|
| Model inference | Per lead (~$0.02–0.03) | Plan §8, measured by #33 | *(fill in from #33)* |
| AWS always-on (NAT/VPC endpoints, RDS, Proxy) | Fixed monthly | #27 | *(fill in from #27)* |
| Stripe fees | ~% of revenue + fixed per charge — check current rates at <https://stripe.com/pricing> | Stripe | *(fill in)* |
| **Implied floor per tenant per month** | fixed share + usage | | *(fill in)* |

**The conclusion this arithmetic forces:** a purely metered price cannot work. A tenant sending 200
leads a month generates roughly $5 of inference cost and a share of a fixed infrastructure bill many
times larger. The fixed cost must be recovered by a fixed charge.

### Recommended model: base subscription + metered usage on leads qualified

**Recommendation for D1: a per-tenant monthly subscription (recovers the fixed infrastructure share
and provides an included allowance) plus a metered per-lead price on leads qualified beyond the
allowance.** This is issue #34's third option, and it fits the economics: the fixed component covers
#27's always-on cost, the metered component tracks #33's per-lead cost with margin, and the marginal
price can sit well above $0.02–0.03 because the value delivered per qualified lead is a
salesperson's time.

The alternatives, for the record:

| Model | Why you might | Why not here |
|---|---|---|
| Flat subscription | Simplest; no usage reporting at all, and #35 shrinks | A heavy tenant is unprofitable and there is no lever short of renegotiating |
| Tiered by leads/month | Predictable for the customer; no meter needed | Cliff edges at tier boundaries; still needs usage counting to enforce a tier |
| **Base + metered overage** | Margin tracks cost; fixed cost recovered | Needs correct, idempotent usage reporting — the risky part of #35 |

**Bill on "leads qualified"** — an assessment actually performed. Deterministically pre-filtered
spam costs no tokens; per #33 decision D4, decide explicitly whether it counts toward the included
allowance, and record the answer, because it is a support conversation waiting to happen.

**Verification for Phase 2**

- [ ] D1 is answered in writing in the table below, with the arithmetic above filled in.
- [ ] The chosen base price clears the fixed monthly cost share from #27 at the expected tenant count.
- [ ] The chosen per-lead price clears the §8/#33 per-lead cost with the intended margin.
- [ ] D4 (is pre-filtered spam billable?) is answered.

| Recorded pricing decision | Value |
|---|---|
| Model chosen | *(flat / tiered / base + metered)* |
| Base price per month | *(fill in)* |
| Included leads per month | *(fill in)* |
| Per-lead price beyond allowance | *(fill in)* |
| Billing currency | *(fill in)* |
| Billing interval | *(monthly)* |
| Spam counted toward allowance? | *(fill in)* |
| Decided by / on | *(fill in)* |

---

## Phase 3 — Products and prices, in test mode

Do all of this with the dashboard's **test-mode toggle on**, or with a `sk_test_…` key on the CLI.
Test-mode and live-mode objects are entirely separate; **nothing created here appears in live mode**,
and the IDs differ. That separation is a feature — it is what lets #35 be built during the
verification wait.

### 3.1 Dashboard route

1. **Product catalogue → Add product.** Name `JAT-LeadQuali`, plus a description customers will see
   on invoices and in the portal. One product per plan tier if tiers are used.
2. Add a **recurring price**: monthly, in the billing currency from Phase 2. This is the base
   subscription price.
3. Add a second price on the same product for **usage-based / metered** billing — see Phase 4 first,
   because how this is created depends on which API shape your account uses.
4. Copy every `price_…` ID into the table below. **The integration references IDs, never names or
   amounts** — a name is display text and an amount is a number that will change.

### 3.2 CLI route (equivalent, and scriptable)

Confirm flags with `stripe products create --help` and `stripe prices create --help` before running;
flag names change between CLI versions.

```bash
# Base subscription price
stripe products create \
  --name="JAT-LeadQuali" \
  --description="AI lead qualification — per-tenant subscription"

stripe prices create \
  --product=prod_XXXX \
  --currency=usd \
  --unit-amount=<base price in cents> \
  -d "recurring[interval]=month"
```

The metered price is created in Phase 4, once you know which shape to use.

**Verification for Phase 3**

```bash
stripe products list --limit=5
stripe prices list --limit=10
```

- [ ] The product appears with the intended name and description.
- [ ] The base recurring price exists with the right amount, currency and monthly interval.
- [ ] Every ID is recorded below and committed to `docs/` (price IDs are **not** secrets — they are
      safe in the repository; keys are not).

| Object | Test-mode ID | Live-mode ID (Phase 12) |
|---|---|---|
| Product `JAT-LeadQuali` | `prod_…` *(fill in)* | *(fill in)* |
| Base subscription price | `price_…` *(fill in)* | *(fill in)* |
| Metered lead price | `price_…` *(fill in)* | *(fill in)* |
| Meter (if applicable, Phase 4) | `mtr_…` *(fill in)* | *(fill in)* |

---

## Phase 4 — Metered usage: confirm the current API shape before #35 is wired

**This is the step most likely to be wrong if taken from memory, including this runbook's.**

Stripe has had (at least) two different mechanisms for reporting metered consumption:

- an **older** approach in which usage is reported against a **subscription item**, as usage records
  on that item; and
- a **newer** approach built on **billing meters and meter events**, where you define a meter, send
  meter events keyed by a customer identifier, and attach a price that reads from that meter.

The newer meters-and-meter-events approach is the one Stripe has been steering new integrations
toward, and it is very likely what a freshly created account should use. **But which one your
account gets, what the exact endpoints and field names are, and how they behave under your pinned
API version are things this runbook will not assert.** They determine the signature of the
`report_usage` method on #35's `BillingPort`, and getting them from a document instead of from the
API reference is how an integration silently under- or over-bills.

### What to check, and where

1. Open <https://docs.stripe.com/billing/subscriptions/usage-based> and follow through to the current
   usage-based billing guide. Note which mechanism the guide presents as current, and whether the
   other is marked legacy or deprecated.
2. Open the API reference at <https://docs.stripe.com/api> and locate the exact resources: the meter
   resource and the meter-event resource if meters are current, or the usage-record resource if not.
   Record the **exact endpoint paths and required fields**.
3. Check your account's pinned **API version** (Developers → API version, or `stripe config --list`
   / the `Stripe-Version` header). Behaviour differs across versions; read the version's changelog
   entry before relying on anything.
4. Check the Python SDK you will actually import (`stripe` on PyPI) — confirm the method names in
   the installed version, not in a blog post. `python -c "import stripe; print(stripe.VERSION)"`.
5. Check aggregation semantics for whichever mechanism you use: is a reported value a **delta to be
   summed** over the period, or a **snapshot of the current total**? This single question is the
   difference between a correct invoice and a wildly wrong one, and it is also what makes #35's
   idempotency requirement meaningful. Record the answer.
6. Check whether meter events carry a **client-side identifier that Stripe deduplicates on**. If they
   do, #35 should send a deterministic identifier derived from the #33 rollup row (tenant + period +
   rollup date), so a retried daily job cannot double-bill. If they do not, #35 must guarantee
   at-most-once reporting on our side. Record which.

Then create the metered price accordingly — dashboard (Product → Add price → usage-based) or CLI —
and record everything:

| Question | Answer found (fill in) | Source URL / doc consulted | Checked on |
|---|---|---|---|
| Mechanism chosen (meters+events / subscription-item usage records) | | | |
| Exact endpoint(s) #35 will call | | | |
| Meter ID / event name, if applicable | | | |
| Metered price ID | | | |
| Value semantics: delta-summed or snapshot | | | |
| Deduplication identifier supported? | | | |
| Account API version at time of check | | | |
| Python SDK version verified against | | | |

> Hand this filled-in table to whoever implements #35. It is the contract for the `report_usage`
> method, and it is more trustworthy than any prose — including this document's.

**Verification for Phase 4**

- [ ] The table above is fully filled in, with URLs and a date.
- [ ] A metered price exists in test mode and its ID is recorded in Phase 3's table.
- [ ] A single hand-sent test usage/meter event is visible in the dashboard against a test customer
      (dashboard meter view, or Developers → Events). Proving the write path once, by hand, before
      #35 automates it, saves a day of debugging later.

---

## Phase 5 — API keys: test vs. live, and where they live

**Developers → API keys.** There are two entirely separate sets of keys, and they are two entirely
separate sets of secrets.

| Key | Prefix | Used by | Storage |
|---|---|---|---|
| Test secret key | `sk_test_…` | Local dev, CI, #35's test suite | Secrets Manager (#28), test path; local `.env` acceptable |
| Live secret key | `sk_live_…` | Deployed production only | **Secrets Manager only (#28)** |
| Test webhook signing secret | `whsec_…` | Local listener + test-mode endpoint | Secrets Manager (#28), test path |
| Live webhook signing secret | `whsec_…` | Production endpoint | **Secrets Manager only (#28)** |
| Publishable key | `pk_…` | Only if a hosted checkout page is built; not a secret | Config |

**The rules, which are not negotiable:**

1. **A live key never appears in a `.env` file, ever** — not on a laptop, not in CI, not "just to
   test something once". Per #28, no secret value appears in a Lambda environment variable, the SAM
   template, CloudFormation parameters, or the repository. `sk_live_…` and the live `whsec_…` are
   secrets of exactly that kind. A live secret key can charge real customers and read real customer
   PII.
2. **Test and live are stored as different secrets**, at different Secrets Manager paths, read
   through the same `config.py` code path (#28: Secrets Manager when `ENV != local`, environment
   variables locally — one code path, no `if prod` scattered through the app).
3. **Price IDs and product IDs are not secrets** and belong in `docs/` and in configuration. Only
   keys and signing secrets are secrets.
4. If a key is ever pasted anywhere it should not be — a chat, a ticket, a commit — **roll it in the
   dashboard immediately**. Rolling is cheap; a leaked live key is not. Stripe also scans public
   repositories and will revoke keys it finds, which is a bad way to learn.
5. Use **restricted keys** where the consumer needs less than full access, rather than handing the
   full secret key to every component.

**Verification for Phase 5**

- [ ] Test secret key retrieved and stored at its Secrets Manager path (#28).
- [ ] `grep -rIn "sk_live\|sk_test\|whsec_" .` over the working tree returns nothing but this
      runbook's prose. Also sweep history, as #28 requires:
      `git log -p | grep -iE "sk_live|sk_test|whsec_"`.
- [ ] A test call authenticates: `stripe customers list --limit=1` (or a scripted call with the key
      fetched from Secrets Manager, which is the path #35 will use).
- [ ] The live key has **not** been created/copied anywhere yet — it is Phase 12's job.

| Recorded | Value |
|---|---|
| Secrets Manager path, test secret key | *(fill in — path/ARN, not the value)* |
| Secrets Manager path, test webhook secret | *(fill in)* |
| Secrets Manager path, live secret key | *(Phase 12)* |
| Secrets Manager path, live webhook secret | *(Phase 12)* |

---

## Phase 6 — Webhooks: endpoint, events, signing secret

Webhooks are how Stripe tells the application that something happened asynchronously — a payment
succeeded overnight, a card was declined on renewal, a customer cancelled from the portal. Without
them, tenant status silently drifts from billing reality.

**This runbook provisions and configures the endpoint. The endpoint itself is code owned by #35.**
Registering it here first is deliberate: the URL and the signing secret are inputs #35 needs on day
one, and Stripe does not require the URL to respond successfully at registration time.

### 6.1 Register the endpoint

1. **Developers → Webhooks → Add endpoint** (test mode).
2. URL: the route #35 exposes — `POST /webhooks/stripe` on the API Gateway stage, i.e.
   `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>/webhooks/stripe`, or the custom
   domain if #26/#27 established one. Record the exact URL below. For purely local development you
   do not need this at all — use the CLI in Phase 7.
3. Subscribe to **at minimum** these events, which are the ones #35's lifecycle handling requires:

| Event | Why #35 needs it |
|---|---|
| `checkout.session.completed` | Signup completed; link tenant ↔ Stripe customer |
| `customer.subscription.created` | Subscription exists; tenant becomes `active` |
| `customer.subscription.updated` | Plan change, trial end, status transitions (including `past_due`) |
| `customer.subscription.deleted` | Cancellation; tenant suspended |
| `invoice.paid` | Period paid; clears any dunning grace state |
| `invoice.payment_failed` | Enters the dunning path — the Phase 10 policy fires here |

   Subscribe to what you handle. A long tail of unhandled event types is noise that makes a real
   delivery failure hard to spot; you can add types later without re-registering.
4. Copy the **signing secret** (`whsec_…`) and store it per Phase 5. Each endpoint has its own
   signing secret, and the CLI listener in Phase 7 has a *different* one again.

### 6.2 The two things that break naïve implementations

**These are the reason this section exists, and both are #35's obligations that this phase's
configuration enables.**

1. **Signature verification is mandatory.** Every request must be verified against the `whsec_`
   secret before the handler acts on a single field. The endpoint is a public URL that mutates
   billing state; without verification, anyone who learns the URL can POST a handcrafted
   `customer.subscription.created` and grant themselves a free subscription, or POST an
   `invoice.paid` to clear their own dunning state. Verify using Stripe's own library helper against
   the **raw request body** — a body that has been parsed and re-serialised will not verify, which is
   a classic and confusing failure in frameworks that eagerly decode JSON. Reject with 400 on
   failure, and do not leak why.
2. **Delivery is at-least-once, so the handler must be idempotent.** Stripe retries on non-2xx
   responses and on timeouts, and a retry can arrive after the original eventually succeeded. The
   same event will be delivered more than once. Handlers must therefore key on the **Stripe event
   ID** (`evt_…`), record the IDs already processed, and make a repeat a no-op. Per #35's acceptance
   criteria, a replayed webhook must change nothing. This matters most for usage and invoicing:
   double-processing overbills a customer, which is worse than under-billing.

   The corollary: **return 200 fast and process asynchronously.** A slow handler produces timeouts,
   timeouts produce retries, and retries produce exactly the duplicate deliveries idempotency has to
   absorb.

**Verification for Phase 6**

- [ ] Endpoint appears under Developers → Webhooks with the intended URL and the event list above.
- [ ] The signing secret is stored per Phase 5 and appears nowhere in the repository.
- [ ] Send a test event from the dashboard ("Send test webhook") and confirm it appears under the
      endpoint's delivery attempts — a 404 or 502 is expected and fine until #35 ships; what you are
      proving here is that Stripe is *attempting* delivery at the right URL.
- [ ] Recorded below.

| Recorded | Value |
|---|---|
| Test endpoint URL | *(fill in)* |
| Test endpoint ID (`we_…`) | *(fill in)* |
| Subscribed events | *(fill in — confirm against the table above)* |
| Live endpoint URL / ID | *(Phase 12)* |

---

## Phase 7 — Local webhook testing with the Stripe CLI

**This is how #35 gets developed and tested without any public URL**, and it works entirely in test
mode, so it works during the verification wait.

1. Install the CLI: <https://docs.stripe.com/stripe-cli>. Then `stripe login` and follow the browser
   pairing flow. Confirm with `stripe config --list` that the right account is linked.
2. Start a listener that forwards live test-mode events to the local FastAPI app:

   ```bash
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```

   On startup it prints **a webhook signing secret of its own** (`whsec_…`) — *different from the
   dashboard endpoint's secret in Phase 6*. That is the secret the local app must be configured with
   while the listener is running. Mixing the two up is the single most common local failure; see
   Troubleshooting.

   Useful variants (confirm with `stripe listen --help`):

   ```bash
   # Only forward the events the handler actually implements
   stripe listen --events invoice.paid,invoice.payment_failed,\
customer.subscription.created,customer.subscription.updated,customer.subscription.deleted \
     --forward-to localhost:8000/webhooks/stripe

   # Skip TLS verification against a local self-signed https listener
   stripe listen --forward-to https://localhost:8000/webhooks/stripe --skip-verify
   ```

3. In a second terminal, generate an event:

   ```bash
   stripe trigger invoice.paid
   stripe trigger invoice.payment_failed
   stripe trigger customer.subscription.deleted
   ```

   `stripe trigger` creates the real underlying test-mode objects needed to produce a genuine event,
   so what the handler sees has the same shape as production. `stripe trigger --help` lists the
   supported event types.

4. **Replaying an event** — the thing that makes the idempotency test in #35 possible:

   ```bash
   stripe events list --limit=10           # find the event ID
   stripe events resend evt_XXXXXXXX       # deliver the same event again
   ```

   Confirm the exact subcommand with `stripe events --help` on your installed version. The dashboard
   can do the same thing: Developers → Events → open the event → **Resend**, and an endpoint's
   delivery-attempts view offers a resend per attempt.

   **A replayed `evt_…` is the same event ID as the original.** Send it twice and assert the tenant's
   state and any usage total are unchanged the second time. That is exactly #35's "a replayed
   webhook changes nothing" criterion, and it is also a faithful simulation of what Stripe's own
   at-least-once retries will do in production.

**Verification for Phase 7**

- [ ] `stripe listen` connects and prints its own `whsec_…`.
- [ ] `stripe trigger invoice.paid` shows a forwarded request in the listener output, and the local
      app receives it. Once #35 exists, it must return 200 and verify the signature.
- [ ] `stripe events resend <evt_id>` delivers the same event a second time. This is issue #34's
      acceptance criterion "`stripe trigger invoice.paid` reaches a local listener", plus the replay
      #35 needs.
- [ ] A deliberately corrupted signature (edit the secret, resend) produces a 400, not a 200. Prove
      the negative case, not just the positive one.

---

## Phase 8 — Tax

**Settings → Tax** (Stripe Tax). Stripe Tax calculates and collects sales tax / VAT / GST on
subscriptions and invoices, but only for jurisdictions where **you have told it you are registered**.

1. Enable Stripe Tax and set the **origin address** for the business.
2. Set the **product tax category** on the `JAT-LeadQuali` product. This is a SaaS / cloud-service
   product; the exact category name is chosen from Stripe's list, and the category drives the rate
   and the taxability rules per jurisdiction. Record what you chose.
3. Add **tax registrations** for every jurisdiction where the business is registered to collect. A
   registration entered in Stripe tells it to start charging there; it does not create the
   registration with the tax authority — that is a filing you do separately.
4. Enable tax ID collection on the customer if selling B2B into jurisdictions with reverse-charge
   rules, so business customers can supply a VAT number.
5. Note that Stripe Tax has its own pricing; check <https://stripe.com/tax> for current terms.

> **Honest statement of ownership.** *Where the business must register for tax, and when a
> registration threshold has been crossed, is a business and legal decision — not an engineering
> one.* Nexus and threshold rules vary by jurisdiction, change, and depend on facts an engineer does
> not have. Nobody on the implementation side should be guessing at this, and no code change follows
> from it. **Owner: aron@vendoworks.com, with the company's accountant or tax adviser. Needed by:
> before the first live charge in any new jurisdiction — this is not a #35 blocker and must not hold
> up Phase 5.5.** Stripe's tax monitoring can flag when thresholds are approaching, which is a
> prompt to ask the adviser, not an answer in itself.

**Verification for Phase 8**

- [ ] Stripe Tax enabled; origin address set.
- [ ] Product tax category set on `JAT-LeadQuali` and recorded below.
- [ ] Registrations entered for the jurisdictions D2 named — or D2 explicitly recorded as
      "home jurisdiction only for now", with a review date.
- [ ] A test-mode invoice for a customer in a registered jurisdiction shows a tax line; one outside
      shows none. Confirming both directions is the point.

| Recorded | Value |
|---|---|
| Stripe Tax enabled | *(yes/no + date)* |
| Product tax category | *(fill in)* |
| Registrations entered | *(fill in)* |
| Adviser consulted / date | *(fill in)* |
| Next threshold review date | *(fill in)* |

---

## Phase 9 — Customer portal

**Settings → Billing → Customer portal.** The portal is a Stripe-hosted page where a tenant updates
their card, downloads invoices, switches plan and cancels. Enabling it removes most billing support
work and means **we do not build billing UI**. #35 only needs an endpoint that creates a portal
session and redirects.

1. Enable the portal and set the business name, logo, and the **return URL** the customer comes back
   to.
2. Choose what customers may do. Sensible starting point:

| Capability | Setting | Why |
|---|---|---|
| Update payment method | **On** | The single biggest driver of involuntary churn is an expired card |
| View invoice history | **On** | Removes "can you send me the invoice" support mail |
| Update billing address / tax ID | **On** | Required for correct tax treatment (Phase 8) |
| Switch plan | **On**, restricted to the prices from Phase 3 | Self-serve upgrades; restrict to prices you actually support |
| Cancel subscription | **On** — decide immediate vs. end-of-period | End-of-period is usually right; it avoids refund arithmetic |
| Pause subscription | Off unless the product supports a paused tenant | A paused tenant is a state #35 would have to handle |

3. Link the portal to the legal terms and privacy policy URLs.
4. Note that portal actions arrive back as the **webhook events from Phase 6** — a cancellation in
   the portal is a `customer.subscription.deleted`/`.updated`. The portal is not a side channel;
   it is a producer of exactly the events #35 already handles.

**Verification for Phase 9**

- [ ] Portal configured and saved in test mode.
- [ ] Open a portal session for a test customer (dashboard, or `stripe billing_portal sessions create
      --customer=cus_… --return-url=…`; confirm with `--help`) and confirm the page loads with the
      intended capabilities.
- [ ] Cancel from the portal in test mode and confirm the corresponding webhook fires to the Phase 7
      listener.

---

## Phase 10 — Dunning and the failed-payment policy

**Settings → Billing → Subscriptions.** Two separate things live here, and only one of them is a
Stripe setting.

### 10.1 The Stripe-side retry configuration

1. Enable **Smart Retries** (or configure a fixed retry schedule) for failed subscription payments.
2. Set what happens **after the final retry fails**: cancel the subscription, mark it unpaid, or
   leave it past due. Record the choice.
3. Configure the customer emails Stripe sends on failure and before cancellation.
4. Set the trial policy and proration behaviour for plan changes while you are on this screen.

Record the resulting timeline — e.g. "retries over N days, then cancel" — because it defines the
grace period the application logic in #35 must agree with.

### 10.2 The product-side policy — **a decision, and a #35 blocker (D3)**

Stripe can retry a card. It cannot decide **what this product does to a tenant whose payment has
failed.** That is a business decision, and it changes the code in #35, so it must be answered
*before* #35 starts.

The question: *when a tenant's payment fails, does lead qualification keep running?*

| Option | Behaviour | Cost of being wrong |
|---|---|---|
| **A — Keep qualifying through the grace period, bill later** | Leads keep being assessed during retries; suspend only after final failure | We absorb inference cost for a tenant who may never pay. Bounded by the grace period |
| **B — Suspend qualification immediately on first failure** | Ingest rejects at once | A tenant loses leads over an expired card. Very likely to lose the customer outright |
| **C — Degrade: keep ingesting, stop assessing** | Leads are stored and emailed to sales unassessed with a banner | Middle ground; more states for #35 to handle |

**Recommendation: Option A, with a grace period matching the Stripe retry window from 10.1.** It
matches the product's asymmetric-cost principle — a missed good lead costs far more than a few
dollars of inference — and it is bounded, because after the final retry the tenant is suspended.

Whatever is chosen, **#35's non-negotiable constraint holds: suspension must never silently drop
leads.** A suspended tenant's ingest returns a clear error to the submitting form, any lead already
in the queue is still delivered, and suspension is reversible. A billing state must never manifest
as a lead quietly disappearing.

| Recorded | Value |
|---|---|
| Stripe retry schedule | *(fill in)* |
| Behaviour after final retry | *(cancel / unpaid / past due)* |
| **D3 — product policy on payment failure** | *(A / B / C)* |
| Grace period (days) | *(fill in)* |
| Decided by / on | *(fill in)* |

**Verification for Phase 10**

- [ ] Retry schedule and post-retry behaviour configured and recorded.
- [ ] D3 answered in writing and handed to #35 **before implementation starts**.
- [ ] In test mode, attach the card that always fails on renewal
      (`4000 0000 0000 0341` — confirm against <https://docs.stripe.com/testing>) and confirm
      `invoice.payment_failed` reaches the Phase 7 listener.
- [ ] Trial policy and proration behaviour set.

---

## Phase 11 — Invoicing and branding

**Settings → Branding** and **Settings → Billing → Invoices.**

1. Upload the logo and set the brand colours — these appear on invoices, receipts, Checkout and the
   customer portal.
2. Set the invoice footer (company legal name, registered address, tax ID as the jurisdiction
   requires).
3. Enable **emailed invoices and receipts** so customers get them without us sending anything.
4. Set the invoice number prefix if the accountant wants a particular scheme.

**Verification for Phase 11**

- [ ] A test-mode invoice PDF renders with the logo, footer and correct legal details.
- [ ] The receipt email arrives for a test payment.

---

## Phase 12 — Live mode (only when going live; **not** a #35 blocker)

Do this only after Phase 1 verification has cleared and #35 is proven in test mode.

1. Confirm **payouts are enabled** and the bank account is verified.
2. **Recreate every object in live mode**: product, prices, meter, tax settings, portal configuration,
   subscription/dunning settings. Nothing carries over from test mode automatically.
3. **Live price IDs differ from test price IDs.** Record both in the Phase 3 table. Configuration
   must select by environment; a hardcoded test price ID in production is a subscription that cannot
   be created, and the reverse is a real charge from a test run.
4. Copy the **live** secret key and register the **live** webhook endpoint with its own signing
   secret. Store both in Secrets Manager per Phase 5. The live key never touches a `.env`.
5. Make one small real charge on a real card and refund it, end to end, before pointing a customer at
   it.

**Verification for Phase 12**

- [ ] Payouts enabled; a payout schedule is set.
- [ ] Live product/prices/meter exist and their IDs are recorded.
- [ ] Live webhook endpoint registered and receiving; live signing secret in Secrets Manager.
- [ ] A real charge succeeded and was refunded.
- [ ] `git log -p | grep -iE "sk_live|whsec_"` is clean.

---

## End-to-end verification — the test-mode proof

This is the acceptance run for this runbook, and the rehearsal for #35's own acceptance criteria. It
is done entirely in **test mode**, so it can be completed while account verification is still
pending. Confirm each command's flags with `--help`; the sequence, not the exact syntax, is the
point.

```bash
# 0. Listener running in another terminal, forwarding to the local app
stripe listen --forward-to localhost:8000/webhooks/stripe

# 1. Create a test customer
stripe customers create --email="tenant-test@example.com" --name="Test Tenant"
#    -> cus_XXXX

# 2. Attach a test payment method (test card 4242 4242 4242 4242 — see docs.stripe.com/testing)
#    Easiest via the dashboard on the customer, or via a test-mode Checkout session.

# 3. Subscribe the customer to the base price + the metered price
stripe subscriptions create \
  --customer=cus_XXXX \
  -d "items[0][price]=price_BASE" \
  -d "items[1][price]=price_METERED"
#    -> sub_XXXX, status should be `active` (or `trialing`)

# 4. Report usage — using whichever mechanism Phase 4 recorded as current.
#    Send e.g. 100 qualified leads for this customer.

# 5. Inspect the upcoming invoice: base amount + metered amount for the reported usage
stripe invoices list --customer=cus_XXXX --limit=5

# 6. Advance the clock, or wait for the period to close, and confirm the invoice is paid.
#    Stripe test clocks simulate a full billing cycle in seconds —
#    see https://docs.stripe.com/billing/testing/test-clocks
```

**What success looks like:**

- [ ] A test customer exists with a working test payment method.
- [ ] A subscription exists in status `active` (not `incomplete` — see Troubleshooting) carrying
      **both** the base price and the metered price.
- [ ] Reported usage is visible against the subscription/meter in the dashboard, and the number
      matches what was sent.
- [ ] The invoice for the period shows **two lines**: the base subscription amount and the metered
      amount, and the metered amount equals `reported units × per-lead price`. This arithmetic
      matching by hand is the proof the pricing model is wired correctly.
- [ ] Tax appears on the invoice if the customer is in a registered jurisdiction (Phase 8).
- [ ] `invoice.paid` was delivered to the local listener.
- [ ] `stripe trigger invoice.payment_failed` was delivered to the local listener.
- [ ] A resent event (`stripe events resend evt_…`) arrives a second time with the same `evt_…` ID —
      the input #35 needs for its idempotency test.
- [ ] A portal session for the customer opens and shows the intended capabilities.

Record the resulting IDs so #35's tests can use the same fixtures:

| Fixture | Test-mode ID |
|---|---|
| Test customer | `cus_…` *(fill in)* |
| Test subscription | `sub_…` *(fill in)* |
| Test invoice | `in_…` *(fill in)* |
| Replayed event | `evt_…` *(fill in)* |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Webhook signature verification fails locally, every time | The app is configured with the **dashboard endpoint's** `whsec_`, but events arrive from `stripe listen`, which prints **its own** different secret | Use the secret `stripe listen` printed on startup while developing locally; use the dashboard endpoint's secret for the deployed endpoint. They are not interchangeable |
| Signature fails only for some frameworks / only in the deployed app | The handler verified against a **re-serialised** body — the framework parsed JSON and the bytes changed. Or a proxy/API Gateway altered the body encoding | Verify against the **raw request bytes** before any parsing. On API Gateway check whether the payload arrives base64-encoded and decode it first |
| Signature fails intermittently with a timestamp/tolerance error | Local clock drift, or the request sat in a queue past the tolerance window | Sync the clock (NTP). Verify at the edge of the handler, not after slow work |
| Events show as "pending"/failed in the dashboard, never arrive | Endpoint URL wrong, not publicly reachable, returning non-2xx, or timing out | Developers → Webhooks → the endpoint → delivery attempts shows the exact response code and body. Return 200 fast and process asynchronously. For local work don't use a public endpoint at all — use `stripe listen` |
| Some events arrive, others never do | The event type is not in the endpoint's subscription list | Edit the endpoint and add the type (Phase 6 table). Adding types does not change the signing secret |
| The same event is processed twice; a customer is overbilled | At-least-once delivery plus a non-idempotent handler | Key on the `evt_…` event ID, persist processed IDs, make repeats a no-op. Reproduce with `stripe events resend` |
| `No such price: price_…` / `No such customer: cus_…` | Test/live key mix-up — a test key against a live object ID, or the reverse. Test and live are separate object spaces | Check the key prefix against the ID's mode. Never hardcode price IDs; select them by environment from config |
| Everything works locally, nothing works after deploy | The deployed environment is reading the test key (or the test webhook secret) from the wrong Secrets Manager path | Verify the resolved secret path per environment (#28). Keep one code path with an env-driven path, not `if prod` branches |
| Subscription is stuck in `incomplete` | The first payment needs an action that never happened: no payment method attached, or the payment requires authentication (3DS) and nobody completed it | Attach a valid payment method before creating the subscription, or drive signup through Checkout, which handles authentication. `incomplete` expires (Stripe voids it after a fixed window) and becomes `incomplete_expired` — recreate rather than trying to revive it. In tests use a card that does not require authentication; use the 3DS-required test card only when deliberately testing that path |
| Subscription is `past_due` and the tenant is confused | A renewal failed and Smart Retries are running | Expected — this is the Phase 10 dunning window. Confirm the D3 policy is what the code implements |
| Reported usage does not appear on the invoice | Usage sent against the wrong subscription item / meter, sent outside the billing period, or the wrong value semantics (delta vs. snapshot) | Re-check the Phase 4 table. Confirm the timestamp falls inside the open period |
| Invoice totals do not match #33's rollup | Reporting job ran twice, or partially, or on a different period boundary | #35 must report idempotently from the daily rollup and reconcile against #33 for the same period |
| Tax is not applied | Stripe Tax not enabled, no registration for the customer's jurisdiction, missing product tax category, or missing customer address | Phase 8. Absence of tax where you are not registered is correct behaviour, not a bug |

---

## Definition of done — matching issue #34's acceptance criteria

| # | Acceptance criterion (from #34) | Done when | ✓ |
|---|---|---|---|
| 1 | **Account verified and payouts enabled** | Phase 1 verification table has a cleared date and payouts show enabled. *(Blocks go-live only, not #35)* | ☐ |
| 2 | **Test-mode products and prices exist; every price ID recorded in `docs/`** | Phase 3 table filled in and committed. IDs are not secrets | ☐ |
| 3 | **Webhook endpoint registered; `stripe trigger invoice.paid` reaches a local listener** | Phases 6 and 7 verified; the trigger appears in the listener output | ☐ |
| 4 | **Secret key and webhook signing secret in Secrets Manager, absent from the repository** | Phase 5 verified; `git log -p \| grep -iE "sk_live\|sk_test\|whsec_"` clean | ☐ |
| 5 | **The chosen pricing model is written down with the margin calculation from #33 behind it** | Phase 2 tables filled in, with #33's per-lead figure and #27's fixed cost | ☐ |

Additional gates this runbook adds, because #35 cannot proceed without them:

| # | Gate | ✓ |
|---|---|---|
| 6 | Phase 4's API-shape table is filled in with URLs and a date, and handed to #35 | ☐ |
| 7 | D3 (payment-failure policy) is answered before #35 starts | ☐ |
| 8 | Customer portal configured and a session opens (Phase 9) | ☐ |
| 9 | Stripe Tax enabled, or D2 explicitly deferred with a named owner and review date (Phase 8) | ☐ |
| 10 | The end-to-end test-mode proof runs: customer → subscription → usage → invoice with two correct lines | ☐ |

When items 2–10 are green, **#35 is unblocked** — item 1 can still be pending and that is fine.
Item 1 gates the first real charge, nothing else.
