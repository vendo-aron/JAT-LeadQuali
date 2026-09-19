# Data residency

Which regions LeadQuali's data lives in, and what an EU-only customer would actually
require. Issue #37.

European buyers ask about this before they ask about almost anything else, and the part they
care about is rarely the database — it is the model provider. So that section is the longest.

---

## Where data is today

**Nothing is deployed.** `infra/network.yaml` and `infra/template.yaml` are region-agnostic:
neither names a region, and the region is chosen by whoever runs `sam deploy`. Everything the
stacks create — VPC, RDS, SQS, Lambda, Secrets Manager, KMS keys, CloudWatch Logs — is
regional and lives in that one region.

> **The owner must record the deployment region here, in this file, at the first deploy.**
> "It is whatever we chose" is not an answer to a questionnaire, and a region nobody wrote
> down is a region somebody will guess wrong in front of a customer.

| Component | Region | Holds |
|---|---|---|
| RDS Postgres + snapshots | _(the deployment region)_ | Everything: lead payloads, assessments, feedback, tenants |
| SQS lead queue + DLQ | _(same)_ | A lead's payload, for seconds to minutes (4 days on the DLQ) |
| Lambda | _(same)_ | A lead's payload in memory, for the duration of one invocation |
| Secrets Manager + KMS | _(same)_ | Signing secrets and API keys; no lead data |
| CloudWatch Logs + metrics | _(same)_ | Operational metadata only — no lead content (invariant 5) |
| **Anthropic API** | **See below** | A lead's free text, for the duration of one assessment |
| **SES** | _(same, or a different SES region)_ | Routing emails, which contain the lead's details |
| **Stripe** | See [`docs/dpa-draft.md`](dpa-draft.md) | Billing contact and payment data. **Never lead data.** |

Two of those need care.

**Cross-region backup copies are not configured.** Snapshots stay in the deployment region.
That is the simple answer for residency and the bad answer for disaster recovery, and it is a
deliberate trade that a customer may want changed — in which case the copy's region becomes a
residency commitment too.

**SES is a separate choice.** SES is not available in every region, and the sending region is
configured independently (`AWS_REGION` for the SES client). A routing email contains the
lead's name, address and message, so if SES sends from a different region to the one the
database is in, **that is a second region holding personal data** and it has to be named. Set
the SES region to the deployment region unless there is a reason not to, and write down the
reason if there is.

---

## The Anthropic hop, which is the one they are asking about

Every lead's free text is sent to Anthropic's API to be assessed. This is the part of the
system a European buyer will focus on, and the honest position has three parts.

**1. Today we call the Anthropic first-party API.** Anthropic operates from the United
States. The transfer is therefore a transfer to a third country under GDPR, and it needs a
lawful transfer mechanism — in practice Standard Contractual Clauses, which Anthropic's
commercial terms and DPA provide. **A lawyer must confirm that the version in force covers
our use**; see [`docs/dpa-draft.md`](dpa-draft.md).

**2. What is actually sent.** Not the whole payload — the rendered lead, which is the
submitter's name, company, role, website and message, delimited as untrusted data. The
tenant's ICP text goes with it. Nothing else: no tenant identifiers beyond the rubric's own
contents, no other lead, no database contents.

**3. Anthropic does not train on API data by default.** This is the sentence every enterprise
review wants, and it belongs in the DPA with a citation rather than here as an assertion.

---

## What an EU-only customer would require

Take the parts in order of difficulty.

### The easy part: deploy in an EU region

`sam deploy --region eu-west-1` (or `eu-central-1`). Nothing in either template assumes a
region. RDS, SQS, Lambda, Secrets Manager, KMS and CloudWatch are all available in every EU
region. Set the SES region to match, and verify the sending identity there. This is a
parameter change and an hour of somebody's time.

There is no per-tenant residency: one deployment is one region for every tenant in it. An
EU-only customer alongside a US customer means **two deployments**, which is two stacks, two
databases and two of every operational procedure. That is a real cost and it should be priced
as one.

