# Tenant onboarding, rotation, revocation and suspension

This is the operational runbook for everything to do with a customer's identity: creating
them, giving them credentials, changing their rubric, taking them off the air and putting
them back. It is written to be followable by someone who has not read the code.

**The claim it exists to make good on:** onboarding a customer is a *config write, not a
deploy*. Nothing below edits Python, and nothing below needs a release.

---

## 0. Before you start

You need:

- a shell with the project installed (`pip install -e ".[dev]"`);
- `DATABASE_URL` pointing at the environment's database — through the RDS Proxy endpoint in
  a deployed environment, which means a bastion or a VPN;
- AWS credentials for that environment with permission to create a Secrets Manager secret
  (`secretsmanager:CreateSecret`, `secretsmanager:DescribeSecret`, and `kms:GenerateDataKey`
  on the secrets key);
- `ENV` set to the environment name (`prod`, `staging`, `dev`), because it is part of the
  secret's path;
- `SECRETS_KMS_KEY_ID` set to the `SecretsKmsKeyArn` output of the network stack, so the
  customer's signing secret lands on the customer-managed key rather than the account's
  default one.

Every command below is `python -m leadquali.tenantctl …`. Run it with no arguments for the
list of commands.

---

## 1. Onboarding a customer

### 1.1 Write the rubric

Copy `tenants/default.json` to `tenants/<slug>.json` and edit it. The slug is the
customer's permanent external identity: lowercase letters, digits, `-` and `_`, starting
with a letter or a digit, at most 63 characters. It appears in the `X-LeadQuali-Tenant`
header of every request their website makes, so **it cannot be changed afterwards** without
breaking their integration.

`tenants/acme-demo.json` is a worked second example, and is deliberately unlike the
default: a different ICP paragraph, weights that are not all 1.0, lower tier thresholds, a
stricter confidence gate, and `warm` routed to a human instead of to an inbox.

The fields:

| Field | Required | What it does |
|---|---|---|
| `tenant_id` | yes | Must equal the slug. Two names for one tenant is how a config ends up applied to the wrong customer's leads. |
| `name` | yes | Human-readable. Appears in the prompt. |
| `icp_description` | yes | Free text, injected into the model's system prompt. This is the single highest-leverage field in the file. |
| `routing_rules` | yes | One rule per tier — `hot`, `warm`, `cold`, `disqualified` — each with an `action` and, unless the action is `suppress`, a `destination`. No defaults: a placeholder address would mean a whole tier's leads going nowhere with no error at all. |
| `weights` | no | Per-dimension multiplier. Defaults to 1.0 for each of the five. |
| `thresholds` | no | Inclusive lower bound of each tier on the 0-100 scale. Defaults to hot 80, warm 55, cold 30. |
| `min_confidence` | no | Below this the lead escalates to a human whatever it scored. Defaults to 0.6. |
| `prompt_version` | no | Which rubric revision the tenant is pinned to. Defaults to `rubric_v1`. |

### 1.2 Create the tenant

```bash
python -m leadquali.tenantctl create acme-demo tenants/acme-demo.json
```

This validates the rubric **before it writes anything**, derives the tenant's database id
from the slug, creates the customer's HMAC signing secret in Secrets Manager, and inserts
the row. It prints the tenant's id and the ARN of its signing secret.

If the rubric is invalid the command exits `1`, says what is wrong, and has written nothing
and created no secret. Fix the file and run it again.

If the command fails *after* the secret was created (a database blip, say), just run it
again: creating the secret is idempotent and will **never** overwrite an existing value.

### 1.3 Issue a key

```bash
python -m leadquali.tenantctl issue-key acme-demo --label "acme marketing site"
```

The key is printed **once**, alone on a line on stdout. It looks like:

```
lq_live_3f1c9a02b7d45e68_kf8Qz1Rr2sK0dW7pYb3nJ4mVxC6tLg9hEu5aZo1QsPI
```

Only an argon2id hash of its secret half is stored. **It cannot be recovered.** If it is
lost before it reaches the customer, revoke it and issue another — that costs nothing.

Add `--env test` for a key that is obviously a test key to anyone reading a config file;
the environment is written into the key itself.

### 1.4 Hand over the two secrets

The customer's website needs two things, and they do different jobs:

1. **The API key** you just issued. It says *who is calling* and travels in a header on
   every request.
2. **The HMAC signing secret.** It never leaves the two ends and authenticates the request
   itself. Read it out of Secrets Manager — the ARN was printed by `create`, and
   `tenantctl show <slug>` prints it again:

   ```bash
   aws secretsmanager get-secret-value \
     --secret-id "leadquali/${ENV}/tenant/acme-demo/hmac" \
     --query SecretString --output text
   ```

Send both through whatever channel your company uses for credentials. Never email them,
and never paste them into a ticket.

### 1.5 Verify with a signed request

The signing construction is documented in `src/leadquali/api/signing.py` and is
reimplementable from `signing_string()` alone. To check an onboarding end to end:

