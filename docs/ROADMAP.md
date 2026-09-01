# JAT-LeadQuali — Delivery roadmap

The tracking epic is [#1](https://github.com/vendo-aron/JAT-LeadQuali/issues/1). This file mirrors its
index so the completion order is readable from a checkout. The epic is the live copy; if the two
disagree, the epic wins.

Every issue below derives from [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

Legend: 🔧 manual setup (console/DNS/dashboard work, no code) · 🧠 business decision, not engineering.

## Phase 0 — Setup (~1 day)

| # | Issue | Depends on |
|---|---|---|
| 0.1 🧠 | [#2](https://github.com/vendo-aron/JAT-LeadQuali/issues/2) Answer the four open product questions | — |
| 0.2 🔧 | [#3](https://github.com/vendo-aron/JAT-LeadQuali/issues/3) Provision Anthropic API access, billing, workspace keys | — |
| 0.3 | [#4](https://github.com/vendo-aron/JAT-LeadQuali/issues/4) Scaffold the repository (pyproject, src layout, tooling) | — |
| 0.4 | [#5](https://github.com/vendo-aron/JAT-LeadQuali/issues/5) CI: ruff, mypy, pytest on every push | #4 |
| 0.5 🔧 | [#6](https://github.com/vendo-aron/JAT-LeadQuali/issues/6) Visual Studio 2026 setup (F5 debug + Test Explorer) | #3, #4, #5 |

## Phase 1 — Core qualification (3–4 days)

| # | Issue | Depends on |
|---|---|---|
| 1.1 | [#7](https://github.com/vendo-aron/JAT-LeadQuali/issues/7) Domain models | #4 |
| 1.2 | [#8](https://github.com/vendo-aron/JAT-LeadQuali/issues/8) TenantConfig — the rubric as data, not code | #7 |
| 1.3 | [#9](https://github.com/vendo-aron/JAT-LeadQuali/issues/9) Deterministic scoring and routing | #7, #8 |
| 1.4 | [#10](https://github.com/vendo-aron/JAT-LeadQuali/issues/10) Rubric prompt v1 (cacheable stable prefix) | #2, #8 |
| 1.5 | [#11](https://github.com/vendo-aron/JAT-LeadQuali/issues/11) Anthropic adapter | #3, #7, #8, #10 |
| 1.6 | [#12](https://github.com/vendo-aron/JAT-LeadQuali/issues/12) Prompt-injection hardening | #10, #11 |
| 1.7 | [#13](https://github.com/vendo-aron/JAT-LeadQuali/issues/13) CLI: score a JSON lead file | #9, #11, #12 |

**Phase gate at #13** — review CLI output with the ICP owner before starting Phase 2.

## Phase 2 — Pipeline (4–5 days)

| # | Issue | Depends on |
|---|---|---|
| 2.1 | [#14](https://github.com/vendo-aron/JAT-LeadQuali/issues/14) Ports + qualify.py orchestration | #9, #11 |
| 2.2 | [#15](https://github.com/vendo-aron/JAT-LeadQuali/issues/15) Postgres schema, migrations, Docker Compose | #4 |
| 2.3 | [#16](https://github.com/vendo-aron/JAT-LeadQuali/issues/16) Postgres store adapter | #14, #15 |
| 2.4 | [#17](https://github.com/vendo-aron/JAT-LeadQuali/issues/17) FastAPI ingest endpoint | #14, #16 |
| 2.5 | [#18](https://github.com/vendo-aron/JAT-LeadQuali/issues/18) Email enrichment adapter | #14 |
| 2.6 | [#19](https://github.com/vendo-aron/JAT-LeadQuali/issues/19) SES routing email + feedback links | #14, #16, #20 |
| 2.7 🔧 | [#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20) Amazon SES setup (start on day one) | #2 |
| 2.8 | [#21](https://github.com/vendo-aron/JAT-LeadQuali/issues/21) Observability | #14, #16 |

## Phase 3 — Evals (3 days)

| # | Issue | Depends on |
|---|---|---|
| 3.1 🔧 | [#22](https://github.com/vendo-aron/JAT-LeadQuali/issues/22) Assemble the golden set (standing task) | #2, #13 |
| 3.2 | [#23](https://github.com/vendo-aron/JAT-LeadQuali/issues/23) Eval harness + four metrics | #13, #22 |
| 3.3 | [#24](https://github.com/vendo-aron/JAT-LeadQuali/issues/24) Effort sweep and rubric tuning | #22, #23 |

## Phase 4 — AWS (4–5 days)

| # | Issue | Depends on |
|---|---|---|
| 4.1 🔧 | [#25](https://github.com/vendo-aron/JAT-LeadQuali/issues/25) AWS account bootstrap (OIDC, budgets) | Phase 3 |
| 4.2 | [#26](https://github.com/vendo-aron/JAT-LeadQuali/issues/26) SAM template: API GW, 2 Lambdas, SQS + DLQ | #17, #21, #25 |
| 4.3 | [#27](https://github.com/vendo-aron/JAT-LeadQuali/issues/27) VPC, RDS, RDS Proxy, concurrency caps | #25, #26 |
| 4.4 | [#28](https://github.com/vendo-aron/JAT-LeadQuali/issues/28) Secrets Manager and KMS | #25, #26 |
| 4.5 | [#29](https://github.com/vendo-aron/JAT-LeadQuali/issues/29) CloudWatch alarms and dashboard | #21, #26, #27 |
| 4.6 | [#30](https://github.com/vendo-aron/JAT-LeadQuali/issues/30) Production cutover | #20, #26–#29 |

## Phase 5 — Multi-tenant (~2 weeks)

| # | Issue | Depends on |
|---|---|---|
| 5.1 | [#31](https://github.com/vendo-aron/JAT-LeadQuali/issues/31) Tenant management, API keys, HMAC secrets | #16, #28, #30 |
| 5.2 | [#32](https://github.com/vendo-aron/JAT-LeadQuali/issues/32) Tenant isolation test suite | #31 |
| 5.3 | [#33](https://github.com/vendo-aron/JAT-LeadQuali/issues/33) Usage metering and margin | #16, #31 |
| 5.4 🔧 | [#34](https://github.com/vendo-aron/JAT-LeadQuali/issues/34) Stripe account, products, prices, webhooks | #33 |
| 5.5 | [#35](https://github.com/vendo-aron/JAT-LeadQuali/issues/35) Stripe billing integration | #31, #33, #34 |
| 5.6 | [#36](https://github.com/vendo-aron/JAT-LeadQuali/issues/36) Admin view | #22, #31, #32 |
| 5.7 | [#37](https://github.com/vendo-aron/JAT-LeadQuali/issues/37) Retention, PII policy, DPA | #21, #27, #32 |

## Manual setup issues

External wait times — start each one before the phase that needs it.

- [#3](https://github.com/vendo-aron/JAT-LeadQuali/issues/3) Anthropic workspaces, keys, spend limits
- [#6](https://github.com/vendo-aron/JAT-LeadQuali/issues/6) Visual Studio 2026 project setup
- [#20](https://github.com/vendo-aron/JAT-LeadQuali/issues/20) Amazon SES — up to 24h AWS review
- [#22](https://github.com/vendo-aron/JAT-LeadQuali/issues/22) Golden set labeling — ongoing human work
- [#25](https://github.com/vendo-aron/JAT-LeadQuali/issues/25) AWS account bootstrap
- [#34](https://github.com/vendo-aron/JAT-LeadQuali/issues/34) Stripe — account verification can take days
