# Runbook — Production cutover: pointing the website form at the live endpoint

**Issue:** [#30](https://github.com/vendo-aron/JAT-LeadQuali/issues/30) · **Epic:** [#1](https://github.com/vendo-aron/JAT-LeadQuali/issues/1) · **Plan:** §3, §8, §9
**Owner:** aron@vendoworks.com · **Audience:** whoever is running cutover day, plus whoever is on call the week after
**Status:** written 2026-09-01, not yet executed

---

## 0. Read this first

### 0.1 This document is not executable in the repository

There is no AWS account, no deployed stack, no SES production access and no live website form in
the development environment this runbook was written in. Every command below is written to be run
by the owner against real infrastructure, with real credentials, on cutover day. Nothing here has
been executed; nothing here can be executed by CI. **This is the owner's to run.** Treat unverified
steps as unverified — the first time each command runs is the first time it has ever run.

Where a step cannot be verified, it says so and gives the observable that stands in for it.

### 0.2 Why the shadow period is the centre of this document

The epic records a decision taken on 2026-09-01, in answer to [#2](https://github.com/vendo-aron/JAT-LeadQuali/issues/2):

> **There is no historical lead data with outcomes.**

Say plainly what that means, because it changes what cutover day is:

- The rubric in `rubric_vN.md` was **authored, not calibrated**. Nobody has ever checked it against
  a lead whose real outcome is known, because no such lead exists in a form anyone can read.
- The golden set ([#22](https://github.com/vendo-aron/JAT-LeadQuali/issues/22)) was assembled by a
  human labelling leads by judgement. The eval numbers from
  [#23](https://github.com/vendo-aron/JAT-LeadQuali/issues/23)/[#24](https://github.com/vendo-aron/JAT-LeadQuali/issues/24)
  therefore measure *agreement with one person's opinion*, not accuracy against reality. They are
  the best number available and they are still not ground truth.
- **The shadow period below is the first time the rubric ever meets live traffic**, and it is the
  first comparison against a judgement made by someone with something at stake — a rep deciding
  whether to pick up the phone.
- **The feedback links in [#19](https://github.com/vendo-aron/JAT-LeadQuali/issues/19) are the only
  ground truth this product will ever have.** Not the main source: the only one. Every future
  golden-set entry, every rubric revision, every claim made to a future customer about
  qualification quality traces back to a rep clicking *good lead* or *bad lead* in an email. If the
  reps do not click, this product has no measurable quality, now or later.

That is why the shadow period gets a numeric exit criterion instead of a feeling (§3), why the comms
in §8 are a required step and not a courtesy, and why "did the reps click?" is a day-1 alarm-grade
metric rather than a nice-to-have.

### 0.3 The shape of the day

```
  pre-flight gate (§1)          all boxes true, or stop
        │
        ▼
  form change deployed (§2)     mirror_pct = 0 — no traffic yet, endpoint live
        │
        ▼
  ramp the mirror (§4, A→C)     10% → 50% → 100%, old path still primary
        │
        ▼
  SHADOW PERIOD (§3)            ≥10 business days, ≥100 compared leads
        │                       exit criterion met? ──no──► fix, re-baseline, stay in shadow
        ▼ yes
  ramp to primary (§4, D)       25% → 50% → 100%, old path still delivering to a review mailbox
        │
        ▼
  retire old path (§4, E)       record the decision
```

Rollback (§5) is available at every stage and reverses one step or all of them with one change.

### 0.4 Names used below

| Placeholder | Meaning | Filled in at |
|---|---|---|
| `<REGION>` | AWS region — the same one as SES ([#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20)) and the Lambdas ([#26](https://github.com/vendo-aron/JAT-LeadQuali/issues/26)) | #25 |
| `<PROD_STACK>` | SAM stack name for prod | #26 |
| `<LEADS_ENDPOINT>` | `https://leads.vendoworks.com/leads` — a custom domain, **not** the raw `execute-api` URL | §2.1 |
| `<TENANT>` | the internal tenant slug, e.g. `vendoworks-internal` | §1.5 |
| `<SITE_CONFIG>` | the runtime config the website form reads its two ramp numbers from | §2.6 |

---

## 1. Pre-flight gate

**Nothing moves until every box below is true.** These are not a summary of work done elsewhere;
each one is a thing to check on the day, with the check written out. A box that cannot be ticked is
a stop, not a risk to accept — with one documented exception per box where one exists.

Run the whole gate in one sitting and write the result next to each line. A gate checked last week
is not a gate.

### 1.1 SES is out of the sandbox and mail actually arrives ([#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20))

In the sandbox, SES will only send to verified addresses. Cutover with SES in the sandbox means the
routing email silently fails for any rep whose address was not verified, which means no feedback
clicks, which means no ground truth — the exact failure this whole project is built to avoid.

```bash
# 1. Production access, not sandbox:
aws sesv2 get-account --region <REGION> \
  --query '{ProductionAccess:ProductionAccessEnabled,SendQuota:SendQuota,Enforcement:EnforcementStatus}'
# expect: ProductionAccess true, EnforcementStatus "HEALTHY"

# 2. Domain identity verified with DKIM:
aws sesv2 get-email-identity --region <REGION> --email-identity vendoworks.com \
  --query '{Verified:VerifiedForSendingStatus,Dkim:DkimAttributes.Status}'
# expect: Verified true, Dkim "SUCCESS"

# 3. The configuration set exists and publishes events (the alarms in #29 have no data without it):
aws sesv2 get-configuration-set-event-destinations --region <REGION> \
  --configuration-set-name <CONFIG_SET>
```

Then the check the API cannot make for you:

- [ ] Send one real routing email to **every** address on the sales distribution list, not just your
      own. Confirm each rep can see it **in their inbox, not their spam folder**, and ask each one to
      reply "got it". Silence is not confirmation.
- [ ] Open one of those emails **on a phone**, on mobile data (not office wifi), and click a feedback
      link. Confirm it renders, confirms, and writes a `feedback` row. Reps triage on phones; a
      feedback link that only works on a laptop is a feedback link that does not work.
- [ ] Check the received headers of a delivered message: `dkim=pass`, `spf=pass`, `dmarc=pass`.
- [ ] Note the granted sending quota and rate limit and compare against expected daily volume. If
      expected volume is within 2× of the quota, request an increase before cutover, not after.

### 1.2 Alarms are armed **and have been seen firing** ([#29](https://github.com/vendo-aron/JAT-LeadQuali/issues/29))

An alarm that has never fired is a hypothesis. #29's acceptance criterion is that every alarm has
been observed firing at least once in dev; this gate re-confirms it in **prod**, because prod has
different thresholds, a different SNS topic and different subscribers.

```bash
# Every alarm on the prod stack is in OK (not INSUFFICIENT_DATA — that means no metric is arriving):
aws cloudwatch describe-alarms --region <REGION> --alarm-name-prefix <PROD_STACK> \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Actions:AlarmActions}' --output table

# Every alarm has an action, and the action is a topic with a confirmed subscriber:
aws sns list-subscriptions-by-topic --region <REGION> --topic-arn <ALERT_TOPIC_ARN> \
  --query 'Subscriptions[].{Endpoint:Endpoint,Confirmed:SubscriptionArn}'
# a SubscriptionArn of "PendingConfirmation" means nobody is being told anything
```

- [ ] All alarms `OK`. Any alarm in `INSUFFICIENT_DATA` is a broken alarm — the metric is not being
      emitted — and must be fixed, not waved through.
- [ ] Every subscription is confirmed, and at least one subscriber is a human who will be awake.
      Send one test notification through the topic and have that human confirm receipt in writing.
- [ ] The seven signals from #29 all exist in prod: DLQ depth, worker error rate, p99 latency
      (worker and ingest separately), daily token spend, tier-distribution drift, DB connection
      count, SES bounce/complaint rate.
- [ ] The alarm runbook from #29 is open in a tab, and the person on call has read it today.

### 1.3 The DLQ is empty and its alarm has been proven to fire

The DLQ alarm is the one that matters most on cutover day: a lead in the DLQ is a lead nobody has
seen, and the whole design promises no lead is ever silently dropped.

```bash
# 1. Empty, including in-flight:
aws sqs get-queue-attributes --region <REGION> --queue-url <DLQ_URL> \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
# expect both 0

# 2. Prove the alarm fires — send one message directly to the DLQ:
aws sqs send-message --region <REGION> --queue-url <DLQ_URL> \
  --message-body '{"cutover_drill":true,"ts":"'"$(date -u +%FT%TZ)"'"}'
```

- [ ] Within the alarm's evaluation period, the DLQ alarm goes to `ALARM` **and a human receives the
      notification**. Confirm the notification arrived; do not confirm from the console alone.
- [ ] Purge the drill message, confirm depth returns to 0, and confirm the alarm returns to `OK`.
- [ ] Confirm the **redrive** path works before you need it under stress:
      `aws sqs start-message-move-task --source-arn <DLQ_ARN> --destination-arn <QUEUE_ARN>` moves
      messages back to the main queue. Run it once against a harmless drill message and watch the
      worker consume it. The point of a DLQ is that those leads get reprocessed, not archived.
- [ ] Confirm the main queue's **message retention period is 14 days** (`MessageRetentionPeriod`
      = `1209600`). §5 depends on this: pausing the worker is only safe if messages survive the pause.

### 1.4 Secrets are in place and reachable ([#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28))

```bash
# The four secrets exist (list metadata only — never print a secret value into a terminal
# that is being screen-shared or logged):
aws secretsmanager list-secrets --region <REGION> \
  --query 'SecretList[].{Name:Name,Changed:LastChangedDate}' --output table
# expect: prod ANTHROPIC_API_KEY, per-tenant HMAC secret, database credentials,
#         feedback-link signing key

# The tenant row points at the ARN, never the value:
psql "$PROD_DSN" -c "select slug, hmac_secret_ref from tenants where slug = '<TENANT>';"
# expect an arn:aws:secretsmanager:... value
```

- [ ] All four secrets present, each with a recent `LastChangedDate` that you recognise.
- [ ] `tenants.hmac_secret_ref` holds an ARN, not a secret.
- [ ] The worker can actually read them: the synthetic lead in §1.6 is the real test. A permissions
      error here shows up as the worker failing on every message and filling the DLQ, so do not skip
      §1.6 on the grounds that the secrets "look fine".
- [ ] Nothing secret is in the SAM template, a CloudFormation parameter, a Lambda environment
      variable, or the repo. Re-run the history sweep from #28:
      `git log -p | grep -iE "sk-ant|aws_secret|password"` — expect no hits.
- [ ] The website's copy of the API key and HMAC secret is stored in the site's own secret store
      (§2.2), not in a source file and never in browser-delivered JavaScript.

### 1.5 Migrations are applied and the internal tenant is seeded

```bash
# Schema is at head, with no pending migration:
alembic -c <prod alembic.ini> current      # note the revision
alembic -c <prod alembic.ini> heads        # must be the same revision

# The tenant exists and carries the config measured in #24:
psql "$PROD_DSN" -c "select slug, status, icp_config->>'effort' as effort,
                            icp_config->>'prompt_version' as prompt_version,
                            icp_config->>'min_confidence' as min_confidence,
                            icp_config->'thresholds' as thresholds
                     from tenants where slug = '<TENANT>';"
```

- [ ] `current` == `heads`. A pending migration on cutover day is a cutover on another day.
- [ ] The tenant's `effort`, `prompt_version`, `min_confidence` and tier thresholds are **the values
      #24 measured**, not the placeholder defaults (`effort: medium`, hot ≥ 80 / warm 55 / cold 30).
      If they are still the defaults, #24 either did not run or its result was never written back —
      stop and resolve that. Cutting over on unmeasured config throws away the only calibration work
      that exists.
- [ ] The tables from plan §4 all exist with `tenant_id`: `tenants`, `leads`, `assessments`,
      `routing_events`, `feedback`.
- [ ] `leads` has the unique constraint on `(tenant_id, submission_id)`. Without it, SQS
      at-least-once delivery means duplicate emails to sales on day one.

### 1.6 A synthetic lead is qualified end-to-end **in production**

This is the single most informative pre-flight check, because it exercises every component in the
real account with the real config. Run it, do not reason about it.

```bash
SUB="cutover-preflight-$(date -u +%Y%m%dT%H%M%SZ)"
BODY=$(cat <<JSON
{"submission_id":"$SUB","source":"preflight",
 "contact":{"name":"Preflight Check","email":"preflight@vendoworks.com","company":"Vendoworks"},
 "message":"Cutover pre-flight synthetic lead. Please ignore.",
 "company_website_2":"", "form_elapsed_ms": 42000}
JSON
)
TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$HMAC_SECRET" -hex | awk '{print $2}')

curl -sS -o /dev/stderr -w '\nHTTP %{http_code} in %{time_total}s\n' \
  -X POST "<LEADS_ENDPOINT>" \
  -H 'Content-Type: application/json' \
  -H "X-LQ-Api-Key: $API_KEY" \
  -H "X-LQ-Timestamp: $TS" \
  -H "X-LQ-Signature: sha256=$SIG" \
  --data "$BODY"
```

Then walk the whole chain. Every one of these must be true:

- [ ] **202** returned, in **under 200 ms** (plan §3 / #17). A 401 means key or signature; a 422
      means the payload shape drifted from what #17 validates; anything 5xx stops the cutover.
- [ ] A `leads` row exists for `(tenant_id, submission_id)` with `status` progressing past `received`.
- [ ] An `assessments` row exists with a tier, a total score, non-null `model_id`, `prompt_version`,
      `effort`, token counts and `cost_usd`. A missing `cost_usd` means the cost acceptance criterion
      in #30 cannot be checked later — fix it now, not in week two.
- [ ] `assessments.cache_read_tokens > 0` on the **second** synthetic lead. Zero across repeated
      requests means something volatile leaked into the cacheable prefix (#11/#24) and the §8 cost
      estimate is wrong before you start.
- [ ] A `routing_events` row exists with a `provider_message_id` from SES.
- [ ] The routing email arrived in a real sales inbox, correctly formatted, with the tier, the five
      dimension scores, extracted facts, reasoning, `missing_information` and
      `suggested_first_question` (#19).
- [ ] Clicking **good lead** from that email, on a phone, writes a `feedback` row bound to the right
      `lead_id` and `tenant_id`, and clicking again **updates rather than duplicates**.
- [ ] The DLQ is still empty afterwards.
- [ ] Repeat the identical POST once. Expect **one** lead, **one** assessment, **one** email — the
      idempotency check holding under a real duplicate.
- [ ] Send one deliberately malformed request (bad signature) and confirm a **401** before any
      parsing, and no `leads` row.

Delete or mark the synthetic leads afterwards so they do not pollute the shadow comparison:
`update leads set source = 'preflight-excluded' where submission_id like 'cutover-preflight-%';`

### 1.7 The eval numbers have been reviewed with the ICP owner

The numbers from #23/#24 are the only quality evidence that exists before traffic. They must be
looked at by the person who owns the ICP definition — not forwarded to them, looked at *with* them,
in a conversation, before traffic moves.

- [ ] Sit down with the ICP owner and go through the #24 effort-sweep table: tier accuracy (exact and
      adjacent), **precision on `hot`**, **recall on contactable**, cost and p95 latency at the
      chosen effort level.
- [ ] The **contactable-recall target was agreed before tuning** (#24's acceptance criterion) and the
      shipped config meets it. If the target was chosen after seeing the numbers, treat it as not
      agreed and agree it now — a target picked retroactively measures nothing.
- [ ] Walk through **every** golden-set lead the agent tiered `disqualified` or `cold` that the human
      labelled contactable. Read them out loud. These are the failure mode that costs money, and
      cutover day is the last cheap moment to notice a pattern in them.
- [ ] The ICP owner is **named** and knows they are the escalation point for tier disputes during
      shadow. #2 still lists this as an open question — if the name is still blank, fill it in before
      cutover. A shadow period with no adjudicator produces disagreements nobody resolves.
- [ ] Record the agreed numbers in this runbook's execution log (§9). The shadow exit criterion in §3
      is compared against them.

### 1.8 Gate summary

| # | Gate | Verified by | Result |
|---|---|---|---|
| 1.1 | SES out of sandbox, mail lands in inboxes, phone click works | `get-account` + a reply from every rep | |
| 1.2 | Alarms armed, subscribed, and seen firing in prod | `describe-alarms` + a received notification | |
| 1.3 | DLQ empty, alarm proven to fire, redrive proven, 14-day retention | drill message end to end | |
| 1.4 | Secrets present, ARN-referenced, readable by the worker | `list-secrets` + §1.6 succeeding | |
| 1.5 | Migrations at head, tenant seeded with #24's measured config | `alembic current`/`heads` + tenant row | |
| 1.6 | Synthetic lead qualified end to end in prod, twice, idempotently | the walk in §1.6 | |
| 1.7 | Eval numbers reviewed with the named ICP owner | the conversation, logged in §9 | |

**Any unticked box stops the cutover.** Reschedule; do not proceed with a compensating control
invented on the day.

---

## 2. The form change

### 2.1 Endpoint

```
POST https://leads.vendoworks.com/leads          ← custom domain, mapped to the prod HTTP API
Content-Type: application/json
```

Use a **custom domain**, not the raw `https://<api-id>.execute-api.<REGION>.amazonaws.com/prod/leads`.
The API id changes if the stack is ever recreated; a form change to chase it is a website deploy you
do not want to be doing under pressure. The custom domain also keeps the rollback in §5 purely a
config change.

### 2.2 The signing must happen server-side. Never in the browser.

**The per-tenant API key and the HMAC secret must never be delivered to a browser.** Anything in
page JavaScript is public: view-source, devtools, the CDN cache, an archived copy. A leaked key lets
anyone POST arbitrary leads into the tenant, poison the assessment history and the feedback-derived
golden set, and spend the Anthropic budget.

So the flow is two hops:

```
visitor's browser
   │  same-origin POST, no secrets, session cookie / CSRF token as today
   ▼
website backend (existing handler, or one small serverless function)
   │  1. do what it does today — persist the lead, send the existing notification
   │  2. THEN sign and forward to LeadQuali, fire-and-forget
   ▼
<LEADS_ENDPOINT>  (API Gateway → ingest Lambda)
```

If the site is fully static with no backend, add exactly one serverless function (Netlify/Vercel/
Cloudflare Worker/Lambda) to do the signing. One function — not the browser, and not a proxy that
forwards the secret onward.

### 2.3 Headers

| Header | Value |
|---|---|
| `X-LQ-Api-Key` | the per-tenant API key issued in #28; the server compares it against the argon2 hash in `tenants.api_key_hash` |
| `X-LQ-Timestamp` | Unix seconds at the moment of signing |
| `X-LQ-Signature` | `sha256=<hex>` — see §2.4 |
| `Content-Type` | `application/json` |
| `User-Agent` | `vendoworks-site/<git sha>` — makes it trivial to tell site traffic from a replay or a test |

### 2.4 HMAC signature

Sign the **exact bytes being sent**, not a re-serialised copy. Serialise the body once, keep it in a
variable, sign that variable, send that variable. Re-serialising between signing and sending (a
different key order, a different separator, a re-encoded unicode escape) is the single most common
cause of a 401 that "should work".

```
signing_string = f"{timestamp}.{raw_body_bytes}"
signature      = "sha256=" + hmac_sha256(hmac_secret, signing_string).hexdigest()
```

Node reference implementation for the site's forwarding function:

```js
const crypto = require("crypto");

const raw = JSON.stringify(payload);                 // serialise ONCE
const ts  = Math.floor(Date.now() / 1000).toString();
const sig = "sha256=" + crypto
  .createHmac("sha256", process.env.LEADQUALI_HMAC_SECRET)
  .update(`${ts}.${raw}`)
  .digest("hex");

await fetch(process.env.LEADQUALI_ENDPOINT, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-LQ-Api-Key": process.env.LEADQUALI_API_KEY,
    "X-LQ-Timestamp": ts,
    "X-LQ-Signature": sig,
    "User-Agent": `vendoworks-site/${process.env.GIT_SHA}`,
  },
  body: raw,                                         // the SAME bytes that were signed
  signal: AbortSignal.timeout(2000),
});
```

Two properties the server enforces, which the form must respect:

- **Timestamp skew window ±300 s.** A signature older than five minutes is rejected as a replay. The
  site's clock must be NTP-synced; a drifting clock presents as intermittent 401s that correlate
  with nothing.
- **`submission_id` is the idempotency key**, unique per tenant. Generate it once per visitor
  submission (a UUIDv4 is fine) and **reuse the same value on a retry**. Generating a fresh one on
  retry turns one lead into two emails to sales.

### 2.5 Honeypot and submit-timing fields

The pre-filters in [#17](https://github.com/vendo-aron/JAT-LeadQuali/issues/17) are the first cost
lever in §8 — spam that never reaches the model costs nothing. **They only work if the real form
markup carries the fields.** A pre-filter looking for a field the form does not send is inert, and
it fails silently: everything looks healthy while every bot submission goes to the model at full
price and, worse, into the assessment history.

**Honeypot** — a field a human never fills and a naive bot always does:

```html
<div class="lq-hp" aria-hidden="true">
  <label for="company_website_2">Leave this field empty</label>
  <input type="text" id="company_website_2" name="company_website_2"
         tabindex="-1" autocomplete="off" value="">
</div>
<style>
  /* Position off-screen rather than display:none — some bots skip hidden inputs. */
  .lq-hp { position: absolute; left: -9999px; width: 1px; height: 1px; overflow: hidden; }
</style>
```

Send `company_website_2` **always**, even when empty. The distinction the server needs is between
"present and empty" (a human) and "absent" (a misconfigured form). #17's pre-filter must treat an
**absent** honeypot as a configuration error — log a warning and let the lead through to the model —
never as a pass. A silent pass is how this check rots.

**Submit timing** — record when the form was rendered and send the elapsed milliseconds:

```html
<input type="hidden" name="form_rendered_at" value="">
<input type="hidden" name="form_elapsed_ms" value="">
<script>
  const t0 = Date.now();
  document.querySelector('[name=form_rendered_at]').value = t0;
  form.addEventListener('submit', () => {
    document.querySelector('[name=form_elapsed_ms]').value = String(Date.now() - t0);
  });
</script>
```

**Run the timing filter in log-only mode for the first two weeks.** The threshold below which a
submission is treated as a bot (3 000 ms is the usual starting guess) is a guess about *your*
visitors, and the cost of guessing high is suppressing a real lead — the expensive error. Log the
elapsed-time distribution during shadow, look at the actual histogram, then set the threshold at a
value with real daylight below the fastest genuine human, and only then let it suppress. Measure
first, enforce second.

**Verify the fields survive to production.** Templates get minified, A/B-tested, re-rendered by a
CMS and edited by whoever owns the marketing page. On cutover day, open the live form in a browser,
view source, and confirm both fields are present in the delivered markup — then re-confirm after any
website deploy during the watch window (§7). A checked-in template is not evidence about what the
CDN is serving.

### 2.6 The two ramp numbers, and where they live

The form reads two numbers from `<SITE_CONFIG>`:

| Name | Meaning | Cutover-day value |
|---|---|---|
| `LEADQUALI_MIRROR_PCT` | % of submissions also forwarded to the agent | `0` at deploy, ramped in §4 |
| `LEADQUALI_PRIMARY_PCT` | % of submissions for which the **agent's** email is the one sales acts on (the old notification is suppressed for that bucket) | `0` until the shadow gate passes |

Three requirements on `<SITE_CONFIG>`, all of which exist to make §5 fast:

1. **Changing either number must not require a website build or deploy.** A CMS setting, an
   environment variable on the forwarding function, or a small JSON document the function reads —
   any of those. If reverting traffic needs a front-end release, the rollback is measured in a
   deploy pipeline's worth of minutes at the worst possible time.
2. **Cache TTL ≤ 60 seconds.** This is what makes §5's stated rollback time honest.
3. **Bucketing is deterministic**: `bucket = crc32(submission_id) % 100 < PCT`. A given lead is
   consistently in or out, so a lead is never both mirrored and not mirrored on a retry, and the
   shadow sample is not skewed by re-submissions.

### 2.7 What the form does with the response

**On `202`:** read `submission_id` from the body and log it beside the site's own lead record, so
the two systems can be reconciled later ("did every site lead reach the agent?" must be an
answerable question — during shadow you will ask it daily). Show the visitor the normal thank-you.

**On any non-2xx, or a timeout:**

1. Show the visitor **the normal thank-you anyway**. The visitor's experience does not depend on
   this system.
2. The existing notification path delivers the lead as it does today. Nothing is lost.
3. Log status code, `submission_id`, response body and elapsed time. Increment a
   `leadquali_forward_failed` counter.
4. Retry **at most once**, after 2 s, on `5xx`, `429` or timeout — **with the same `submission_id`**,
   which makes the retry idempotent server-side. Do not retry any other `4xx`: a `401` means the key
   or signature is wrong and a `422` means the payload is wrong, and neither improves on a second
   attempt. Both are alert-worthy immediately.
5. Alert the website owner if the failure rate exceeds **5 % over 15 minutes**. A steady trickle of
   forwarding failures during shadow is a silently shrinking sample; a cliff is an outage.

### 2.8 The form must never block the visitor

Non-negotiable, and worth stating as its own requirement because it is the one that gets quietly
broken by a later refactor:

- The forward to LeadQuali happens **after** the site's own lead handling has succeeded — after the
  lead is persisted and the existing notification is queued. If the forward throws, the visitor's
  submission has already succeeded.
- The forward is **fire-and-forget with a 2 s timeout** (`AbortSignal.timeout(2000)` above, or a
  background task / `waitUntil()` where the platform offers one). The thank-you page must not await
  it.
- **No `await` on the LeadQuali call in the request path that renders the response.** If the endpoint
  is down and the platform lacks a background primitive, prefer dropping the forward over delaying
  the visitor.
- Added visitor-visible latency budget: **0 ms**. Measure it — compare form-submit p95 before and
  after §4 stage B and confirm no change. If the number moved, the forward is on the critical path
  and must be moved off it before ramping further.

Rationale, from plan §3: a Claude call with adaptive thinking takes seconds. Every part of this
architecture — the 202, the queue, the worker — exists so that the visitor never waits on the model.
A form that blocks on the forward reintroduces exactly the failure the design removed.

---

## 3. The shadow period

### 3.1 What runs during shadow

Both paths, in full, for every lead:

- The existing notification path delivers to whoever handles leads today. **They keep working
  exactly as they do now.** Nothing about their job changes during shadow.
- The agent also receives the lead, assesses it, and sends its routing email — clearly banner-marked:

  > **SHADOW MODE — this is an evaluation copy. The lead below also reached you through the normal
  > path; work it from there. Please still click *good lead* or *bad lead* below: those clicks are
  > the only way we can tell whether this is working.**

- Reps click the feedback links on the shadow emails. That is the entire ask of them, and §8's comms
  exist to make it happen.

### 3.2 Why the shadow period is doing real work here

For a system with historical data, a shadow period is a regression check against a known baseline.
Here there is no baseline (§0.2). This shadow period is the **first and only** measurement of the
rubric against live traffic and real human judgement before it is given authority. Treat it as an
experiment being run for the first time, not a formality between deploy and go-live — the answer is
genuinely not known in advance, and "the eval numbers were good" is not a substitute, because the
eval numbers measure agreement with the same person who wrote the rubric.

### 3.3 The comparison table

One row per shadow lead. Build it as a SQL view so it is never hand-maintained:

| Column | Source |
|---|---|
| `lead_id`, `received_at` | `leads` |
| `agent_tier`, `total_score`, `confidence`, `prompt_version`, `effort` | `assessments` |
| `rep_verdict` (`good` / `bad` / `unsure` / *null*) | `feedback` |
| `rep_contacted` (bool) | asked at the weekly review, or read from the CRM if one exists |
| `human_tier` | assigned by the ICP owner — **only for the labelled subset**, see below |
| `agreement` | derived |

**Label the subset, not everything.** Labelling every lead by hand is how a shadow period quietly
stops being done in week two. Label:

- **100 % of disagreements** — every lead where `rep_verdict = bad`, and every lead the agent tiered
  `cold` or `disqualified` that the rep contacted anyway. These are the cases with information in
  them, and they go straight into the golden set (#22).
- **A random 20 % of agreements** — so that agreement-by-luck and a rubric that is right for the
  wrong reason both show up.

### 3.4 Metrics, defined identically to #23 so the numbers are comparable

- **Contactable-recall** = of leads the human judged should have been contacted, the fraction the
  agent tiered `hot` or `warm`. *This is the false-disqualification rate, the number that costs
  money.*
- **Hot-precision** = of leads the agent tiered `hot`, the fraction the human agreed were worth a
  rep's time immediately. *This is the number sales feels.*
- **Exact and adjacent tier agreement** against `human_tier` on the labelled subset.
- **Feedback coverage** = fraction of shadow emails with at least one `feedback` row. *This is the
  health of the only ground-truth source that exists.*
- **Cost per lead**, from `assessments.cost_usd`, against the §8 estimate of **$0.02–0.03**.

### 3.5 Default window and sample size

**Ten business days, and at least 100 compared leads, whichever comes later.**

Why these numbers:

- **Ten business days** covers two full working weeks. Inbound lead traffic is weekday-shaped and
  campaign-shaped; a single week can be entirely one campaign, one conference follow-up, or one
  quiet stretch. Two weeks is the shortest window that has ever seen two different weeks.
- **100 compared leads** is where the metric that matters becomes meaningful rather than
  where it becomes precise. Be honest about the arithmetic: at 100 leads, with perhaps 30 tiered
  below `warm`, a contactable-recall estimate has a confidence interval several points wide. 100 is
  enough to detect a *badly* miscalibrated rubric — the failure mode that actually threatens this
  launch — and is not enough to certify a fine one. That is the right trade for a first cutover; the
  fine calibration comes from the feedback table over the following months, which is the mechanism
  that was always going to have to do this job.
- At the §8 planning volume (~1 000 leads/month, ~50 per business day) the lead count is reached
  well inside ten days, so the calendar window binds and the sample is comfortably larger than 100.
  At low volume the sample count binds instead. Both floors must clear.

### 3.6 Exit criterion — promote to primary only when all of these are true

Numbers, not a feeling. Compute them from the §3.3 table and write them into §9.

| # | Criterion | Threshold |
|---|---|---|
| 1 | Compared leads with a human signal (`rep_verdict` or `rep_contacted`) | **≥ 100** |
| 2 | Elapsed window | **≥ 10 business days** |
| 3 | **Contactable-recall** | **≥ 0.95**, and *every* miss individually root-caused — either fixed and re-measured, or accepted in writing by the ICP owner |
| 4 | **Hot-precision** | **≥ 0.70** (more than 3 wasted calls in 10 and reps stop opening the email — at which point the feedback loop dies and so does the product) |
| 5 | Adjacent-tier agreement on the labelled subset | **≥ 0.90** (exact-match will never be high; tier boundaries are judgement calls) |
| 6 | **Feedback coverage** | **≥ 0.50** of shadow emails, from **≥ 3 distinct raters** |
| 7 | DLQ messages | **0**, or every one root-caused, fixed, and its lead confirmed processed after redrive |
| 8 | Ingest p95 / worker p99 | within the #29 alarm thresholds for the whole window; ingest p95 **< 200 ms** |
| 9 | Measured cost/day | within **1.5×** the §8 estimate, or reconciled in writing before ramping |
| 10 | Sales sign-off | **≥ 3 reps say, in writing and unprompted-by-a-yes/no-question, that the emails are useful.** Ask "what would you change about these emails?", not "are these OK?". Silence is not agreement |

Criterion 3 has no numeric escape hatch on purpose. A false `disqualified` costs a deal, silently,
forever (plan §2). One unexplained miss is a reason to keep shadowing, not a rounding error.

### 3.7 What makes the window longer

Extend, do not compress. Each of these resets or extends the clock:

- **Sample not reached.** Fewer than 100 compared leads at day 10 → keep running until 100. Do not
  lower the bar to fit the calendar.
- **Any prompt, rubric, `effort`, threshold or `min_confidence` change mid-window.** The measurement
  is of a specific configuration. Changing it invalidates the leads assessed before the change:
  re-baseline and accumulate at least **50** further compared leads under the new config, and re-run
  the #23 eval on the golden set as well.
- **An unrepresentative tier mix** — e.g. zero `hot` leads in the whole window, so hot-precision is
  undefined, or a single campaign supplying 60 % of the sample. Extend until the mix looks like
  normal traffic. A shadow period that only ever saw one kind of lead has measured one kind of lead.
- **A website form change** during the window. Field names, new fields, a removed field, a CMS
  template edit. Re-verify §2.5 and treat it like a config change.
- **Feedback coverage below 0.50.** The sample is not really 100 leads; it is however many were
  actually judged. Fix the human problem (§8) and keep going.
- **Sales absence** — holidays, quarter-end, a conference — during which the human side of the
  comparison is not really happening.
- **Any unexplained DLQ message, alarm, or forwarding-failure spike.** Root-cause first; the clock
  continues, but criterion 7 does not clear until the cause is known.
- **A model or SDK version change.** Same reasoning as a prompt change.

---

## 4. Ramp

One flip is not a plan: it removes the ability to learn anything cheaply, and it makes every problem
arrive at full volume. Ramp in stages, holding at each, watching a named signal.

### Stage A — endpoint live, zero real traffic

- Deploy the form change (§2) with `LEADQUALI_MIRROR_PCT = 0`, `LEADQUALI_PRIMARY_PCT = 0`.
- Run the §1.6 synthetic lead against the deployed form path (not curl this time — a real submission
  through the real form, with the honeypot and timing fields as the browser sends them).
- **Watch:** the synthetic lead completes end-to-end; the honeypot and timing fields are present in
  what the server received; ingest 5xx = 0.
- **Hold:** until the synthetic lead is confirmed through to a `feedback` row.

### Stage B — mirror ramp: 10 % → 50 % → 100 %

Real traffic reaches the endpoint. Sales still works entirely from the old path. This stage exists
to find out whether the endpoint survives real traffic *before* it carries any authority.

| Step | `MIRROR_PCT` | Hold | Watch |
|---|---|---|---|
| B1 | 10 | 4 business hours (≥ 5 real leads) | ingest 5xx and 4xx = 0; ingest p95 < 200 ms; DLQ = 0; forwarding-failure counter ≈ 0; visitor-visible form latency unchanged |
| B2 | 50 | 1 business day | as B1, plus: worker error rate, worker p99, DB connection count (the #27 risk — this is the first time real concurrency touches RDS Proxy), cost/day tracking toward the §8 estimate |
| B3 | 100 | — (this is where §3's clock starts) | as B2, plus: tier distribution — a first look at what live traffic actually is, and the baseline the drift alarm will be measured against |

A 4xx at B1 is almost always the signature (§2.4) or a payload shape mismatch, and is a stop-and-fix,
not a ramp-onward.

### Stage C — shadow period at 100 % mirror

Per §3. Duration ≥ 10 business days and ≥ 100 compared leads. `PRIMARY_PCT` stays 0 throughout.

**Do not skip to stage D because things look fine.** Things looking fine after three days is exactly
what a badly calibrated rubric looks like after three days: the false disqualifications are the
cases nobody hears about, and they only surface when someone deliberately looks at what was binned.

### Stage D — promote to primary: 25 % → 50 % → 100 %

Only after every §3.6 criterion is met. Now the agent's email is the one reps act on, and the old
notification is suppressed **for the bucketed share only**. The old path keeps running for the rest.

| Step | `PRIMARY_PCT` | Hold | Watch |
|---|---|---|---|
| D1 | 25 | 2 business days | reps confirm they are getting and acting on agent emails for that share; no lead reported missing; feedback coverage holds; tier mix unchanged from stage C |
| D2 | 50 | 2 business days | as D1, plus response-time-to-lead versus the old path — if reps are slower to act on agent emails, the email format is the problem, not the rubric |
| D3 | 100 | 5 business days | full §7 watch list; the old path still delivers, to a **review mailbox** rather than to reps |

### Stage E — retire the old path

After a full business week at `PRIMARY_PCT = 100` with the old path shadowing into a review mailbox
and nothing found in it:

1. Confirm the review mailbox surfaced no lead that the agent path missed.
2. Turn off the old notification path.
3. **Record the decision** — date, the shadow numbers it was based on, who signed off — in the epic's
   decisions table and in §9 below. Six months from now, "why do we trust this?" must have an answer
   with numbers in it.
4. Keep the old path's code and config for **30 days** before deleting, so §5 remains possible.

---

## 5. Rollback

**This is the most important section in this document.** It is written to be executed by a stressed
person at an inconvenient hour who did not write any of this. Every decision is pre-made. Follow the
numbers in order.

### 5.1 Who can do it

- Anyone on the on-call list, **and** the website owner, **and** the ICP owner. Deliberately more
  than one person, and deliberately not only engineers.
- **No AWS console access is required.** **No code deploy is required.** **No engineer is required.**
  If any of those turn out to be needed, the rollback design is broken — fix that before it is
  needed for real.
- Nobody needs permission to roll back. Rolling back unnecessarily costs a day of shadow data.
  Rolling back too late costs deals.

### 5.2 Which rollback — decide mechanically, do not deliberate

| If… | Do |
|---|---|
| The **only** complaint is about qualification quality — wrong tiers, bad reasoning, unhelpful or ugly emails, too many `hot`, a rep says "this thing is rubbish" | **Rollback A (quality)** |
| **Any** of: ingest returning non-2xx, the DLQ alarm, the spend alarm, the DB-connection alarm, the SES bounce/complaint alarm, a suspected key or secret leak, a suspected prompt-injection incident, personal data appearing somewhere it should not | **Rollback B (availability / safety)** |
| You are not sure | **Rollback B.** B is strictly safer and costs only shadow data |

Do not spend time diagnosing before rolling back. Roll back, then diagnose.

### 5.3 Rollback A — quality (keep collecting data, remove authority)

The agent stops having any say in what sales does; leads keep flowing so the shadow data keeps
accruing and the problem stays observable.

1. Set `LEADQUALI_PRIMARY_PCT = 0` in `<SITE_CONFIG>`. Leave `LEADQUALI_MIRROR_PCT` unchanged.
2. Confirm the value is live: `curl -s <SITE_CONFIG_URL> | grep -i primary` (allow up to the 60 s TTL).
3. Confirm the old notification path is delivering for **all** leads: submit one test lead through
   the live form and confirm the old-path notification arrives.
4. Post in the incident channel: *"LeadQuali rolled back to shadow (quality). Sales: work leads from
   the normal notification as before. Agent emails are evaluation copies again — please keep clicking
   the feedback links."*
5. Do **not** touch AWS. The worker keeps processing; those emails are now shadow copies.
6. Record in §9: time, trigger, who.

### 5.4 Rollback B — availability or safety (stop sending traffic)

**The one change that reverts traffic is step 1. Everything after it is verification and care of
leads already in flight.**

1. **Set `LEADQUALI_MIRROR_PCT = 0` and `LEADQUALI_PRIMARY_PCT = 0` in `<SITE_CONFIG>`.**
   That is the rollback. No new lead is forwarded to the agent from this moment.
2. Confirm the config is live (≤ 60 s, per §2.6): `curl -s <SITE_CONFIG_URL>` and read both values.
3. Confirm no new leads are arriving:
   ```bash
   psql "$PROD_DSN" -c "select count(*) from leads
                        where tenant_id = '<TENANT>' and received_at > now() - interval '5 minutes';"
   ```
   Run it twice, five minutes apart. The count must go to 0. If it does not, the config did not
   propagate — check the CDN cache for `<SITE_CONFIG>` and purge it.
4. Submit one test lead through the live form. Confirm the visitor sees the normal thank-you and the
   old notification path delivers it. **The visitor-facing form must still work.** If it does not,
   that is now the incident: the forward is on the critical path, contrary to §2.8 — revert the form
   change itself.
5. **Do not touch the queue.** Specifically, and this is the part that gets got wrong under pressure:
   - **Do NOT** `sqs purge-queue`.
   - **Do NOT** delete or re-deploy the stack.
   - **Do NOT** disable the SQS event source mapping on the worker unless step 6 applies.
   Leads already enqueued are real leads from real visitors. They must still be assessed and routed.
   The design promises no lead is ever silently dropped, and a form rollback is not permission to
   break that promise.
6. **Only if the worker itself is the fault** (it is erroring on every message, leaking data, or
   spending uncontrollably), **pause** it — do not delete anything:
   ```bash
   aws lambda put-function-concurrency --region <REGION> \
     --function-name <PROD_STACK>-worker --reserved-concurrent-executions 0
   ```
   Messages stay on the queue. Retention is 14 days (§1.3), so the pause is safe for up to 14 days —
   after which leads are lost, so set a calendar reminder for **day 7** to force the issue.
   To resume after the fix: restore the original reserved concurrency
   (`--reserved-concurrent-executions <N>`, the value from the SAM template) and watch the queue drain.
7. **Drain what is in flight.** Watch until both are 0:
   ```bash
   watch -n 30 'aws sqs get-queue-attributes --region <REGION> --queue-url <QUEUE_URL> \
     --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible'
   ```
   Anything that lands in the DLQ during the drain gets redriven **after** the fix:
   ```bash
   aws sqs start-message-move-task --region <REGION> \
     --source-arn <DLQ_ARN> --destination-arn <QUEUE_ARN>
   ```
8. **Reconcile.** Every lead the site received during the incident must have reached a human by one
   route or the other. Compare the site's own lead log against `leads` and `routing_events` for the
   incident window. Anything in the site's log with no `routing_events` row gets forwarded to sales
   by hand, today, with an apology. This step is the whole reason the old path stays running.
9. Post in the incident channel: *"LeadQuali disabled (availability). No lead is lost — everything is
   flowing through the normal notification path. Leads already accepted are still being processed."*
10. Record in §9: time, trigger, who, how many leads were in flight, and the reconciliation result.

### 5.5 How long it takes, honestly

| From | To | Elapsed |
|---|---|---|
| Decision to roll back | Config value changed | ~30 seconds (one field) |
| Config changed | New page loads use it | ≤ 60 seconds (the `<SITE_CONFIG>` TTL) |
| Config changed | **Every** in-flight submission stopped | **Not bounded** — see below |
| Config changed | Queue fully drained | Queue depth × worker throughput; typically minutes |

**A browser that already has the page loaded holds the old configuration until the visitor reloads.**
Nothing on the server side changes that. So for up to a session's length after a rollback, a few
submissions will still be forwarded. Two consequences, both deliberate:

- **In Rollback A the endpoint stays up on purpose.** Those stragglers get a 202 and are assessed as
  shadow leads. Harmless.
- **In Rollback B those stragglers get whatever the endpoint is currently doing.** If it is erroring,
  they get a non-2xx, the form falls back per §2.7, and the visitor is unaffected. This is why §2.7's
  non-2xx behaviour and §2.8's never-block rule are load-bearing rather than tidy: they are what makes
  Rollback B safe for visitors mid-flight.

Do **not** try to close this window by taking the API Gateway down. That converts a graceful fallback
into timeouts on the website's forwarding function, which is worse for visitors and buys nothing.

### 5.6 Rollback rehearsal — before cutover day, not during

Execute Rollback B once during stage A or B, in full, with a stopwatch, with the person most likely
to be on call driving and the author of this runbook watching silently. Record the elapsed time in
§9. A rollback that has never been rehearsed is an aspiration.

---

## 6. Day 1

The first day of stage B, and again the first day of stage D. Someone owns the screen; it is not
"the team's" job.

- **Hour 1, every 10 minutes:** ingest 5xx/4xx, ingest p95, DLQ depth, worker errors, and the first
  real leads walked end-to-end by hand — `leads` → `assessments` → `routing_events` → the rep's
  inbox. Read three complete routing emails as a rep would. Do they tell a rep what to do?
- **Hour 2–4, every 30 minutes:** the same, plus tier distribution and cost accumulating in
  `assessments.cost_usd`.
- **End of day:**
  - Total leads, tier mix, DLQ depth (must be 0), forwarding-failure count.
  - Cost/day versus the §8 estimate ($0.02–0.03/lead).
  - `cache_read_tokens > 0` across the day's assessments — if not, the cost model is wrong and #11's
    prefix stability regressed.
  - **Ask two reps directly** whether the emails were useful and what they would change. Not a
    channel post — a message to a person.
  - Confirm at least one `feedback` row exists. If day 1 ends with zero feedback clicks, the loop is
    already broken; act on §8 immediately rather than waiting to see.

---

## 7. Week 1 watch list

| Signal | Where | What it means | First response |
|---|---|---|---|
| **DLQ depth > 0** | #29 alarm | A lead nobody has seen. Page-worthy | Read the message, root-cause, fix, **redrive** (§5.4 step 7). Every one is root-caused, per #30's acceptance criteria |
| **Tier-distribution drift** | #29 alarm, per tenant vs trailing 7-day | **The leading indicator.** A jump in `hot` is almost never a great sales week — it is a prompt change, a config change, or the website form changing shape (a renamed field arriving empty makes every lead look thin, or rich) | Check for a deploy and a website release in the same window; re-verify §2.5 fields in the live markup; compare `prompt_version` on recent assessments against the day before |
| **Worker error rate** | #29 alarm | Anthropic errors, DB errors, SES errors | Check which; Anthropic 429/5xx are retried by the SDK and then by SQS, so a sustained rate means something structural |
| **Ingest p95 / worker p99** | #29 alarm | Ingest > 200 ms means the 202 promise is broken | Ingest is not supposed to touch the model; if it is slow, something moved onto the wrong path |
| **DB connection count** | #29 alarm | The accepted #27 risk arriving | Check worker reserved concurrency and RDS Proxy pinning before raising the instance size |
| **SES bounce / complaint rate** | #29 alarm | *A rising bounce rate quietly kills the feedback loop* — the emails stop arriving and nothing else looks wrong | Check the bouncing addresses; a rep who changed name or left is the usual cause |
| **Daily token spend** | #29 alarm + the AWS Budget from #25 | Cost drift, or a bot flood reaching the model | Check volume first: a spend spike with flat lead count is a per-lead regression (caching lost, effort raised); a spend spike with lead count up is spam getting past the §2.5 pre-filters |
| **Feedback coverage** | `feedback` vs `routing_events`, daily | The health of the only ground truth that will ever exist | Below 50 %: this is a people problem, not a metrics problem. Go and ask a rep why, in person |
| **Forwarding failures** | website-side counter (§2.7) | The shadow sample is silently shrinking | A steady trickle is usually clock skew (§2.4) or timeouts; a cliff is an outage |
| **Cost per lead** | `select avg(cost_usd) from assessments where created_at > now() - interval '1 day'` | #30's acceptance criterion — must match §8 | Reconcile **before** scaling volume, not after |

### The daily feedback review — 15 minutes, every working day

This is a habit, not a task, and it is the mechanism by which this product gets better. Skipping it
is not a delay; it is permanent loss, because with no historical archive the golden set grows **only**
from the `feedback` table (#22). A week of skipped review is a week of product value that cannot be
recovered later.

Every working day:

```sql
-- Yesterday's disagreements — the leads with information in them.
select l.id, a.tier, a.total_score, a.confidence, f.verdict, f.notes, a.reasoning
from feedback f
join leads l on l.id = f.lead_id
join assessments a on a.lead_id = l.id
where f.created_at > now() - interval '1 day'
  and (f.verdict = 'bad' or (f.verdict = 'good' and a.tier in ('cold','disqualified')))
order by f.created_at desc;
```

1. Read each one. Decide: was the agent wrong, or was the rep's click wrong (a misclick, or a rep
   marking "bad" because the lead was rude rather than unqualified)?
2. Agent wrong → **add it to the golden set with the human's tier label** (#22). This is the entire
   growth mechanism for the golden set. Do it the same day, while the context is fresh.
3. A pattern across several → a rubric change, evaluated through #23/#24 with numbers, never a
   hand-edit to the prompt on a hunch. Note that a rubric change during shadow resets the clock (§3.7).
4. Log the count in §9. "Leads added to the golden set this week" is the number that says whether
   this system is compounding or standing still.

---

## 8. Comms

The feedback loop is a human loop. It only works if reps actually click, and they will only click if
someone told them why it matters and then thanked them for doing it. Budget real attention here; it
is not overhead around the technical work, it is the part the technical work depends on.

### Before cutover

| Who | What they are told | When |
|---|---|---|
| **Sales team** | The message below, in a meeting, not only in writing. Then again in writing so they can find it later | 2 days before stage B |
| **ICP owner** | The eval numbers (§1.7), the shadow exit criterion (§3.6), and that they are the adjudicator for tier disputes | Before stage B |
| **Whoever handles leads today** | Nothing changes for them until stage E. They will see duplicate-looking traffic and should not act on it | Before stage B |
| **Website owner** | The form change, the two ramp numbers, the rollback procedure, and that they can execute it | Before stage A |
| **On-call** | This runbook, the #29 alarm runbook, and the rehearsed rollback | Before stage A |

### During and after

- **Stage B start / stage D start / stage E:** one short note in the shared channel each time.
- **Daily during shadow:** a one-line status — leads, tier mix, feedback clicks, DLQ. Boring is the
  goal. A daily line also means that when a bad day comes, everyone already knows what a normal day
  looked like.
- **Weekly during shadow:** the §3.4 metrics, and *thank the reps by name for their clicks*, with the
  count. Attention follows acknowledgement. This is the cheapest reliable way to keep coverage above
  the 0.50 exit criterion.
- **Any rollback:** the message in §5.3/§5.4, immediately, before diagnosing.

### The message to the sales team

Adapt the wording; keep every element.

> **Subject: You'll start seeing a second email for each new website lead — and I need 2 seconds from
> you on each one**
>
> Starting <DATE>, every lead from the website form also gets read by our new qualification agent. It
> scores the lead, works out how promising it looks, and emails you a summary: the tier (hot / warm /
> cold), what it found out about the company, why it scored it that way, what it could not tell, and a
> suggested opening question.
>
> **For the next two weeks nothing changes about how you work.** Keep working leads exactly as you do
> now, from the notification you already get. The new email is marked **SHADOW** and is an evaluation
> copy — it exists so we can find out whether the thing is any good before we let it decide anything.
>
> **The one thing I need from you.** At the bottom of every one of those emails there are two links:
> **good lead** and **bad lead**. One click, no login, works on your phone. Please click one, on every
> email, even when you disagree — *especially* when you disagree.
>
> Here is why that matters more than it sounds. We have no historical lead data with outcomes. None.
> So there is no dataset anywhere that tells us whether this thing is right, and there never will be
> one — except the one your clicks create. Every click is a labelled example. Those examples are the
> only way the agent ever improves, and they are also the only evidence we will have that it works
> when we try to sell this. If nobody clicks, we are guessing, permanently.
>
> A "bad lead" click is the most valuable one you can send. It tells us something we could not have
> found any other way, and it directly changes how the next lead gets scored.
>
> If the emails are unhelpful, hard to read on a phone, or wrong in a way the two links do not
> capture — tell me. I will ask you directly at the end of week one, and I would rather hear it then
> than find out in a quarter.
>
> — <NAME>

Two things to watch for in how this lands: reps who stop clicking after the first few days (usually
because nothing visibly happened as a result — so tell them what changed because of their clicks),
and reps who click "good" on everything (usually because clicking is faster than reading — check
whether their good-clicks correlate with anything).

---

## 9. Definition of done

Matched to #30's scope and acceptance criteria. Fill this in as it happens; this table is the record
that the cutover was done rather than declared.

### Scope

- [ ] Prod stack deployed; migrations applied (§1.5); internal tenant seeded with the config measured
      in #24 — **the measured values, not the defaults**.
- [ ] Website form POSTs to `<LEADS_ENDPOINT>` with API key and HMAC signature, signed **server-side**
      (§2.2–2.4).
- [ ] The existing notification path ran **in parallel** for the whole shadow period and through
      stage D. No blind cutover happened at any point.
- [ ] Shadow period completed: agent tier compared against human judgement over ≥ 10 business days and
      ≥ 100 compared leads; every disagreement fed into the golden set (#22).
- [ ] Honeypot (`company_website_2`) and timing (`form_elapsed_ms`) fields **confirmed present in the
      live production markup**, so the #17 pre-filters are not inert.
- [ ] Feedback links verified from a real inbox on a real phone (§1.1).
- [ ] Old path retired after the dual-run, and **the decision recorded** with the numbers behind it.

### Acceptance criteria

- [ ] **Live leads flow end-to-end**: form → 202 → assessment → routing email → feedback click →
      `feedback` row. Verified on real traffic, not only on the synthetic lead.
- [ ] **No DLQ messages during the first week, or every one root-caused** — and its lead confirmed
      processed after redrive.
- [ ] **Sales reps report the emails are useful — asked explicitly.** ≥ 3 reps, in their own words,
      in writing. Silence is not agreement.
- [ ] **Measured cost/day matches the §8 estimate** ($0.02–0.03/lead; ~$25/month at 1 000/month), or
      is reconciled in writing **before** volume is scaled.

### Execution log

| Date | Stage | Numbers | Who | Notes |
|---|---|---|---|---|
| | pre-flight gate (§1.8) | | | |
| | rollback rehearsal (§5.6) | elapsed: | | |
| | stage A | | | |
| | stage B1 / B2 / B3 | | | |
| | shadow start | eval baseline from §1.7: | | |
| | shadow exit review | §3.6 criteria 1–10: | | |
| | stage D1 / D2 / D3 | | | |
| | stage E — old path retired | | | |

---

## 10. Related documents

- Plan §3 (architecture, the 202 split), §8 (cost, alarms, security), §9 (phases) —
  [`docs/IMPLEMENTATION_PLAN.md`](../IMPLEMENTATION_PLAN.md)
- Alarm runbook and DLQ redrive detail — [#29](https://github.com/vendo-aron/JAT-LeadQuali/issues/29)
- Ingest contract, pre-filters, idempotency — [#17](https://github.com/vendo-aron/JAT-LeadQuali/issues/17)
- Routing email and feedback links — [#19](https://github.com/vendo-aron/JAT-LeadQuali/issues/19)
- SES setup and deliverability — [#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20)
- Golden set (standing task) — [#22](https://github.com/vendo-aron/JAT-LeadQuali/issues/22)
- Eval harness and metric definitions — [#23](https://github.com/vendo-aron/JAT-LeadQuali/issues/23)
- Effort sweep and the measured config — [#24](https://github.com/vendo-aron/JAT-LeadQuali/issues/24)
- SAM template, queue and DLQ topology — [#26](https://github.com/vendo-aron/JAT-LeadQuali/issues/26)
- Secrets and KMS — [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28)
