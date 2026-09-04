# What the Postgres decision costs per month

Plan §4 chose Postgres over DynamoDB and called the VPC an **accepted cost**. Plan §8
prices the model at roughly **$25/month at 1,000 leads/month**. This page is the other
number, so the two are visible next to each other rather than one being in the plan and
the other being on an invoice.

All figures are us-east-1 list price, 730 hours, the default parameters in
`infra/network.yaml`. They are estimates from published rates, not measurements — no AWS
account exists in this repository, so nothing here has been observed on a bill.

## The bill, with the defaults as written

| Line | Rate | Monthly |
|---|---|---|
| NAT gateway | $0.045/h | $32.85 |
| NAT data processing | $0.045/GB × ~0.05 GB | $0.01 |
| Public IPv4 address (the NAT's EIP) | $0.005/h | $3.65 |
| RDS `db.t4g.micro`, single-AZ | $0.016/h | $11.68 |
| RDS gp3 storage, 20 GiB | $0.115/GB | $2.30 |
| RDS automated backups, 7 days | free up to allocated storage | $0.00 |
| KMS customer-managed key | $1/key | $1.00 |
| KMS requests | $0.03/10k | ~$0.01 |
| CloudWatch Logs (`postgresql` export) | $0.50/GB ingest | ~$0.50 |
| **RDS Proxy** | $0.015/vCPU-h, **8 vCPU floor for T-family** | **$87.60** |
| | | **≈ $139.60** |

At 1,000 leads/month that is **$0.140 per lead of infrastructure** against **$0.025 per
lead of tokens**. The networking is not a rounding error next to the model; it is the
larger half of the bill by a factor of five.

## The line worth arguing about

**RDS Proxy is $87.60/month in front of a database that costs $11.68/month.** The price is
not set by our volume: RDS Proxy bills per vCPU-hour of the underlying instance with a
floor of eight vCPUs for T-family instances, and `db.t4g.micro` has two. We pay for eight.

What it buys at today's volume, stated honestly, is **not** connection headroom. With
`pool_size=1` and a reserved concurrency of 5, the worker holds 5 of 112 connections; the
database would survive without a proxy for a long time. What it buys is connection
establishment taken off the Lambda cold-start path, transparent failover, and the ability
to raise reserved concurrency later without reopening the connection question.

#27 mandates the proxy, so `EnableRdsProxy` defaults to `true`. It is a parameter rather
than a hardcoded resource so that the owner can see this number and decide. Turning it off
drops the stack to **≈ $52.00/month** and points the application at the instance endpoint;
the connection arithmetic holds either way, because it is bounded by reserved concurrency
rather than by the proxy.

## The egress decision, priced

The worker must reach api.anthropic.com, SES, Secrets Manager, CloudWatch Logs and X-Ray.
#27 states a preference for interface VPC endpoints over a NAT gateway. That preference
does not survive the first item on the list: Anthropic is a third-party public API with no
PrivateLink endpoint and no fixed prefix list, so *some* route to the public internet is
required regardless. Endpoints are therefore an addition to the NAT, not a substitute.

| Option | Monthly |
|---|---|
| **A — one NAT gateway, no endpoints (chosen)** | **$36.51** |
| B — interface endpoints for the four AWS services (4 × 2 AZs × $0.01/h = $58.40) plus the NAT that Anthropic still requires | $94.91 |

Option B costs 2.6× Option A and buys a few milliseconds off the Secrets Manager call on a
cold start. It would win if AWS-service traffic were large enough for endpoint data rates
($0.01/GB) to beat NAT processing ($0.045/GB); the crossover is near 1.7 TB/month and this
workload moves about 50 MB.

**One NAT, not one per AZ.** A second costs another $36.51/month and removes exactly one
failure: an outage of the NAT's own AZ. What that failure does here is stop egress for both
private subnets, so Claude calls fail, SQS redelivers, and leads are qualified when egress
returns. Nothing is dropped — invariant 3 is held by the queue, not by the NAT. Converting
a delay into no delay is not worth $438/year at this volume.

## When to revisit

- **Volume crosses plan §12's thresholds.** Under ~50 leads/day, RDS Proxy and possibly
  SQS are deferrable. Over ~5,000/day the proxy's fixed price stops being the headline and
  the instance class becomes the question.
- **`db.t4g.micro` stops being enough.** Moving to `db.t4g.small` doubles the instance to
  $23.36 and, because the proxy's floor is already above the instance, does not change the
  proxy line at all. It also raises `max_connections` to 225, which the concurrency
  arithmetic in `infra/network.yaml` reads from a mapping rather than a memory.
- **Multi-AZ.** `DbMultiAz` defaults to `false`; setting it doubles the instance line.
- **Aurora Serverless v2**, allowed by plan §4, was priced and rejected: at $0.12/ACU-hour
  even a 0.5-ACU floor is $43.80/month before storage and I/O, and a worker that runs
  every few minutes keeps the cluster from reaching the 0-ACU auto-pause that would make
  it cheap. It costs roughly 4× the instance it would replace.