```bash
TENANT=acme-demo
KEY='lq_live_…'
SECRET='…'               # the HMAC signing secret, not the key
URL='https://api.example.com/prod/leads'
BODY='{"submission_id":"11111111-2222-4333-8444-555555555555","form":{"full_name":"Test Person","email":"test@example.invalid","message":"Checking the integration."},"elapsed_ms":12000}'
TS=$(date +%s)
NONCE="verify-$(date +%s%N)"
BODY_HASH=$(printf '%s' "$BODY" | openssl dgst -sha256 -hex | awk '{print $2}')
STRING="LEADQUALI-HMAC-SHA256
v1
POST
/leads
${TENANT}
${TS}
${NONCE}
${BODY_HASH}"
SIG=$(printf '%s' "$STRING" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $2}')

curl -sS -i "$URL" \
  -H "Content-Type: application/json" \
  -H "X-LeadQuali-Tenant: ${TENANT}" \
  -H "X-LeadQuali-Key: ${KEY}" \
  -H "X-LeadQuali-Timestamp: ${TS}" \
  -H "X-LeadQuali-Nonce: ${NONCE}" \
  -H "X-LeadQuali-Signature: v1=${SIG}" \
  --data-raw "$BODY"
```

Note the **path in the signed string is `/leads`**, not the deployed URL's `/prod/leads`.
The logical path is the contract; the stage prefix is a deployment detail the customer's
form neither knows nor should have to know.

Expected: `202` with a JSON body echoing the `submission_id`.

| You got | It means |
|---|---|
| `401` | The key or the signature was rejected. Every reason looks the same on purpose. Check the key, the secret, the clock (five minutes of skew is allowed), and that the signed path is `/leads`. See also the note below about intermittent 401s. |
| `403 {"detail":"tenant is not active"}` | The key is *fine* — it verified — and the tenant is suspended or disabled. |
| `413` | The body is over the limit. |
| `422` | Authenticated, but the payload does not match the schema. |
| `429` | Over the tenant's rate limit; `Retry-After` says when to come back. |
| `503` | Not the caller's fault: something we depend on (the tenant's signing secret, most likely) could not be read. `Retry-After` says when to come back, and the same request will work. |

**An intermittent 401 that clears after a few seconds, with the *correct* key.** This is
expected under one specific condition and is not a misconfiguration. A `key_id` is public —
it travels in the clear in a header on every submission — so anyone who has seen one request
can send wrong secrets for it. After ten wrong guesses in a minute, that `key_id` is
throttled to one argon2 verification every six seconds *on the container that saw them*.
A container that has already verified the key once is unaffected (the answer is memoised),
so in practice this shows up only on a cold start during an attack, and one retry clears it.

If a customer reports it: it means someone is guessing against their key, which is worth
knowing. `ingest.rejected` log lines with `reason: "bad_key"` and their `claimed_tenant`
will show the volume. Rotating the key does not help — the new `key_id` is just as public —
and nothing needs to be done for the customer's traffic, which is getting through.

### 1.6 Confirm

```bash
python -m leadquali.tenantctl show acme-demo
python -m leadquali.tenantctl list-keys acme-demo
```

`list-keys` never shows a key — only its prefix, its label, and when it was created, last
used, expires and was revoked.

---

## 2. Changing a rubric

```bash
python -m leadquali.tenantctl update-config acme-demo tenants/acme-demo.json
```

The document is validated in full **before** the database is touched. A config that would
break scoring — overlapping tier bands, a weight for a dimension the model does not score,
a tier with no routing rule, a delivering action with no destination — is refused, and the
tenant keeps running on the config it had. There is no partial application.

The change takes effect on the next lead: the worker reads `icp_config` fresh every time.

Keep `tenants/<slug>.json` in the repository as the reviewable source of truth even though
the database is what is read at runtime. It is the only place a rubric change gets a diff
and a second pair of eyes.

---

## 3. Rotating a key

Rotation is a customer-side deploy, so it is scheduled, not coordinated.

```bash
python -m leadquali.tenantctl rotate-key acme-demo 3f1c9a02b7d45e68
```

This issues a new key and sets the old one to expire in **7 days** — the default overlap,
defined once as `DEFAULT_ROTATION_OVERLAP` in `src/leadquali/app/tenants.py`. Both keys
work for the whole window.

1. Run the command; send the new key to the customer.
2. Tell them the old key stops working on a named date, seven days out.
3. Watch `list-keys`: `last used` on the old key tells you whether they have switched.
4. After the deadline the old key is refused automatically. Nothing else to do.

Use `--overlap-days N` for a different window. **`--overlap-days 0` retires the old key
immediately** — that is what to use for a key that has leaked, rather than revoking first
and leaving the customer with nothing.

There is no way to supply a key: the service generates them. That is deliberate and it is
load-bearing (see §7).

---

## 4. Revoking a key

```bash
python -m leadquali.tenantctl revoke-key acme-demo 3f1c9a02b7d45e68
```

Effective on the **very next request**, with no window and nothing to wait out: the key row
is read from Postgres on every authentication and no part of it is cached anywhere.

The row stays in `list-keys`, marked revoked, with the timestamp. That is on purpose —
"which key did we revoke, and when?" is a question an incident review asks.

