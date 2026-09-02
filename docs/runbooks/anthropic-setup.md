# Runbook — Anthropic API access, billing, and workspace keys

**Issue:** [#3](https://github.com/vendo-aron/JAT-LeadQuali/issues/3) (Phase 0 · P0.2) ·
**Owner:** aron@vendoworks.com · **Time:** ~30 minutes · **Type:** manual, console-only

Nothing in Phase 1 runs without a working API key. Do this before P1.5.

> **This is the owner's step, not the agent's.** No Anthropic API key is provisioned in the
> development environment this repository was built in, and no key can be created from code — key
> creation is a console action tied to a paying account. Every command in §9 (Verification) must be
> run by a human on the development machine, after §7. Until then the repository is complete but
> unproven against the live API.

Read the whole runbook before starting. Each phase ends with a **Verify** block; do not move to the
next phase until its verification passes.

---

## 0. Before you start

**You need:**

- The **company** Google/email account for the Anthropic Console — not a personal one. Keys and
  billing must survive an employee leaving. A personal key is archived the moment its owner is
  removed from the organization, and archived keys are never restored.
- A company payment method.
- Organization **admin** rights in the Console. Only organization admins can create workspaces.
- The repository cloned and the venv created (plan §10.3) if you intend to run §9 today.

**Where things live.** The Claude Console is at **<https://platform.claude.com/>**. If you have an
older `console.anthropic.com` bookmark, use the new host; every URL below is the current one.

| Page | URL |
|---|---|
| Billing and spend limits | <https://platform.claude.com/settings/billing> |
| Rate limits, tier, increase requests | <https://platform.claude.com/settings/limits> |
| Workspaces | <https://platform.claude.com/settings/workspaces> |
| API keys | <https://platform.claude.com/settings/keys> |
| Service accounts | <https://platform.claude.com/settings/service-accounts> |
| Usage and cost charts | <https://platform.claude.com/usage> |

**The numbers this project is built on** (plan §5, §8):

| Fact | Value |
|---|---|
| Model | `claude-opus-5` (exact id — never append a date suffix) |
| Input price | $5.00 per million tokens |
| Output price | $25.00 per million tokens |
| Prompt-cache write (5-minute TTL) | 1.25× input = $6.25 / MTok |
| Prompt-cache read | 0.1× input = $0.50 / MTok |
| Minimum cacheable prefix on `claude-opus-5` | 512 tokens (the §5 rubric block, ~1.5k tokens, clears it) |
| Cost per lead | ~$0.02–0.03 (~2k input, ~800 output-plus-thinking tokens) |
| Expected monthly spend | ~$25 at 1,000 leads/month; ~$250 at 10,000 |

Thinking is on by default on `claude-opus-5` and thinking tokens are billed as output tokens and
count against `max_tokens` — that is already priced into the per-lead figure above.

---

## 1. Sign in with the company account

1. Open <https://platform.claude.com/> and sign in with the **company** account.
2. If the account is brand new, complete organization creation. Give the organization the company
   name, not a project name — this project is one workspace inside it (§4).
3. Top-right → account menu → confirm you hold the **Organization admin** role.

**Verify.** <https://platform.claude.com/settings/workspaces> loads and shows a **Create workspace**
button. If it does not, you are not an org admin; stop and get the role before continuing.

---

## 2. Billing: payment method and initial credits

1. Go to <https://platform.claude.com/settings/billing>.
2. Add the company payment method.
3. Buy an initial credit block. **$50 is enough for Phase 1–3.** That covers roughly two months of
   the 1,000-leads/month steady state plus the golden-set eval runs and effort sweeps in Phase 3,
   which spend real money every time you run them (plan §7).

**Verify.** The Billing page shows a payment method on file and a non-zero credit balance. Note
which **usage tier** the page reports — you will need it in §10.

---

## 3. Organization spend limit and alert

This is the guardrail against a prompt bug looping. Set it *before* you create any key.

Two different ceilings exist, and they fail differently:

- **Your tier's cap** — a hard monthly maximum you do not choose. Start tier is $500/month, Build
  $1,000, Scale $200,000. New organizations may begin in an **Evaluation** tier below the Start
  numbers while account history is established; limits rise automatically with usage.
- **Your own spend limit** — anything at or below the tier cap. This is the one you set here.

1. On <https://platform.claude.com/settings/billing>, find the **Spend limits** section.
2. Click **Set limit** (or **Adjust limit**).
3. Enter **$100** per month.
4. Set a spend **alert at 50%** — $50 — so the email arrives while there is still a month of
   headroom to react in.

**Why $100.** Expected spend is ~$25/month at the v1 volume. A 4× ceiling absorbs a heavy month,
a re-run of the eval suite, and a couple of days of debugging without paging you — while capping a
runaway loop at $100 instead of letting it run to the tier's $500. Revisit the number when real
volume is known; raising it is a two-click change. If you onboard a tenant at 10,000 leads/month
(~$250), raise this *first* — the limit will stop the pipeline before the tier cap does.

**What happens when a limit is hit** — this matters because the pipeline behaves differently for
each:

| Limit hit | Response | Recoverable by retry? |
|---|---|---|
| Your own org or workspace limit | HTTP **400** `invalid_request_error`, message begins `You have reached your specified API usage limits` (or `... specified workspace API usage limits`) and states when access resumes | **No.** 400 is not retried by the SDK. Raise or remove the limit. |
| Your tier's cap | HTTP **429** `rate_limit_error` with `error.details.error_code` = `enforced_spend_limit_reached`, and **no** `retry-after` header | **No.** Usage is paused until 00:00 UTC on the 1st of next month unless you move to a higher tier. The SDK's automatic 429 retries will burn through and still fail. |

Neither is a transient error. In the pipeline (plan §8) the worker's failure path fires: the lead is
emailed to sales unqualified with the "system could not assess" banner, SQS redelivers, and the
message lands in the DLQ with a CloudWatch alarm. **Leads are degraded, never dropped** — but the
whole point of the 50% alert is that you act before that happens.

**Verify.** The Billing page shows `$100` as your spend limit and one alert configured at `$50`.

---

## 4. Create the two workspaces

Every organization has a **Default Workspace** that cannot be renamed, archived, or deleted, and on
which **no spend or rate limits can be set at all**. That single fact is why this project does not
live there.

Create two workspaces:

1. Go to <https://platform.claude.com/settings/workspaces> → **Create workspace**.
2. Name it `leadquali-dev`, pick a colour, **Create**.
3. Repeat for `leadquali-prod`.

**Why this project gets its own workspaces:**

- **Blast radius.** Every request runs in exactly one workspace and can only reach resources in it.
  A key scoped to `leadquali-dev` cannot spend `leadquali-prod`'s budget, read its files or batches,
  or be used to bill production traffic. Archiving a workspace archives every key created in it,
  within seconds — a whole environment can be killed in one action.
- **Per-workspace spend limits.** Eval sweeps (§7) and a prompt bug live in dev. They must not be
  able to consume the production budget. Workspace limits can only be set *below* the org limit, and
  the org limit always applies on top — so the two-level structure is a floor and a ceiling, not a
  choice between them.
- **Key rotation without touching other projects.** Rotating `leadquali-prod`'s key is one key in
  one workspace. Nothing else in the company — Claude Code, another product, someone's notebook —
  changes. That is not true of a key in the Default Workspace.
- **Attribution.** Usage and cost reports and the Usage page split by workspace, so "what does
  qualification actually cost per lead" is a filter, not an estimate. (Traffic in the Default
  Workspace reports `workspace_id: null`, which is exactly the ambiguity you are avoiding.)
