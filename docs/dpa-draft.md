# Data Processing Agreement — DRAFT

> ## ⚠️ DRAFT FOR LEGAL REVIEW. NOT LEGAL ADVICE. DO NOT SIGN OR SEND.
>
> This was written by the engineering team to describe accurately **what the system does**.
> It is not a contract and it has not been reviewed by anybody qualified to write one.
>
> Every clause below is either (a) a factual description of the software, which is accurate
> and testable, or (b) a legal position, which is a placeholder. The legal positions are
> marked **[LEGAL]** throughout and collected in
> [§12](#12-what-a-lawyer-must-decide-before-this-is-used). There are **eleven** of them.
>
> **It must be signed off by whoever signs contracts for the company before it is sent to a
> prospect**, including in a data room, including "just for reference". A draft that reaches
> a customer's legal team is read as a position.

Issue #37.

---

## 1. Parties and roles

**[LEGAL]** The Customer is the **controller** of the personal data of individuals who submit
their web form. LeadQuali is a **processor**, acting only on the Customer's documented
instructions.

This is the right analysis for the ordinary case and it is the one the product is built
around: the customer chooses what their form collects, what their rubric is, and where leads
are routed; we qualify and route them and do nothing else with them. A lawyer should confirm
it holds for every case, and in particular for the eval and rubric-tuning work described in
§7, where we use the customer's leads to improve a shared product — that is the clause most
likely to be wrong as written.

The **Customer's own billing contact** is a different matter: for that data we are a
controller in our own right, not a processor. See §8.

---

## 2. Subject matter, duration, nature and purpose

**Subject matter.** Qualification and routing of inbound sales enquiries submitted through
the Customer's web form.

**Duration.** For the term of the service agreement, plus the retention windows in §5.

**Nature and purpose.** Receiving a form submission; sending its free text to a large language
model to be scored against the Customer's own criteria; computing a tier and a routing action
from that score; notifying the Customer's staff; and retaining the record so the Customer can
measure and tune the result.

**Categories of data subject.** Individuals who submit the Customer's web form. In practice:
prospective business customers, and whoever else fills the form in.

**Categories of personal data.** Whatever the Customer's form collects. In the shipped shape:
name, email address, telephone number, employer, job title, website, and free text the person
types. Plus data derived from it: a score, a tier, and the model's written assessment.

**Special categories.** None are requested and none are needed. The Customer controls what
their form asks for; a form asking for special-category data is outside what this service is
designed for and the Customer must not use it that way.

**[LEGAL]** Whether that last sentence is an adequate contractual exclusion, or whether we
need a positive warranty from the Customer, is for a lawyer.

---

## 3. Sub-processors

Three, and the list is complete. Each is named with what it processes and where.

### AWS (Amazon Web Services)

**Processes:** everything. All lead data at rest and in transit — the database, the queue,
the compute, the logs, the routing emails sent through SES.

**Where:** one AWS region, chosen at deployment. **The region must be named here before this
document is sent.** See [`docs/data-residency.md`](data-residency.md), which also covers what
an EU-only deployment would require.

**Safeguards:** the data is encrypted at rest with customer-managed KMS keys and in transit
with TLS on every hop; the database has no public address; each function can read only the
secrets it names. [`docs/security-overview.md`](security-overview.md) is the detail, and
every claim in it has a test behind it.

**[LEGAL]** AWS's own DPA and its transfer terms must be referenced properly here rather than
described.

### Anthropic

**Processes:** the lead's **free text** — the rendered submission (name, company, role,
website, message) — sent to the Claude API to be assessed, together with the Customer's own
ICP description. One call per lead. Nothing is stored by us on their side and no other data
of ours is sent: no database contents, no other lead, no customer identifiers beyond what the
rubric itself contains.

**Where:** the United States, via Anthropic's first-party API.