### The hard part: the model call

The only way to keep the assessment inside the EU is to stop calling the Anthropic
first-party API and call a Claude model hosted in an EU region instead — **Amazon Bedrock**
in an EU region, or **Google Vertex AI** in one.

Issue #11's single-adapter design is what keeps this cheap. `CLAUDE.md` says `anthropic` is
imported in `adapters/llm_anthropic.py` and nowhere else, and
`tests/unit/test_layering.py` enforces it. Everything above that file sees
`LeadAssessorPort` — one method, `assess(config, rendered_lead) -> AssessmentOutcome` — so
the prompt, the rubric, the scoring, the routing, the storage, the evals and the CLI are all
unaware of which provider answered.

**What would have to change:**

1. **One new file**, `adapters/llm_bedrock.py`, implementing the same port. The Anthropic SDK
   ships `AnthropicBedrock` and `AnthropicVertex` clients with the same `messages.create`
   surface, so most of the adapter is the existing one with a different constructor.
2. **One credential**, swapped: an AWS role with `bedrock:InvokeModel` in place of
   `ANTHROPIC_API_KEY_SECRET_ARN`. On Bedrock this is IAM, so it is *fewer* secrets, not more.
3. **One wiring line**, where the worker builds its assessor.
4. **The price table.** `CLAUDE_OPUS_5_PRICES` is a documented snapshot of the first-party
   rate card; Bedrock and Vertex bill differently, so `docs/metering-and-billing.md` and the
   reconciliation tolerance would need re-checking against a real invoice.
5. **Model availability in the chosen region.** Which Claude models are served from which
   Bedrock region changes, and it is the thing to check first rather than last. **Verify it
   before promising a region to a customer.**

**What it would *not* solve**, and say this before a customer discovers it:

- **Prompt caching behaves differently**, and the cacheable-prefix strategy the cost model
  depends on may not carry across unchanged. The first symptom is the bill.
- **The eval numbers are not transferable.** The golden-set results in
  [`docs/rubric-tuning.md`](rubric-tuning.md) were measured against the first-party API.
  Serving through a different platform is a different deployment of the model, and the sweep
  has to be re-run before quoting accuracy to anybody.
- **It does not make the deployment EU-only by itself.** If the database is in `us-east-1`
  and the model call goes to Bedrock in `eu-west-1`, the lead data is still in the US. The
  two decisions are independent and both have to be made.
- **It does not remove Anthropic from the sub-processor list** in any obvious way. Whether
  Claude-on-Bedrock makes AWS the only sub-processor for that hop, or Anthropic remains one,
  is a contractual question about the Bedrock terms. **A lawyer must answer it before the DPA
  is changed.**
- **Stripe is unaffected and is a separate transfer.** Stripe is a US company with EU entities
  and its own transfer mechanism; billing data goes there wherever we deploy. It never
  receives lead data.

### The part nobody asks about until late: support access

Data residency is about where data is stored *and* who can reach it. If our engineers are
outside the EU and can open the staff admin or connect to the database, that is access from a
third country regardless of where the bytes sit. A customer with a strict residency
requirement will eventually ask, and the answer is a matter of fact about the team rather
than about the code.

> **The owner must record:** where the people with production access are located.

---

## The short version, for a sales conversation

> Today: one AWS region, chosen at deploy time, holding everything. Lead free text is sent to
> Anthropic's API in the United States for assessment, under Standard Contractual Clauses,
> and Anthropic does not train on it. Billing data goes to Stripe; lead data never does.
>
> An EU-only deployment is available: the infrastructure is region-agnostic, and the model
> call moves to Claude on Amazon Bedrock in an EU region — one new adapter file and one
> credential, because the system talks to exactly one interface for model calls. It is a
> separate deployment per residency requirement, and the accuracy numbers have to be
> re-measured on the new platform before we quote them.

Everything after the first sentence of that is a commitment somebody has to keep, so do not
quote it without the owner and a lawyer having read this file.