- **Prompt caches are isolated per workspace.** Dev and prod warm separate caches for the same
  rubric prefix. That is the intended behaviour — it also means a dev cache hit rate tells you
  nothing about prod's, so measure §5's `cache_read_input_tokens` in the environment you care about.

You may also see a **Claude Code** workspace that Anthropic created automatically when someone in
the org first signed in to Claude Code. Leave it alone. Do not archive it — that disables Claude
Code sign-in through Console billing for the entire organization.

**Verify.** Settings → Workspaces lists `leadquali-dev` and `leadquali-prod`, each with a
`wrkspc_`-prefixed **ID** in the ID column. Copy both IDs into your notes; §9 uses them and they are
not secret.

---

## 5. Per-workspace spend limits

Each workspace's settings page has two tabs: **Rate limits** and **Spend limits**.

1. Open `leadquali-prod` → **Spend limits** → set a monthly cap of **$75** with an alert at 50%.
2. Open `leadquali-dev` → **Spend limits** → set a monthly cap of **$25** with an alert at 50%.

The two workspace caps do not have to sum to the org limit — the organization-wide $100 applies
regardless, and workspace limits are checked in addition to it. Dev's $25 is the number that matters
most: it is the ceiling on an eval sweep or a looping prompt during development.

Leave the **Rate limits** tab alone for now; §10 explains when to touch it.

