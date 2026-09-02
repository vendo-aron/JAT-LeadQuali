# Runbook — Amazon SES setup: domain verification, DKIM, sandbox exit

> ## ⚠️ Start this on day one of Phase 2
>
> This is the only task in Phase 2 with an **external wait you cannot compress**. AWS reviews
> production-access requests by hand — historically up to ~24 hours, and sometimes longer over a
> weekend or a holiday. DNS propagation is a second, independent wait. Nothing in this runbook is
> hard, but if you start it in the last week of Phase 2 you will sit idle waiting for other people.
>
> **File the production-access request on the first day of Phase 2, before writing any of #19.**

**Issue:** [#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20) · **Phase 2 — Pipeline** ·
Manual AWS console + DNS work · ~0.5 day of hands-on time, plus review latency.

**Depends on:** [#2](https://github.com/vendo-aron/JAT-LeadQuali/issues/2) (volume estimate) and
[#25](https://github.com/vendo-aron/JAT-LeadQuali/issues/25) (an AWS account with a deploy role
exists). **Blocks:** end-to-end verification of
[#19](https://github.com/vendo-aron/JAT-LeadQuali/issues/19).

---

## Who runs this

**No AWS account, credentials, or live SES access exist in the development environment this
document was written in.** Every step below is the project owner's to execute, by hand, against a
real AWS account and a real DNS zone. Nothing here can be automated away by the codebase or
verified by CI, and no test in this repository asserts against live SES.

The runbook *is* the deliverable for #20. Working through it and ticking the
[definition of done](#definition-of-done) closes the issue; record the actual values you get in
your own secure notes (not in this repository).

Commands are written for the AWS CLI v2 with a profile that can administer SES. Set these once per
shell and every command below is copy-pasteable:

```bash
export AWS_PROFILE=leadquali-prod        # the profile from #25
export AWS_REGION=eu-central-1           # your chosen region — see Phase 0
export SES_DOMAIN=vendoworks.com         # your sending domain
export SES_MAIL_FROM=bounce.vendoworks.com   # custom MAIL FROM subdomain — see Phase 2
```

---

## Timeline: what is blocked while you wait

| When | What you do | What is blocked meanwhile |
|---|---|---|
| **Day 1, hour 0** | Phases 0–2: choose region, create the domain identity, publish DKIM + SPF + MAIL FROM + DMARC DNS records. | Nothing yet. |
| **Day 1, hour 1** | Phase 3: verify the sales recipient addresses (sandbox requirement) and file the production-access request **immediately** — do not wait for DKIM to finish verifying. | Nothing. Sandbox sending to verified recipients is enough to build #19 against. |
| **Day 1, +5 min to ~72 h** | **Wait: DNS propagation + DKIM verification.** Usually minutes to a couple of hours. | #19's *real* send path. Build #19 against the fake `Notifier` and the SES mailbox simulator in the meantime. |
| **Day 1 → Day 2+** | **Wait: AWS production-access review.** Historically ~24 h; plan for longer. | #19's acceptance criterion "a test send reaches a sales inbox"; anything sending to an address you have not individually verified; realistic volume testing. |
| **After DKIM verifies** | Phases 4–5: configuration set, bounce/complaint SNS topic, test send, header check. | — |
| **After production access** | Phase 6 + full verification. Record the granted quota. | [#29](https://github.com/vendo-aron/JAT-LeadQuali/issues/29) needs real bounce/complaint metrics to alarm on; [#30](https://github.com/vendo-aron/JAT-LeadQuali/issues/30) cutover needs an out-of-sandbox account. |

**What is *not* blocked.** Do not let this stall #19. The `Notifier` port means the adapter can be
developed and unit-tested against a fake, and the SES **mailbox simulator** addresses
(`success@simulator.amazonses.com`, `bounce@simulator.amazonses.com`,
`complaint@simulator.amazonses.com`) work **inside the sandbox without verification**. Only the
last mile — "a real rep receives a readable email, and DKIM and SPF pass in the received headers" —
genuinely waits on this runbook.

---

## Phase 0 — Choose the region

SES is regional, and this choice is more load-bearing than it looks.

1. Decide the region you will deploy the Lambdas into in
   [#26](https://github.com/vendo-aron/JAT-LeadQuali/issues/26) and the RDS instance into
   [#27](https://github.com/vendo-aron/JAT-LeadQuali/issues/27). **Use that same region for SES.**
   For an EU customer base with EU personal data (plan §8: leads are personal data), `eu-central-1`
   or `eu-west-1` is the sane default.
2. Confirm SES is available there and note the region's sending endpoint
   (`email.<region>.amazonaws.com`).

**Why the region must match the Lambdas:**

- **Latency.** The worker Lambda calls SES synchronously on the routing path. A cross-region call
  adds tens of milliseconds of round-trip for no benefit — avoidable latency on the one call that
  stands between a hot lead and a salesperson's inbox.
- **An extra egress path.** Traffic to an out-of-region endpoint leaves the VPC's region. With the
  private subnets and NAT of #27 that is a second network path to configure, pay for, monitor, and
  reason about in a security review. A same-region SES endpoint can be reached through a VPC
  endpoint instead, and never traverses the public internet.
- **Region-scoped resources.** Identities, configuration sets, sending quotas, reputation metrics,
  and the suppression list are all **per region**. A configuration set named `leadquali-prod` in
  `eu-west-1` does not exist in `eu-central-1`.
- **The MAIL FROM MX record encodes the region** (`feedback-smtp.<region>.amazonses.com`). Moving
  region later means re-verifying the identity *and* re-doing DNS *and* waiting again.
- **Data residency.** Message content transits the SES region. If you told a customer their lead
  data stays in the EU, SES must be in the EU too.

> Changing region after production access is granted means requesting production access **again** —
> the grant is per-region. Get this right now.

### ✅ Verify Phase 0

```bash
aws sesv2 get-account --region "$AWS_REGION" --query 'Details.MailType' --output text
```

A response (rather than an endpoint error) proves SES is reachable in that region with your
credentials. Write the region down; it is one of the three values the app needs later.

---

## Phase 1 — Create the domain identity

**Verify a domain, not a single email address.**

1. Console → **Amazon SES** → confirm the region selector matches Phase 0 → **Identities** →
   **Create identity**.
2. Choose **Domain**. Enter `vendoworks.com`, or a dedicated subdomain such as
   `mail.vendoworks.com` if you want the app's mail reputation kept separate from the corporate
   mail your staff send by hand.
3. Leave **Easy DKIM** selected with **RSA_2048_BIT** (Phase 2 covers it).
4. Do **not** tick "Use a custom MAIL FROM domain" yet if you want to keep the DNS changes in two
   reviewable batches — Phase 2 adds it either way.

Or via the CLI:

```bash
aws sesv2 create-email-identity \
  --region "$AWS_REGION" \
  --email-identity "$SES_DOMAIN" \
  --dkim-signing-attributes NextSigningKeyLength=RSA_2048_BIT
```

**Why a domain identity and not `leads@vendoworks.com`:**

- **The From address must survive a change of sender.** #19's routing email will change its From at
  least once — `leads@` becomes `no-reply@`, or a per-tenant `leads+acme@`, or a friendlier
  `qualification@`. With a domain identity that is a one-line config change. With an address
  identity it is a new verification email to an inbox someone has to still control, plus a
  deploy — a change that should take a minute takes a day.
- **Multi-tenancy (Phase 5).** Per-tenant sender addresses under one verified domain cost nothing.
  Verifying an address per tenant does not scale.
- **DKIM alignment, and therefore DMARC.** Only a domain identity gets Easy DKIM signing with a
  `d=` of *your* domain. An address identity signs under `amazonses.com`, so neither SPF nor DKIM
  aligns with your From domain and any DMARC policy you publish will fail your own mail.
- **Custom MAIL FROM requires a domain identity.**
- **Bounce handling is per identity.** One identity means one configuration set, one SNS topic, one
  reputation dashboard — not one per address.

### ✅ Verify Phase 1

```bash
aws sesv2 get-email-identity --region "$AWS_REGION" --email-identity "$SES_DOMAIN" \
  --query '{type:IdentityType,verified:VerifiedForSendingStatus,dkim:DkimAttributes.Status}'
```

Expect `type: DOMAIN` and, at this point, `verified: false` / `dkim: PENDING`. That is correct —
you have not published DNS yet.

---

## Phase 2 — DNS: DKIM, SPF, MAIL FROM, DMARC

This is the phase where mistakes are expensive, because each one costs another propagation wait.
Publish **all** the records in one sitting.

Retrieve every value SES wants:

```bash
# Three DKIM CNAME tokens
aws sesv2 get-email-identity --region "$AWS_REGION" --email-identity "$SES_DOMAIN" \
  --query 'DkimAttributes.Tokens' --output text
```

### 2.1 Easy DKIM — three CNAME records

SES gives three tokens. For each token `T`, create:

| Type | Name | Value | TTL |
|---|---|---|---|
| CNAME | `T._domainkey.vendoworks.com` | `T.dkim.amazonses.com` | 1800 |

All three. Two out of three leaves DKIM `PENDING` forever.

**Gotchas that cost a day each:**

- Many DNS UIs append the zone automatically. If your provider asks for the record *name* relative
  to the zone, enter `T._domainkey` — entering the FQDN produces
  `T._domainkey.vendoworks.com.vendoworks.com`.
- Some providers silently require a trailing dot on the CNAME target.
- CNAME targets must not be flattened, proxied, or "CDN-ified". On Cloudflare, set these to
  **DNS only** (grey cloud), never proxied.

### 2.2 Custom MAIL FROM subdomain — MX + SPF

**Decision: use a custom MAIL FROM.** It is optional in SES and recommended here.

Without it, SES uses `amazonses.com` as the envelope MAIL FROM. Mail still delivers and DKIM still
aligns, but the SPF check authenticates `amazonses.com`, not you — so **SPF is unaligned for
DMARC**, and your DMARC result then rests on DKIM alone. One DNS misconfiguration or one forwarding
hop that breaks the DKIM signature and the message fails DMARC outright. A custom MAIL FROM gives
you two independent aligned authentication mechanisms instead of one. It also puts bounce traffic
on a subdomain you control, so a bad bounce run damages `bounce.vendoworks.com`, not the domain
your CEO sends from.

Choose a subdomain that is used for nothing else — `bounce.vendoworks.com` or
`mail.vendoworks.com`. Then:

```bash
aws sesv2 put-email-identity-mail-from-attributes \
  --region "$AWS_REGION" \
  --email-identity "$SES_DOMAIN" \
  --mail-from-domain "$SES_MAIL_FROM" \
  --behavior-on-mx-failure USE_DEFAULT_VALUE
```

`USE_DEFAULT_VALUE` means "if the MX record ever disappears, fall back to `amazonses.com` rather
than refusing to send". For a system whose whole job is to not drop leads, degrading beats failing.

Records to create on the **MAIL FROM subdomain**:

| Type | Name | Value | Note |
|---|---|---|---|
| MX | `bounce.vendoworks.com` | `10 feedback-smtp.eu-central-1.amazonses.com` | **Region-specific** — must match Phase 0 |
| TXT | `bounce.vendoworks.com` | `"v=spf1 include:amazonses.com -all"` | Authorises SES for the envelope domain |

### 2.3 SPF on the sending domain

On `vendoworks.com` itself, you almost certainly already have an SPF record (Google Workspace,
Microsoft 365, …). **Edit the existing one — never add a second.** A domain with two `v=spf1` TXT
records is a PermError and *every* SPF check fails, including the mail your company already sends.

| Type | Name | Value |
|---|---|---|
| TXT | `vendoworks.com` | `"v=spf1 include:_spf.google.com include:amazonses.com ~all"` |

Keep the record under the 10-DNS-lookup limit; each `include:` costs at least one.

### 2.4 DMARC

Start at `p=none` so you get reports without risking delivery of anything, and tighten to
`quarantine` and then `reject` only after a couple of weeks of clean reports.

| Type | Name | Value |
|---|---|---|
| TXT | `_dmarc.vendoworks.com` | `"v=DMARC1; p=none; rua=mailto:dmarc@vendoworks.com; fo=1; adkim=r; aspf=r"` |

Without SPF and DMARC in place the routing emails land in spam and, worse, the deliverability
failure is *silent* — a lead is "sent" from the app's point of view and never read by a human.
Relaxed alignment (`adkim=r`, `aspf=r`) is what lets `bounce.vendoworks.com` authenticate mail from
`vendoworks.com`.

### 2.5 The other wait: DNS propagation

Publishing is instant; **propagation is not**. Resolvers honour the TTL of the record that was
previously cached, so a zone that used to serve a 24-hour TTL can take that long to show a new
record to some resolvers. SES itself re-checks periodically and can take up to 72 hours to flip an
identity to `Verified`, though minutes to an hour is typical.

Two practical moves: lower the TTL on records you are about to change *before* changing them, and
always confirm against the **authoritative** nameserver rather than your local resolver's cache.

### ✅ Verify Phase 2 — `dig`

```bash
# 1. Authoritative nameservers for the zone (query these directly to skip caches)
dig +short NS "$SES_DOMAIN"
NS=$(dig +short NS "$SES_DOMAIN" | head -1)

# 2. DKIM — run once per token; each must return <token>.dkim.amazonses.com
for T in $(aws sesv2 get-email-identity --region "$AWS_REGION" \
             --email-identity "$SES_DOMAIN" --query 'DkimAttributes.Tokens' --output text); do
  echo "== $T"; dig @"$NS" +short CNAME "${T}._domainkey.${SES_DOMAIN}"
done

# 3. MAIL FROM MX — expect: 10 feedback-smtp.<region>.amazonses.com.
dig @"$NS" +short MX "$SES_MAIL_FROM"

# 4. MAIL FROM SPF — expect exactly one v=spf1 record
dig @"$NS" +short TXT "$SES_MAIL_FROM"

# 5. Sending-domain SPF — expect exactly ONE v=spf1 line, including amazonses.com
dig @"$NS" +short TXT "$SES_DOMAIN" | grep spf1

# 6. DMARC
dig @"$NS" +short TXT "_dmarc.${SES_DOMAIN}"
```

Then confirm SES agrees — this is the gate for the rest of the runbook:

```bash
aws sesv2 get-email-identity --region "$AWS_REGION" --email-identity "$SES_DOMAIN" \
  --query '{verified:VerifiedForSendingStatus,dkim:DkimAttributes.Status,mailfrom:MailFromAttributes.MailFromDomainStatus}'
```

Target state: `verified: true`, `dkim: SUCCESS`, `mailfrom: SUCCESS`. Anything still `PENDING`
after an hour means re-reading the `dig` output above, not waiting longer.

---

## Phase 3 — The sandbox, and getting out of it

**File this on day one.** It is the long pole.

### 3.1 What the sandbox actually restricts

Every new SES account, in every region, starts in the sandbox:

| Restriction | Value | What it means for this project |
|---|---|---|
| Recipients | **Only verified addresses or domains** | You cannot email a sales rep until you have verified their address individually. `MessageRejected: Email address is not verified` is the error. |
| Volume | **200 messages / 24 h** | Fine for development; a single load test will blow through it. |
| Rate | **1 message / second** | The worker Lambda's concurrency (#27) can exceed this trivially. |

The **sender** must be verified in *both* states — leaving the sandbox does not remove the identity
requirement, it removes the *recipient* requirement and raises the limits. The mailbox simulator
addresses are exempt from the recipient rule in the sandbox, which is what makes them useful for
testing bounce and complaint handling before you are out.

### 3.2 Verify the sales recipient addresses (needed while in the sandbox)

```bash
aws sesv2 create-email-identity --region "$AWS_REGION" --email-identity sales@vendoworks.com
```

The owner of that mailbox clicks the link in the confirmation email, which expires in 24 hours. Do
this for every address `cfg.action_for(tier)` in #19 can route to.

### 3.3 Request production access

Console → **Amazon SES** → **Account dashboard** → **Request production access**. Or:

```bash
aws sesv2 put-account-details \
  --region "$AWS_REGION" \
  --production-access-enabled \
  --mail-type TRANSACTIONAL \
  --website-url "https://vendoworks.com" \
  --contact-language EN \
  --additional-contact-email-addresses ops@vendoworks.com \
  --use-case-description "$(cat use-case.txt)"
```

**Exactly what to write.** Approval first time comes from answering the reviewer's four unspoken
questions — *where do the addresses come from, how many, what happens on a bounce, and how does
someone stop the mail* — concretely and without marketing language. Copy this, substitute the real
figures, and keep it factual:

> **Mail type:** Transactional.
>
> **Use case.** We operate an internal lead-qualification service for our own website's contact
> form. When a prospect submits the form, the service scores the enquiry and sends **one
> notification email to our own sales team's internal mailbox** containing the enquiry details and
> the qualification result. Recipients are named employees of the company operating the service —
> our own staff, and in future our customers' own sales staff under contract. **There is no
> marketing list, no purchased or rented list, no bulk mail, and no mail is ever sent to the person
> who submitted the form.**
>
> **Volume.** Approximately **1,000 emails per month — about 35 per day, with an expected peak of
> around 100 on a busy day**. One email per qualified enquiry, plus a small number of operational
> notifications. We do not expect this to grow beyond a few thousand per month in the next twelve
> months. (Figure taken from our own capacity plan for inbound form volume.)
>
> **Recipient list management.** The recipient set is a short, static list of internal mailboxes
> held in our application configuration and changed only by an administrator. Addresses are never
> harvested, imported, or bought. Because these are transactional notifications to our own
> employees about our own business enquiries, an unsubscribe link is not applicable; a recipient is
> removed by an administrator editing the configuration, and we will honour any such request
> immediately.
>
> **Bounces and complaints.** All sending goes through an SES configuration set with event
> publishing for `bounce`, `complaint`, `delivery`, `reject` and `send` to Amazon SNS and Amazon
> CloudWatch. A CloudWatch alarm pages our on-call engineer when the bounce rate exceeds 2% or any
> complaint is recorded. Hard-bounced addresses are removed from the routing configuration and
> suppressed; we rely on the SES account-level suppression list in addition. Because the recipients
> are a handful of known internal mailboxes rather than an acquired list, we expect a bounce rate
> at or near zero, and we monitor it so that a mailbox being decommissioned is noticed immediately
> rather than accumulating failures.
>
> **Sending domain.** `vendoworks.com`, verified in this region with Easy DKIM, a custom MAIL FROM
> subdomain (`bounce.vendoworks.com`) with matching SPF, and a published DMARC policy.

Points that matter to a reviewer, and why:

- **"Transactional", stated plainly.** A request that reads like bulk marketing gets extra scrutiny.
- **A real number, not "low volume".** 1,000/month with a daily figure and a peak is checkable
  against your requested quota; "low" is not.
- **Who the recipients are.** The single strongest fact in the request: the recipients are the
  customer's own sales team, not a list. Nobody there can complain about receiving mail they asked
  their own employer to send them. Say it explicitly.
- **Bounce and complaint handling described as already built**, not planned — which is why Phase 4
  is worth doing before or alongside the request.
- **Unsubscribe addressed, not ignored.** Say *why* it does not apply and how removal works anyway.
  Silence on this reads as evasion.

### 3.4 If it is rejected

A rejection arrives as a reply in an AWS Support case, and it almost always names a specific
concern.

1. **Reply in the same case.** Do not open a new request or re-submit through the console; a
   duplicate request slows you down and looks worse.
2. **Answer the specific objection with specifics.** The usual ones:
   - *"Your use case is unclear"* → describe the trigger, the single recipient, and the content in
     two sentences. Offer a screenshot of the form and a sample of the email body.
   - *"How did you obtain the recipients' consent?"* → these are employees of the account holder
     acting in the course of their work; there is no acquired list.
   - *"Describe your bounce and complaint process"* → paste the configuration set name, the SNS
     topic ARN, and the alarm threshold. Concrete ARNs are far more convincing than a description.
   - *"Volume seems inconsistent with your quota request"* → ask for a quota near your real need
     (say 1,000–2,000/day), not an inflated one.
3. **Fix anything genuinely missing first** — a domain still `PENDING`, no DMARC, no configuration
   set — then reply. Replying with the gap closed converts most rejections.
4. **Meanwhile, keep working in the sandbox.** Verified internal recipients plus the mailbox
   simulator cover everything except the final acceptance criterion.
5. If a second round fails, escalate through your AWS account team or a support case at Developer
   support tier. Record a **dated note** of what is outstanding — issue #20's acceptance criteria
   explicitly allow "production access granted, **or** a dated note explaining what is
   outstanding".

### ✅ Verify Phase 3

```bash
aws sesv2 get-account --region "$AWS_REGION" \
  --query '{sandbox:ProductionAccessEnabled,quota:SendQuota,enabled:SendingEnabled,status:EnforcementStatus}'
```

`sandbox: true` means you are **out** of the sandbox (the field is "production access enabled").
Record `SendQuota.Max24HourSend` and `MaxSendRate` and check them against expected volume: at
1,000 leads/month you need ~35/day sustained and headroom for a burst — a default grant of
50,000/day at 14/s is ample. If the granted quota is *below* your peak, request an increase now
rather than discovering it during a busy week.

---

## Phase 4 — Bounce and complaint handling

### 4.1 Why this is not optional

SES reputation is enforced per account, per region, on two ratios:

| Metric | Healthy | AWS review | Risk of sending pause |
|---|---|---|---|
| Bounce rate | < 2% | ≥ 5% | ≥ 10% |
| Complaint rate | < 0.1% | ≥ 0.1% | ≥ 0.5% |

An unhandled bounce rate does not just degrade delivery of the offending message — **AWS can place
the entire account under review and ultimately pause sending for the whole region**. That is every
routing email for every tenant, stopped, because one mailbox was decommissioned and nobody noticed
the failures accumulating. The blast radius is the sending identity itself, not the message.

**In our case the bounce risk is genuinely low**: the routing email goes to a known internal sales
address that the account holder controls, not to an acquired list. But the monitoring is still
required, for two reasons. First, low-volume accounts are the *most* exposed to the ratios — at 35
messages a day, **two** hard bounces is a 5% bounce rate and an AWS review. Second, the resell case
in Phase 5 puts customers' addresses in the recipient set, on domains you do not control, changing
without warning; by then the monitoring must already exist and be trusted.

### 4.2 Configuration set

Create one configuration set and make every send reference it, so metrics are attributable.

```bash
aws sesv2 create-configuration-set \
  --region "$AWS_REGION" \
  --configuration-set-name leadquali-prod \
  --reputation-options ReputationMetricsEnabled=true \
  --delivery-options TlsPolicy=REQUIRE \
  --suppression-options SuppressedReasons=BOUNCE,COMPLAINT
```

- `ReputationMetricsEnabled` publishes per-configuration-set bounce and complaint metrics to
  CloudWatch — the data source for the alarms in #29.
- `TlsPolicy=REQUIRE` refuses to deliver over an unencrypted connection. Leads are personal data
  (plan §8); a routing email contains a name, an email address, and a business enquiry.
- Suppression on `BOUNCE,COMPLAINT` means SES itself stops re-sending to an address that already
  hard-bounced, which is the single most effective protection for the ratio.

### 4.3 SNS topic and event destinations

```bash
# A topic for events a human must act on
TOPIC_ARN=$(aws sns create-topic --region "$AWS_REGION" --name leadquali-ses-events \
  --query TopicArn --output text)
aws sns subscribe --region "$AWS_REGION" --topic-arn "$TOPIC_ARN" \
  --protocol email --notification-endpoint ops@vendoworks.com

# Actionable events → SNS
aws sesv2 create-configuration-set-event-destination \
  --region "$AWS_REGION" \
  --configuration-set-name leadquali-prod \
  --event-destination-name sns-bounces \
  --event-destination "Enabled=true,SnsDestination={TopicArn=$TOPIC_ARN},MatchingEventTypes=BOUNCE,COMPLAINT,REJECT,RENDERING_FAILURE"

# Everything countable → CloudWatch, for the #29 dashboard and alarms
aws sesv2 create-configuration-set-event-destination \
  --region "$AWS_REGION" \
  --configuration-set-name leadquali-prod \
  --event-destination-name cw-metrics \
  --event-destination "Enabled=true,CloudWatchDestination={DimensionConfigurations=[{DimensionName=ses:configuration-set,DimensionValueSource=MESSAGE_TAG,DefaultDimensionValue=leadquali-prod}]},MatchingEventTypes=SEND,DELIVERY,BOUNCE,COMPLAINT,REJECT"
```

Two destinations on purpose: SNS is for the handful of events that need a human today; CloudWatch
is for the counters #29 alarms on. Do not put every `DELIVERY` event into an email topic.

> **Later, not now.** Subscribing a Lambda to `$TOPIC_ARN` to write bounces back into the database
> and quarantine the address belongs to the Phase 5 resell work, not to #20. For v1, an email to
> ops plus the SES suppression list is the correct amount of machinery. Confirm the SNS email
> subscription — an unconfirmed subscription delivers nothing and looks identical to "no bounces".

### ✅ Verify Phase 4

```bash
aws sesv2 get-configuration-set --region "$AWS_REGION" --configuration-set-name leadquali-prod
aws sesv2 get-configuration-set-event-destinations --region "$AWS_REGION" \
  --configuration-set-name leadquali-prod --query 'EventDestinations[].{name:Name,on:Enabled,types:MatchingEventTypes}'
aws sns list-subscriptions-by-topic --region "$AWS_REGION" --topic-arn "$TOPIC_ARN" \
  --query 'Subscriptions[].{ep:Endpoint,arn:SubscriptionArn}'
```

A `SubscriptionArn` of `PendingConfirmation` means nobody clicked the confirmation link. Then force
a real bounce through the simulator and confirm it arrives:

```bash
aws sesv2 send-email --region "$AWS_REGION" \
  --from-email-address "leads@${SES_DOMAIN}" \
  --configuration-set-name leadquali-prod \
  --destination ToAddresses=bounce@simulator.amazonses.com \
  --content 'Simple={Subject={Data=bounce drill,Charset=UTF-8},Body={Text={Data=bounce drill,Charset=UTF-8}}}'
```

Within a minute or two you should get the SNS notification, and `Bounce` should appear in
CloudWatch under the `AWS/SES` namespace for the `leadquali-prod` configuration set. Repeat with
`complaint@simulator.amazonses.com`. **Simulator bounces do not count against your reputation
metrics** — which is exactly why they are safe to drill with. Issue #20's acceptance criterion
"bounce/complaint events appear in CloudWatch" is satisfied here.

---

## Phase 5 — The values the code needs, and how they reach it

Three values, and one non-value that matters:

| Value | Example | Consumed by |
|---|---|---|
| Sender address | `leads@vendoworks.com` | #19 `adapters/notify_ses.py` — the `From` |
| Configuration set name | `leadquali-prod` | #19 — passed on every send so #29 has metrics |
| SES region | `eu-central-1` | the boto3 SES client |
| *(no credential)* | — | see below |

**There is no SES secret.** The worker Lambda sends through its IAM execution role
(`ses:SendEmail` scoped to the identity ARN and the configuration set), so there is no SMTP
username or password to store anywhere. Do not create SES SMTP credentials for this project; an
IAM role you cannot leak beats a credential you must rotate.

**How the three values reach the app** (forward pointer to
[#26](https://github.com/vendo-aron/JAT-LeadQuali/issues/26) and
[#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28)):

- They are **configuration, not secrets** — but they are also environment-specific, so **they never
  appear as literals in source**. No default in `config.py` that happens to be the production
  address.
- They are declared as **SAM template parameters** in `infra/template.yaml` (#26) and passed into
  the worker Lambda as environment variables. Staging and production differ by parameter values,
  not by code.
- **Secrets Manager (#28) is for the credential-shaped things** — the Anthropic API key, per-tenant
  HMAC secrets, database credentials. Putting a non-secret like a configuration set name in there
  buys nothing and costs an API call on every cold start.
- Proposed environment variable names — **confirm these against `src/leadquali/config.py` before
  hard-coding them anywhere**, since #19 and #28 own that file:

  | Env var | Value |
  |---|---|
  | `LEADQUALI_SES_SENDER` | `leads@vendoworks.com` |
  | `LEADQUALI_SES_CONFIGURATION_SET` | `leadquali-prod` |
  | `LEADQUALI_SES_REGION` | `eu-central-1` |

  Note that `AWS_REGION` is set by the Lambda runtime itself and must not be overridden in the SAM
  environment block; a distinct `LEADQUALI_SES_REGION` also leaves the door open to a deliberate
  cross-region SES setup later without a code change.
- For local development (`run_local.py`, Phase 2 end-to-end), put these in an untracked `.env`.
  Confirm `.env` is git-ignored before you write a real address into it.

### ✅ Verify Phase 5

Record the three values in the project's secure notes (a password manager or the AWS account's own
parameter store) — **not in this repository, not in a commit message, not in an issue comment**.
Then confirm they resolve:

```bash
aws sesv2 get-configuration-set --region "$AWS_REGION" \
  --configuration-set-name "$LEADQUALI_SES_CONFIGURATION_SET" >/dev/null && echo "config set OK"
```

---

## Phase 6 — End-to-end verification

Run this once everything above is green. It is what actually closes the issue.

### 6.1 Identity and account state

```bash
# Classic API — the exact call named in issue #20
aws ses get-identity-verification-attributes --region "$AWS_REGION" \
  --identities "$SES_DOMAIN"
# expect: "VerificationStatus": "Success"

aws ses get-identity-dkim-attributes --region "$AWS_REGION" --identities "$SES_DOMAIN"
# expect: DkimEnabled true, DkimVerificationStatus "Success"

# v2 API — account-level state
aws sesv2 get-account --region "$AWS_REGION" \
  --query '{production:ProductionAccessEnabled,sending:SendingEnabled,enforcement:EnforcementStatus,quota:SendQuota}'
# expect: production true, sending true, enforcement "HEALTHY"
```

### 6.2 A real test send to the sales address

```bash
aws sesv2 send-email \
  --region "$AWS_REGION" \
  --from-email-address "leads@${SES_DOMAIN}" \
  --configuration-set-name leadquali-prod \
  --destination ToAddresses=sales@vendoworks.com \
  --content 'Simple={Subject={Data=[LeadQuali] SES verification test,Charset=UTF-8},Body={Text={Data=If you can read this in the inbox (not spam) SES is configured.,Charset=UTF-8},Html={Data=<p>If you can read this in the <b>inbox</b> (not spam) SES is configured.</p>,Charset=UTF-8}}}'
```

**What a successful send looks like:**

1. The CLI returns a `MessageId` (`010f0192...-000000`) and exit status 0. That means *accepted for
   delivery*, nothing more — it is not proof of delivery.
2. The message arrives in the **inbox** of `sales@vendoworks.com` within seconds. Spam folder does
   not count.
3. Open the raw headers (Gmail: **Show original**; Outlook: **View source**) and check:

   ```
   Authentication-Results: mx.google.com;
          dkim=pass header.i=@vendoworks.com header.s=<token> ;
          spf=pass (... domain of bounce@bounce.vendoworks.com designates ...) ;
          dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=vendoworks.com
   ```

   All three must say `pass`, and `dkim` must show `header.i=@vendoworks.com` — **not**
   `@amazonses.com`. A `dkim=pass` on `amazonses.com` means the domain identity is not the one
   signing and the DMARC alignment you think you have does not exist.
4. Gmail's **Show original** page also prints DKIM/SPF/DMARC as a summary table — quicker than
   reading headers, and worth screenshotting into the issue as evidence.

Also drill the failure path once with `bounce@simulator.amazonses.com` (Phase 4) so you have seen
both outcomes before real traffic does.

### 6.3 Sending statistics

Where to look, in increasing order of usefulness:

```bash
# Last two weeks of aggregate send/bounce/complaint/reject counts
aws ses get-send-statistics --region "$AWS_REGION" \
  --query 'SendDataPoints | sort_by(@,&Timestamp)[-5:]'

# Today's usage against the granted quota
aws ses get-send-quota --region "$AWS_REGION"
```

In the console: **SES → Account dashboard** for the reputation summary (bounce and complaint rates
with the review thresholds marked), and **SES → Configuration sets → `leadquali-prod`** for
per-configuration-set metrics. The same counters appear in CloudWatch under the `AWS/SES`
namespace, which is what #29 builds the dashboard and alarms from. Confirm your test send and your
simulator bounce both show up there — if they do not, the event destinations from Phase 4 are wrong
and the alarms in #29 will be silently blind.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MessageRejected: Email address is not verified. The following identities failed the check in region <R>: sales@…` | You are in the sandbox and the **recipient** is not verified. (Also appears if the *sender* identity is not verified, or is verified in a different region.) | Verify the recipient (`create-email-identity`), or finish sandbox exit. Check the region in the message matches `$AWS_REGION` — a client pointed at the wrong region is the second most common cause. |
| SMTP `554 Message rejected` / "Account is in sandbox" on a non-verified recipient | Same restriction at the SMTP layer. | As above. Use `*@simulator.amazonses.com` for testing until production access lands. |
| `Throttling: Maximum sending rate exceeded` or `Daily message quota exceeded` | Exceeded `MaxSendRate` (1/s in the sandbox) or `Max24HourSend` (200/day). | Retry with exponential backoff and jitter — boto3's `standard` retry mode does this. Longer term: cap worker Lambda reserved concurrency (#27) below the send rate, and request a quota increase. Never retry a throttle in a tight loop. |
| DKIM stuck at `PENDING` for hours | One of the three CNAMEs missing or wrong; the zone appended the domain twice; the record is proxied/flattened by the DNS provider; the identity was recreated so the tokens changed. | Re-run the `dig` loop in Phase 2 against the **authoritative** nameserver. Compare each answer to the current `DkimAttributes.Tokens` — after deleting and recreating an identity, the old tokens are dead. SES retries for 72 h then marks the identity failed; fix DNS and it re-verifies without recreating. |
| Identity `Verified` but mail lands in spam | Missing or unaligned SPF/DMARC, or a brand-new domain with no sending history. | Confirm `dmarc=pass` in the headers (§6.2). Warm up gently. Check the domain against a blocklist. Ensure `From` is a real, monitored mailbox — not `noreply@` on a domain with no reverse path. |
| Two `v=spf1` records on the domain | Someone added SES's SPF as a new record instead of editing the existing one. | Merge into a single record. Two `v=spf1` TXT records is a PermError: **all** SPF checks fail, including your existing corporate mail. |
| `MAIL FROM domain status: PENDING` / `FAILED` | MX record missing, or pointing at the wrong region's `feedback-smtp` host. | The MX host must be `feedback-smtp.<your region>.amazonses.com`. This is why Phase 0 comes first. |
| Sending suddenly stops; `EnforcementStatus` is not `HEALTHY` | Bounce or complaint rate crossed a threshold and the account is under review or paused. | Read the AWS notice, stop sending to the failing addresses, show the remediation (suppression list, alarm, removed addresses), and reply in the support case. Prevention is Phase 4. |
| SNS bounce notifications never arrive | Subscription still `PendingConfirmation`, or the event destination is disabled or missing the event type. | `list-subscriptions-by-topic`; confirm the emailed link; check `MatchingEventTypes` includes `BOUNCE` and `Enabled=true`. |

---

## Definition of done

Matching issue #20's acceptance criteria. Tick these in the issue, with the evidence noted.

- [ ] **Region chosen and recorded**, identical to the region the Lambdas (#26) and RDS (#27) will
      deploy into.
- [ ] **Domain identity shows `Verified`** — `get-identity-verification-attributes` returns
      `VerificationStatus: Success`.
- [ ] **DKIM enabled and verified** — all three CNAMEs resolve; `DkimVerificationStatus: Success`.
- [ ] **SPF present and single** on the sending domain, including `amazonses.com`; SPF also present
      on the custom MAIL FROM subdomain.
- [ ] **Custom MAIL FROM configured**, MX record matches the deploy region, status `SUCCESS`.
- [ ] **DMARC record published** (`p=none` at minimum) and `dmarc=pass` observed on a real message.
- [ ] **Recipient addresses verified** for every destination `cfg.action_for(tier)` can route to.
- [ ] **A test send from the deploy region reaches a sales inbox, not spam**, with `dkim=pass`,
      `spf=pass` and `dmarc=pass` in the received headers, and `header.i=@<your domain>`.
- [ ] **Production access granted** — `ProductionAccessEnabled: true` — **or** a dated note in
      issue #20 recording what is outstanding and the support case ID.
- [ ] **Granted quota recorded and checked against expected volume** (~35/day sustained, ~100/day
      peak, at 1,000 leads/month).
- [ ] **Configuration set `leadquali-prod` created** with reputation metrics on, TLS required, and
      bounce/complaint suppression enabled.
- [ ] **Bounce and complaint events appear in CloudWatch** — confirmed with the mailbox simulator,
      with the SNS email subscription confirmed (not `PendingConfirmation`).
- [ ] **Sender address, configuration set name and region recorded** in secure notes, and handed to
      #26/#28 as SAM parameters — never committed to this repository.
- [ ] **Issue #19 unblocked**: its `Notifier` implementation can perform a real send end-to-end.
