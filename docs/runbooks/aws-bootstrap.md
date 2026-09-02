# Runbook — AWS account bootstrap

**Issue:** [#25](https://github.com/vendo-aron/JAT-LeadQuali/issues/25) · **Phase 4** ·
**Blocks:** #26, #27, #28, #30 · **Owner:** aron@vendoworks.com

> **This runbook is the deliverable. It has not been executed.**
> No AWS account, credentials, or console access exist in the environment this document was
> written in. Every command below is written to be run by the account owner on a machine with
> real AWS credentials. Nothing here has been verified against a live account; the verification
> steps exist precisely so that the person executing it proves each phase rather than trusting
> this page.

Work through the phases in order. Each ends with a **Verify** block — do not start the next phase
until it passes. Phases 1 and 2 are decisions that get recorded; phases 3–10 are execution.

---

## 0. Prerequisites

On the workstation you will execute from:

| Tool | Minimum | Check |
|---|---|---|
| AWS CLI | v2.15+ | `aws --version` |
| AWS SAM CLI | v1.100+ | `sam --version` |
| GitHub CLI | v2.40+ | `gh --version` |
| `jq` | any | `jq --version` |

You also need:

- Access to the DNS zone for the sending domain (needed by #20, and confirmed here).
- A password manager, and a virtual or hardware MFA device.
- An email alias you control that is **not a personal mailbox** — see §4.

Throughout, these placeholders appear. Substitute your real values:

```bash
export REGION=eu-west-1                       # see §2
export ORG_EMAIL_BASE=aws@vendoworks.com      # a distribution alias, see §4
export GH_REPO=vendo-aron/JAT-LeadQuali
export DEFAULT_BRANCH=claude/jat-leadquali-agent-plan-6y2xh1   # see §6.3 — this WILL change
```

---

## 1. Decision: account layout

### The recommendation

**Two AWS accounts — `jat-leadquali-dev` and `jat-leadquali-prod` — inside a new AWS
Organization, with the management account holding no workload resources.** Do this now, not
later.

This is a firm recommendation, not a menu. The alternative (one account, two CloudFormation
stacks separated by a `Environment` tag) is genuinely cheaper in setup time and it is what most
solo projects do. It is still the wrong call here, for three reasons specific to this project:

1. **The blast radius is a database of other people's personal data.** Plan §8 classifies leads
   as personal data requiring KMS encryption, a retention policy and a DPA. In a single account,
   a `sam deploy` pointed at the wrong `samconfig.toml` profile, or an IAM policy scoped by tag
   where the tag was forgotten, can drop the prod RDS instance. A tag boundary is a convention;
   an account boundary is enforced by AWS.
2. **The commercial goal is to resell this** (plan §1). The first enterprise security
   questionnaire asks how dev and prod are isolated. "Separate AWS accounts" ends that thread in
   one line. "Separate stacks in one account with tagging" starts a negotiation.
3. **SES sandbox becomes a safety feature.** SES production access is granted per account, per
   region. If you only request it for prod, the dev account stays in the SES sandbox permanently,
   which means **a bug in dev physically cannot email a real prospect** — it can only reach
   addresses you explicitly verified. In a single account, dev and prod share one SES identity
   with production sending rights, and the only thing between a test run and a stranger's inbox
   is application code.

### The honest cost, at this project's size

- **Setup:** roughly 60–90 minutes once. AWS Organizations itself is free.
- **Recurring:** ~$0 extra for the boundary. Two accounts do not cost more than one — you pay for
  resources, and consolidated billing rolls both up to one invoice and one set of free-tier
  allowances (note: free tier is *shared* across the org, not doubled). The dev environment's
  ~$75/month floor (§10) is a cost of having a dev environment at all, not of having a second
  account — and §10 tells you how to avoid paying it.
- **Friction:** two sets of credentials to switch between (solved once with IAM Identity Center
  and named CLI profiles), a second OIDC provider and deploy role, and a second SES domain
  verification in dev if you ever want dev to send at all.

### What it costs to change later

Going from one account to two after prod is live is a **live-traffic migration**, roughly a full
day with a cutover window:

- RDS snapshot → cross-account share → restore → catch up the delta, or a logical dump/restore
  with write downtime.
- A new SES domain identity plus a fresh **production access request** in the new account, which
  is a human review at AWS taking up to 24 hours and can be refused — you cannot schedule around
  it (#20).
- Every secret in Secrets Manager re-created (they cannot move) and therefore rotated.
- New API Gateway endpoint → DNS change → every embedded website form updated, on the customer's
  release schedule, not yours.
- New OIDC provider, deploy role and trust policy; the old ones revoked.

Going the other direction, from two accounts to one, is trivial but nobody ever does it. The
asymmetry is the whole argument: **do the 90 minutes now.**

### If you overrule this

If you decide on a single account anyway, the minimum bar is: two stacks named
`jat-leadquali-dev` and `jat-leadquali-prod`, a mandatory `Environment` tag on every resource,
**two separate OIDC deploy roles** whose IAM policies are scoped by `aws:ResourceTag/Environment`
and by stack-name ARN pattern, and a prod stack with termination protection enabled. Record the
decision and this compensating control set in §12 rather than leaving it implicit.

### Record it

Append to §12 of this file (the decision log) with today's date. Issue #25's acceptance criteria
require the layout to be recorded in `docs/`; §12 is that record.

---

## 2. Decision: region

### The recommendation

**`eu-west-1` (Ireland).** Every resource in this system — Lambda, API Gateway, SQS, RDS, RDS
Proxy, Secrets Manager, KMS, SES, CloudWatch, the SAM artifact bucket — goes in that one region.

Reasoning: leads are personal data belonging to identifiable EU-resident prospects under a
vendoworks.com domain, plan §8 commits to having a DPA ready, and keeping the data in-region
removes an international-transfer question from every future contract. `eu-west-1` is a
full-featured region — SES, RDS Proxy and every service in §3/§4 are available — and it is the
cheapest EU region for this workload.

**The one question that flips this:** if the sales team receiving the routing emails and the
prospects filling in the form are predominantly US-based, use **`us-east-1`** instead and change
every occurrence of `eu-west-1` below. Decide before phase 3, not after.

### The hard constraint

The region here **must equal the SES region chosen in #20**. Not "should" — SES identities,
DKIM verification, configuration sets and production sending limits are all per-region and none
of them are visible from another region. A worker Lambda in `eu-west-1` calling
`ses:SendEmail` against an identity verified only in `us-east-1` fails with
`MessageRejected: Email address is not verified`, and it fails at dispatch time — after the
Claude call has been paid for.

Cross-region also costs money for nothing: inter-region data transfer on every send, plus added
latency inside the worker's timeout budget.

If #20 has already been executed in a different region, either redo it in `$REGION` or change
`$REGION` to match #20. **Changing region later means re-requesting SES production access** —
another up-to-24-hour AWS human review (#20 step 7) — plus an RDS snapshot copy, new endpoints
and a DNS change. It is the same class of migration as §1's account move.

### Verify

```bash
# 1. Confirm every service this build needs exists in the chosen region.
for svc in lambda apigateway sqs rds secretsmanager kms email logs cloudformation s3; do
  printf '%-18s %s\n' "$svc" \
    "$(aws ssm get-parameters-by-path \
        --path /aws/service/global-infrastructure/regions/$REGION/services \
        --region us-east-1 --query "Parameters[?ends_with(Name,'/$svc')].Value" \
        --output text | grep -q . && echo AVAILABLE || echo MISSING)"
done

# 2. Once #20 is done, prove SES lives in the same region (must print your domain).
aws ses list-identities --region "$REGION"
```

`rds-proxy` is not a separate SSM entry; confirm it in the RDS console under
**Proxies → Create proxy** in `$REGION`.

---

## 3. Create the Organization and accounts

Skip 3.1–3.2 if you overruled §1 and are using a single account; go straight to phase 4.

### 3.1 Console

1. Sign in to the AWS account that will become the **management account**. If you do not have
   one, create it at <https://portal.aws.amazon.com/billing/signup> using
   `aws-root+mgmt@vendoworks.com` as the root email.
2. **AWS Organizations** → **Create an organization** → *Enable all features* (not
   consolidated-billing-only; all features is required for SCPs later).
3. **AWS accounts** → **Add an AWS account** → *Create an AWS account*:
   - Account name `jat-leadquali-prod`, email `aws-root+jatlq-prod@vendoworks.com`
   - IAM role name: leave as `OrganizationAccountAccessRole`
4. Repeat for `jat-leadquali-dev` / `aws-root+jatlq-dev@vendoworks.com`.
5. Account creation takes a few minutes each. Both must reach **Active**.

The `+suffix` addressing works with Google Workspace and most providers, and means one alias
receives all root mail. Every one of these addresses must be a **distribution list or alias you
control**, never a personal mailbox — losing access to a root email address means losing the
ability to recover the account.

### 3.2 CLI equivalent

```bash
aws organizations create-organization --feature-set ALL

aws organizations create-account \
  --email "aws-root+jatlq-prod@vendoworks.com" \
  --account-name "jat-leadquali-prod" \
  --iam-user-access-to-billing ALLOW

aws organizations create-account \
  --email "aws-root+jatlq-dev@vendoworks.com" \
  --account-name "jat-leadquali-dev" \
  --iam-user-access-to-billing ALLOW
```

`create-account` is asynchronous. Poll it:

```bash
aws organizations list-create-account-status --states IN_PROGRESS FAILED SUCCEEDED \
  --query 'CreateAccountStatuses[].{Name:AccountName,State:State,Id:AccountId,Reason:FailureReason}' \
  --output table
```

### 3.3 Human access — IAM Identity Center, not IAM users

Do not create an IAM user for yourself. In the management account:

1. **IAM Identity Center** → **Enable** (choose `$REGION` as the Identity Center region).
2. **Users** → add yourself with your real work email.
3. **Permission sets** → create `AdministratorAccess` (from the AWS managed policy) and set the
   session duration to 1 hour.
4. **AWS accounts** → select `jat-leadquali-dev` and `jat-leadquali-prod` → **Assign users** →
   you → `AdministratorAccess`.
5. Enable MFA for your Identity Center user (**Settings → Multi-factor authentication →**
   require MFA every time they sign in).

Configure CLI profiles once:

```bash
aws configure sso --profile jatlq-dev    # follow the prompts; region $REGION
aws configure sso --profile jatlq-prod
aws sso login --profile jatlq-prod
```

From here on, every command in this runbook runs with `--profile jatlq-prod` (or `jatlq-dev`).
Export it to avoid repeating yourself:

```bash
export AWS_PROFILE=jatlq-prod
export AWS_REGION=$REGION
```

### Verify

```bash
aws organizations list-accounts \
  --query 'Accounts[].{Name:Name,Id:Id,Status:Status,Email:Email}' --output table
# Expect 3 accounts (management + dev + prod), all ACTIVE.

aws sts get-caller-identity --profile jatlq-prod
# Expect Arn: arn:aws:sts::<PROD_ACCOUNT_ID>:assumed-role/AWSReservedSSO_AdministratorAccess_*/<you>
# NOT an ...:user/... ARN. If it says :user/, you are still on an IAM user — fix that first.

export PROD_ACCOUNT_ID=$(aws sts get-caller-identity --profile jatlq-prod --query Account --output text)
echo "$PROD_ACCOUNT_ID"
```

---

## 4. Root account hygiene, MFA and break-glass

Do this for **every** account: management, dev and prod. Root is the one identity that no IAM
policy, SCP or deny statement can constrain — it is the account's last resort and its largest
liability.

### 4.1 Per account

1. Sign in as root with the account's root email → **Account settings**:
   - Set **Alternate contacts** → Billing, Operations and **Security** to a monitored address.
     The security contact is how AWS reaches you about an abuse or compromise report; leaving it
     unset means that mail goes to the root inbox nobody reads.
2. **IAM → My security credentials → Multi-factor authentication → Assign MFA device.**
   Prefer a hardware key (YubiKey) for prod; a TOTP app in a password manager is acceptable for
   dev. AWS supports multiple MFA devices per root user — register **two** for prod so a lost
   phone is not an account-recovery ticket.
3. **Delete any root access keys.** There should be none. If the account is older and has them,
   delete them — a root access key is an unconstrainable, un-revocable-by-policy credential.
4. Set a long random root password, store it in the password manager, and **stop using root.**
5. Sign out. Use Identity Center from now on.

### 4.2 Break-glass

Root is needed for a small, real set of operations: closing an account, changing the support
plan, restoring an S3 bucket policy or KMS key policy that locked every principal out, some
billing changes, and recovering from "I deleted the last administrative role."

The break-glass procedure:

- Root password and the second MFA device for **prod** live offline — a sealed envelope in a safe,
  or your password manager's emergency-access/legacy feature with a named second person. Not on
  the same laptop as the primary MFA device.
- Any root use is a written event: who, when, why, what was changed. One line in an incident
  note is enough; the point is that it is never routine.
- After any break-glass use: rotate the root password, and confirm the CloudTrail record of the
  session matches what you wrote down.

### 4.3 Alarm on root usage

Root login should be a surprise, so make it noisy. In each account (requires phase 5's CloudTrail
first, so run this after §5):

```bash
aws sns create-topic --name jatlq-security-alerts
export SEC_TOPIC=$(aws sns list-topics \
  --query "Topics[?ends_with(TopicArn,':jatlq-security-alerts')].TopicArn" --output text)
aws sns subscribe --topic-arn "$SEC_TOPIC" --protocol email \
  --notification-endpoint "$ORG_EMAIL_BASE"
# Confirm the subscription from your inbox before continuing.

aws events put-rule --name jatlq-root-activity \
  --description "Any console sign-in or API call made by the root user" \
  --event-pattern '{
    "detail-type": ["AWS Console Sign In via CloudTrail", "AWS API Call via CloudTrail"],
    "detail": { "userIdentity": { "type": ["Root"] } }
  }'

aws events put-targets --rule jatlq-root-activity \
  --targets "Id=1,Arn=$SEC_TOPIC"
```

Also grant EventBridge permission to publish to the topic (`sns set-topic-attributes` with a
policy allowing `events.amazonaws.com` to `SNS:Publish` on `$SEC_TOPIC`).

### Verify

```bash
# MFA on root, and zero root access keys. Both numbers matter.
aws iam get-account-summary --query 'SummaryMap.{RootMFA:AccountMFAEnabled,RootKeys:AccountAccessKeysPresent}'
# Expect: {"RootMFA": 1, "RootKeys": 0}

# Full credential inventory, including when root was last used.
aws iam generate-credential-report >/dev/null && sleep 5
aws iam get-credential-report --query Content --output text | base64 --decode | column -t -s,
```

Run the first command in all three accounts. `RootMFA: 0` or `RootKeys: 1` anywhere is a stop
condition — fix it before continuing.

---

## 5. CloudTrail

Turn this on before the deploy role exists, so the role's very first action is already recorded.

### 5.1 Organization trail (management account)

1. Sign in to the **management account** → **CloudTrail** → **Create trail**.
2. Name `jatlq-org-trail`. Tick **Enable for all accounts in my organization**.
3. Storage: create a new S3 bucket `jatlq-cloudtrail-<MGMT_ACCOUNT_ID>`.
4. Tick **Log file SSE-KMS encryption** and **Log file validation** — validation is what lets you
   later prove logs were not tampered with, and it cannot be applied retroactively.
5. Multi-region: **yes**. An attacker's first move in an unfamiliar account is to work in a region
   you are not watching.
6. Event types: **Management events** (read + write). Data events for S3/Lambda are chargeable and
   not needed for a deploy audit trail — leave them off for now.

CLI equivalent (after creating and policy-attaching the bucket):

```bash
aws cloudtrail create-trail \
  --name jatlq-org-trail \
  --s3-bucket-name "jatlq-cloudtrail-${MGMT_ACCOUNT_ID}" \
  --is-organization-trail \
  --is-multi-region-trail \
  --enable-log-file-validation \
  --kms-key-id "$TRAIL_KMS_KEY_ARN"

aws cloudtrail start-logging --name jatlq-org-trail
```

### 5.2 Where deploy audit lives

Three places, and you need all three to answer "who changed prod and why":

| Question | Where |
|---|---|
| Which AWS API calls did the deploy make? | CloudTrail, `userIdentity.arn` = `arn:aws:sts::<acct>:assumed-role/JATLeadQualiDeploy/<session-name>` |
| Which GitHub run made them? | The **role session name**, which §9's workflow sets to `gha-jatlq-${{ github.run_id }}` — this is the join key between CloudTrail and GitHub |
| What resources actually changed? | CloudFormation **stack events** for `jat-leadquali-*`, plus the changeset diff |

That session-name convention is the whole reason a CloudTrail entry is traceable to a commit.
Do not let it drift.

### Verify

```bash
aws cloudtrail get-trail-status --name jatlq-org-trail \
  --query '{Logging:IsLogging,LastDelivery:LatestDeliveryTime,Error:LatestDeliveryError}'
# Expect IsLogging: true and no error. First events land within ~15 minutes.

# After phase 9, this is the query that proves deploy attribution works:
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=Username,AttributeValue=JATLeadQualiDeploy \
  --max-results 10 \
  --query 'Events[].{Time:EventTime,Event:EventName,User:Username}' --output table
```

---

## 6. GitHub Actions OIDC — the identity provider and trust policy

This is the phase that keeps a long-lived AWS access key out of GitHub. Read §6.4 before you
write a trust policy; a badly scoped one is worse than no automation at all.

Run everything in this phase in the **prod** account, then repeat in **dev**. Each account needs
its own OIDC provider and its own role — an OIDC identity provider is an account-scoped IAM
resource.

### 6.1 Create the IAM OIDC identity provider

Console: **IAM → Identity providers → Add provider → OpenID Connect**
Provider URL `https://token.actions.githubusercontent.com`, Audience `sts.amazonaws.com`.

CLI:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

Two notes on that thumbprint. AWS no longer validates GitHub's OIDC certificate against a
caller-supplied thumbprint for this well-known provider — it uses its own trusted CA store — so
the value is effectively vestigial, but the API still accepts (and some CLI versions still
require) it. **Do not build alerting or rotation around it**, and do not panic when GitHub rotates
certificates; nothing breaks. If your CLI version rejects the flag as unsupported, omit it.

The audience is `sts.amazonaws.com` because that is what `aws-actions/configure-aws-credentials`
requests. Do not add other audiences.

### 6.2 Create the deploy role with a scoped trust policy

Write the trust policy to a file. This is the exact document, with the real repository in it:

```bash
cat > /tmp/jatlq-deploy-trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsOIDC",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::PROD_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:vendo-aron/JAT-LeadQuali:ref:refs/heads/claude/jat-leadquali-agent-plan-6y2xh1",
            "repo:vendo-aron/JAT-LeadQuali:environment:prod"
          ]
        }
      }
    }
  ]
}
JSON

sed -i "s/PROD_ACCOUNT_ID/${PROD_ACCOUNT_ID}/" /tmp/jatlq-deploy-trust.json

aws iam create-role \
  --role-name JATLeadQualiDeploy \
  --description "GitHub Actions OIDC deploy role for vendo-aron/JAT-LeadQuali (issue #25)" \
  --assume-role-policy-document file:///tmp/jatlq-deploy-trust.json \
  --max-session-duration 3600
```

For the **dev** account, the same document with `PROD_ACCOUNT_ID` replaced by the dev account id
and the `sub` list replaced by:

```json
"repo:vendo-aron/JAT-LeadQuali:ref:refs/heads/claude/jat-leadquali-agent-plan-6y2xh1",
"repo:vendo-aron/JAT-LeadQuali:environment:dev"
```

### 6.3 The branch condition, and the fact that `main` does not exist yet

**This repository has no `main` branch.** Its default branch today is
`claude/jat-leadquali-agent-plan-6y2xh1`, which is why that string — not `main` — appears in the
trust policy above. Issue #25 step 4 says `refs/heads/main`; that instruction is correct about
*intent* and wrong about the *current* branch name, and copying it verbatim produces a role that
can never be assumed.

**When a real default branch exists** (the epic's branches are squashed onto `main`, or the
default is renamed), exactly three things must be updated, in this order:

1. The `sub` condition in the trust policy of the **prod** role — this file's §6.2 document.
2. The same condition in the **dev** role.
3. The `on: push: branches:` filter in the deploy workflow (#27).

Update the roles **before** you flip the default branch, or the first deploy from the new branch
fails to assume the role. Then remove the old branch's `sub` entry — leaving a stale branch in a
trust policy is a live path for anyone who can recreate that branch name.

```bash
# The update, once the real default branch exists:
sed -i 's#refs/heads/claude/jat-leadquali-agent-plan-6y2xh1#refs/heads/main#' /tmp/jatlq-deploy-trust.json
aws iam update-assume-role-policy \
  --role-name JATLeadQualiDeploy \
  --policy-document file:///tmp/jatlq-deploy-trust.json
```

Set a calendar reminder or an issue for this. It is a two-minute change that silently breaks
deploys if it is missed, and a silent security regression if the stale entry is left behind.

### 6.4 Why the `sub` condition is not optional

The GitHub OIDC token's `sub` claim is the only thing that says *which* repository and *which*
ref is asking. Getting this wrong is the classic supply-chain hole:

| Trust policy `sub` condition | Who can assume the role |
|---|---|
| *(omitted entirely)* | **Every GitHub Actions workflow on github.com.** Anyone can point a workflow in their own public repo at your role ARN and get credentials. This is not theoretical; it is a well-documented, actively scanned-for misconfiguration. |
| `repo:vendo-aron/*` | Any repo in your org, including a new throwaway one, including one created by a compromised collaborator account. |
| `repo:vendo-aron/JAT-LeadQuali:*` | Any branch, any tag, any PR workflow, any environment in this repo. A contributor who can push a branch can deploy to prod. |
| `repo:vendo-aron/JAT-LeadQuali:ref:refs/heads/main` | Only workflows running on `main`. Correct. |
| `repo:vendo-aron/JAT-LeadQuali:environment:prod` | Only workflows whose job declares `environment: prod` — and GitHub enforces that environment's protection rules (required reviewers, wait timers) *before* it mints the token. Strongest of the four. |

Two subtleties that trip people up:

- **A job that declares an `environment:` gets `...:environment:NAME` as its `sub`, not the ref
  form.** The two conditions are alternatives, not a conjunction — which is why §6.2 uses
  `StringLike` with a *list* rather than a single `StringEquals`. If you list only the ref form
  and then add `environment: prod` to the job, the assume-role call starts failing.
- **`aud` must still be pinned** to `sts.amazonaws.com` alongside `sub`. Both, always.

For prod, prefer the environment condition and configure the GitHub environment with **required
reviewers**, so a production deploy needs a human click. That converts "a merge deploys to prod"
into "a merge asks a person to approve a prod deploy" — worth having before real leads flow.

### 6.5 The workflow that assumes it

This snippet belongs in the deploy workflow built in #27. It is reproduced here so phase 11 can
verify OIDC end to end before #26/#27 exist.

```yaml
name: Verify AWS OIDC

on:
  workflow_dispatch:
  push:
    branches: [claude/jat-leadquali-agent-plan-6y2xh1]   # update with §6.3

permissions:
  id-token: write      # REQUIRED — without this no OIDC token is minted at all
  contents: read       # setting `permissions:` at all drops every other scope to none

jobs:
  verify:
    runs-on: ubuntu-latest
    # environment: prod          # uncomment for the prod job; changes the `sub` claim (§6.4)
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials via OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
          role-session-name: gha-jatlq-${{ github.run_id }}   # the CloudTrail join key (§5.2)

      - name: Prove the role was assumed
        run: aws sts get-caller-identity
```

`permissions: id-token: write` is the single most commonly missed line. Without it the job gets
no OIDC token and `configure-aws-credentials` fails with
`Credentials could not be loaded ... OIDC token not available`. Note also that declaring a
`permissions:` block sets every unlisted scope to `none`, which is why `contents: read` is spelled
out — `actions/checkout` needs it.

### Verify

```bash
# 1. The provider exists, with exactly one audience.
aws iam list-open-id-connect-providers
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn "arn:aws:iam::${PROD_ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com" \
  --query '{Url:Url,Audiences:ClientIDList}'
# Expect Audiences: ["sts.amazonaws.com"]

# 2. Read back the trust policy and eyeball the sub condition. This is the security review.
aws iam get-role --role-name JATLeadQualiDeploy \
  --query 'Role.AssumeRolePolicyDocument' | jq .
# The `sub` values MUST contain "vendo-aron/JAT-LeadQuali" and a ref or environment segment.
# A bare "*" anywhere in the sub is a stop condition.

# 3. Automated check — prints FAIL if the sub is missing or wildcarded.
aws iam get-role --role-name JATLeadQualiDeploy --query 'Role.AssumeRolePolicyDocument' \
| jq -e '
  [ .Statement[].Condition | (.StringEquals,.StringLike) | objects
    | to_entries[] | select(.key|endswith(":sub")) | .value ] | flatten
  | if length == 0 then false
    elif any(.[]; test("^repo:vendo-aron/JAT-LeadQuali:(ref|environment):[^*]+$") | not) then false
    else true end' >/dev/null && echo "PASS: sub scoped" || echo "FAIL: sub missing or wildcarded"
```

The live end-to-end assume test is in phase 11 — it needs the GitHub variables from phase 9.

---

## 7. The deploy role's permissions

### `AdministratorAccess` is the fast wrong answer

It will be tempting. `sam deploy` touches nine services, working out the exact action set takes
an hour, and `AdministratorAccess` makes the error go away in ten seconds. Do not attach it.

The reason is not policy hygiene, it is what the role *is*: a credential that any workflow run
matching the trust policy can obtain, non-interactively, without a human present. With
`AdministratorAccess` on it, a single bad merge to the deploy branch — a compromised action
version, a malicious dependency in a workflow step, a mistaken `workflow_dispatch` — yields full
control of the account: read every lead in RDS, exfiltrate every secret, create an IAM user with
its own access keys and persist after you revoke the role. The trust policy is your only control,
and it is one `sed` away from being wrong. Least privilege is what makes a trust-policy mistake
survivable.

It also destroys the audit story from §5.2: with admin, CloudTrail tells you what happened but
never that anything was out of bounds.

### What to grant instead

Start from what SAM actually needs for the §3/§4 architecture:

| Service | Why SAM needs it | Scope to |
|---|---|---|
| CloudFormation | creates/updates the stack and changesets | `stack/jat-leadquali-*/*` **plus** `arn:aws:cloudformation:$REGION:aws:transform/Serverless-2016-10-31` |
| S3 | uploads the packaged Lambda artifacts | the artifact bucket only (§8) |
| Lambda | ingest + worker functions, versions, event source mappings | `function:jat-leadquali-*` |
| API Gateway | HTTP API, routes, stages, usage plans | `apigateway:*` on `/apis/*` (v2 APIs have no stable pre-creation ARN) |
| SQS | main queue + DLQ, redrive policy | `queue/jat-leadquali-*` |
| IAM | creates the two function execution roles, then **PassRole**s them to Lambda | `role/jat-leadquali-*` only, with a `PassedToService` condition |
| RDS | instance, subnet group, RDS Proxy | `db:jat-leadquali-*`, `proxy/jat-leadquali-*` |
| EC2 | VPC, subnets, route tables, NAT gateway, security groups | region condition; see the caveat below |
| Secrets Manager | creates/reads the DB and HMAC secret entries | `secret:jat-leadquali/*` |
| KMS | the CMK for encryption at rest (plan §8) | the specific key ARN |
| CloudWatch / Logs | log groups, the §8 alarms | `log-group:/aws/lambda/jat-leadquali-*`, `alarm:jat-leadquali-*` |
| SES | configuration set, identity policies (#20) | `configuration-set/jat-leadquali-*` |

Two things to be honest about:

- **`iam:PassRole` is the dangerous one.** Without a condition, a principal that can create a role
  and pass it can pass *any* role — including one more privileged than itself — to a Lambda it
  controls. Always constrain it:

  ```json
  {
    "Sid": "PassOnlyOurFunctionRolesToLambda",
    "Effect": "Allow",
    "Action": "iam:PassRole",
    "Resource": "arn:aws:iam::PROD_ACCOUNT_ID:role/jat-leadquali-*",
    "Condition": { "StringEquals": { "iam:PassedToService": "lambda.amazonaws.com" } }
  }
  ```

- **VPC/EC2 actions resist resource scoping.** `ec2:CreateVpc`, `ec2:CreateSubnet`,
  `ec2:CreateNatGateway` and friends operate on resources that do not exist yet, so ARN scoping
  is partly unavailable. Constrain by region instead
  (`"Condition": {"StringEquals": {"aws:RequestedRegion": "eu-west-1"}}`), require a tag on
  creation (`aws:RequestTag/Project: jat-leadquali`), and scope the *destructive* actions
  (`Delete*`, `Revoke*`, `Modify*`) with `aws:ResourceTag/Project`. This is a real, acknowledged
  gap, not something to paper over.

### The managed-policy starting point — acceptable, with conditions

**A scoped-but-broad managed-policy start is acceptable for the first deploy, and I am saying so
explicitly**, because hand-writing the action list before you have ever run `sam deploy` produces
a day of `AccessDenied` ping-pong and, usually, a policy that is broader than the one you would
have derived from evidence.

Day-1 policy set for `JATLeadQualiDeploy` — note what is deliberately absent:

```bash
for p in AWSCloudFormationFullAccess AWSLambda_FullAccess AmazonAPIGatewayAdministrator \
         AmazonSQSFullAccess AmazonRDSFullAccess AmazonSESFullAccess \
         SecretsManagerReadWrite CloudWatchFullAccess AmazonVPCFullAccess; do
  aws iam attach-role-policy --role-name JATLeadQualiDeploy \
    --policy-arn "arn:aws:iam::aws:policy/$p"
done
```

- **`IAMFullAccess` is never attached.** IAM is granted only by the narrow inline policy below.
  This is the line that separates "broad" from "admin-equivalent": without `iam:*`, a compromised
  workflow cannot mint itself a persistent identity.
- **`AdministratorAccess` is never attached**, not even temporarily "to unblock the first deploy."
- `AmazonVPCFullAccess` rather than `AmazonEC2FullAccess` — it covers the VPC/NAT/subnet surface
  #26 needs without granting instance launch.

Then the inline policy carrying the IAM, KMS and S3 grants that no managed policy should provide:

```bash
cat > /tmp/jatlq-deploy-inline.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ManageOnlyOurFunctionRoles",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
        "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
        "iam:UpdateAssumeRolePolicy"
      ],
      "Resource": "arn:aws:iam::PROD_ACCOUNT_ID:role/jat-leadquali-*",
      "Condition": {
        "StringEquals": {
          "iam:PermissionsBoundary":
            "arn:aws:iam::PROD_ACCOUNT_ID:policy/JATLeadQualiFunctionBoundary"
        }
      }
    },
    {
      "Sid": "PassOnlyOurFunctionRolesToLambda",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::PROD_ACCOUNT_ID:role/jat-leadquali-*",
      "Condition": { "StringEquals": { "iam:PassedToService": "lambda.amazonaws.com" } }
    },
    {
      "Sid": "SamArtifactBucket",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::jat-leadquali-sam-artifacts-PROD_ACCOUNT_ID-REGION",
        "arn:aws:s3:::jat-leadquali-sam-artifacts-PROD_ACCOUNT_ID-REGION/*"
      ]
    },
    {
      "Sid": "SamTransform",
      "Effect": "Allow",
      "Action": ["cloudformation:CreateChangeSet"],
      "Resource": "arn:aws:cloudformation:REGION:aws:transform/Serverless-2016-10-31"
    },
    {
      "Sid": "AppDataKey",
      "Effect": "Allow",
      "Action": [
        "kms:CreateKey", "kms:DescribeKey", "kms:CreateAlias", "kms:TagResource",
        "kms:PutKeyPolicy", "kms:GetKeyPolicy", "kms:EnableKeyRotation"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "REGION" } }
    },
    {
      "Sid": "NeverTouchTheseNoMatterWhatElseIsAttached",
      "Effect": "Deny",
      "Action": [
        "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile",
        "iam:UpdateLoginProfile", "iam:CreateSAMLProvider",
        "iam:CreateOpenIDConnectProvider", "iam:UpdateOpenIDConnectProviderThumbprint",
        "iam:DeleteOpenIDConnectProvider",
        "organizations:*", "account:*",
        "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:UpdateTrail",
        "kms:ScheduleKeyDeletion", "kms:DisableKey",
        "rds:DeleteDBInstance", "rds:DeleteDBCluster"
      ],
      "Resource": "*"
    }
  ]
}
JSON

sed -i "s/PROD_ACCOUNT_ID/${PROD_ACCOUNT_ID}/g; s/REGION/${REGION}/g" /tmp/jatlq-deploy-inline.json
aws iam put-role-policy --role-name JATLeadQualiDeploy \
  --policy-name JATLeadQualiDeployGuardrails \
  --policy-document file:///tmp/jatlq-deploy-inline.json
```

The explicit `Deny` block is the important part: an explicit deny beats any allow, including
one from a managed policy someone attaches later in a hurry. It means the deploy role can never
create a long-lived credential, never disable its own audit trail, never tamper with the OIDC
provider that authenticates it, and never delete the production database. `rds:DeleteDBInstance`
being denied means a stack teardown of prod requires a human — which is correct; CI should not be
able to delete the lead database, ever.

You will also need the `JATLeadQualiFunctionBoundary` permissions-boundary policy referenced
above — a policy granting the union of what the two Lambda roles may ever do (per #26: worker gets
SES send, Secrets Manager read, SQS consume, RDS connect, logs; ingest gets SQS send and logs
only). Requiring a boundary on every role the deploy role creates is what stops the classic
escalation: create role → attach `AdministratorAccess` → pass it to a Lambda → run anything.

### The hardening step — do not skip it

The managed-policy start is a **week-1 position with a week-4 deadline**, and the way you close it
is with evidence rather than guesswork:

1. Run the first successful `sam deploy` (phase 8) and let #26/#27 deploy a few times.
2. Wait 24 hours for CloudTrail to settle, then generate a policy from actual activity:
   **IAM console → Roles → JATLeadQualiDeploy → Access Advisor**, and
   **IAM → Access Analyzer → Generate policy** based on CloudTrail for the trail from §5,
   over a window covering those deploys.
3. Review the generated policy, add back the actions a *first* deploy needs that an *update*
   deploy does not (`Create*` on resources that already existed), and attach it as a
   customer-managed policy `JATLeadQualiDeployScoped`.
4. Detach the nine AWS managed policies. Redeploy. Fix any `AccessDenied` by adding the specific
   action, never by reattaching a `*FullAccess` policy.
5. Record the date in §12. An undated "we'll tighten it later" is how it stays broad forever.

```bash
aws iam list-attached-role-policies --role-name JATLeadQualiDeploy \
  --query 'AttachedPolicies[].PolicyName' --output text
# After hardening this should print exactly: JATLeadQualiDeployScoped
```

### Verify

```bash
# No AdministratorAccess, ever.
aws iam list-attached-role-policies --role-name JATLeadQualiDeploy \
  --query "AttachedPolicies[?PolicyName=='AdministratorAccess']" --output text
# Expect empty output.

# The guardrail deny is in place.
aws iam get-role-policy --role-name JATLeadQualiDeploy \
  --policy-name JATLeadQualiDeployGuardrails \
  --query 'PolicyDocument.Statement[?Effect==`Deny`].Action' | jq .

# Simulate: the role must NOT be able to create an access key or stop the trail.
aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::${PROD_ACCOUNT_ID}:role/JATLeadQualiDeploy" \
  --action-names iam:CreateAccessKey cloudtrail:StopLogging rds:DeleteDBInstance \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}' --output table
# Expect explicitDeny for all three.

# Simulate: the role MUST be able to do the deploy basics.
aws iam simulate-principal-policy \
  --policy-source-arn "arn:aws:iam::${PROD_ACCOUNT_ID}:role/JATLeadQualiDeploy" \
  --action-names cloudformation:CreateChangeSet lambda:CreateFunction sqs:CreateQueue \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}' --output table
# Expect allowed for all three.
```

---

## 8. The SAM artifact bucket and `sam deploy` bootstrap

### 8.1 Create the bucket

Bucket names are globally unique, so include the account id and region:

```bash
export ARTIFACT_BUCKET="jat-leadquali-sam-artifacts-${PROD_ACCOUNT_ID}-${REGION}"

aws s3api create-bucket \
  --bucket "$ARTIFACT_BUCKET" \
  --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
# (omit --create-bucket-configuration entirely if REGION is us-east-1)

aws s3api put-public-access-block --bucket "$ARTIFACT_BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-versioning --bucket "$ARTIFACT_BUCKET" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket "$ARTIFACT_BUCKET" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
      "BucketKeyEnabled": true
    }]
  }'

# Versioning on an artifact bucket grows without bound. Expire old versions.
aws s3api put-bucket-lifecycle-configuration --bucket "$ARTIFACT_BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-old-artifact-versions",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
    }]
  }'
```

Versioning is on because CloudFormation references artifacts by key; deleting or overwriting one
mid-rollback breaks the rollback. The lifecycle rule is what stops that from becoming a slowly
growing bill.

Deny non-TLS access:

```bash
cat > /tmp/jatlq-bucket-policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": [
      "arn:aws:s3:::${ARTIFACT_BUCKET}",
      "arn:aws:s3:::${ARTIFACT_BUCKET}/*"
    ],
    "Condition": { "Bool": { "aws:SecureTransport": "false" } }
  }]
}
JSON
aws s3api put-bucket-policy --bucket "$ARTIFACT_BUCKET" --policy file:///tmp/jatlq-bucket-policy.json
```

### 8.2 Bootstrap `sam deploy`

`infra/template.yaml` is #26's deliverable and does not exist yet. To prove the bucket, the role
and the CloudFormation path work *before* #26 lands, deploy a throwaway stack that creates one
SQS queue. Write it outside the repository — this is scaffolding, not a committed file:

```bash
mkdir -p ~/jatlq-bootstrap && cat > ~/jatlq-bootstrap/template.yaml <<'YAML'
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: Throwaway stack proving the SAM deploy path works (issue #25 verification)
Resources:
  BootstrapCheckQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: jat-leadquali-bootstrap-check
Outputs:
  QueueUrl:
    Value: !Ref BootstrapCheckQueue
YAML

sam deploy \
  --template-file ~/jatlq-bootstrap/template.yaml \
  --stack-name jat-leadquali-bootstrap-check \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix bootstrap-check \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --no-confirm-changeset \
  --tags Project=jat-leadquali Environment=prod
```

`CAPABILITY_AUTO_EXPAND` is required by the `AWS::Serverless-2016-10-31` transform and is the
usual cause of a first-deploy failure. The `Project`/`Environment` tags should be on every deploy
— they are what makes the §10 budget filters and the §7 tag-scoped IAM conditions work.

The real stack in #26 will use `samconfig.toml` with `dev` and `prod` parameter sets rather than
these flags; record the bucket name and region there.

Tear the throwaway down once phase 11 has passed:

```bash
sam delete --stack-name jat-leadquali-bootstrap-check --region "$REGION" --no-prompts
```

### Verify

```bash
aws s3api get-bucket-versioning --bucket "$ARTIFACT_BUCKET"        # Status: Enabled
aws s3api get-public-access-block --bucket "$ARTIFACT_BUCKET"      # all four true
aws s3api get-bucket-encryption --bucket "$ARTIFACT_BUCKET"        # AES256

aws cloudformation describe-stacks --stack-name jat-leadquali-bootstrap-check \
  --query 'Stacks[0].{Status:StackStatus,Created:CreationTime}' --output table
# Expect CREATE_COMPLETE.

aws s3 ls "s3://${ARTIFACT_BUCKET}/bootstrap-check/" --recursive | head
# Expect at least one uploaded template artifact.
```

---

## 9. GitHub Actions variables

Per issue #25 step 8, the role ARN and region go in as **variables, not secrets**. Neither is
sensitive — a role ARN is useless without a matching OIDC token, and the trust policy is the
actual control — and having them visible in logs makes a failed deploy diagnosable instead of
`***`.

```bash
export DEPLOY_ROLE_ARN="arn:aws:iam::${PROD_ACCOUNT_ID}:role/JATLeadQualiDeploy"

gh variable set AWS_REGION           --repo "$GH_REPO" --body "$REGION"
gh variable set AWS_DEPLOY_ROLE_ARN  --repo "$GH_REPO" --body "$DEPLOY_ROLE_ARN"
gh variable set SAM_ARTIFACT_BUCKET  --repo "$GH_REPO" --body "$ARTIFACT_BUCKET"
```

Create the `prod` environment with required reviewers, so the environment-scoped trust condition
from §6.4 has teeth:

```bash
gh api -X PUT "repos/${GH_REPO}/environments/prod" \
  -F "reviewers[][type]=User" -F "reviewers[][id]=$(gh api user --jq .id)"

gh api -X PUT "repos/${GH_REPO}/environments/dev"

# Per-environment overrides for the dev account:
gh variable set AWS_DEPLOY_ROLE_ARN --repo "$GH_REPO" --env dev \
  --body "arn:aws:iam::${DEV_ACCOUNT_ID}:role/JATLeadQualiDeploy"
```

### Verify

```bash
gh variable list --repo "$GH_REPO"
# Expect AWS_REGION, AWS_DEPLOY_ROLE_ARN, SAM_ARTIFACT_BUCKET.

gh secret list --repo "$GH_REPO"
# Expect NO AWS_ACCESS_KEY_ID and NO AWS_SECRET_ACCESS_KEY. If either exists, delete it —
# that is the exact thing this phase removes the need for:
#   gh secret delete AWS_ACCESS_KEY_ID --repo "$GH_REPO"
#   gh secret delete AWS_SECRET_ACCESS_KEY --repo "$GH_REPO"
```

---

## 10. Budgets and cost guardrails

### 10.1 What this architecture actually costs

Plan §8 gives the Anthropic figure: **~$0.02–0.03 per lead, ≈$25/month at 1,000 leads/month**.
That is the number everyone quotes. It is not the number that shows up on the bill.

Below is the AWS floor for the §3/§4 architecture — one prod environment, `eu-west-1`, on-demand
list prices as of 2026-09. Verify against <https://calculator.aws> before trusting it; AWS
prices move.

| Line item | Basis | ≈ $/month |
|---|---|---|
| **NAT gateway** | 1 × $0.045/hr × 730 hr + ~$0.045/GB | **32.85** |
| **RDS Proxy** | 2 vCPU × $0.015/vCPU-hr × 730 hr | **21.90** |
| RDS `db.t4g.micro`, single-AZ | $0.018/hr × 730 hr | 13.14 |
| RDS storage, 20 GB gp3 | $0.115/GB-mo | 2.30 |
| Secrets Manager | 3 secrets × $0.40 | 1.20 |
| KMS customer-managed key | 1 × $1.00 | 1.00 |
| CloudWatch alarms + logs | 5 alarms × $0.10, ~1 GB ingest | 1.00 |
| Lambda (1,000 invocations) | well inside free tier | ~0.00 |
| API Gateway HTTP API | $1.00 per million requests | ~0.00 |
| SQS | first 1M requests free | 0.00 |
| SES | $0.10 per 1,000 emails | ~0.00 |
| CloudTrail | first management-event trail free | ~0.00 |
| **AWS subtotal** | | **≈ $73** |
| Anthropic (plan §8) | 1,000 leads @ ~$0.025 | 25 |
| **Total, prod** | | **≈ $98/month** |

**The dominant line item is not the model — it is the NAT gateway, and RDS Proxy right behind
it.** Together they are ~$55 of the ~$73 AWS floor: three quarters of the infrastructure bill,
and more than double the Anthropic spend. Both exist for exactly one reason — plan §4's decision
to put Postgres behind the worker, which forces the worker Lambda into a VPC.

The part people forget, and the reason you cannot simply delete the NAT gateway: **the worker
calls `api.anthropic.com`, which is the public internet.** VPC interface endpoints solve egress
to *AWS* services (Secrets Manager, SES, SQS, KMS, Logs) but there is no VPC endpoint for a
third-party API. A Lambda in a private subnet with no NAT has no route to Anthropic and every
qualification fails. And it fails *quietly at first* — the SDK retries, SQS redelivers, messages
pile up, and you find out from the DLQ alarm.

That also means swapping NAT for endpoints is not the saving it looks like: five interface
endpoints at ~$0.01/hr each is ~$36/month, *more* than the NAT gateway, and you would still need
the NAT for Anthropic. Endpoints are a security improvement (AWS traffic never leaves the AWS
network), not a cost one.

What genuinely reduces this:

- **One NAT gateway, not one per AZ.** The multi-AZ default doubles or triples this line. At this
  volume, accept the AZ-failure blast radius and run one. (~$33 saved per AZ removed.)
- **No RDS Proxy in dev.** Plan §4 offers the alternative — cap the worker's reserved concurrency
  so the connection count is bounded. That is adequate for one developer. Keep the proxy in prod,
  where a burst genuinely can exhaust `db.t4g.micro`'s connection limit. (~$22/month saved.)
- **Do not leave dev running.** The dev floor is the same ~$73 whether it processes ten leads or
  none, because it is nearly all hourly charges. Create the dev stack on demand and
  `sam delete` it when you are done; Phase 2's local Docker Postgres covers day-to-day work.
  This is the single biggest lever, worth more than every application-level optimisation combined.
- The bill is ~$73 at 100 leads/month and ~$73 at 10,000. **This architecture has a high floor and
  a low slope** — the marginal lead costs ~$0.025 in Anthropic tokens and roughly nothing in AWS.
  Do not spend engineering time on per-lead AWS cost until volume is 100× today's.

### 10.2 Create the budgets

The Budgets and Cost Explorer APIs are global and served from `us-east-1`. Pass
`--region us-east-1` regardless of `$REGION`.

Set up the notification topic first and confirm the email subscription — an unconfirmed
subscription silently drops every alert:

```bash
aws sns create-topic --name jatlq-cost-alerts --region us-east-1
export COST_TOPIC=$(aws sns list-topics --region us-east-1 \
  --query "Topics[?ends_with(TopicArn,':jatlq-cost-alerts')].TopicArn" --output text)
aws sns subscribe --region us-east-1 --topic-arn "$COST_TOPIC" \
  --protocol email --notification-endpoint "$ORG_EMAIL_BASE"
# Click the confirmation link, then:
aws sns list-subscriptions-by-topic --region us-east-1 --topic-arn "$COST_TOPIC" \
  --query 'Subscriptions[].SubscriptionArn'
# A value of "PendingConfirmation" means alerts will NOT be delivered.
```

**Prod budget: $140/month.** That is ~1.4× the $98 expected steady state — high enough not to cry
wolf, low enough that a runaway is caught within days.

```bash
cat > /tmp/jatlq-budget-prod.json <<'JSON'
{
  "BudgetName": "jatlq-prod-monthly",
  "BudgetType": "COST",
  "TimeUnit": "MONTHLY",
  "BudgetLimit": { "Amount": "140", "Unit": "USD" },
  "CostFilters": { "TagKeyValue": ["user:Environment$prod"] },
  "CostTypes": {
    "IncludeTax": true, "IncludeSubscription": true, "IncludeCredit": false,
    "IncludeRefund": false, "IncludeDiscount": true, "UseAmortized": false
  }
}
JSON

cat > /tmp/jatlq-notifications.json <<JSON
[
  { "Notification": { "NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
      "Threshold": 50, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [{ "SubscriptionType": "SNS", "Address": "${COST_TOPIC}" }] },
  { "Notification": { "NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [{ "SubscriptionType": "SNS", "Address": "${COST_TOPIC}" }] },
  { "Notification": { "NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
      "Threshold": 100, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [{ "SubscriptionType": "SNS", "Address": "${COST_TOPIC}" }] },
  { "Notification": { "NotificationType": "FORECASTED", "ComparisonOperator": "GREATER_THAN",
      "Threshold": 100, "ThresholdType": "PERCENTAGE" },
    "Subscribers": [{ "SubscriptionType": "SNS", "Address": "${COST_TOPIC}" }] }
]
JSON

aws budgets create-budget --region us-east-1 \
  --account-id "$PROD_ACCOUNT_ID" \
  --budget file:///tmp/jatlq-budget-prod.json \
  --notifications-with-subscribers file:///tmp/jatlq-notifications.json
```

Concrete dollar thresholds on the $140 prod budget:

| Threshold | Amount | What it means |
|---|---|---|
| 50% ACTUAL | **$70** | Fires around day 21 every normal month. Treat it as a **heartbeat**, not an alarm — if it stops arriving, the alert path has broken. |
| 80% ACTUAL | **$112** | Meaningful. Before ~day 26 this means spend is running hot; check lead volume and NAT data processing. |
| 100% ACTUAL | **$140** | Investigate today. |
| 100% FORECASTED | **$140 projected** | The one that earns its keep — catches a runaway on day 3, not day 30. |

The 50% alert firing monthly by design is a deliberate calibration choice, and it is the honest
tradeoff of a budget set close to real spend. After two months of real data, either raise the
budget or drop the 50% notification. A budget that always fires is a budget nobody reads.

**Dev budget: $40/month** (assumes the stack is created on demand per §10.1):

```bash
sed 's/jatlq-prod-monthly/jatlq-dev-monthly/; s/"Amount": "140"/"Amount": "40"/; s/user:Environment\$prod/user:Environment$dev/' \
  /tmp/jatlq-budget-prod.json > /tmp/jatlq-budget-dev.json
aws budgets create-budget --region us-east-1 \
  --account-id "$DEV_ACCOUNT_ID" \
  --budget file:///tmp/jatlq-budget-dev.json \
  --notifications-with-subscribers file:///tmp/jatlq-notifications.json
```

Dev exceeding $40 almost always means one thing: a NAT gateway left running. Check that first.

Optionally add an **organization-wide $200 budget** in the management account with no cost filter,
as the backstop that catches spend in an account or service you forgot to tag.

### 10.3 Cost anomaly detection

Budgets catch *magnitude*. Anomaly detection catches *shape* — a service that suddenly costs 4×
its baseline while the total is still under budget. That is what a runaway Lambda retry loop or an
accidentally-enabled Multi-AZ looks like in week one.

```bash
export MONITOR_ARN=$(aws ce create-anomaly-monitor --region us-east-1 \
  --anomaly-monitor '{
    "MonitorName": "jatlq-service-monitor",
    "MonitorType": "DIMENSIONAL",
    "MonitorDimension": "SERVICE"
  }' --query MonitorArn --output text)

aws ce create-anomaly-subscription --region us-east-1 \
  --anomaly-subscription "{
    \"SubscriptionName\": \"jatlq-anomaly-alerts\",
    \"MonitorArnList\": [\"${MONITOR_ARN}\"],
    \"Subscribers\": [{\"Type\": \"EMAIL\", \"Address\": \"${ORG_EMAIL_BASE}\"}],
    \"Frequency\": \"DAILY\",
    \"ThresholdExpression\": {
      \"Dimensions\": {
        \"Key\": \"ANOMALY_TOTAL_IMPACT_ABSOLUTE\",
        \"MatchOptions\": [\"GREATER_THAN_OR_EQUAL\"],
        \"Values\": [\"10\"]
      }
    }
  }"
```

$10 absolute impact is the right threshold for a ~$100/month bill: large enough to ignore noise,
small enough that a doubled NAT or an extra RDS instance trips it. Cost Explorer must be enabled
(it enables itself on first console visit) and the detector needs ~24 hours plus about 10 days of
history to build a baseline — it will be quiet at first, which is expected, not broken.

### 10.4 The one guardrail AWS cannot give you

**AWS Budgets cannot see Anthropic spend.** The ~$25/month of model cost is billed by Anthropic,
not AWS, and a prompt bug or a retry storm can multiply it without moving the AWS bill at all.
Set a monthly spend limit and an email alert in the Anthropic Console for the API key used by the
worker, at **$75** (3× expected). Record it in §12 alongside the AWS budgets — a cost guardrail
that covers only three quarters of the spend is not a guardrail.

### Verify

```bash
aws budgets describe-budgets --region us-east-1 --account-id "$PROD_ACCOUNT_ID" \
  --query 'Budgets[].{Name:BudgetName,Limit:BudgetLimit.Amount,Spend:CalculatedSpend.ActualSpend.Amount}' \
  --output table

aws budgets describe-notifications-for-budget --region us-east-1 \
  --account-id "$PROD_ACCOUNT_ID" --budget-name jatlq-prod-monthly \
  --query 'Notifications[].{Type:NotificationType,Threshold:Threshold}' --output table
# Expect four rows: ACTUAL 50, ACTUAL 80, ACTUAL 100, FORECASTED 100.

aws ce get-anomaly-monitors --region us-east-1 \
  --query 'AnomalyMonitors[].{Name:MonitorName,Dimension:MonitorDimension}' --output table
```

**Proving the alert actually delivers** — this is an explicit acceptance criterion of #25, and
you cannot force a budget notification on demand. The reliable trick is a disposable budget whose
threshold is already breached, which AWS evaluates (several times a day) and alerts on for real:

```bash
cat > /tmp/jatlq-budget-canary.json <<'JSON'
{
  "BudgetName": "jatlq-alert-delivery-canary",
  "BudgetType": "COST",
  "TimeUnit": "MONTHLY",
  "BudgetLimit": { "Amount": "1", "Unit": "USD" }
}
JSON

aws budgets create-budget --region us-east-1 \
  --account-id "$PROD_ACCOUNT_ID" \
  --budget file:///tmp/jatlq-budget-canary.json \
  --notifications-with-subscribers file:///tmp/jatlq-notifications.json
```

Month-to-date spend already exceeds $1, so all three ACTUAL thresholds are breached and AWS sends
real notifications, typically within 12 hours and always within 24. When they arrive:

1. Note the arrival time and the sender — budget mail comes from
   `no-reply@budgets.amazonaws.com`; **check the spam folder**, and allowlist it if it landed
   there. A budget alert in spam is a budget alert that does not exist.
2. Record the date and the delivery time in §12 as evidence for the acceptance criterion.
3. Delete the canary so it does not become permanent noise:

```bash
aws budgets delete-budget --region us-east-1 \
  --account-id "$PROD_ACCOUNT_ID" --budget-name jatlq-alert-delivery-canary
```

Do not skip this in favour of "the SNS subscription is confirmed, so it must work." Confirming SNS
proves the topic delivers; the canary proves the *Budgets service* is wired to that topic and that
the mail survives your spam filter. Those fail independently.

---

## 11. End-to-end verification

Everything above verified a component. This verifies the system, and these are the checks that map
onto #25's acceptance criteria.

### 11.1 An Actions job assumes the deploy role via OIDC

Add the workflow from §6.5 as `.github/workflows/aws-oidc-verify.yml` (this belongs to #27's
workflow work — it is not created by this runbook), then:

```bash
gh workflow run "Verify AWS OIDC" --repo "$GH_REPO"
sleep 20
gh run list --workflow "Verify AWS OIDC" --repo "$GH_REPO" --limit 1
gh run view --repo "$GH_REPO" --log | grep -A6 'get-caller-identity'
```

**Expected output** in the job log:

```json
{
    "UserId": "AROA...:gha-jatlq-1234567890",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/JATLeadQualiDeploy/gha-jatlq-1234567890"
}
```

Three things to check, not one: the `Arn` names `JATLeadQualiDeploy`; the session-name suffix is
the GitHub run id (§5.2's join key); and the `Account` is the prod account, not dev.

### 11.2 The negative test — prove the scoping actually binds

A passing assume proves the role works. It does **not** prove the trust policy is scoped. Prove
that separately, or you have tested nothing about the supply-chain hole in §6.4:

```bash
git checkout -b oidc-negative-test
# Temporarily change the workflow's `on.push.branches` to include this branch, and push.
git push -u origin oidc-negative-test
gh run list --repo "$GH_REPO" --branch oidc-negative-test --limit 1
```

**Expected: the job FAILS**, at the `configure-aws-credentials` step, with:

```
Error: Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

That failure is the pass condition. If the job *succeeds* from an arbitrary branch, the `sub`
condition is not doing its job — go back to §6.2 and fix it before anything else. Delete the
branch afterwards.

### 11.3 `sam deploy` works from CI

Extend the verify workflow with a deploy step (or wait for #27) and confirm the throwaway stack
from §8.2 can be updated from Actions rather than from your laptop:

```yaml
      - name: SAM deploy (bootstrap check)
        run: |
          sam deploy \
            --template-file infra/bootstrap/template.yaml \
            --stack-name jat-leadquali-bootstrap-check \
            --s3-bucket ${{ vars.SAM_ARTIFACT_BUCKET }} \
            --region ${{ vars.AWS_REGION }} \
            --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
            --no-fail-on-empty-changeset \
            --no-confirm-changeset \
            --tags Project=jat-leadquali Environment=prod
```

Then confirm from the CLI that the change came from CI, not from a human:

```bash
aws cloudformation describe-stack-events --stack-name jat-leadquali-bootstrap-check \
  --query 'StackEvents[0:5].{Time:Timestamp,Status:ResourceStatus,Resource:LogicalResourceId}' \
  --output table

aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateChangeSet \
  --max-results 5 \
  --query 'Events[].{Time:EventTime,User:Username}' --output table
# Username must be JATLeadQualiDeploy.
```

### 11.4 No long-lived deploy keys exist anywhere

```bash
# AWS side: no IAM users at all is the ideal; no access keys is the requirement.
for u in $(aws iam list-users --query 'Users[].UserName' --output text); do
  echo "== $u"
  aws iam list-access-keys --user-name "$u" \
    --query 'AccessKeyMetadata[].{Id:AccessKeyId,Status:Status,Created:CreateDate}' --output table
done
# Expect no users, or users with zero keys.

# GitHub side:
gh secret list --repo "$GH_REPO" | grep -i aws && echo "FAIL: AWS secret present" || echo "PASS: no AWS secrets"
```

Then tear down the bootstrap stack (§8.2) — leaving it costs nothing but it is untracked
infrastructure, and #26 will create the real one.

---

## 12. Definition of done

Mapped directly onto issue #25's acceptance criteria and steps. Tick each with a date and, where
noted, paste the evidence into this section.

**Acceptance criteria (#25)**

- [ ] **A GitHub Actions job assumes the deploy role via OIDC and runs
      `aws sts get-caller-identity` successfully.** — §11.1. Paste the `Arn` line here.
- [ ] **No IAM user access keys exist for deployment.** — §11.4, both AWS and GitHub sides clean.
- [ ] **Root MFA enabled** in all accounts — §4, `AccountMFAEnabled: 1` and
      `AccountAccessKeysPresent: 0` everywhere.
- [ ] **Budget alerts confirmed by a test notification** — §10.4 canary; record the date and
      arrival time of the received email.
- [ ] **Region and account layout recorded in `docs/`** — the decision log below.

**Steps (#25)**

- [ ] 1. Account layout decided and recorded — §1.
- [ ] 2. Root MFA on, root no longer used — §4.
- [ ] 3. One region picked, matching SES from #20 — §2.
- [ ] 4. OIDC provider + deploy role + repo/branch-scoped trust policy — §6.
- [ ] 5. SAM S3 bucket: versioned, public access blocked, TLS-only, lifecycle rule — §8.
- [ ] 6. Monthly budget with 50/80/100% alerts (plus forecast) — §10.2.
- [ ] 7. CloudTrail on in the deploy region (org trail, multi-region) — §5.
- [ ] 8. Role ARN + region as GitHub Actions **variables** — §9.

**Added beyond #25, and why**

- [ ] Explicit `Deny` guardrail on the deploy role (no key creation, no trail tampering, no RDS
      deletion) — §7. Cheap, and it makes a future trust-policy mistake survivable.
- [ ] Permissions boundary required on every role the deploy role creates — §7.
- [ ] Cost anomaly detection at $10 absolute impact — §10.3.
- [ ] Anthropic Console spend limit at $75 — §10.4. AWS Budgets cannot see this spend.
- [ ] Root-activity EventBridge alarm — §4.3.
- [ ] Negative OIDC test proving the `sub` condition binds — §11.2.
- [ ] Dated reminder to harden the deploy role off managed policies — §7.
- [ ] Dated reminder to update the trust policy when a real default branch exists — §6.3.

---

## Decision log

Fill these in when the phases are executed. This section is the `docs/` record that #25's final
acceptance criterion asks for.

| Decision | Value | Date | Notes |
|---|---|---|---|
| Account layout | *(two accounts: `jat-leadquali-dev`, `jat-leadquali-prod`, under an Organization — recommended §1)* | | |
| Prod account id | | | |
| Dev account id | | | |
| Region | *(`eu-west-1` recommended §2)* | | Must equal the SES region from #20 |
| SES region (#20) | | | Must equal the row above |
| Deploy role ARN | `arn:aws:iam::<prod>:role/JATLeadQualiDeploy` | | |
| Trust policy `sub` | `repo:vendo-aron/JAT-LeadQuali:ref:refs/heads/…` | | **Update when a real default branch exists — §6.3** |
| SAM artifact bucket | `jat-leadquali-sam-artifacts-<acct>-<region>` | | |
| Prod budget | $140/month, 50/80/100% + forecast | | |
| Dev budget | $40/month | | |
| Anthropic spend limit | $75/month | | Set in the Anthropic Console, not AWS |
| Budget alert delivery proven | | | §10.4 canary; record received-email timestamp |
| Deploy role hardened off managed policies | | | Target: 4 weeks after first deploy (§7) |
| Break-glass root credentials location | | | Do not record the credentials here — only where they live |