**Verify.** Both workspaces show a spend limit and an alert on their Spend limits tab.

---

## 6. Create the API keys

Three distinct keys, created at three different times. **Never one key for two purposes.**

| Key | Workspace | Type | Created | Lives in |
|---|---|---|---|---|
| `leadquali-dev-local` | `leadquali-dev` | Personal key (you) | Now | `ANTHROPIC_API_KEY` on your machine (§8) |
| `leadquali-prod-lambda` | `leadquali-prod` | Service account key | Now, then **held** | AWS Secrets Manager in [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28) — nowhere else |
| `leadquali-ci` | `leadquali-dev` | Service account key | **Only when CI first needs it** | GitHub Actions secret, scoped to the one workflow that calls the live API |

**Why never the same key for local dev and CI/deploy:**

- **Revocation is independent.** A laptop is lost, or a key is pasted into a chat window: you delete
  `leadquali-dev-local` and CI and production keep running. Share one key and every revocation is an
  outage.
- **Attribution.** Cost and usage reports break down by key. One key for everything means you can
  never answer "was that spend my debugging or the deploy pipeline?".
- **Blast radius.** A key exfiltrated from CI logs — the classic leak — reaches only the dev
  workspace and its $25 cap, not production.
- **Identity lifecycle.** A personal key acts as *you*, with your permissions, and stops working the
  moment you lose access to the org. That is correct for your laptop and wrong for an unattended
  workload: CI would break when a person leaves. Machine workloads get a **service account**, which
  has its own identity and outlives individuals.
- **Rotation cadence differs.** Human keys rotate on a person's schedule; production keys rotate on
  the service's. Shared keys force the strictest cadence on everyone and get rotated by nobody.

### 6a. The local development key

1. Go to <https://platform.claude.com/settings/keys> → **Create key**.
2. Name: `leadquali-dev-local`.
3. **Linked account:** yourself (this makes it a personal key).
4. **Workspace:** `leadquali-dev`. Scoping the key to one workspace means requests never need an
   `anthropic-workspace-id` header — and cannot accidentally run against prod.
5. **Expiration:** `90 days`. This is a laptop key; give it a fuse.
6. **Copy the key now.** The Console shows the secret exactly once and never again. If you lose it,
   delete the key and create another — there is no "show again".
7. Paste it straight into §8. Do not park it in a note, a chat, or a screenshot on the way.

### 6b. The production key

1. If the org has no service account yet: <https://platform.claude.com/settings/service-accounts> →
   create one named `leadquali-prod`, then add it to the `leadquali-prod` workspace.
2. Settings → API keys → **Create key**, named `leadquali-prod-lambda`, **Linked account** = the
   `leadquali-prod` service account, **Workspace** = `leadquali-prod`, **Expiration** = `Never`
   (it will live in a secrets manager and be rotated deliberately — see §12).
3. Copy it and **hold it in your password manager until [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28)** creates the Secrets Manager entry.
   Do **not** put it in a GitHub secret, a Lambda environment variable, a SAM template, or a
   CloudFormation parameter — #28's acceptance criteria explicitly forbid all four.

If you would rather not hold a live production secret in a password manager for several weeks, skip
6b entirely and create the key during #28 instead. Nothing in Phases 1–3 needs it.

### 6c. The CI key — not yet

CI does not get a key today. Every test in Phases 0–3 runs against fakes and recorded fixtures; a
key in GitHub Actions right now is a secret with no consumer, which is pure risk. When a live
smoke test is added to CI, create `leadquali-ci` as a **service account key scoped to
`leadquali-dev`** with a short expiration, and add it as a repository secret for that workflow only.

