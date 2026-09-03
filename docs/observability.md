# Observability: log fields, metric names, and what to alarm on

Plan §8, issue #21. This is the reference #29 writes alarms against and #33 meters billing
from, so treat the names below as a published contract: renaming a metric orphans every
alarm that mentions it, and renaming a log field breaks every saved Logs Insights query.

Everything here is emitted by `leadquali.observability`. One function per event lives in
`observability/events.py`; nothing else in the codebase builds a log field dict by hand.

## Configuring it

`configure_logging()` — once per process, at the entry point. It is already called by
`leadquali.api.main` (at import, so a request cannot be served before logging exists) and
by `leadquali.cli` (to **stderr**, so `--json` on stdout stays parseable). #26's worker
handler must call it too.

- `LOG_LEVEL` sets the root level. Third-party loggers (`botocore`, `sqlalchemy.engine`,
  `anthropic`, `httpx`, …) are pinned no lower than `WARNING`, which at `DEBUG` is what
  keeps SQLAlchemy's echo of bound parameters — a lead's email address — out of the log.
- `ENV` picks the format: `local` gets one readable line per record, every deployed
  environment gets JSON. The fields are identical either way.
- It is **safe to call twice**. A Lambda container is reused across invocations, and a
  handler added per invocation would double every line and every metric derived from it, so
  the function converges on exactly one handler instead of accumulating. It also detaches
  the handler the Lambda runtime pre-installs, which would otherwise print every line a
  second time in its own format.

## The log field set

One JSON object per line. Nothing is nested except `exception`.

| Field | On | Meaning |
|---|---|---|
| `timestamp` | every record | ISO-8601 UTC, milliseconds, trailing `Z`. Sortable as a string. |
| `level` | every record | `DEBUG` … `CRITICAL`. |
| `logger` | every record | The emitting module, e.g. `leadquali.app.qualify`. |
| `message` | every record | Human text. Equals `event` for structured events. |
| `service` | every record | `leadquali`. |
| `env` | every record | `local` / `dev` / `staging` / `prod`. |
| `event` | our records | Stable dotted name (see below). Absent on third-party records. |
| `trace_id` | every record in a lead's scope | 32 hex chars. **The join key for a lead's whole journey.** |
| `tenant_id`, `submission_id` | in a lead's scope | Bound to the context, so they ride on records written by code that was never handed them. |
| `lead_id` | from the edge onwards | The `leads` row. |
| `exception` | error records | `{type, message, stack}`. Redacted; never carries frame locals. |

### Events and their own fields

| `event` | Emitted by | Fields beyond the core set |
|---|---|---|
| `lead.accepted` | `app.ingest` | `disposition` (queued/suppressed/duplicate), `source`, `is_new_lead`, `contact_email_hash`, `spam_reason` |
| `assessment.completed` | `app.qualify` | `assessed`, `tier`, `action`, `total_score`, `confidence`, `escalation_reason`, `enrichment_available`, `model_id`, `prompt_version`, `effort`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `model_latency_ms` |
| `lead.routed` | `app.qualify` | `tier`, `action`, `destination_hash`, `used_fallback_destination`, `provider_message_id`, `latency_ms` |
| `lead.suppressed` | `app.qualify` | `tier`, `suppression_cause` (`spam` / `below_threshold`), `total_score`, `latency_ms` |
| `lead.duplicate` | `app.qualify` | `latency_ms` |
| `lead.dispatch_failed` | `app.qualify` | `tier`, `destination_hash`, `error_type`, `exception` |
| `http.ingest` | `api.main` | `disposition`, `status`, `latency_ms` |
| `ingest.rejected` | `api.main` | `reason`, `claimed_tenant`, `client`, `status` |
| `ingest.rate_limited` | `api.main` | `retry_after_seconds` |

### PII

Invariant 5, and it is enforced by a test rather than by this paragraph
(`tests/unit/test_observability_pipeline.py`): a lead with a distinctive address and a
distinctive message goes through the real pipeline with logging captured, and neither
string appears in any record — on the happy path **and** on the exception path, where a
traceback that formats a `LeadSubmission` would otherwise leak the lot.

- Addresses appear only as `contact_email_hash` / `destination_hash` — SHA-256 of the
  normalised address, the same value in `leads.contact_email_hash`, so a log line joins to
  a row.
- `LeadSubmission` declares every field `repr=False`, so no traceback can render one.
- Both formatters redact address-shaped runs from messages and tracebacks as a last resort.
  That net catches what arrives from outside (a bounce quoted by SES, a library echoing its
  input); it cannot recognise a lead's prose, so it is not a substitute for discipline.

## Metrics: CloudWatch EMF

Namespace **`LeadQuali`**. Metrics ride on the log line as an Embedded Metric Format
document, so there is no `PutMetricData` call in the path of a customer's lead: no added
latency, no extra IAM, no per-call cost, and nothing that can fail and silently stop
feeding an alarm. Off AWS the same line is ordinary JSON and the numbers stay queryable.