**Training.** **Anthropic does not train its models on data submitted through the API by
default.** This is stated in Anthropic's Commercial Terms of Service — see the "Customer
Content" / "Use of Customer Content" section, which provides that Anthropic will not train on
Inputs or Outputs from commercial API use — and in Anthropic's published privacy and trust
documentation.

> **Cite the term, do not paraphrase it.** Before this document goes out, somebody must
> (a) fetch the current Anthropic Commercial Terms of Service and Privacy Policy, (b) quote
> the clause and its section number, and (c) record the date the version in force was
> checked. Terms change; a bare assertion that "they don't train on it" is worth nothing in a
> security review and worse than nothing if it has gone stale. **This document deliberately
> does not quote a version number, because writing one from memory is exactly the failure it
> is warning about.**

**Retention at Anthropic.** **[LEGAL]** Anthropic's API retains inputs and outputs for a
limited period for trust-and-safety purposes, and enterprise arrangements including
zero-retention exist. The current default period and whether we are on it must be checked and
stated, not assumed.

**[LEGAL]** The transfer mechanism (Standard Contractual Clauses via Anthropic's DPA), and
whether the version in force covers our processing, must be confirmed.

### Stripe

**Processes:** the **Customer's** billing contact — name, email, company, address — and their
payment method. Stripe holds the card data; we never see or store a card number.

**Stripe never receives lead data.** No submission, no assessment, no lead identifier is sent
to Stripe, ever. This is worth stating explicitly because it is the question a reviewer asks
next, and because the separation is structural rather than a matter of care.

**Where:** Stripe's own infrastructure. **[LEGAL]** Stripe's regions, its DPA and its
transfer terms must be referenced properly.

[`docs/billing-integration.md`](billing-integration.md) is the authority on what Stripe
actually receives and is not restated here. Two facts from it bear on this document. First,
**Stripe never receives lead data** — that is structural, not a matter of care. Second, we
store the verified Stripe webhook bodies **verbatim** in `stripe_events.payload`, and an
invoice object carries the billing contact's name, email and postal address. That is a
second class of personal data, about a different subject, with the opposite retention
constraint from everything in §5 — see §8 and
[`docs/data-retention-policy.md`](data-retention-policy.md).

### Changes to the list

**[LEGAL]** The Customer is to be notified before a new sub-processor is added, with a period
in which to object. **The notice period and what an unresolved objection means** — a right to
terminate, usually — are commercial terms.

---

## 4. Security measures

Rather than a list of adjectives, this clause should reference
[`docs/security-overview.md`](security-overview.md), which states what is done **and what is
not**. The headline measures:

- encryption at rest with customer-managed KMS keys, with automatic rotation;
- TLS on every network hop, forced by the server rather than chosen by the client;
- no public address for the database; all compute inside a VPC;
- per-customer API keys, argon2id-hashed at rest, plus an HMAC signature on every request;
- **no personal data in logs**, enforced by a test that enumerates every log event the system
  emits and fails when a new one is not covered;
- shared-schema multi-tenancy with a structural guarantee — the schema makes a cross-tenant
  row unstorable, not merely unselected — documented and tested in
  [`docs/tenant-isolation.md`](tenant-isolation.md).

**[LEGAL]** Whether to attach the security overview as a schedule (so it is contractually
binding and therefore has to be maintained as such) or to reference it as a policy we may
update is a real choice with real consequences. The engineering preference is to attach a
dated snapshot, because a document that can be quietly weakened is not an assurance.

---

## 5. Retention and deletion

[`docs/data-retention-policy.md`](data-retention-policy.md) is the full statement.

- **Raw lead payloads: 90 days** by default, then replaced by a tombstone. Configurable per
  Customer.
- **Assessment records: 24 months** by default, then deleted with everything attached to
  them. Configurable per Customer.
- **Aggregate usage figures** (counts and costs per day, containing nothing about any
  individual) are retained indefinitely.
- A **daily automated job** enforces both windows. Deletion is permanent and is not reversible
  from the application.
- Personal data is removed from **automated backups** within a further 7 days.

**Deletion requests** are handled per [`docs/deletion-requests.md`](deletion-requests.md),
which includes the evidence produced: a receipt carrying a SHA-256 of the address and never
the address itself, plus a durable audit row.

**[LEGAL]** The **response time we contractually commit to** for a Customer's deletion
instruction. Our operational target is five working days; a contract usually says something
different and usually says it about the controller's own one-month obligation.

**[LEGAL]** What happens **on termination**: the period within which we delete or return
everything, and in what format "return" means. The engineering answer is that an export of a
Customer's rows in JSON is straightforward and has not been built.

---

## 6. Data subject rights

We assist the Customer in responding to requests, as a processor must. In practice:

| Right | What we can do today |
|---|---|
| Access | The staff admin shows a lead's stored payload and assessment. There is no self-service export; a request is answered by hand. |
| Rectification | Not supported. A lead is a record of *what was submitted*; editing it would falsify the record. **[LEGAL]** — whether that is a defensible position. |
| Erasure | `retentionctl erase`, with a receipt and an audit row. See the runbook. |
| Restriction | Not supported as a distinct state. In practice we would erase instead. **[LEGAL]** |
| Portability | Not built. The data is small and JSON; an export is a day's work when somebody asks. |
| Objection | A matter between the individual and the Customer, who is the controller. |

**[LEGAL]** Rectification and restriction are the two gaps, and a reviewer who reads Article
16 and Article 18 will find them. Whether the position above is adequate, and whether the
gaps need building before signing anything, is the decision.

---

## 7. Use of Customer data to improve the service

This clause has to exist and it has to be honest, because the product genuinely does this and
a DPA that pretended otherwise would be false.

**What happens.** The Customer's staff mark assessments good or bad through a one-click link
in the routing email. Those verdicts, together with the lead they are about, are used to tune
the qualification rubric and to measure whether changes improve it. A small number of real
leads may be **promoted into an evaluation set** that is used to test changes to the shared
product.

**What protects the individual.** A promoted lead is **rewritten, not copied**: identifying
fields are replaced with deterministic pseudonyms, addresses and phone numbers are stripped
from the free text, and a person reads the result before it is committed. The rewrite is
checked by the same validator that fails the build if a real address ever reaches the
evaluation file. See [`docs/labeling-golden-set.md`](labeling-golden-set.md).

**What does not happen.** One Customer's leads are never used to score another Customer's
leads. There is no shared model, no fine-tuning, and no cross-customer training of any kind —
the rubric is per-customer configuration, not a learned artifact.

**[LEGAL]** This is a use of the controller's data for our own product development, which is
processing beyond the Customer's immediate instruction. It needs a clause the Customer agrees
to, and a lawyer should decide whether the pseudonymisation above is sufficient or whether
promotion needs the Customer's explicit opt-in. **The engineering recommendation is an
opt-in**, because a customer who finds out about it later will be angrier than one who was
asked.

---

## 8. The Customer's own data

Separate from everything above, and often forgotten in a processor DPA.

We hold the Customer's account contact — name and email — as a **controller** in our own
right, for the purposes of running the commercial relationship. We also hold the billing
contact's name, email and postal address inside the verified Stripe webhook bodies stored
verbatim in `stripe_events.payload`, and Stripe holds their payment details as our
processor.

`stripe_events.payload` is **retained indefinitely today**, and nothing deletes it: no
retention job touches it, and the tenant reference is `ON DELETE SET NULL` so that closing an
account does not destroy the billing history. That is the current behaviour and it is stated
here so that nobody has to infer it.

**[LEGAL]** This is the clause that needs the most care, and it is not really a DPA clause at
all:

- Invoice and payment records are **financial records**, and in most jurisdictions there is a
  **statutory minimum retention period** measured in years. That is the opposite shape of
  obligation from §5's maximums, and it normally **overrides a deletion request** for that
  data.
- A lawyer must state the **jurisdiction we invoice from, the statutory period, and the
  lawful basis** for keeping the records against a request. Until they do, a deletion request
  from a billing contact must not be actioned — see the corresponding section of
  [`docs/deletion-requests.md`](deletion-requests.md).
- A minimum is not a licence. Once the period is settled, somebody has to decide whether we
  **delete these rows when it elapses** or keep them for ever. "Indefinitely" is what the
  code does today because no period has been set, not because it was chosen.
- This should probably be a **privacy notice** rather than a clause in a processor DPA, and
  the two documents should not contradict each other.

---

## 9. Breach notification

**[LEGAL]** The whole clause. What we undertake to tell the Customer, how quickly, and with
what content.

For reference: GDPR Article 33(2) requires a processor to notify the controller **without
undue delay** after becoming aware of a breach, so that the controller can meet their own
72-hour obligation. Contracts commonly specify a number of hours. That number is a commitment
somebody has to be able to keep at 3am on a Saturday, so the engineering ask is that it be set
by someone who knows what our on-call arrangement actually is — see
[`docs/security-overview.md`](security-overview.md) §11, which currently says the owner must
fill it in.

---

## 10. Audit and inspection

**[LEGAL]** Article 28(3)(h) gives the controller a right to audit. What we offer in practice
— documentation and answers, versus an on-site audit, versus a third-party report we do not
have — is a commercial decision with a cost attached.

What exists today and can be offered without new work: this document,
[`docs/security-overview.md`](security-overview.md),
[`docs/tenant-isolation.md`](tenant-isolation.md) with its test results and its negative
control, and the retention and deletion runbooks.

What does not exist: SOC 2, ISO 27001, a penetration test report, or any third-party
attestation. **Do not imply otherwise.** A prospect who asks for a SOC 2 report and is given
a document that talks around it will conclude there is one.

---

## 11. International transfers

See [`docs/data-residency.md`](data-residency.md) for where data actually is and what an
EU-only deployment would require.

**[LEGAL]** The transfer mechanism for each sub-processor, and the transfer impact assessment
if one is needed. Anthropic is the transfer that will be scrutinised, because it is the one
where a lead's own words leave the region.

---

## 12. What a lawyer must decide before this is used

Collected, in the order they appear. Nothing here is an engineering question.

1. **§1** — the controller/processor analysis, and whether it survives §7's use of customer
   data for product improvement.
2. **§2** — whether excluding special-category data by description is sufficient, or whether a
   Customer warranty is needed.
3. **§3** — each sub-processor's own DPA and transfer terms, referenced properly; and the
   Anthropic terms *verified against the current published version*, with the section number
   and the date checked, rather than asserted.
4. **§3** — Anthropic's API retention period for inputs and outputs, and whether we are on the
   default.
5. **§3** — the sub-processor change-notice period and what an unresolved objection means.
6. **§4** — whether the security overview is an attached schedule or a referenced policy.
7. **§5** — the contractual response time for a deletion instruction, and the
   return-or-delete obligation on termination.
8. **§6** — whether the absence of rectification and restriction is defensible.
9. **§7** — whether promotion of a lead into the evaluation set needs the Customer's explicit
   opt-in.
10. **§8** — the statutory retention period for `stripe_events.payload` and our other invoice
    records, the jurisdiction, and whether we delete them once it elapses. **This is the one
    an engineer must not answer**, and it is why that column is the one piece of personal
    data in the schema that no retention job touches.
11. **§8** — whether a **billing contact's erasure request reaches `stripe_events.payload` at
    all**, given that the same rows are the evidence of a transaction. The interim operating
    answer in [`docs/deletion-requests.md`](deletion-requests.md) is "no, and escalate";
    that needs confirming, not assuming.
12. **§9** — the breach-notification commitment.

Plus two facts that are not legal questions but must be filled in before this document
leaves the building: the **AWS deployment region** (§3) and the **security contact**
([`docs/security-overview.md`](security-overview.md) §11).