**Verify.** The API keys page lists `leadquali-dev-local` (and `leadquali-prod-lambda` if you made
it) with the right workspace in the scope column, the right linked account, and the expiration you
chose. Both key secrets are out of your clipboard and out of your shell history.

---

## 7. Store the local key

**Windows / Visual Studio 2026** (plan §10.7). In Developer PowerShell:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-api03-..."
```

`setx` writes to the *user* environment for **future** processes. The shell you typed it in does not
have it, and neither does an already-running Visual Studio.

**Restart Visual Studio** (fully close it, not just the solution) so it inherits the variable, then
open a **new** terminal.

**Never** put the key in: source, `pyproject.toml`, a committed `.env`, a `launch.json`/`*.pyproj`
file, a docstring, a test fixture, a commit message, a screenshot, or a chat message. The repository
`.gitignore` already excludes `.env`, `.env.*`, `.venv/`, and `*.pyproj` — confirm that before your
first push.

**In AWS, this variable is not how the key gets there.** The prod key lives in **AWS Secrets
Manager** ([#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28)), fetched at runtime and
cached in the Lambda execution context with a TTL, with `config.py` ([#4](https://github.com/vendo-aron/JAT-LeadQuali/issues/4))
reading Secrets Manager when `ENV != local` and environment variables locally — one code path, no
`if prod` branching. No secret value appears in a Lambda environment variable or in the SAM template.

**Verify**, in a new PowerShell window (this prints a prefix, never the whole key):

```powershell
$env:ANTHROPIC_API_KEY.Length
$env:ANTHROPIC_API_KEY.Substring(0,14)
```

Expect a length around 100–110 and a prefix of `sk-ant-api03-`. If you get an empty result, the
window predates the `setx` — open another one.

And sweep the repository before the first push (an acceptance criterion of #3):

```powershell
git grep -i "sk-ant"          # working tree — expect no output
git log -p | Select-String "sk-ant"   # whole history — expect no output
```

Both must return nothing. If a key ever *does* appear in a commit: **delete the key in the Console
first**, then clean the history. Rewriting history does not un-leak a secret that has already been
pushed; revoking it does.

---

## 8. A note on how the SDK finds the key

The Python SDK resolves credentials in this order, first match wins:
`ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → an `ant auth login` profile on disk.

Two consequences worth knowing before §9 confuses you:

- If **both** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are set, the SDK sends both headers and
  the API rejects the request with a 401. Unset the one you do not want.
- A set `ANTHROPIC_API_KEY` shadows any `ant auth login` profile. If you get spend on an unexpected
  workspace, this is usually why.

`anthropic.Anthropic()` with no arguments is the correct construction everywhere in this project —
the key never appears in code.

---

## 9. Verification — prove the key works and watch the meter move

Run this on the development machine, after §7. It is the only step in this runbook that spends
money, and it spends less than a fifth of a cent.

### 9a. Activate the project venv

```powershell
cd <repo root>
.\.venv\Scripts\Activate.ps1
python -c "import anthropic; print(anthropic.__version__)"
```

