# The staff admin

`/admin` is the interface that makes the rubric-tuning loop — the product's actual moat —
usable by someone who is not you with a `psql` prompt. It is server-rendered HTML inside
the same Lambda as the ingest API, behind a signed session cookie.

**This is a staff tool. What is built here is the floor, not the ceiling.** The
recommended production posture is to put it behind SSO or an identity-aware proxy
(Cloudflare Access, AWS Verified Access, an ALB with OIDC) *as well*. Section
[Hardening](#hardening-the-deployment) says why, and what the in-process controls do and do
not bound.

---

## 1. What it does

| Screen | URL | What it is for |
|---|---|---|
| Tenants | `/admin/` | The way in to everything else. |
| Dashboard | `/admin/tenants/{slug}` | Volume, tier mix, cost, feedback agreement over time. |
| Rubric editor | `/admin/tenants/{slug}/config` | Edit → preview → confirm, with a field-by-field diff. |
| Edit history | `/admin/tenants/{slug}/config/history` | Who changed the rubric, when, to what — and the revert button. |
| Re-run | `/admin/tenants/{slug}/rerun` | Run a candidate rubric against real history. Dispatches nothing. |
| Lead browser | `/admin/leads` | Filter by tenant, tier, date, confidence. Keyset paging. |
| Lead detail | `/admin/leads/{id}` | Payload, assessment, reasoning, routing, feedback. |
| Feedback review | `/admin/review` | *Hot leads the rep marked bad, grouped by industry.* |
| Golden set | `/admin/promotions` | The JSONL lines to commit into the eval set. |

There is deliberately **no route that deletes a tenant or a lead**. Erasure is a separate,
audited operation (#37) driven from a CLI with an operator's own credentials, not a button
on a page somebody might have left open.

There is also **no route that displays a key or a secret**. `AdminDeps` carries no
credential source at all, so there is nothing for a page to render even by accident. A key
listing, when one is added, may show the prefix, the label and the lifecycle dates and
nothing else — the plaintext key exists only in `tenantctl`'s output at the moment it is
issued.

---

## 2. Configuration

Four settings, on top of what the rest of the system already needs
(`DATABASE_URL`/`DATABASE_SECRET_ARN`, `ANTHROPIC_API_KEY`).

| Variable | What it is |
|---|---|
| `ADMIN_SESSION_SECRET` | 32+ bytes of random material; signs the session cookie. |
| `ADMIN_SESSION_SECRET_ARN` | Secrets Manager ARN holding the above. Takes precedence. |
| `ADMIN_CREDENTIALS` | `{"<username>": "$argon2id$..."}` — hashes only. |
| `ADMIN_CREDENTIALS_SECRET_ARN` | Secrets Manager ARN holding the above. Takes precedence. |

Both follow #28's pattern: an ARN wins over the plain variable, because the plain variable
is the half of the configuration that is visible on a Lambda's console page and it must
never be the half that wins.

There is no fallback for either. An admin that generated a session secret at startup would
appear to work until the second container served a request and signed everybody out; an
admin that started with an empty credential map would either refuse every login or, once
somebody "fixed" that, accept any.

### Not yet wired into `infra/template.yaml`

The two settings above are **not** declared in the SAM template. Deploying the admin needs,
in `infra/template.yaml`: two `NoEcho`-free ARN parameters (`AdminSessionSecretArn`,
`AdminCredentialsSecretArn` — ARNs are not secrets), those ARNs as environment variables on
the API function, and a `secretsmanager:GetSecretValue` statement scoped to exactly those
two ARNs. Until then the admin runs locally and in any deployment that sets the variables
by hand, and returns a `RuntimeError` naming the missing variable in one that does not.

That file belongs to #26–#28 and is being edited in parallel, so the change was left out
rather than merged blind. It is four lines of YAML and a policy statement.

### Generating a credential

```python
from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
print(Argon2KeyHasher().hash_secret("the password you chose"))
```

Put the result in the map against the username. The password itself is never stored, never
transmitted anywhere but the login form, and cannot be recovered from the hash.

**Rotating `ADMIN_SESSION_SECRET` signs every staff member out immediately.** That is the
intended emergency response to a suspected cookie theft, and it takes effect within
`SECRETS_CACHE_TTL_SECONDS` (five minutes by default) with no redeploy.

---

## 3. Access control, and what each mechanism actually bounds

### The session cookie

`__Host-lq_admin`, HMAC-SHA256 over `{subject, issued_at, expires_at}` with stdlib `hmac`.
`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`.

The `__Host-` prefix means a browser will only accept it over HTTPS, with `Path=/` and no
`Domain` — so a sibling subdomain cannot set or overwrite it. **The admin therefore does
not work over plain HTTP**, which is correct for a tool that edits every customer's routing
policy and is worth knowing before you try it on `http://localhost`.

**Absolute expiry, twelve hours, no sliding renewal.** A sliding session is one a stolen
cookie keeps alive forever as long as the thief keeps using it, which is exactly the case
the expiry exists for. The cost is that a long day ends with a second login.

### The login

Verified with `Argon2KeyHasher` — the same hasher #31 uses for ingest API keys, at the same
OWASP parameters, **for the opposite reason**. `adapters/keyhash_argon2.py` argues that a
memory-hard KDF is not what protects an ingest key: that key is 128 bits of machine
randomness, so an offline attacker with the hash has nothing to guess, and the argon2 there
is defence in depth. A staff password is chosen by a person: perhaps 30 bits of entropy,
very likely reused somewhere else. Against that, **the cost per guess is the defence**, and
a memory-hard KDF is the only thing standing between a leaked hash map and every account in
it. The two decisions read as contradictory until you notice they are the same question
asked of two inputs a thousandfold apart in entropy.

Three properties, all of them tested without a database:

* **An unknown username costs the same as a known one.** The verifier runs against a fixed
  non-matching hash when there is no such account, so response time is not an oracle for
  which staff accounts exist.
* **A username is gated after 10 failures in 5 minutes**, refused *before* the KDF runs.
  Same shape as #31's key gate and for the same reason: an attacker who can make us run
  argon2 at will has a denial of service whether or not they ever guess anything.
* **A failure says only "Login failed."** Not "no such user", not "wrong password", not
  "too many attempts" — each of those answers a question a stranger was asking.

### CSRF

Every state-changing form carries a token derived from the session token with the same
secret, verified server-side before the handler body runs. A config editor with no CSRF
protection is a one-click rubric rewrite from any page a logged-in operator happens to open,
and `SameSite=Lax` alone does not cover a top-level form post.

The login POST itself carries no CSRF token, because there is no session yet to bind one
to. The residual risk is *login CSRF* — an attacker making a victim's browser sign in as
the attacker. On a tool whose login has no side effects beyond setting a cookie, and where
the victim can see whose name is on the screen, that is accepted rather than defended with
a pre-session token.

### Structural, not remembered

There are two routers. One holds the login page; the other holds every other admin route
and is constructed with a **router-level dependency** on the session. A route added to the
guarded router is protected because of where it lives, not because its author remembered a
decorator. `tests/unit/test_api_admin.py` enumerates every `/admin` route out of the
application itself and asserts each one redirects when unauthenticated, so a route added to
the wrong router fails the suite rather than shipping open.

### Hardening the deployment

The login gate and the rate limits are **per process**. Under N warm Lambda containers an
attacker gets N times the allowance. That is the honest limit of an in-memory counter, and
it is accepted rather than hidden: the alternative is a round trip to DynamoDB on every
login attempt.

The bound that does not depend on process count is a layer in front. Put the admin behind
SSO or an identity-aware proxy, and keep the controls here as the second factor — a proxy
misconfiguration should not be the only thing between the internet and every tenant's
routing rules.

---

## 4. Editing a rubric

Editing a rubric is **the highest-risk action in the product**. Invariant 1 makes it
configuration rather than code, which is what lets a customer be onboarded without a
deploy — and which also means there is no build, no code review and no `git revert` in the
way. A threshold typed wrong at 5pm mis-routes every lead that tenant receives overnight.

So the flow is **edit → preview → confirm**, and never a single POST that saves:

1. The form posts the candidate document.
2. The server validates it with `TenantConfig` — #8's validator, the only one there is.
   On failure it re-renders the form **with your text intact**. Losing an edit because
   somebody mistyped a threshold is how people stop using the tool.
3. On success it renders a **field-by-field diff**: `thresholds.hot 80 → 85`, not three
   lines of JSON that happen to have moved.
4. Only the confirm POST writes, through `TenantService.update_config`, appending a row to
   `tenant_config_versions` **in the same transaction**. A saved config with no audit row
   is impossible, not merely unlikely.

Every version row holds the **whole** config as it stood after that change, not a patch: a
chain of patches is one bad apply away from being unreplayable, and the point of the table
is that a bad rubric can be undone at 3am by somebody who is not you.

**Reverting appends.** Restoring version 4 writes version 9 whose config is version 4's. The
history only answers "who changed this and to what" if nothing is ever removed from it, and
a revert is itself a change somebody made.

Version 1 was seeded for every existing tenant by the migration, from that tenant's stored
config, attributed to `migration` — so the first real edit has something to diff against.

---

## 5. Testing a rubric against real history

`/admin/tenants/{slug}/rerun` picks the most recent leads, runs them through the **real**
pipeline against a candidate rubric, and shows old tier against new tier side by side. It
is the thing that turns "`thresholds.hot` moved from 80 to 85" into "eleven of last month's
hot leads become warm", which is what you actually wanted to know.

**Nothing is written and nothing is sent.** The pipeline is wired to a null store and a
null notifier. That is a substitution, not a flag: `RerunService` takes no store and no
notifier parameter at all, so there is no branch that could get it wrong.

**It costs money.** Every lead in the batch is a model call:

* the batch is capped at **25 leads** (`RERUN_BATCH_CAP`);
* the estimate is shown first, from this tenant's actual recent cost per billable lead;
* the run refuses without an explicit confirmation — a request that spends money must not
  be one a stray click or a link prefetcher can make.

**The spend is ours, not the customer's.** It cannot reach an invoice: #33 computes
`leads_billable` from `assessments` rows and a re-run writes none. It is therefore also
invisible to `usage_daily` entirely — that table has no column that could mean "spent on
this tenant, not billable to them" — so the number is emitted as an `admin.rerun_completed`
log event carrying `cost_usd` and `billable: false`. Total it with a CloudWatch metric
filter if it ever becomes material.

A tenant with no history gets a page saying so, not an empty table.

---

## 6. The feedback review and the golden set

`/admin/review` is the query the whole storage design was made for: *every lead scored hot
last month that the rep marked bad, grouped by industry.* Industry comes from
`assessments.extracted`; the filter side is served by
`ix_assessments_tenant_id_tier_created_at` and the join side by `ix_feedback_lead_id`.

Each row is one click from the eval golden set (#22). Promotion:

* **records a decision, not a case.** `golden_leads.jsonl` lives in git, the labels in it
  are human judgements that belong in a reviewed commit, and a Lambda's filesystem is
  read-only. `/admin/promotions` renders the JSONL lines; you paste them into a commit.
* **rewrites the payload.** Names, addresses, companies, websites and phone numbers are
  replaced with deterministic pseudonyms at reserved never-resolving domains, and anything
  address-, URL- or phone-shaped inside the free text is replaced with `[removed]`. The
  role, the headcount, the industry, the timeline and the money figures are kept: they are
  the signal the case exists to test.
* **is idempotent.** `UNIQUE (tenant_id, lead_id)` — a second click returns the first
  label rather than adding the lead twice, which would make the eval harness weigh it twice.

> **Read each line before you commit it.** The rewrite catches patterns. It cannot tell that
> "spoke to Priya about this last Tuesday" names a person, because no regular expression
> can. The admin renders the line for review rather than committing it for you precisely so
> that there is a human in that loop. `docs/labeling-golden-set.md` §4 is the rest of the
> rule.

---

## 7. PII

The lead detail page renders the submitter's own words, contact details included. **That is
allowed and it is the point of the page**: invariant 5 is a rule about logs, not about a
screen a person is looking at on purpose.

What follows from it:

* Listing rows carry `contact_email_hash`, not the address. The address is on the detail
  page, where a human is looking at one lead.
* No admin log event carries a payload, a form body or a row. They carry event names,
  identifiers, counts and money.
* The error handler renders a **fixed page** with one of the application's own sentences.
  It never echoes the request, the row, or the exception — an error page rendered while
  something went wrong *around a lead* is exactly where a payload would otherwise surface,
  and from there into whatever collects unhandled exceptions.
* Every page is `Cache-Control: no-store`, `X-Robots-Tag: noindex`, `Referrer-Policy:
  no-referrer`, and carries a Content-Security-Policy of `default-src 'none'` — no script,
  no external stylesheet, no image, nothing to fetch.

---

## 8. Performance

The acceptance criterion is *"the hot-but-marked-bad view returns in under a second on a
realistic dataset"*. There are two halves to holding it, and they are checked in two places:

* **The plan.** `tests/integration/test_store_admin.py` asserts via `EXPLAIN` that the
  review query uses an index scan rather than a sequential scan on `assessments`. That is
  the regression which makes the timing fail, and it is checkable on a small dataset.
  Marked `integration`, so it needs Postgres.
* **The number.** `scripts/seed_benchmark.py` generates a realistic dataset (200k leads
  across three tenants, with a funnel-shaped tier distribution and feedback on about one
  lead in eight) and times both the review and a deep browser page:

  ```bash
  docker compose up -d
  export DATABASE_URL=postgresql+psycopg://leadquali:leadquali@localhost:5432/leadquali_bench
  alembic upgrade head
  python scripts/seed_benchmark.py --leads 200000
  ```

  It exits non-zero if the review query misses a second, so it can be wired into a release
  check rather than only read.

The lead browser pages with a **keyset cursor** over `(created_at, id)`, never `OFFSET`.
Offset paging over a growing table is slow at depth and, worse, lossy: a lead inserted
while somebody is on page 3 shifts every later row down by one, and the row that moved from
the top of page 4 to the bottom of page 3 is never shown — silently, with no error and no
gap in the output.

Dashboards read #33's `usage_daily` rollups for volume and cost and never touch
`assessments`; the tier mix and the agreement rate are two direct queries bounded by a
tenant and a date range. A chart whose query scans `assessments` unbounded does not ship.

---

## 9. Why there is no build step

FastAPI plus Jinja2, progressive forms, no JavaScript framework, no bundler, no npm:

* it ships inside the **same Lambda** as everything else, so there is no second artefact
  and nothing that can be stale relative to the API;
* the audience is **a handful of staff**, none of whom is waiting on a 40ms interaction
  budget;
* a build step is **a second deployment pipeline to keep alive** — Node versions, a
  lockfile, a CI job, a cache — permanently, for a page that lists rows.

The Content-Security-Policy makes that a property the browser enforces rather than a habit
somebody has to keep: `default-src 'none'` with only inline styles allowed means a script
tag or a CDN stylesheet added later simply would not load.

Templates live in `src/leadquali/api/templates/admin/` and are declared in
`[tool.setuptools.package-data]`. A template that is not packaged is a 500 in Lambda and a
pass in the tests, which is the worst combination available, so
`tests/unit/test_admin_templates.py` checks every file on disk against those globs.
