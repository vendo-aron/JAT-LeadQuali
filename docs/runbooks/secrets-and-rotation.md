# Secrets, KMS and rotation

Every secret this system uses, where it comes from, who can decrypt it, and what to do
when one has to change. Written for the person doing it at 3am, so each procedure is a
list of commands with the consequence of each stated.

`<stage>` is `dev`, `staging` or `prod` throughout.

## The inventory

| Secret | Origin | Encrypted with | Rotation |
|---|---|---|---|
| Database credentials | **RDS creates** (`ManageMasterUserPassword`) | `aws/secretsmanager` (RDS's choice) | **Automatic**, on RDS's managed schedule |
| `leadquali/<stage>/feedback-token` | **CloudFormation generates** (`GenerateSecretString`) | `alias/leadquali-<stage>-secrets` | Manual, and disruptive — see below |
| `leadquali/<stage>/ingest-credentials` | **CloudFormation creates the container, empty**; tenant onboarding writes the value | `alias/leadquali-<stage>-secrets` | Manual per tenant today; automated in #31 |
| Anthropic API key | **A human creates it**, per #43's runbook | `alias/leadquali-<stage>-secrets` (do this) | Manual only — Anthropic has no rotation API |

Three different origins, and the difference is deliberate:

- **Generated** is right when the value is opaque random material with exactly one holder.
  The feedback signing key is the only one of those, and CloudFormation generating it
  means no human ever sees it.
- **Created empty** is right when CloudFormation must *not* own the value. The ingest
  credential map is per-tenant and half of it is shared with a customer's website. If the
  template carried even a placeholder `SecretString`, CloudFormation would rewrite it on
  the next stack update that touched the resource — editing the description would wipe
  every customer's integration. With no `SecretString` property there is nothing to
  overwrite. Until onboarding writes the first value, the ingest function fails at cold
  start with the ARN in the message, which is correct for a system with no tenants.
- **Referenced** is right when the value comes from somewhere else entirely. Nothing in
  AWS can produce an Anthropic key.

The database is a fourth case: RDS generates *and* rotates the password and publishes the
ARN. The application never stores a database URL — `leadquali.config` assembles
`postgresql+psycopg://…?sslmode=require` from that secret plus the endpoint, port and
name from `infra/network.yaml`'s outputs. A second secret holding a full URL would be a
copy of the thing RDS is rotating, and it would go stale the first time it did.

## Who can decrypt

Two customer-managed keys, and they are separate on purpose.

`alias/leadquali-<stage>-db` (#27) encrypts the database and its snapshots.
`alias/leadquali-<stage>-secrets` (#28) encrypts the secrets above. The second key's
policy carries a statement the first one could never carry:

```yaml
Effect: Deny
Principal: "*"
Action: [kms:Decrypt, kms:Encrypt, kms:GenerateDataKey*, kms:ReEncrypt*]
Condition:
  StringNotEquals:
    kms:ViaService: secretsmanager.<region>.amazonaws.com
```

The key is usable *only* through Secrets Manager, by anybody, including an administrator.
The RDS key cannot have that, because the RDS service itself must use it to encrypt
volumes and snapshots. Sharing one key would also mean granting each Lambda role
`kms:Decrypt` on the key that protects the database backups — so a compromised function
could decrypt a copied snapshot, the whole lead table included, rather than one API key.
The second CMK costs $1/month.

**Who can decrypt, concretely:** any IAM principal in the account holding `kms:Decrypt` in
its own identity policy, and only through Secrets Manager. In this system that is exactly
the three functions in `infra/template.yaml`, via the `DecryptSecretsPolicy` managed
policy, and each of those can additionally only call `GetSecretValue` on the ARNs its own
policies name:

| Function | May read |
|---|---|
| `leadquali-<stage>-ingest` | ingest credentials, feedback token, database |
| `leadquali-<stage>-worker` | Anthropic key, feedback token, database |
| `leadquali-<stage>-migrate` | database |

The worker cannot read the ingest credentials — those are the customers' half of the
system and a compromised worker must not be able to sign as a customer's website. The
ingest function cannot read the Anthropic key: it never calls the model.
`tests/unit/test_infra_secrets.py` asserts this table in both directions, because a
one-directional check passes a template that over-grants.

Each secret also carries a resource policy denying every principal outside this account.
That is the one thing an identity policy cannot say, since the identity policy is written
by whoever holds the role rather than by whoever owns the secret.

## The cache, and what "rotated" means

Secrets are read at cold start and cached in the execution context for
`SECRETS_CACHE_TTL_SECONDS` — **300 seconds** by default
(`SecretsCacheTtlSeconds` in `infra/template.yaml`,
`DEFAULT_SECRETS_CACHE_TTL_SECONDS` in `leadquali/config.py`; the test suite asserts they
agree).

That number is the answer to two questions at once:

- **How long after a rotation does the fleet use the new value?** At most the TTL. This
  is also how long a leaked credential stays in use after somebody replaces it, which is
  the argument for keeping it small.
- **What does the cache cost?** A per-invocation fetch would put four HTTPS round trips —
  over the NAT gateway, from a private subnet — on the path of *every lead*, billed per
  call against an account-wide rate limit. At 300 s the whole fleet is bounded by the
  reserved concurrency in `infra/template.yaml`: at most 80 warm containers holding four
  secrets each, refreshed every five minutes, is roughly one call per second and a few
  dollars a month. Beside the $87/month RDS Proxy in the same stack, that is noise.

Five minutes sits where both answers are cheap. Raise it if the fleet grows by an order of
magnitude; lower it only for a specific incident, and remember that `0` means every cold
start pays four round trips.

If a refresh fails — throttling, a network blip — the resolver keeps serving the value it
already has and retries after 30 seconds rather than a full TTL. With *nothing* cached it
raises: there is no safe fallback, and a process that started with an empty ingest
credential map would be worse than one that did not start.

## Rotation procedures

### Database credentials — automatic

RDS owns this. It generates the master password into its own secret and rotates it on the
managed schedule; you can force one:

```bash
aws rds modify-db-instance \
  --db-instance-identifier <db-identifier> \
  --rotate-master-user-password --apply-immediately
```

**The one caveat, stated plainly.** The application resolves the password at cold start
and hands the assembled URL to SQLAlchemy, which captures it when it builds the engine.
A container that is already warm keeps the old password in its pool for the life of the
container — the secret cache TTL does not help, because nothing re-reads the URL. In
practice: RDS Proxy accepts both the current and previous secret values during the
rotation window, so most rotations are invisible; any connection that does fail surfaces
as an SQS redelivery (the lead is never dropped — see invariant 3) and the container is
replaced. If you have rotated manually and want it settled immediately, publish a new
version of the functions, which recycles every container:

```bash
aws lambda update-function-configuration \
  --function-name leadquali-<stage>-worker \
  --description "recycled after master password rotation $(date -u +%FT%TZ)"
```

### Anthropic API key — manual, zero downtime

It cannot be automated: the value only exists in the Anthropic console. Because both keys
are valid during the overlap, this is a clean cutover.

1. Create a **new** key in the Anthropic console. Do not revoke the old one yet.
2. Write it to the existing secret — a new version, not a new secret, so no template
   changes and no deploy:
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id leadquali/<stage>/anthropic-api-key \
     --secret-string "sk-ant-…"
   ```
3. Wait one TTL (five minutes) plus a margin. Every warm container is now on the new key.
4. Confirm: `leadquali-<stage>-worker` logs show no `llm.` errors, and a test lead is
   qualified end to end.
5. **Now** revoke the old key in the Anthropic console.

Reverse step 2 to roll back — Secrets Manager keeps the previous version at the
`AWSPREVIOUS` stage.

### Feedback token secret — manual, and it breaks live links

Rotating this invalidates **every feedback link already sent**, up to
`FEEDBACK_TOKEN_TTL_DAYS` (30 days) of routing emails, because a link is an HMAC over the
old key. Those links are the only source of the golden set, so this is not a routine
hygiene task. Rotate it when it is believed compromised, and not otherwise.

```bash
aws secretsmanager put-secret-value \
  --secret-id leadquali/<stage>/feedback-token \
  --secret-string "$(openssl rand -base64 48 | tr -d '/+=' | head -c 64)"
```

The new value must be at least 32 characters (`MIN_TOKEN_SECRET_CHARS`) or the worker
fails at cold start, and it must not equal any tenant's ingest signing secret —
`Settings.require_feedback_token_secret` refuses to start if it does. That is a real rule,
not a style note (#60): an ingest signing secret is *given to a customer's website*, while
this key authorises writes to the training data, so reusing one as the other would let any
customer mint feedback verdicts for every tenant.

Expect support contacts about dead links for a day or two; there is no way to avoid it.

### Per-tenant ingest credentials — manual today, #31 automates it

`tenants.hmac_secret_ref` holds the **ARN** of a secret, never a secret value (§4 of the
plan), so a per-tenant secret is read by the same resolver as everything else.

To roll one tenant's credentials without an outage, add the new entry alongside the old
one, let the customer switch, then remove the old:

1. Read the current map:
   **Superseded by #31.** Ingest credentials no longer live in
   `leadquali/<stage>/ingest-credentials`, and that secret is read by nothing. A tenant's
   API keys are rows in `tenant_api_keys` (only an argon2 hash of each is stored) and its
   HMAC signing secret has its own entry at `leadquali/<stage>/tenant/<slug>/hmac`.

   Rotate a key with a seven-day overlap, which breaks nothing:

   ```bash
   python -m leadquali.tenantctl rotate-key <slug> <key_id>
   ```

   Revoke one immediately — effective on the very next request, because nothing about the
   row is cached:

   ```bash
   python -m leadquali.tenantctl revoke-key <slug> <key_id>
   ```

   Rotating a tenant's **HMAC signing secret** is a different and *breaking* operation with
   no overlap, and must be coordinated with the customer. The full procedure, and why the
   two differ, is in `docs/tenant-onboarding.md`.

A raw API key is never written anywhere: only an argon2 hash of its secret half is stored,
and the key itself exists only in the customer's site configuration.

## Checks

```bash
# No secret value has ever been committed.
git log -p | grep -iE "sk-ant|aws_secret|password" | grep -v "ManageMasterUserPassword"

# No Lambda environment variable holds a value rather than an ARN.
aws lambda get-function-configuration --function-name leadquali-<stage>-worker \
  --query "Environment.Variables"

# What each function may read.
aws iam list-attached-role-policies --role-name <function-role>
```

The first two are also asserted offline, on every commit, by
`tests/unit/test_infra_secrets.py` and `tests/unit/test_infra_template.py`.
