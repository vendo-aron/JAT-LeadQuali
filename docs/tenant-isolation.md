# Tenant isolation

What stops one customer of LeadQuali seeing, influencing or being billed for another
customer's leads — and how each of those claims is tested. Issue #32.

This is written for a security reviewer. Every claim below has a test behind it, named, in
this repository. The last section is the one to read if you are short of time: it says what
is **not** isolated, because a document that only lists its own strengths is not evidence of
anything.

Encryption, key management and secret rotation are deliberately not repeated here; they are
in [`docs/security-overview.md`](security-overview.md) (issue #37).

---

## What isolation means here

LeadQuali is a shared-schema multi-tenant application: one PostgreSQL database, one schema,
and a `tenant_id` column on every table that holds customer data. Isolation rests on three
things, in ascending order of strength. Every repository method takes a `tenant_id` and
every SQL statement it emits carries a tenant predicate — including the statements where the
key is a UUID that could not collide, because a rule with exceptions is a rule nobody can
review. Every child table (`assessments`, `routing_events`, `feedback`) is bound to its
parent by a **composite foreign key** `(tenant_id, lead_id) → leads (tenant_id, id)`, which
makes a row belonging to one tenant and pointing at another tenant's lead not merely
invisible but *unstorable*. And the tenant a request acts as is never taken from the request
body: it comes from the API key presented, resolved against `tenant_api_keys`, and a
`tenant_id` in the payload is either rejected outright or kept as inert form data.

The tenant boundary is the database row, not the database instance. See
[What is not isolated](#what-is-not-isolated).

---

## The seven axes, and what was tested

The suite is `tests/isolation/`. It runs as part of the ordinary `pytest` invocation, which
is what makes "it runs in CI on every push" true without depending on a CI job somebody has
to remember; `tests/isolation/test_suite_is_collected.py` asserts that the directory is
inside `testpaths`, that nothing filters it back out, and that no workflow narrows its
`pytest` command line.

**178 of the suite's 204 tests run with no database** — 177 pass and one is skipped, the
one covering the single method whose tenant check is a comparison in Python rather than a
`WHERE` clause, and whose behaviour is asserted elsewhere in the same sweep. **The remaining
26 need PostgreSQL** and are marked `integration`. Which tests need a database is called out
per axis below, because a property asserted only by a test that skips is a property that is
not asserted — see [The negative control](#the-negative-control).

### 1. Data isolation — every repository method

`tests/isolation/test_repository_isolation.py` (78 tests, no database)
`tests/isolation/test_repository_isolation_integration.py` (26 tests, PostgreSQL)

Not a hand-written test per method. The sweep enumerates the six concrete repository classes
by introspection and fails in three ways, none of which is a skip:

- a **new public method that names no tenant** fails the enumeration test by name, unless it
  is added to a short allowlist (constructors, and three deliberately fleet-wide operator
  queries) with a written reason;
- a **new tenant-scoped method with no argument recipe** fails the sweep with a message
  telling the author to write one — arguments are never synthesised from type hints, because
  a generic harness produces a test that passes because the call errored;
- a method **whose statement loses its tenant predicate** fails the SQL check.

For each of the 26 swept methods the test builds the statement the method would execute,
compiles it against the real `postgresql` dialect, and asserts two things: the SQL contains a
tenant column being compared to a value (or, for an `INSERT`, writes the tenant column), and
the value bound to it is the tenant the caller named. A third test calls each method as
tenant B while holding tenant A's row identifiers and asserts that tenant A's identity never
reaches the database at all.

The integration half runs the same recipes against a real server with both tenants seeded
and a full set of rows for tenant A. Every call is bracketed by a byte-for-byte snapshot of
every row tenant A owns, across all seven tables, so "and nothing of A's changed" is checked
for every method rather than being asserted method by method. It also writes a cross-tenant
child row directly and confirms the server refuses it.

**Result: pass.** No repository method reaches the database without a tenant predicate. Two
exceptions are documented below and are enforced as exceptions rather than tolerated.

*This axis was live-tested during development.* Issue #33 added two store methods
(`compute_day` and `fleet_tenants_with_quota`) after the sweep was written, by a different
author, with no knowledge of it. The sweep failed on both — one for having no recipe, one for
taking no tenant — which is exactly the behaviour it exists for.

### 2. Auth isolation

`tests/isolation/test_auth_isolation.py` (35 tests, no database)

- Tenant A's real, unrevoked API key presented with tenant B's `X-LeadQuali-Tenant` header is
  refused, and refused as `unknown_tenant` so it is indistinguishable on the wire from a key
  that has never existed.
- A request with A's key and A's header but signed with B's HMAC secret is a 401. Signing
  secrets are per tenant.
- A `tenant_id` in the request **envelope** is a 422 — the envelope forbids unknown fields —
  and nothing is stored.
- A `tenant_id` **form field** is accepted, because the form belongs to the customer and they
  may name a field whatever they like. The assertion is on what the store received: the row
  is filed under the authenticated tenant, and the claimed value survives only as inert form
  data. This is the one people get wrong, and a 202 status code proves nothing about it.
- A forged cross-tenant request does not consume the nonce a legitimate request is about to
  use, so one tenant cannot deny another service by guessing nonces.

**One decision, in one place.** Since #31's review fixes, every rule about whether a presented
key may authenticate a request — is the row the claimed tenant's, is the key revoked, has its
rotation overlap closed, does its secret verify, is the tenant active — lives in
`decide_credential` in `src/leadquali/app/credentials.py`. It is pure: no database, no secret
store, no clock of its own. Both resolvers call it once their own `key_id` lookup has produced
a row. The suite drives it directly and exhaustively, and then asserts that **both**
resolvers answer exactly what it answers on every input. That is the same trap this whole
suite exists for, in #31's own words: the rules used to be written twice, once on the path
production runs and once on the path the offline test suite exercised.

Every credential test still runs against both implementations of the port — the in-memory one
and `PostgresIngestCredentials`, driven through its real statement against a double of the
`tenant_api_keys` table — because sharing a decision is not the same as sharing the lookup
that feeds it.

**Ingest suspension** is in the same module: suspending tenant A stops tenant A's ingest with
a 403 and leaves tenant B's working with a 202, asserted in one test so that a change which
broke the endpoint for everybody could not pass. Two orderings matter here and both are
tested. The **ownership check runs first**, so a suspended tenant's key presented under
another tenant's name is an `unknown_tenant`, not a `tenant_suspended` — the 403 cannot be
used to ask whether an account you do not hold a key for has been suspended. And the **status
check runs after the secret is verified**, which costs an argon2 verification a cheaper
ordering would avoid. That is deliberate: `403 "tenant is not active"` is the one answer this
endpoint gives that is not identical to every other rejection, so it must be reachable only
by a caller who has proved it holds the secret. An API key travels in a header, in the clear,
on every submission; checking status first would let anyone who had ever seen one discover
that the account had been suspended for non-payment. A suspended tenant presenting a *wrong*
secret is told its key is bad, and the suite asserts exactly that.

**A dependency outage is not an authentication failure.** If a tenant's signing secret cannot
be read — Secrets Manager throttling, a network blip — the answer is `AuthFailure.UNAVAILABLE`,
which becomes a **503 with a `Retry-After`**, not a 401 and not a 500. The caller has already
proved it holds a live key, and a browser form told that its key is bad does not retry: the
lead would be gone, which invariant 3 forbids. The isolation claim on top of that is tested
too — one tenant's secret store being down leaves the other tenant posting leads normally,
and an outage still does not let a key be used under another tenant's name, because the
secret is fetched only *after* the decision has accepted.

**Result: pass.**

### 3. Config isolation

`tests/isolation/test_config_isolation.py` (10 tests, no database)

One identical model output, decided under the two real shipped tenant configurations
(`tenants/default.json` and `tenants/acme-demo.json`), produces a different **tier**, a
different **action** and a different **destination** — all three. The scores sit at least ten
points inside their bands on both sides, so this is a statement about two policies rather
than about rounding. A second axis: the same confidence (0.70) is above one tenant's floor
and below the other's, so one gets a routed lead and the other a low-confidence escalation.

The same lead then goes through the real qualification pipeline twice, once per tenant, from
**one** pipeline instance — because a pipeline built fresh per tenant could not exhibit the
failure this is looking for, which is cached or shared state. The assertions are on what the
notifier was told to do and what the store recorded, and on the fact that each model call was
made against its own tenant's ICP text. Running the two tenants in the opposite order
produces the same two answers.

**Result: pass.**

### 4. Feedback link isolation

`tests/isolation/test_feedback_isolation.py` (17 tests, no database)

A feedback link is a signed bearer capability: "record verdict V for lead L on behalf of
rater R until time T". Every field is inside the signed material, and each one is attacked:

- a link for tenant A's lead, edited to name a different lead — refused;
- edited to claim tenant B — refused, twice over, because the MAC key is derived per tenant;
- edited to flip its verdict — refused;
- a claim naming tenant B re-signed with tenant A's *derived* key — refused, which is the
  scenario per-tenant derivation exists for.

Each refusal is asserted at the token layer **and** by checking that the store was never
called, since a rejection that still wrote a row would be a poisoned training set behind a
green suite. The rejection page names neither tenant nor lead. The signature is verified
before the expiry, so a forged claim can never learn "that lead exists, the link just
expired".

Below the token, the store refuses a verdict against a lead the tenant does not have — in
memory here, and against the composite foreign key in the integration sweep.

**Result: pass.**

### 5. Metering isolation

`tests/isolation/test_metering_isolation.py` (23 tests, no database)

On a day when both tenants were active, with deliberately dissimilar spend, each tenant's
rollup counts only its own leads, assessments, tokens and cost; each period total reads back
only its own rollups; a third tenant that has done nothing reads zero rather than the fleet's
figures. That last one matters because the failure a missing filter actually produces is not
an error — it is a plausible-looking total on an invoice.

Quotas are measured against the tenant's own usage, and writing one tenant's plan does not
move another's.

The billing SQL is also read directly: `rollup_day` and `compute_day` are single statements
over two source tables, and **both** tables are checked for their own tenant predicate, since
a filter on one and not the other would produce a row that is half one tenant's and half the
fleet's — internally consistent enough to bill from.

**Result: pass**, with one documented exception below.

### 6. Log isolation

`tests/isolation/test_log_isolation.py` (9 tests, no database)

A full pipeline run for tenant A, in a process that has already served tenant B through the
same store, the same pipeline, the same queue and the same config source. Every log record is
captured through the real JSON formatter and searched for any of tenant B's identifiers:
slug, row id, lead id, submission id, contact address, the SHA-256 of that address, routing
destination, the SHA-256 of *that*, company name, and a distinctive phrase from the message.

The forbidden set is **derived from tenant B's fixture**, never typed as literals, so renaming
a fixture cannot silently empty the assertion. The hashed forms are in the set deliberately:
invariant 5 says an address is logged as its digest, so the correct rendering of another
tenant's contact is a 64-character hash — and that hash in the wrong tenant's log is a leak
wearing the uniform of a privacy control.

Both orderings are tested, because a leaked context variable only leaks forwards. Every
absence assertion has a presence assertion beside it, so none of them can pass on an empty
buffer.

**Result: pass.** No tenant's identifiers appear in another tenant's trace output, and
neither tenant's lead text, address or company name appears in any log record at all.

### 7. Row-level security

A decision rather than a test. See [the next section](#row-level-security-the-decision).

---

## The documented exceptions

Four methods do not carry a tenant predicate. Each is enforced as an exception by the sweep —
the tests fail if one of them quietly changes shape — rather than merely tolerated.

**`fleet_billable_leads`, `fleet_daily_spend`, `fleet_tenants_with_quota`.** Reconciling our
usage against Anthropic's invoice, and allocating shared infrastructure cost, are questions
about the whole workspace and cannot be answered one tenant at a time. Three rules contain
them: the name carries `fleet_` so it is visible at every call site; the method takes no
tenant, so it cannot be mistaken for a query somebody forgot to filter; and it returns the
per-tenant *breakdown* rather than an anonymous total that could be printed anywhere. Every
per-tenant report produced by `usagectl` — human and JSON — is rendered in the test suite and
searched for the other tenant's slug and figures.

One fleet-derived number does reach a single tenant's report: `fleet_billable_leads` on the
margin report, the denominator of the pro-rata infrastructure allocation, carried so that a
reader can see what the share was computed from. It names nobody. It is an aggregate, and in
a two-tenant fleet the other tenant's billable count is one subtraction away from it — so it
is acceptable only because the margin report is an internal operator tool run by us and is
never rendered to a customer. **If that report is ever put in front of a customer, this field
has to go.**

**`PostgresTenantAdminStore.list_tenants`.** The control plane's own enumeration, for an
operator running `tenantctl`. It is on no request path and must never be reachable from an
authenticated tenant context.

**`PostgresIngestCredentials.resolve`.** The one method whose tenant check is a comparison in
Python rather than a `WHERE` clause. Its single indexed read is by `key_id` — which is what
makes argon2 affordable on the request path, since a stranger who does not hold a real
`key_id` can never make us spend the KDF — and the row it finds carries the owning tenant's
slug, which `decide_credential` then compares. So the sweep asserts its *behaviour* instead
of its SQL: a real key presented under another tenant's name is refused, as `unknown_tenant`,
and the signing secret is never fetched.

This is the *only* place in the codebase where a tenant check is not a predicate. In
particular `PostgresIngestCredentials._touch`, the `last_used_at` stamp, **is** tenant-scoped:
its `UPDATE` filters on the tenant as well as on the globally unique `key_id`, redundantly
against both the index and the row the same call has just read. An earlier version of this
document listed it as a third exception on the grounds that it was provably safe; #31's
review closed it, on the grounds that an exception is what a later reader copies.
`tests/isolation/test_repository_isolation.py::test_the_last_used_write_is_tenant_scoped_like_every_other_write`
holds it to that, and would have failed if the fix had not landed.

---

## Row-level security: the decision

**We do not enable PostgreSQL row-level security in v1.**

For it: defence in depth against exactly the class of bug this suite hunts for.

Against, and this is what decides it:

1. **The structural guarantee already exists and is stronger where it applies.** Every child
   table's composite `(tenant_id, lead_id)` foreign key makes a cross-tenant row
   *unrepresentable*, not merely unselected. RLS would filter rows the schema already cannot
   contain.
2. **RLS moves the tenant filter out of the code a reviewer can read.** It needs the current
   tenant in a session setting (`SET LOCAL app.tenant_id`) that every transaction must
   establish. That turns a `WHERE` clause anybody can read into a side effect nobody can, and
   a missing `SET` fails *open* on a superuser-ish role or *closed* on everything else —
   neither of which is a good failure. The application would also need a second,
   non-owning database role, because both `BYPASSRLS` and table ownership defeat it.
3. **It interacts with connection pooling through the RDS Proxy.** Session-scoped state and
   proxy connection reuse are in tension. Whether a given statement pins a connection is a
   property of the proxy's behaviour, and **we make no claim about what RDS Proxy does here**:
   it would have to be measured on the real thing before RLS could be relied on, and that
   measurement is part of the cost of adopting it.

**Review trigger.** Revisit this decision if any of the following becomes true: a second
application database role appears; any customer-facing SQL surface is exposed (a read
replica, an analytics connection, a BI tool); or a contract requires row-level security by
name.

---

## The negative control

"We tested our tests" is a claim, so here is the evidence.

The tenant filter was removed from one repository method by hand, the suite was run, and the
change was reverted. It was **not** committed.

```diff
--- a/src/leadquali/adapters/store_postgres.py
+++ b/src/leadquali/adapters/store_postgres.py
@@ -482,7 +482,6 @@ class PostgresLeadStore:
         statement = (
             select(RoutingEvent.id)
             .where(
-                RoutingEvent.tenant_id == tenant,
                 RoutingEvent.lead_id == lead,
                 or_(
                     RoutingEvent.dispatched_at.is_not(None),
```

**The rest of the test suite stayed completely green: 1989 passed, 197 skipped.** That is the
point of this exercise. Removing a cross-tenant filter from the code that runs in production
was, until this suite existed, invisible.

The isolation suite failed:

```
FAILED tests/isolation/test_repository_isolation.py::
  test_every_statement_is_scoped_to_the_tenant_it_was_given[PostgresLeadStore.already_routed]

AssertionError: PostgresLeadStore.already_routed statement 1 of 1 has no tenant predicate
and writes no tenant column.
  select routing_events.id
  from routing_events
  where routing_events.lead_id = %(lead_id_1)s::uuid and (routing_events.dispatched_at is
  not null or routing_events.action = %(action_1)s)
   limit %(param_1)s
Every statement is filtered on the tenant, including the ones where the key is unique
anyway (CLAUDE.md invariant 4).

1 failed, 176 passed, 27 skipped
```

A second, subtler mutation was run and reverted the same way: the predicate was left in place
but made vacuous, `RoutingEvent.tenant_id == RoutingEvent.tenant_id`. A check that only looked
for the column name in the SQL would have passed it. The suite failed on the second assertion:

```
AssertionError: PostgresLeadStore.already_routed statement 1 mentions a tenant column but
does not bind the tenant it was called with.
  where routing_events.tenant_id = routing_events.tenant_id and routing_events.lead_id = ...
  bound: ['1', '3a5c9e10-0b47-4d2f-9c61-7e8a04b5d213', 'suppress']
```

Neither mutation is in the repository. `tests/isolation/test_repository_isolation.py::test_a_deleted_filter_is_visible_to_the_evidence_rules`
keeps the same two checks honest against synthetic SQL on every run, so the mechanism does not
depend on anybody repeating this by hand.

---

## What is not isolated

Honest limits, each with its mitigation and what a dedicated-infrastructure tier would change.

### Shared compute

All tenants' leads are processed by the same Lambda functions. A bug in the qualification
worker, a poison-pill message or a sustained burst from one tenant affects the service every
tenant receives.

*Mitigation:* per-tenant rate limiting from each tenant's own `tenants` row, with an
account-wide API Gateway throttle behind it; reserved worker concurrency caps; a dead-letter
queue with an alarm, so one tenant's bad message cannot block the queue. No lead is ever
dropped — invariant 3 — so the failure mode is delay, not loss.

*A dedicated tier would change:* a separate function, queue and concurrency reservation per
tenant, so the blast radius of a bad message or a burst is one customer.

### Shared database instance

One RDS instance, one database, one schema. Tenants are separated by rows, not by database,
schema or instance. A sufficiently severe bug in the application layer — not in the schema,
which refuses cross-tenant rows structurally — could read across the boundary. That is the
bug this entire suite exists to catch, and it is a real category rather than a hypothetical.

*Mitigation:* the sweep above, plus the composite foreign keys, plus the fact that no query
in the codebase is assembled from strings, so a tenant id cannot be injected into a predicate.

*A dedicated tier would change:* a database per tenant, and with it the operational cost of
migrating N databases instead of one.

### Shared Anthropic workspace and rate limit

Every tenant's assessments are made with one Anthropic API key against one workspace rate
limit. A tenant sending a large volume consumes rate limit that another tenant's leads then
have to wait for.

*Mitigation:* the SDK retries with backoff; an assessment that still cannot be made escalates
the lead to a human rather than scoring it low (invariant 3); per-tenant quotas make a tenant
whose volume has changed visible before it becomes everybody's problem. No prompt or lead
content is ever shared between tenants — each call carries one tenant's ICP text and one
lead — and no tenant's data enters another tenant's prompt.

*A dedicated tier would change:* a separate Anthropic workspace and key per tenant, which
also separates the invoice.

### Shared replay guard and rate limiter, per process

The nonce replay guard and the per-tenant token-bucket rate limiter are in-process
structures. Behind several concurrent Lambda instances, both are per-instance: a replayed
request can land on a different instance inside the signing window, and a tenant's effective
rate limit is the configured limit times the number of warm instances.

The limiter holds two per-process maps keyed by tenant — cached allowances and token buckets
— and **both are capped and evict the least recently used tenant**. So enough traffic from
enough other tenants can drop a tenant's bucket and hand it a fresh full one. That is one
tenant's volume affecting another's effective throttle. It is not a data leak: nothing of one
tenant's is readable by another, and the tests pin the keying — one tenant spending its whole
burst leaves the other's untouched, and a failure of the allowance source falls back to *the
default*, never to whichever tenant's limit was read most recently.

*Mitigation:* for replay, the ingest handler's own `(tenant_id, submission_id)` idempotency
means a replayed body creates no second lead and no second enqueue, and the stage-level
throttle caps how fast anyone can try. For rate limiting, the account-wide API Gateway
throttle is the real backstop; the per-tenant limiter is a fairness measure, not a security
control, and is documented as such. Eviction failing open — granting a fresh bucket rather
than refusing — is the right direction for invariant 3, since the alternative is dropping a
paying customer's lead to protect a fairness measure.

*A dedicated tier would change:* nothing here by itself. Closing these properly means a
shared nonce and counter store (Redis or DynamoDB), which is a change for every tier.

### Logs and metrics are in one place

All tenants' structured logs go to one CloudWatch log group and all metrics to one namespace,
dimensioned by tenant. Anyone with access to our AWS account can see every tenant's
operational metadata. No lead content, no email address and no free text is in there —
invariant 5, tested across the whole pipeline — but lead counts, tiers, latencies and cost
are.

*Mitigation:* access to the AWS account is the control; see
[`docs/security-overview.md`](security-overview.md).

*A dedicated tier would change:* a log group and metric namespace per tenant, and the option
of delivering a tenant's own logs to their account.

### A feedback link is a bearer capability

Anyone holding a valid feedback link can use it, for the one verdict on the one lead it was
minted for, until it expires. There is no login: a sales rep who has to authenticate does not
click, and a feedback loop nobody uses collects nothing. The link cannot be edited to point at
a different lead, tenant or verdict, and a second click updates the same row rather than
creating a new one.

*Mitigation:* a signed 30-day expiry, one capability per link, and the fact that the only
thing a link can do is record one opinion about one lead.

*A dedicated tier would change:* nothing. Closing this means authenticating reps, which is
issue #29's per-rep identity work and a product decision rather than an infrastructure one.

---

## Reproducing this

```bash
pytest tests/isolation                 # 177 passed, 27 skipped, no database needed
docker compose up -d                   # see docs/local-database.md
export DATABASE_URL=...
pytest tests/isolation -m integration   # the 26 that need PostgreSQL
```

Without a database the `integration` tests skip with the reason printed; they never fail for
want of Docker, which is why the offline half carries the weight.

Every number in this document comes from a run of the suite in this repository at the commit
that added it.