Expect a `1.x` version. If the import fails, install the SDK into the venv
(`python -m pip install anthropic`); it will be a pinned dependency of `pyproject.toml` from
[#4](https://github.com/vendo-aron/JAT-LeadQuali/issues/4) onward.

### 9b. The smoke call

Write the script to a temp file (PowerShell here-strings avoid the quoting mess of `python -c`):

```powershell
@'
import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

# Confirms the org can actually reach the model this project is built on.
print("model available:", client.models.retrieve("claude-opus-5").id)

raw = client.messages.with_raw_response.create(
    model="claude-opus-5",
    max_tokens=64,
    output_config={"effort": "low"},
    messages=[{"role": "user", "content": "Reply with the single word: pong"}],
)
print("workspace:", raw.headers.get("anthropic-workspace-id"))
print("request-id:", raw.headers.get("request-id"))

msg = raw.parse()
u = msg.usage
print("model:", msg.model)
print("stop_reason:", msg.stop_reason)
print("text:", "".join(b.text for b in msg.content if b.type == "text"))
print("usage:", u.input_tokens, "in /", u.output_tokens, "out")

cost = u.input_tokens / 1e6 * 5.00 + u.output_tokens / 1e6 * 25.00
print(f"cost of this call: ${cost:.6f}")
'@ | Out-File -Encoding utf8 $env:TEMP\lq_smoke.py

python $env:TEMP\lq_smoke.py
Remove-Item $env:TEMP\lq_smoke.py
```

### 9c. What a good response looks like

```
model available: claude-opus-5
workspace: wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ
request-id: req_018EeWyXxfu5pfWkrYcMdjWG
model: claude-opus-5
stop_reason: end_turn
text: pong
usage: 15 in / 9 out
cost of this call: $0.000300
```

Check four things, in this order:

1. **`model available: claude-opus-5`** — the org has access to the model. A `404`/`not_found_error`
   here means a typo in the id; a `403` means the org or key is not permitted the model, and you
   should request access now rather than in Phase 1.
2. **`workspace:` matches the `wrkspc_` ID you noted for `leadquali-dev` in §4.** This is the whole
   verification of §4 and §6 in one line: it proves which workspace's budget and rate limits this
   key actually draws on. If it shows a different ID, the key is scoped to the wrong workspace —
   delete it and create another; do not "fix" it with a header.
3. **`stop_reason`** — `end_turn` is ideal. **`max_tokens` is also a pass.** Adaptive thinking is on
   by default on `claude-opus-5` and thinking tokens count against `max_tokens`, so a 64-token
   ceiling can be reached before any visible text is produced. The call still authenticated, still
   ran the model, and still billed — which is exactly what this test is proving. `refusal` on a ping
   would be surprising; re-run it before investigating.
4. **`cost of this call:`** — a real number, in the low thousandths of a dollar, computed from
   `usage` at the §0 prices. This is the same arithmetic the worker will log per lead (plan §8) and
   the first time you see the unit economics of this project as a number rather than an estimate.

### 9d. See the meter move in the Console

1. Open <https://platform.claude.com/usage>.
2. Filter to the `leadquali-dev` workspace.
3. You should see one request and a handful of tokens against `claude-opus-5`. Console usage data
   can lag several minutes — if the smoke call returned 200 with a `request-id`, the call happened;
   refresh in a few minutes rather than re-running it.
4. On <https://platform.claude.com/settings/billing>, the credit balance has moved by roughly the
   number printed in 9c.

Once you have seen that, you have verified end to end: account → billing → workspace → key →
environment variable → SDK → model → cost. That is the whole of #3.

---

## 10. Rate limits

### Where to read the current limits

- **<https://platform.claude.com/settings/limits>** — your organization's usage tier and the current
  RPM / input-tokens-per-minute (ITPM) / output-tokens-per-minute (OTPM) limits **per model**, plus
  the **Request rate limit increase** button.
- **<https://platform.claude.com/usage>** — two rate-limit charts (input and output) showing your
  hourly peak against the limit, and your cache hit rate. This is where you decide whether to ask
  for more, not by guessing.

Limits are set at the **organization** level and applied **per model**. `claude-opus-5` has its own
bucket, separate from the shared Opus 4.x bucket — traffic to other Opus models does not eat into
it. At Start tier, `claude-opus-5` allows 1,000 RPM, 2,000,000 ITPM and 400,000 OTPM. A new
organization may sit in an Evaluation tier below those numbers until it builds usage history; the
tier rises automatically.

**Only *uncached* input tokens count toward ITPM** (`input_tokens` + `cache_creation_input_tokens`);
`cache_read_input_tokens` do not. The §5 rubric cache therefore buys throughput as well as cost.

**Headroom for this design.** At ~2k input and ~800 output tokens per lead, OTPM binds first:
400,000 ÷ 800 ≈ **500 leads per minute**. The v1 target is 1,000 leads per *month*. You are three
orders of magnitude clear of the limit — the realistic way to hit it is a form-spam flood, not
growth, which is what the deterministic pre-filters in plan §8 are for. Note also that limits use a
token bucket that refills continuously, so a short burst can trip a per-minute limit even when the
minute's average is fine, and a sudden sharp increase in org-wide usage can trip a separate
acceleration limit — ramp traffic gradually.

### What the ingest/worker design does when it hits one

A 429 with a `retry-after` header is a transient error and the design already absorbs it, in three
layers (plan §3, §8):

1. **The SDK retries.** The Anthropic SDK automatically retries 429 and 5xx and connection errors
   with exponential backoff (`max_retries` defaults to 2), honouring `retry-after`. Most rate-limit
   blips never reach your code.
2. **SQS redelivers.** If the SDK exhausts its retries, the worker raises, the message is not
   deleted from the queue, and SQS redelivers it after the visibility timeout. The lead is *queued*,
   not lost — which is the entire reason qualification is asynchronous rather than inline.
3. **DLQ and alarm.** After N redeliveries the message lands in the DLQ and the CloudWatch alarm on
   DLQ depth fires. The lead is emailed to sales unqualified with the "system could not assess"
   banner. **A lead is never dropped and never silently disqualified.**

Note the asymmetry with §3: a *spend* rejection looks like an error but is not transient. The
spend-cap 429 carries **no** `retry-after`, and the SDK's retries will fail against it just as
surely; a self-set limit returns a 400, which is not retried at all. Diagnose by reading
`error.details.error_code` — `enforced_spend_limit_reached` is a billing problem wearing a rate
limit's status code.

### What to raise, and when

**Raise the rate limit** via **Request rate limit increase** on the Rate limits page when any of:

- The Usage rate-limit charts show your hourly peak consistently above ~50–70% of the limit.
- 429s with `retry-after` start appearing in worker logs, or the DLQ-depth alarm fires for
  rate-limit reasons rather than model errors.
- You know a step change is coming — onboarding a tenant at 10,000 leads/month, or a bulk backfill.
  Ask *before* the traffic, and ramp into it.

**Raise the spend limit first** in most of those cases. At this design's volumes the $100 org cap
(§3) is reached long before any rate limit is — spend is the binding constraint, not throughput.

**Per-workspace rate limits** (each workspace's **Rate limits** tab) can be set *below* the org
limit, per limiter type. Leave `leadquali-prod` at the org limit — do not throttle production. If a
Phase 3 eval sweep ever competes with production traffic, cap `leadquali-dev`'s RPM instead. You
cannot set limits on the Default Workspace, and unset workspace limits simply inherit the org's.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 authentication_error` on every call | `ANTHROPIC_API_KEY` unset in this process (a Visual Studio or terminal started before `setx`), or the key was deleted, disabled, or expired | Restart VS / open a new shell and re-check §7's verify. If the variable is present and correct, the key is gone — check its status on the API keys page and create a replacement. Expired keys cannot be reactivated. |
| `401` even though the key is definitely right | Both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are set; the SDK sends both headers and the API rejects the request | Unset `ANTHROPIC_AUTH_TOKEN`. |
| `403 permission_error` | The key's identity is not permitted the requested model or feature — e.g. a workspace or org restriction, or model access not yet granted to the org | Check the key's linked account and workspace on the API keys page; confirm model access with `client.models.retrieve("claude-opus-5")` (§9b). Request model access from Anthropic if it is gated. |
| Requests succeed but spend lands on the wrong workspace; or `400 invalid_request_error: anthropic-workspace-id is required...`; or ``404 not_found_error: Workspace `<id>` not found`` | The key is not scoped to one workspace (a multi-workspace key needs an explicit `anthropic-workspace-id` header on every request), or is scoped to the wrong one, or the linked identity has no access to the workspace | Read the `anthropic-workspace-id` **response** header (§9b) — it names the workspace a request actually ran in. Prefer a single-workspace key: delete the key and create one scoped to `leadquali-dev` / `leadquali-prod`. Do not paper over a wrongly-scoped key with a header. |
| `429 rate_limit_error` **with** a `retry-after` header | RPM, ITPM, or OTPM exceeded — often a burst, since the bucket is per-minute | Nothing to do in the moment: the SDK retries and SQS redelivers (§10). If it recurs, check the Usage rate-limit charts and request an increase. |
| `429 rate_limit_error` with **no** `retry-after` and `error.details.error_code = enforced_spend_limit_reached` | The organization hit its **tier's** monthly spend cap. Usage is paused until 00:00 UTC on the 1st of next month | Retrying will not help. Move to a higher tier via **Request rate limit increase** on the Rate limits page, or wait for the reset. |
| `400 invalid_request_error` beginning `You have reached your specified API usage limits` (or `... specified workspace API usage limits`) | You hit the org (§3) or workspace (§5) limit **you** set | Raise or remove the limit on the Billing page or the workspace's Spend limits tab. Then find out why spend was 4× the forecast before raising it again. |
| `400` mentioning `temperature`, `top_p`, `top_k`, or `budget_tokens` | Those parameters are removed on `claude-opus-5` | Delete the parameter. Use `output_config={"effort": ...}` to control depth, per plan §5. |
| `404 not_found_error` on the model | Typo in the model id, or a date suffix appended | The id is exactly `claude-opus-5`. Never append a date. |
| `stop_reason: "max_tokens"` with empty text on the smoke call | Adaptive thinking consumed the 64-token ceiling before visible text | Not a failure — see §9c point 3. Raise `max_tokens` if you want to see the word "pong". |

Every response carries a `request-id`. Capture it from failures; it is the first thing Anthropic
support will ask for.

---

## 12. Who has access, and rotation

### Least privilege on the Console

- **Organization admin:** as few people as the company can tolerate — ideally two, so nobody is a
  single point of failure. Org admins automatically get **Workspace Admin** on *every* workspace,
  including ones they were never added to, so this role is the real blast radius.
- **Everyone else** gets an organization **user** or **developer** role and is then added
  **explicitly, per workspace**. That is the mechanism that makes §4's separation real.
- **Inside a workspace**, assign the narrowest role that works:
  `Workspace User` (playground only) < `Workspace Limited Developer` (keys and API, no session
  tracing views, no file downloads) < `Workspace Developer` (keys and API) < `Workspace Admin`
  (settings and members). Most engineers on this project need `Workspace Developer` on
  `leadquali-dev` and **nothing at all** on `leadquali-prod`.
- **Nobody needs a personal key in `leadquali-prod`.** Production is reached by the service account
  through Secrets Manager, not by a human.
- The **billing** role is separate from admin — finance can watch spend without being able to mint
  keys.

### Rotation cadence

| Key | Cadence | How |
|---|---|---|
| `leadquali-dev-local` | Every **90 days**, enforced by the expiration set in §6a | Anthropic emails the creator 7 days before expiry. Create the new key, `setx` it, restart VS, verify with §9, then delete the old one. |
| `leadquali-prod-lambda` | Every **90 days**, and **immediately** on any suspected exposure or when someone with production access leaves | Create the new key in `leadquali-prod` → update the Secrets Manager entry → confirm the worker picks it up within the cache TTL, with no redeploy → delete the old key. The Anthropic key **cannot be auto-rotated**; this manual procedure is the documented runbook that [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28) requires. |
| `leadquali-ci` | Every **90 days** via a short expiration, and immediately on any CI log leak | Rotate the GitHub secret and delete the old key. |

Rotate by **overlap, then delete** — create the new key, deploy it, verify, and only then delete the
old one. Never delete first.

Standing hygiene:

- **On the API keys page, `Disable` is reversible and `Delete` is permanent.** Disable first if you
  are unsure; it is the fast, undoable way to stop a suspected leak.
- Any key you suspect has leaked is deleted **immediately**, before any investigation. It costs
  minutes to replace and it is the only action that actually revokes access.
- Review the API keys page and each workspace's member list **quarterly**: delete keys with no
  owner, no purpose, or a "last used" date in the distant past, and remove people who have moved on.
- Re-run the history sweep from §7 before any push that touches configuration.

---

## 13. Done checklist

Maps 1:1 to the acceptance criteria of [#3](https://github.com/vendo-aron/JAT-LeadQuali/issues/3).

- [ ] A **spend limit and an alert** exist on the organization (§3), and on each workspace (§5).
- [ ] **Two workspaces** exist — `leadquali-dev` and `leadquali-prod` — with one key each, named for
      its use (§4, §6).
- [ ] The **verification call returns a response from `claude-opus-5`**, from the expected workspace,
      with a printed cost (§9).
- [ ] **No key is anywhere in the repository** — `git grep -i "sk-ant"` and
      `git log -p | Select-String "sk-ant"` both return nothing (§7).
- [ ] The **prod key is held** for [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28) and
      is not in a GitHub secret, a Lambda environment variable, or the SAM template (§6b, §7).