| Metric | Unit | Dimension sets | Emitted on |
|---|---|---|---|
| `Assessments` | Count | `[TenantId, Tier]`, `[TenantId]` | `assessment.completed` |
| `AssessmentFailures` | Count | `[TenantId]` | `assessment.completed` (1 when the model returned nothing) |
| `Escalations` | Count | `[TenantId, EscalationReason]` | `assessment.completed` when a reason exists |
| `InputTokens`, `OutputTokens`, `CacheReadTokens`, `CacheCreationTokens` | Count | `[TenantId]` | `assessment.completed` when the call was billed |
| `CostUsd` | None | `[TenantId]` | `assessment.completed` when the call was billed |
| `ModelLatencyMs` | Milliseconds | `[TenantId]` | `assessment.completed` when the call was billed |
| `EnrichmentUnavailable` | Count | `[TenantId]` | `assessment.completed` |
| `Dispatches` | Count | `[TenantId, Tier]`, `[TenantId]` | `lead.routed` |
| `FallbackDestinations` | Count | `[TenantId]` | `lead.routed` |
| `PipelineLatencyMs` | Milliseconds | `[TenantId]` | `lead.routed`, `lead.suppressed`, `lead.duplicate` |
| `Suppressions` | Count | `[TenantId, SuppressionCause]` | `lead.suppressed` |
| `Duplicates` | Count | `[TenantId]` | `lead.duplicate` |
| `DispatchFailures` | Count | `[TenantId]` | `lead.dispatch_failed` |
| `IngestedLeads` | Count | `[TenantId, Disposition]` | `lead.accepted` |
| `IngestSuppressions` | Count | `[TenantId, SpamReason]` | `lead.accepted` when a pre-filter fired |

Dimension values are all bounded — `Tier` has four, `EscalationReason` five,
`SuppressionCause` two, `Disposition` three — which keeps this at roughly twenty custom
metrics per tenant. `lead_id` and `trace_id` are deliberately **fields, not dimensions**:
free to query in Logs Insights, and unbounded as a dimension.

## What #29 should alarm on

Ordered by how likely the page is to be real.

1. **DLQ depth > 0** (SQS metric, #26's queue). The backstop; everything below is an
   earlier warning of it.
2. **`DispatchFailures` sum > 0 over 5 minutes**, per tenant. A lead that could not be
   delivered is still on the queue; this fires before the DLQ does.
3. **`AssessmentFailures` / `Assessments` > 0.05 over 15 minutes.** Break down by
   `EscalationReason` via `Escalations` to route it: `api_error` / `timeout` is
   operational, `parse_error` / `model_refusal` is a prompt problem.
4. **p99 `PipelineLatencyMs`**, with p99 `ModelLatencyMs` on the same dashboard. The gap
   between them says whether it is Anthropic or us.
5. **`CostUsd` sum per day** above roughly 2× the plan's $25/1,000-leads baseline, and
   **`CacheReadTokens` sum per day dropping to ~0**, which is the only visible symptom of a
   broken cache prefix and shows up as a cost rise first.
6. **Tier-distribution drift** — the one plan §8 singles out:

   > alarm on `Assessments` with dimensions `[TenantId, Tier="hot"]` as a **ratio** of
   > `Assessments` with dimensions `[TenantId]`, using a CloudWatch metric math expression
   > (`m_hot / m_all`), evaluated hourly per tenant, against a band derived from the
   > previous 7 days (anomaly detection, or a static band once a baseline exists).

   `Assessments` is emitted for **every decision, before the suppress/dispatch branch**,
   which is what makes this a distribution over decisions rather than over the leads that
   happened to be emailed — a distribution over dispatches would move whenever a tenant
   edited its routing table, and that false positive is how a drift alarm gets switched
   off. A sudden jump in `hot` is a prompt change or an upstream form change far more often
   than it is a good sales week.
7. **`EnrichmentUnavailable` sustained above ~10% over an hour** (#58). Not an outage —
   every lead was still assessed — but every lead is being judged on less than it should
   be, and nothing else in the product looks different.
8. **`Suppressions` by `SuppressionCause`**, per tenant, daily. A rise in `spam` is a bot
   campaign; a rise in `below_threshold` is our rubric rejecting real leads. #52 separated
   the two notes so that these could be different alarms with different owners.

## Useful queries

Cost per lead, without opening the database:

```
fields cost_usd
| filter event = "assessment.completed" and tenant_id = "acme"
| stats sum(cost_usd) as spend, count(*) as leads, spend / leads as cost_per_lead by bin(1d)
```

One lead's whole journey:

```
fields @timestamp, event, tier, message
| filter trace_id = "0d2288cddb204fa990b5e15f73fc2d2c"
| sort @timestamp asc
```

Tier distribution, per tenant per hour (the drift signal, as a query rather than an alarm):

```
fields tier
| filter event = "assessment.completed"
| stats count(*) by tenant_id, tier, bin(1h)
```