A tenant with no live key cannot submit anything. If revocation was an accident, issue a
new key; the old one cannot be un-revoked.

---

## 5. Suspending and resuming a tenant

```bash
python -m leadquali.tenantctl suspend acme-demo    # reversible
python -m leadquali.tenantctl resume  acme-demo
python -m leadquali.tenantctl disable acme-demo    # gone for good
```

A suspended tenant's submissions get **403 `tenant is not active`** — not the 401 every
other rejection gets. That is deliberate: a caller who reaches this has already presented a
live key *and passed the argon2 check on it*, so there is no enumeration oracle left to
protect, and the integrator on the customer's side needs to know it is the account and not
their integration that stopped working.

The status is checked **after** the key verifies, which costs an argon2 verification that a
cheaper ordering would avoid. That is the point: a `key_id` is public, so checking the
status first would let anyone who had ever seen one of the customer's requests find out
whether that account had been suspended for non-payment — a business-sensitive fact about a
third party — without holding the secret at all. Someone with a *wrong* key on a suspended
tenant gets the ordinary, indistinguishable 401.

Suspension affects **that tenant only**. Nothing already stored is touched, nothing is
deleted, and the rubric is untouched — `resume` puts them back exactly as they were.
Erasure is a separate, deliberate operation (#37).

---

## 6. Rotating a tenant's HMAC signing secret

**This is a breaking change for that customer's forms, and there is no overlap.** Unlike
API keys, where a tenant holds several rows at once, a tenant has exactly one signing
secret. The moment the new value propagates (within `SECRETS_CACHE_TTL_SECONDS`, five
minutes by default), every request signed with the old one fails with a 401.

That is a **recorded limitation of v1**, not an oversight. Supporting two live signing
secrets would mean the verifier computing the HMAC twice on the path of every lead, to
serve an operation performed roughly never.

So: **coordinate it with the customer.** Agree a window, have them ready to deploy, then:

```python
from leadquali.adapters.secrets_manager import TenantSecretsProvisioner
TenantSecretsProvisioner.from_env().rotate_tenant_hmac_secret(arn)   # arn from `tenantctl show`
```

Then send them the new value and confirm with a signed curl (§1.5) that their form works
again. If you only need to replace a *credential* rather than the signing construction —
which is almost always the case, including after a leak — **rotate the API key instead**
(§3). That has a seven-day overlap and breaks nothing.

---

## 7. Two decisions recorded here on purpose

### Why rate limits are not API Gateway usage plans

Plan §8 called for a per-tenant API Gateway usage plan. #31 deliberately does not build
one. The per-tenant limit lives on the `tenants` row (`rate_limit_per_minute`,
`rate_limit_burst`) and is enforced in the application by `TenantRateLimiter`. Three
reasons:

- A usage plan is keyed by an **API Gateway API key**, which the caller must send as
  `x-api-key`. That is a *second* credential, unrelated to the HMAC identity we actually
  authenticate on, and it would have to be embedded in the customer's page alongside the
  first.
- The default account quota is **300 usage plans**, which caps the customer count at a
  number the business plans to exceed. Raising it is a support ticket, per region.
- Provisioning one is a **control-plane call in the onboarding path**, with its own
  throttles and its own way to fail half way through creating a customer.

The honest cost of the choice: the application's buckets are per-process, so under N warm
Lambda containers a tenant can get up to N times its configured limit. The **stage-level
throttle** in `infra/template.yaml` is what bounds the absolute worst case, in front of the
runtime. The per-tenant limit is there to stop a runaway integration, not a determined
attacker.

To change a customer's allowance, update their row's `rate_limit_per_minute` /
`rate_limit_burst`. It takes effect within a minute, with no redeploy.

### Why the service generates keys and no command accepts one

There is no `--key` flag, anywhere, by design. The reason argon2 is affordable on the
request path at all is that a key carries its own row handle in the clear: verification is
one indexed read by `key_id`, and the KDF runs only for a caller who already holds a real
handle. That argument depends on `key_id` being 64 uniformly random bits and the secret
being 256 — both of which a customer-chosen key would make false. The full reasoning is in
the module docstring of `src/leadquali/api/signing.py`.

---

## 8. Quick reference

| Task | Command |
|---|---|
| Onboard | `tenantctl create <slug> tenants/<slug>.json` |
| List tenants | `tenantctl list [--json]` |
| Inspect one | `tenantctl show <slug> [--json]` |
| Change the rubric | `tenantctl update-config <slug> tenants/<slug>.json` |
| Stop ingest (reversible) | `tenantctl suspend <slug>` |
| Start it again | `tenantctl resume <slug>` |
| Stop it for good | `tenantctl disable <slug>` |
| New key | `tenantctl issue-key <slug> [--label L] [--env live\|test]` |
| Rotate a key | `tenantctl rotate-key <slug> <key_id> [--overlap-days 7]` |
| Kill a key now | `tenantctl revoke-key <slug> <key_id>` |
| List keys | `tenantctl list-keys <slug> [--json]` |

Exit codes: `0` success, `1` the command was understood and refused (no such tenant, bad
rubric, duplicate slug), `2` a usage or input problem.
