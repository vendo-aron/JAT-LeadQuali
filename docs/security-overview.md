# Security overview

The answer sheet for a customer security questionnaire. Issue #37.

Written so that someone who has never read this codebase can answer a questionnaire from it.
Each section says what is true today, and where something is *not* done it says so rather
than leaving the reader to infer it — a document that only lists its strengths is not
evidence of anything.

Three things are deliberately not repeated here:

- **Tenant isolation** is [`docs/tenant-isolation.md`](tenant-isolation.md), which is longer
  and better than a summary would be, and which ends with a section on what is *not*
  isolated. Send that document, not a paraphrase of it.
- **Retention and deletion** are [`docs/data-retention-policy.md`](data-retention-policy.md)
  and [`docs/deletion-requests.md`](deletion-requests.md).
- **Data residency** is [`docs/data-residency.md`](data-residency.md), because European
  buyers ask about it specifically and at length.

**Status.** LeadQuali is pre-first-customer. Some answers below are "the infrastructure
defines it and it has not been deployed yet". Those are marked. Do not upgrade them in a
customer conversation.

---

## 1. Encryption at rest

Two customer-managed KMS keys (`infra/network.yaml`, issue #27), both with automatic key
rotation enabled and a 30-day deletion window, both `DeletionPolicy: Retain`.

| Key | Covers |
|---|---|
| `alias/leadquali-<stage>-db` | The RDS Postgres instance's storage and **every snapshot taken from it** — so the lead table, the assessments and the backups. |
| `alias/leadquali-<stage>-secrets` | The application secrets in Secrets Manager: each tenant's HMAC signing secret, the feedback-link signing key, the Anthropic API key. |

They are two keys rather than one, and the reason is worth giving to a reviewer because it is
the interesting part of the design. The secrets key carries a `Deny` statement that makes it
usable **only through Secrets Manager**, by anybody in the account including an
administrator. The database key cannot carry that statement, because the RDS service itself
has to use the key to encrypt volumes and snapshots. Sharing one key would therefore mean
granting each Lambda role `kms:Decrypt` on the key that protects the database backups — so a
compromised function could decrypt a copied snapshot, the whole lead table included, rather
than one API key.

**What the customer-managed keys do *not* cover**, and this is the part to say out loud:

- **SQS.** The lead queue and its dead-letter queue use **SQS-managed server-side encryption**
  (`SqsManagedSseEnabled`), not a CMK. Messages are encrypted at rest with an AWS-managed key.
  A lead's payload is on that queue for seconds to minutes between ingest and the worker. The
  queue retains messages for 4 days and the DLQ for 14.
- **CloudWatch Logs.** The log groups use CloudWatch Logs' own service-side encryption, not a
  CMK. No lead content is in the logs at all (section 5), so the exposure is operational
  metadata: tenant slugs, lead ids, tiers, latencies, costs.
- **Lambda environment variables.** Encrypted at rest by Lambda's own AWS-managed key. No
  secret is ever a Lambda environment variable — the variables hold Secrets Manager ARNs and
  the functions fetch values at runtime (`tests/unit/test_infra_template.py` asserts that no
  secret is a plaintext parameter or environment variable).

Moving SQS and CloudWatch Logs onto the CMK is a small change to two templates and is the
right answer if a customer asks for it in writing; it is not done today.

---

## 2. Encryption in transit

- **Browser → ingest API.** HTTPS only, terminated at API Gateway. Every submission is also
  HMAC-signed with a per-tenant secret, so the endpoint authenticates the body as well as the
  channel.
- **Lambda → RDS.** TLS, and not by convention: the instance's parameter group sets
  `rds.force_ssl = 1` and the RDS Proxy sets `RequireTLS: true`. A connection URL that
  negotiated plaintext would be **refused by the server**, so this is not a client-side
  setting somebody can forget. The application's URL carries `sslmode=require`.
- **The RDS Proxy hop.** Both legs are TLS: client → proxy (`RequireTLS`) and proxy →
  instance (`force_ssl`). This is the hop reviewers ask about, because a proxy that
  terminated TLS and spoke plaintext to the database would be a plaintext database
  connection with a TLS badge on it. It does not.
- **Lambda → Anthropic, SES, Secrets Manager, SQS.** HTTPS, through the SDKs. Egress leaves
  the VPC through a NAT gateway; there is no route from the private subnets to the internet
  that does not go through it.
- **Staff admin.** HTTPS, with a `__Host-`-prefixed `Secure` session cookie, so the cookie
  cannot be set or read over plaintext and cannot be scoped to a parent domain.

**Not done:** TLS certificate pinning anywhere, and mutual TLS between the customer's site
and our endpoint. Neither is planned; the HMAC signature is what authenticates a submission.

---

## 3. Network

One VPC (`infra/network.yaml`). Three private subnets with no route to an internet gateway,
a single NAT gateway for egress, and a database with `PubliclyAccessible: false` and
`DeletionProtection: true`.

**There is no public address for the database.** That is the acceptance criterion of #27 and
it is also what makes the migration story honest: migrations run from a Lambda inside the
VPC because there is no other way in, not because somebody remembered not to expose it.

Security groups are referenced by group id rather than by CIDR: the database accepts 5432
only from the Lambda security group (or, when the proxy is on, only from the proxy's).

---

## 4. Authentication and key handling

### Customers (the ingest API) — issue #31

- Each tenant holds one or more API keys. Only an **argon2id hash of the secret half** is
  stored (`tenant_api_keys.key_hash`); the secret is shown once at issue and cannot be
  recovered.
- A key is `leadquali_live_<key_id>_<secret>`. The `key_id` is a public 64-bit handle that
  names the row; the lookup is on it, which is what makes an argon2 verification affordable
  on the request path — a stranger who does not hold a real `key_id` can never make us spend
  the KDF.
- **Every request is also HMAC-signed** with a per-tenant secret held in Secrets Manager,
  over method, path, tenant, timestamp, nonce and body. A stolen key alone is not enough.
- **Rotation is a non-event.** A tenant may hold several keys at once: issue the new one, set
  an `expires_at` a week out on the old one, the customer redeploys, the old row closes.
  `tenantctl rotate-key <slug> <key_id> --overlap-days 7`.
- **Revocation is immediate and permanent.** `tenantctl revoke-key`. The row stays, marked
  revoked with a timestamp, because "which key did we revoke, and when?" is a question an
  incident review asks. A revoked key cannot be un-revoked.
- Every rejection is indistinguishable on the wire: a key for another tenant, a revoked key
  and a key that never existed are all `401 unknown_tenant`. The one exception is a suspended
  tenant, which gets a 403 — and only *after* the secret has verified, so it cannot be used
  as an oracle by somebody who has merely seen a key in a page's source.
- A failure to read a tenant's signing secret is a **503 with `Retry-After`**, never a 401: a
  caller who has already proved it holds a live key must not be told its key is bad, because
  a browser form told that does not retry and the lead would be lost.

### Staff (the admin UI) — issue #36

- Username and **argon2id** password. Argon2 is the right choice here for the reason it is
  merely defence in depth for machine keys: a human's password is guessable, so the cost per
  guess is the control.
- A **login gate** makes every guess cost the same whoever it is for, and makes repeated
  wrong guesses for one username cost nothing at all.
- The session is one HMAC over `{subject, issued_at, expires_at}` with an **absolute
  12-hour expiry and no sliding renewal**. A stolen cookie expires on schedule rather than
  being kept alive by the thief using it.
- Every state-changing post carries a **CSRF token derived from the session token**.
- Every admin route is behind the session; `tests/unit/test_api_admin.py` enumerates the
  routes from the application itself, so a route added to the wrong router fails the suite
  rather than shipping open.

**Not done:** SSO or SAML for staff, hardware MFA, and per-rep identity for the feedback
links (a feedback link is a signed bearer capability for one verdict on one lead — see
[`docs/tenant-isolation.md`](tenant-isolation.md)).

### Secrets

Nothing is a literal and nothing is a plaintext parameter. Every secret is a Secrets Manager
ARN passed as a parameter and fetched at runtime: the database credentials, the per-tenant
ingest signing secrets, the feedback-link signing key, the Anthropic API key and — since #35
— the Stripe API key and the Stripe webhook signing secret. Origins and rotation procedures
are in [`docs/runbooks/secrets-and-rotation.md`](runbooks/secrets-and-rotation.md). Database credentials are generated **and rotated** by
RDS itself; the application never stores a database URL, it assembles one from the RDS-managed
secret and the endpoint.

Each Lambda can read only the secrets it names. The worker cannot read the ingest credential
map; the migration function can read the database secret and nothing else; the retention
function likewise; the billing functions hold neither the ingest credential map nor the
model key, and the function that serves a customer's web form holds no Stripe secret at all
— which is why billing is separate functions rather than another route on the ingest one. `tests/unit/test_infra_secrets.py` checks that in **both** directions — a
grant without a matching environment variable is over-privilege nobody notices, and a
variable without a grant is an `AccessDenied` on the first cold start.

---

## 5. Logging and PII

**Invariant 5 of `CLAUDE.md`: no personal data in logs. An address is logged as its SHA-256,
never as an address, and a raw payload is never logged at all.**

This is enforced by tests, not by a paragraph:

- `tests/unit/test_observability_pipeline.py` runs a real lead with a distinctive address and
  a distinctive free-text message through the real ingest service, the real queue message and
  the real pipeline, with real logging captured, and asserts neither string appears in any
  record — on the happy path **and** on the exception path, where a traceback that formatted
  a submission would leak the lot.
- `tests/unit/test_pii_log_sweep.py` (issue #37) makes that structural. It **enumerates every
  log event the source emits** by walking the AST of every module, and fails when one is not
  covered: each event is either driven through its real code path with a lead's data in scope
  and swept, or declared to carry no fields at all — and that declaration is checked against
  the AST, so adding a field to such an event fails the suite. **55 events, all covered.** A
  new event cannot quietly opt out.
- `tests/isolation/test_log_isolation.py` proves one tenant's identifiers never appear in
  another tenant's trace output.

The mechanisms underneath: `LeadSubmission` declares every field `repr=False`, so no
traceback can render one; the event helper functions in `observability/events.py` cannot be
*passed* a submission, which is the enforcement rather than the convention; both log
formatters run every message, every field and every traceback through an address redactor as
a last resort; and third-party loggers (`sqlalchemy.engine` above all, whose echo would print
bound parameters) are pinned no lower than `WARNING`.

**The staff admin renders raw payloads to a human, by design** (#36). That is the point of
the lead detail page and it is allowed — invariant 5 is about logs and error pages, not about
screens a signed-in operator opened deliberately. What is not allowed is one reaching a log
line or an error page, and both are tested: an admin page that raises while rendering a lead
logs the exception's **class and nothing else** (no traceback, because its frames hold the
row) and returns a fixed error page that echoes neither the record nor the request.

**What redaction cannot do** is in [`docs/data-retention-policy.md`](data-retention-policy.md)
and is the honest limit: an address is a pattern and a person's name is not.

The sweep covers the billing surface too. `stripe_events.payload` is stored verbatim and an
invoice object carries the billing contact's name, email and postal address, so every one of
the 22 billing events is driven with those three strings planted in the payload and the
output searched for them. A log line that quoted an invoice would be the same disclosure as
one that quoted a lead.

---

## 6. Backups and restore

- **Automated backups**, 7 days by default (`DbBackupRetentionDays`, minimum 1 — the template
  cannot express 0). Backup window 03:10–03:40 UTC.
- **Point-in-time recovery** within the retention window, which is what automated backups give
  you on RDS.
- `DeletionPolicy: Snapshot` on the instance, so deleting the stack takes a final snapshot
  rather than the database.
- `DeletionProtection: true`, so the instance cannot be deleted from the console by accident.
- Snapshots are encrypted with the same customer-managed key as the instance.
- **Multi-AZ is off by default.** At hundreds of leads a day behind SQS, an AZ failure delays
  qualification and drops nothing (invariant 3: no lead is ever dropped, so the failure mode
  is latency). It is a parameter; turn it on when a contract says so.

**Not done, and say so:** no restore has been rehearsed, because nothing is deployed. A
restore drill is the honest gap here. When one is run, note that **a restore reintroduces
data that retention had deleted**, so re-running the purge is the last step of the procedure
(see the retention policy).

**Cross-region backup copies:** not configured. See
[`docs/data-residency.md`](data-residency.md) — this is also a residency question, not only a
durability one.

---

## 7. Availability and data loss

Not a security question, and it is asked on the same form.

- Leads are accepted at the edge, **persisted, and queued** before any model work. The 202
  does not wait on an assessment.
- SQS with a dead-letter queue and a bounded retry count; the DLQ has a depth alarm (#29).
- **Invariant 3: a lead is never silently dropped.** Low confidence, an API failure, a model
  refusal, a timeout, a parse error — every one escalates to a human rather than scoring the
  lead low. Only an explicit spam determination suppresses, and even that is recorded.
- Reserved concurrency on every function, sized against the database's connection budget.

---

## 8. Sub-processors and where data goes

The full list with what each one processes is in [`docs/dpa-draft.md`](dpa-draft.md). In
brief: **AWS** (hosting and all lead data), **Anthropic** (the lead's free text, sent for
assessment), **Stripe** (billing contact and payment data, **never lead data** —
[`docs/billing-integration.md`](billing-integration.md) is the authority on exactly what it
receives).

**Anthropic does not train on API data by default.** The citation is in the DPA draft; do not
assert it from memory in a customer conversation, quote the term.

---

## 9. Vulnerability management and the supply chain

- Dependencies are pinned by a lockfile and installed from PyPI. Runtime dependencies are
  listed in `pyproject.toml` with a comment saying why each is a runtime dependency rather
  than a dev tool.
- `ruff` (including its security rules, `S`), `mypy --strict` over `src` and `tests`, and the
  full test suite run on every push. A failure blocks the merge.
- **Not done:** no scheduled dependency-vulnerability scan (Dependabot, `pip-audit`), no SAST
  beyond ruff's `S` rules, no penetration test, no bug bounty. These are the honest answers
  to four questions that are on every questionnaire, and the first of them is cheap to fix.

---

## 10. Prompt injection

Worth a line because a reviewer who knows what an LLM is will ask.

A lead's text is rendered into the prompt as **delimited untrusted data** with a per-request
nonce, and the nonce is stripped if it appears inside the payload. More importantly, the
model's output cannot make a decision: it returns a `LeadAssessment` with scores and
extracted facts, and **tier, total score and routing action are computed in Python** from the
tenant's configuration (invariant 2 — `tier` and `action` are not even in the model's output
schema). So the worst a successful injection achieves is a wrong score on one lead, which the
confidence gate and the human review loop are there to catch. It cannot route a lead
somewhere, change a rubric, or reach the database.

`tests/unit/test_injection_corpus.py` runs a corpus of attempts against the renderer.

---

## 11. Incident response

> **The owner must fill these in before this document is sent to anybody.** They are the
> three questions on every questionnaire that cannot be answered from a repository.

- **Security contact:** _(an address that is monitored — `security@…` — not an individual's
  inbox)_
- **Notification commitment:** _(how quickly we tell a customer about a breach affecting their
  data. GDPR Article 33 gives a controller 72 hours from *their* awareness, so a processor's
  undertaking is normally "without undue delay" and often a stated number of hours. **A
  lawyer sets this**; it is a contractual term, not an engineering one.)_
- **On-call:** _(who is paged, and when)_

What exists today on the technical side: CloudWatch alarms on DLQ depth, worker error rate,
p99 latency, daily token spend and tier-distribution drift (#29); a structured log with a
trace id spanning each lead's whole journey; CloudTrail for the AWS account; and
`tenantctl revoke-key` as the immediate containment action for a leaked customer credential
(see [`docs/runbooks/secrets-and-rotation.md`](runbooks/secrets-and-rotation.md) for the
rotation procedures).

---

## 12. Access to production

- Access to customer data means access to the AWS account. There is no separate
  application-level route to another tenant's data (see
  [`docs/tenant-isolation.md`](tenant-isolation.md)).
- The database has no public address; reaching it means being inside the VPC.
- The staff admin can read any tenant's leads. It is the deliberate exception and it is
  behind the staff login described in section 4.

> **The owner must state:** how many people have production AWS access, whether that access
> requires MFA, and whether it is federated or by long-lived IAM users. These are facts about
> the account, not about the code, and they are asked on every questionnaire.

---

## Reproducing the claims

```bash
pytest tests/unit/test_pii_log_sweep.py          # every log event, swept for PII
pytest tests/isolation                           # the seven isolation axes
pytest tests/unit/test_infra_secrets.py          # least privilege, both directions
pytest tests/unit/test_infra_network.py          # no public database, TLS forced
pytest tests/unit/test_retention.py              # the retention tiers and the erasure receipt
```

Every one of those runs without a database, without AWS credentials and without an Anthropic
key. That is deliberate: a claim in a security document that is only checked by a test
requiring Docker is a claim that is checked on nobody's machine.
