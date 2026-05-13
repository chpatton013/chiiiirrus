# Personal Cloud Deployment

[![test](https://github.com/chpatton013/chiiiirrus/actions/workflows/test.yml/badge.svg)](https://github.com/chpatton013/chiiiirrus/actions/workflows/test.yml)

This repo hosts the infrastructure-as-code (IaC) for my personal cloud
deployment, which is split across AWS (in progress) and a private homelab
(coming soon).

## Getting Started

**Do not skip any of these steps!**

### Write your `config.toml`

Update [`config.toml`](./config.toml) based on your domain and hosting needs.

`foundation.public_domain` and `foundation.private_domain` are the values most
likely to change for a fresh deployment. `public_domain` hosts the
control-plane URLs (Authentik, WebFinger, etc.); `private_domain` is reserved
for services that don't need to be reachable from the public internet. Look at
the [Contents](#Contents) section for a full breakdown of what services you're
configuring.

### Setup your AWS credentials

Set up your `default` profile in `~/.aws/config` and `~/.aws/credentials` 

We use the `boto3` and `aws_cdk` Python packages to do all of our AWS API
interaction. Those packages should both use the standard AWS config and
environment variable conventions for configuration and secrets injection, so if
you're familiar with those, you can do something fancier than using the
`default` profile.

### Run automated one-time bootstrap

Run `bin/bootstrap` to complete all one-time setup steps.

This script will:
1. ensure that `dotslash` is installed on your system (which we use extensively
   to fetch all our dependency tools), and
2. interactively setup the persistent AWS resources that will be referenced by
   the deploy process.

You can perform these bootstrapping steps manually if you want more control, or
need to rerun only a subset of them for some reason.
See the [Manual Bootstrapping](#Manual-Bootstrapping) section below for more
details.

### Perform manual one-time bootstrap

After the hosted zone are created by the bootstrap script, copy the 4 NS records
from Route53 into your registrar's DNS config.

### Run tests

Run `bin/test` to run all tests in the repository.

These tests will, among other things, ensure that your config is setup
correctly, and there are no obvious errors with the CDK IaC before you deploy.

### Deploy

Run this command to deploy the AWS infrastructure:

```sh
bin/cdk deploy --all --trace \
    --require-approval never \
    --concurrency="$(nproc --all)" \
    --asset-build-concurrency="$(nproc --all)"
```

You can modify those concurrency parameters or replace `--all` with the names of
specific stacks, as needed. `--require-approval` can be omitted if you aren't
running with any concurrency.

## Post-Deploy Setup

The OIDC applications (Tailscale, Headscale, Headplane, and Vaultwarden), users,
and group memberships are provisioned automatically by Authentik blueprints in
[`assets/authentik/blueprints/`](./assets/authentik/blueprints/), synced to each
Authentik task from S3 by an init container on every deploy. The only remaining
manual step is the Tailscale SaaS-side registration.

- Tailscale (SaaS-side)
    - Create a new account with OIDC provider
        - Email address: tailscale@example.com
        - WebFinger URL (automatically populated to https://example.com/.well-known/webfinger)
        - Which identity provider: Authentik
        - Get OIDC issuer
        - Client ID / Client Secret: use the values stored in the
          `authentik/oidc/tailscale` Secrets Manager entry
        - Prompts: consent
        - Sign up with OIDC
    - Authentik redirecting to Tailscale
        - Continue
- Headplane
    - Log in at `https://headplane.<public_domain>/admin` using the Headscale
      admin API key, which is written to Secrets Manager during the
      HeadscaleStack deploy:
      ```sh
      aws secretsmanager get-secret-value \
          --secret-id headscale/admin-api-key \
          --query SecretString --output text | jq -r .secret
      ```
- Vaultwarden
    - Log in at `https://vaultwarden.<public_domain>` using your SSO identity.
- Mail server (`MailStack`)
    - **SES production access** (required for outbound to non-verified addresses).
        - Enter the email address associated with your AWS account.
        - Enter your mail domain.
            - Set MAIL FROM domain to `bounce.<public_domain>`
        - Disable Virtual Deliverability Manager, Auto Validation, Dedicated IP Pools, and Tenant Management
        - Complete necessary Open Tasks
            - Confirm your email with SES by following the confirmation email link
            - Send a test email using the SES mailbox simulator
            - Verify sending domain by publishing all DNS records in SES > Configuration Identities > your domain (DKIM, MAIL FROM, and DMARC)
            - Request production access (type = Transactional, website = your domain)
    - **PTR records on the NLB IPs** (optional). Only matters if you ever
      bypass SES and have the MailStack speak SMTP directly to remote
      MTAs from its own IPs — receiving servers may downgrade or reject
      mail without a matching reverse DNS. Outbound through SES uses
      Amazon's IPs (which already have clean PTRs), so this can be left
      undone unless mail-tester.com or a remote MTA flags it.
        - PTR for an EC2/NLB EIP can only be set by AWS Support. Open a
          case → "Account and billing" → "Service: Elastic Compute Cloud
          (EC2)" → "Reverse DNS for EIP".
        - Provide each NLB IP and the desired PTR
          (`smtp.<public_domain>` for both is fine).
        - Get the current NLB IPs with:
          ```sh
          dig +short smtp.<public_domain>
          ```
        - Note: the NLB IPs are AWS-managed and can change if the NLB is
          replaced — switch to static EIPs (`subnet_mappings` with
          `AllocationId`) before requesting PTR if you care about stability.
- Matrix homeserver (`MatrixStack`)
    - First SSO sign-in to materialize your account. Open
      `https://matrix.<public_domain>` in a Matrix client (Element web
      works); start the SSO flow; sign in via Authentik. Your MXID
      becomes `@<authentik.user.username>:<public_domain>`.
- Matrix → OpenClaw control bot (`OpenClawStack`)
    - The bot account (`@openclaw-bot:<public_domain>`; tracked by
      the bootstrap CR's `BOT_USERNAME` constant in
      `infra/stacks/matrix_stack.py`) is registered automatically
      by a Custom Resource in MatrixStack and its access token
      written to Secrets Manager at `matrix/openclaw-bot-token`. On
      its first start, when the
      `/openclaw-matrix-bot/control-room-id` SSM parameter is unset,
      the bot creates an encrypted DM, invites
      `@<authentik.user.username>:<public_domain>`, and persists
      the new room ID back to SSM. Subsequent restarts pick up the
      same room from SSM.
    - In Element, accept the invite from the bot.
    - **Verify the bot** so Element shows it as a fully trusted
      user. matrix-bot-sdk's bundled rust-crypto adapter doesn't
      implement the SAS dance, so Element's interactive "Verify
      user" hangs against the bot. Instead, run the side-channel
      CLI once:
      ```sh
      bin/matrix-bot-verify '@openclaw-bot:<public_domain>'
      ```
      It prompts for your Element access token (Settings > Help &
      About > Advanced > "Access Token") and your Element recovery
      key (the base58 string Element gave you when you set up
      Secure Backup). It decrypts your `user_signing_private_key`
      out of Synapse's 4S secret storage, signs the bot's master
      cross-signing key with it, and posts the signature back. After
      reloading Element, the bot shows as a fully verified user.
    - Test by sending a message in the control room. The bot
      forwards the body to the OpenClaw loopback gateway and replies
      in-thread. If you see `gateway HTTP 4xx/5xx` replies, the
      gateway HTTP shape in
      [`assets/openclaw_bot/src/openclaw.ts`](./assets/openclaw_bot/src/openclaw.ts)
      may need adjusting for your `openclaw` version's API.
    - To rebind the bot to a different room (e.g. you abandoned the
      first one), run
      `bin/matrix-bot-bind-room '!xxx:<public_domain>'` with the new
      room's Internal ID (Element: room Settings → Advanced →
      "Internal room ID"). The bot needs to be already invited to
      that room from your account.

## Operations

### Renaming the Headscale exit-node user

If you ever want to rename the Headscale user that owns the AWS exit node, there
are a couple manual steps you need to follow.

The `headscale.exit_node.preauthkey_user` config value names the Headscale
user that owns the Tailscale-side preauthkey for the exit node. The
preauthkey itself lives in Secrets Manager at `headscale/exit-node/preauthkey`
and is managed by an `ExitNodePreauthkey` Custom Resource Lambda that
regenerates the key when the stored value isn't a current preauthkey for
that user.

After changing `preauthkey_user` (or otherwise invalidating the user —
deleting them in Headplane, restoring Headscale state from a backup,
etc.) the Lambda will detect the orphan on the next deploy and rotate
the secret automatically. But the **running ECS task** caches the old
preauthkey from its `TS_AUTHKEY` env injection; if the task isn't
otherwise replaced by the deploy, force a new deployment so the fresh
secret value is read at container start:

```sh
aws ecs update-service \
    --cluster <ExitNodeCluster name> \
    --service <ExitNodeService name> \
    --force-new-deployment
```

If the EC2 host's `/var/lib/tailscale` state directory predates the user
rename, terminate the instance to wipe it — the ASG will replace it,
and the fresh container will register cleanly via the new preauthkey.

To clean up the orphaned old user, delete it manually in Headplane
(Headscale's REST API has no idempotent user delete).

### Renaming the project

The umbrella has two distinct identifiers:

- **Repo name** (e.g. `chiiiirrus`) — names the on-disk directory and
  the GitHub repository. Embedded in the README badge URL and in the
  Claude memory directory path. Per-fork; pick whatever you like.
- **Project name** (e.g. `chiiiirs`) — `[foundation].project_name` in
  `config.toml`. Used as the prefix for shared AWS resources whose
  names span the umbrella (currently just the shared `BackupVault`,
  named `<project_name>-backups`).

Anything named after a *service* (e.g. the OpenClaw EC2 instance, the
`mail-efs-backups` BackupPlan, Authentik OIDC scope mappings) is
service-scoped, not umbrella-scoped, and is not affected by this
rename.

#### 1. Rename the GitHub repo and the local checkout

```sh
mv ~/github/<owner>/<old-repo> ~/github/<owner>/<new-repo>
cd ~/github/<owner>/<new-repo>
git remote set-url origin git@github.com:<owner>/<new-repo>.git
```

Then go to GitHub → repo → Settings → "Rename" and apply the new
name. GitHub keeps a 301 redirect from the old URLs, so existing
clones keep resolving — but the README badge embeds the URL literally
and needs an in-tree update (line 3).

If you use Claude Code, its per-project memory directory is derived
from the cwd, so move that too to preserve history:

```sh
mv ~/.claude/projects/-Users-<you>-github-<owner>-<old-repo> \
   ~/.claude/projects/-Users-<you>-github-<owner>-<new-repo>
```

#### 2. Change the project name

Edit `[foundation].project_name` in `config.toml`. The shared
`BackupVault`'s physical name is templated from this value, and
`vault_name` is immutable in CloudFormation — so a rename requires a
**three-deploy dance**, because the BackupPlans in MailStack and
OpenClawStack reference the vault via cross-stack export, and CFN
won't let you delete an export that's still imported:

1. **Drop the consumers' references.** Comment out the `BackupPlan` /
   `add_selection` blocks in `infra/stacks/mail_stack.py` and
   `infra/stacks/openclaw_stack.py`, then deploy:
   ```sh
   bin/cdk deploy MailStack OpenClawStack --exclusively
   ```
   This removes the import side of the export.
2. **Rename the vault.** Deploy FoundationStack — CFN replaces the
   vault under the new name and orphans the old one (it has
   `RemovalPolicy.RETAIN` and pre-existing recovery points anyway):
   ```sh
   bin/cdk deploy FoundationStack
   ```
3. **Restore the consumers.** Revert the BackupPlan removal from step
   1 and redeploy. CDK now needs FoundationStack to re-emit the
   export (it's only emitted when something imports it), so include
   it in the deploy:
   ```sh
   bin/cdk deploy FoundationStack MailStack OpenClawStack --exclusively
   ```

The old `<old-project>-backups` vault lingers with its recovery points
intact. AWS Backup refuses to delete a non-empty vault, so wait for
your longest retention window to age out (90 days for OpenClaw's
monthly rule × 3) and then delete it via the AWS Backup console. No
new backups land in the old vault during or after the migration.

If a prior failed attempt already created the new-named vault, CFN
will refuse to re-create it ("Backup vault with the same name already
exists"). Delete the empty orphan first:

```sh
bin/aws backup delete-backup-vault --backup-vault-name <new-project>-backups
```

## Manual Bootstrapping

If you don't want to use the automated bootstrapping script, you can perform
each of its steps yourself. To do so, you can either run the associated helper
script for each step, or take matters into your own hands.

1. Install `dotslash` on your system.
    - Manual install
        - [instructions](https://dotslash-cli.com/docs/installation/)
    - Helper script
        - `bash scripts/bootstrap/dotslash.sh`
2. Create a public hosted zone for your domain in AWS.
    - AWS console
        - Route 53 > Hosted Zones > Create hosted zone
        - Fill in your domain, select "Public hosted zone", then "Create hosted zone"
    - Helper script
        - `bin/aws-create-hosted-zone DOMAIN`
3. Bootstrap CDK in every region a stack deploys to. Most stacks live
   in your default region; SiteStack is pinned to `us-east-1` because
   CloudFront's ACM certificate has to live there. Skip a region and
   `bin/cdk deploy` will fail with `SSM parameter
   /cdk-bootstrap/hnb659fds/version not found`. Each call is idempotent
   — no-ops if the `CDKToolkit` stack already exists.
    - Helper script
        - `bin/cdk bootstrap aws://ACCOUNT_ID/DEFAULT_REGION`
        - `bin/cdk bootstrap aws://ACCOUNT_ID/us-east-1`
4. Create persistent secrets used by AWS services.
    - AWS console
        - TODO
    - Helper script
        - `bin/aws-write-secret ecr-pullthroughcache/ghcr --template='{"username":"GITHUB_USERNAME"}' --key=accessToken  # GitHub PAT with read:packages scope`
        - `bin/aws-write-secret ecr-pullthroughcache/dockerhub --template='{"username":"DOCKERHUB_USERNAME"}' --key=accessToken  # Docker Hub PAT`
        - `bin/aws-write-secret authentik/secret-key --template='{}' --key=secret --length=50 --exclude-punctuation`
        - `bin/aws-write-secret authentik/bootstrap --template='{"email":"EMAIL"}' --key=password`
        - `bin/aws-write-secret data/database --template='{"username":"USERNAME"}' --key=password`
          (RDS master; read by the `DataStack` init Lambda only — no service uses it)
        - `bin/aws-write-secret authentik/database --template='{"username":"authentik"}' --key=password --length=32 --exclude-punctuation`
        - `bin/aws-write-secret headscale/database --template='{"username":"headscale"}' --key=password --length=32 --exclude-punctuation`
        - `bin/aws-write-secret authentik/oidc/tailscale --template='{"client_id":"CLIENT_ID"}' --key=client_secret`
          (values from the `tailscale` provider registered in Authentik)
        - `bin/aws-write-secret authentik/oidc/headscale -`
          (JSON blob: `{"client_id":"...","client_secret":"..."}` — blueprint
          seeds both into Authentik on first apply)
        - `bin/aws-write-secret authentik/oidc/headplane -`
          (same shape as headscale)
        - `bin/aws-write-secret authentik/oidc/vaultwarden -`
          (same shape as headscale)
        - `bin/aws-write-secret authentik/oidc/rspamd -`
          (same shape as headscale; consumed by the MailStack internal ALB to gate the rspamd web UI)
        - `bin/aws-write-secret authentik/oidc/roundcube -`
          (same shape as headscale; consumed by the WebmailStack public ALB to gate Roundcube)
        - `bin/aws-write-secret headscale/noise-private-key --template='{}' --key=secret --bytes=32`
        - `bin/aws-write-secret headplane/cookie-secret --template='{}' --key=secret --bytes=32`
        - `echo -n pending | bin/aws-write-secret headscale/admin-api-key --template='{}' --key=secret -  # sentinel placeholder; replaced by HeadscaleStack`
        - `bin/aws-write-secret vaultwarden/database --template='{"username":"vaultwarden"}' --key=password --length=32 --exclude-punctuation`
        - `bin/aws-write-secret vaultwarden/admin-token --template='{}' --key=secret --length=64 --exclude-punctuation`
        - `bin/aws-write-secret mail/postmaster-password --template='{}' --key=secret --length=32 --exclude-punctuation`
        - `bin/aws-write-secret mail/users/USERNAME --template='{}' --key=secret --length=32 --exclude-punctuation  # one per `[mail].users` entry; init container reads `mail/users/<name>` at boot`
        - `echo -n pending | bin/aws-write-secret mail/dkim-private-key --template='{}' --key=secret -  # sentinel placeholder; the MailStack DKIM Custom Resource replaces it on first deploy`
        - `mail/ses-relay` is a multi-step setup, so it has no single
          `aws-write-secret` line. The manual equivalent of what
          `bin/bootstrap` does:
          ```sh
          # 1. IAM user dedicated to SES SMTP submission. The name must
          #    match `[mail.relay].iam_user_name` in config.toml.
          bin/aws iam create-user --user-name "$IAM_USER_NAME"
          bin/aws iam attach-user-policy --user-name "$IAM_USER_NAME" \
              --policy-arn arn:aws:iam::aws:policy/AmazonSESFullAccess
          # 2. Mint an access key. Capture both fields from the JSON.
          bin/aws iam create-access-key --user-name "$IAM_USER_NAME"
          # 3. Derive the SES SMTP password from the secret access key
          #    via the documented HMAC-SHA256 algorithm. The helper in
          #    scripts/bootstrap/aws_resources.py is the simplest way:
          bin/python -c "
          import sys
          sys.path.insert(0, 'scripts')
          from bootstrap.aws_resources import derive_ses_smtp_password
          print(derive_ses_smtp_password('$SECRET_ACCESS_KEY', '$REGION'))"
          # 4. Write {access_key_id, smtp_password} into Secrets Manager.
          echo "$SMTP_PASSWORD" | bin/aws-write-secret mail/ses-relay \
              --template="{\"username\":\"$ACCESS_KEY_ID\"}" --key=password -
          ```
5. SES domain setup (also handled automatically by `bin/bootstrap`):
    - `aws ses verify-domain-identity --domain DOMAIN`
      (publish the returned token at `_amazonses.DOMAIN` as a TXT record)
    - `aws ses verify-domain-dkim --domain DOMAIN`
      (publish each of the three returned tokens as
      `<token>._domainkey.DOMAIN` CNAME records pointing to
      `<token>.dkim.amazonses.com`)
    - SES production access (and optional PTR records) require AWS
      Support tickets and can't be automated — see the "Mail server"
      bullet under [Post-Deploy Setup](#post-deploy-setup) for the
      step-by-step.

## Secrets Format Convention

All single-value secrets (passwords, tokens, keys) are stored as JSON objects
with a `"secret"` key rather than as bare strings:

```json
{"secret": "the-actual-value"}
```

Multi-value secrets (credentials with username + password, OIDC clients, etc.)
continue to use their natural field names (`"username"`, `"password"`,
`"client_id"`, `"client_secret"`, etc.).

### Why JSON for single values?

AWS Secrets Manager assigns every secret a random 6-character suffix in its
ARN (e.g., `my-secret-AbCdEf`). CDK's ECS secret grant uses the wildcard
pattern `my-secret-??????` to match it.

When ECS resolves a container secret, it calls `GetSecretValue` with either:

- **A JSON field ref** (`name:field::`): Secrets Manager resolves the name to
  the full ARN first, then the IAM check is against the full ARN —
  `my-secret-??????` matches `my-secret-AbCdEf`. ✓
- **A bare name or partial ARN**: the IAM check is against the partial ARN
  (`my-secret`) — `my-secret-??????` requires 6 extra characters and does not
  match `my-secret` (zero extra characters). ✗

Wrapping every single-value secret in `{"secret": "..."}` and referencing it
with `ecs.Secret.from_secrets_manager(secret, "secret")` ensures the first
(working) resolution path is always used, without needing to hard-code the
random ARN suffix anywhere in the codebase.

The same applies to init-container shell scripts: passing the **secret name**
(not partial ARN) to `--secret-id` triggers name→full-ARN resolution before the
IAM check, so CDK's grants work correctly there too.

## Development

### Pre-commit hook

Install the git pre-commit hook (symlinks `bin/pre-commit` into `.git/hooks/`):

```sh
bin/pre-commit --install
```

### Validators

Run validators against the whole repo (or a file/dir/glob subset):

```sh
bin/validate              # all tracked files
bin/validate --fix        # apply fixers then check
bin/validate infra/       # restrict to a subtree
bin/validate --dirty      # only staged files
```

Per-directory validator scoping is driven by `.validator.toml` files. The
repo-root file enables `python-black` and `python-pyright` on all `.py` files;
nested `.validator.toml` files can narrow or extend that config.

### Tests

Run tests against the whole repo:

```sh
bin/test
```

### Other useful commands

```sh
bin/cdk ls          # list all stacks in the app
bin/cdk synth       # emits the synthesized CloudFormation template
bin/cdk deploy      # deploy this stack to your default AWS account/region
bin/cdk diff        # compare deployed stack with current state
bin/cdk docs        # open CDK documentation
```

## Organization

CDK organizes infrastructure into "stacks". Each stack has its own name, and can
be deployed individually. Each stack is composed of one or more "constructs",
which are either individual infrastructure resources, or collections of
infrastructure resources. The relationships between a parent stack to its child
constructs, and parent constructs to their child constructs creates a resource
dependency tree. Constructs may declare relationships to other constructs across
branches of that tree, or even into the trees of other stacks, forming a rich
DAG. But they can never declare a cyclical dependency.

 ## Contents

### AWS

- [Foundation Stack](./infra/stacks/foundation_stack.py)
    - Account-wide shared infrastructure every other stack imports.
    - Resources:
        - VPC (public + private-with-egress + private-isolated subnets, no NAT)
        - Public Route53 hosted zone for `<public_domain>`
        - Private Route53 hosted zone for `<private_domain>` (VPC-scoped)
        - ECS cluster (shared by every Fargate service)
        - AWS Backup Vault (`<project_name>-backups`) every EFS plan
          writes into
        - Two ECR pull-through cache rules + their credential secrets
          (GHCR for `juanfont/headscale` etc., Docker Hub for the rest),
          wrapped by the `PullThroughCacheRule` construct
- [Data Stack](./infra/stacks/data_stack.py)
    - Shared Postgres for stateful services (Authentik, Headscale,
      Vaultwarden, Matrix).
    - Resources:
        - RDS PostgreSQL 16 instance in a private-isolated subnet, 14-day
          automated snapshot retention
        - A CDK custom resource (Provider + Lambda using `pg8000`) that
          creates each logical database named in the stack's
          `databases=[...]` list if it doesn't exist yet, owned by a
          per-DB role whose password lives in `<X>/database` Secrets
          Manager entries
- [Authentik Stack](./infra/stacks/authentik_stack.py)
    - OIDC identity provider for every other SSO-aware service.
    - Resources:
        - Storage: S3 media bucket, dedicated logical DB on the shared RDS
        - Services: Authentik server + worker Fargate tasks
        - Network: public ALB at `auth.<public_domain>`
        - Blueprints: `assets/authentik/blueprints/*.yaml` are deployed
          to an S3 bucket on each stack update; an init container on
          both the server and worker tasks `aws s3 sync`s them onto a
          shared volume on container start. `_stamp_blueprints` hashes
          the YAML + AK_BP_* env vars so any input change flips the
          stored blueprint hash and forces the worker to reapply
        - Provisions OIDC applications + groups + the primary user
          declaratively via those blueprints (one blueprint per
          downstream service: tailscale, headscale, headplane,
          vaultwarden, matrix, rspamd, roundcube)
- [WebFinger Stack](./infra/stacks/webfinger_stack.py)
    - Lambda + HTTP API Gateway serving the WebFinger discovery
      protocol (RFC 7033) for the apex. Tailscale's OIDC discovery
      flow needs `<public_domain>/.well-known/webfinger?resource=...`
      to point at Authentik; this is the smallest thing that does
      that.
    - Resources:
        - Lambda (`infra/lambdas/webfinger`) that returns a static
          JSON response pointing at `auth.<public_domain>`
        - HTTP API Gateway in front of the Lambda (regional endpoint)
    - Exports: `api_invoke_domain` — passed into `ApexEdgeStack` via
      app_builder as an `ApexBehavior` so CloudFront forwards
      `/.well-known/webfinger*` to this API
- [Headscale Stack](./infra/stacks/headscale_stack.py)
    - Self-hosted Tailscale control server + Headplane admin UI +
      always-on Tailscale exit-node.
    - Resources:
        - Services:
            - `headscale` Fargate service
            - `headplane` Fargate service
            - `aws-exit` Fargate service running the Tailscale
              userspace daemon as a network-namespace exit-node so
              clients can route VPC-internal traffic through it
        - Network: public ALB serving both `headscale.<public_domain>`
          and `headplane.<public_domain>` via host-header routing; a
          CloudMap private namespace (`headscale.local`) wires
          Headplane → Headscale internally
        - Storage: dedicated logical DB on the shared RDS
        - Init (via `SharedVolumeInit`):
            1. noise private key materialized from
               `headscale/noise-private-key` onto a task-scoped volume
               by an aws-cli init container, then read by the headscale
               container at `${NOISE_KEY_PATH}`
            2. headplane config rendered into `/etc/headplane/config.yaml`
               from a similar init container (script under
               `assets/headscale/headplane-config-init.sh`)
        - Custom resources:
            - `headscale/admin-api-key`: populated by a one-shot
              Fargate task that runs `headscale apikeys create`
              the first time the stack is deployed
            - exit-node preauthkey: another one-shot task that issues
              a Headscale preauthkey, stores it in
              `headscale/exit-node/preauthkey`, and that the `aws-exit`
              service reads at startup
    - MagicDNS base domain:
      `{headscale.private_subdomain}.{foundation.private_domain}`
      (e.g. `ts.example.net`)
- [Matrix Stack](./infra/stacks/matrix_stack.py)
    - Self-hosted Matrix Synapse homeserver with Authentik SSO.
      `server_name` is the apex (so MXIDs are `@user:<public_domain>`),
      with the actual Synapse listener at `matrix.<public_domain>` and
      `.well-known/matrix/{server,client}` discovery served from
      ApexEdgeStack.
    - Resources:
        - Service: single Fargate task running `matrixdotorg/synapse`,
          public ALB at `matrix.<public_domain>` (both Client-Server
          API + federation share the one HTTP listener)
        - Storage:
            - dedicated logical DB on the shared RDS
            - EFS for `/data` (signing key, media store, generated
              `homeserver.yaml`, registration shared secret), backed up
              into the shared BackupVault on a daily/weekly/monthly
              schedule
        - Init container: renders `homeserver.yaml` from the env-injected
          DB password + Authentik OIDC client secret, plus a signing
          key + macaroon/form/registration secrets that are generated
          on first boot and persisted to EFS. Script lives at
          `assets/matrix/init.sh`
        - Custom resource: a one-shot Fargate task registers an
          `@openclaw-bot:<public_domain>` user via Synapse's
          shared-secret-nonce admin API, logs in to mint an access
          token, and writes the token + user_id + device_id to
          `matrix/openclaw-bot-token` Secrets Manager. The CR's
          trigger property is a content-derived hash so any change to
          the bootstrap script or the Lambda code naturally re-fires
          the registration. Script lives at `assets/matrix/bootstrap.sh`
        - OIDC SSO via Authentik (one OIDC provider blueprint
          provisions Matrix as an Authentik application; users sign
          into Element via the apex Authentik flow)
        - `bin/db-sql matrix` opens an ECS-Exec shell on the Synapse
          container with DB env vars in scope for ad-hoc SQL surgery
- [Mail Stack](./infra/stacks/mail_stack.py)
    - Self-hosted `docker-mailserver` (Postfix + Dovecot + Rspamd +
      ClamAV) on Fargate, with AWS SES as the outbound relay.
    - Resources:
        - Services: single Fargate task at `smtp.<public_domain>` with
          ports 25 / 465 / 587 / 993 fronted by a public NLB
        - Storage: encrypted EFS with three access points
          (`mail`, `config`, `clamav`); daily/weekly snapshots into
          the shared `BackupVault` from FoundationStack
          (`<foundation.project_name>-backups`)
        - Init container (one task, several jobs, each idempotent):
            1. fetches the DKIM private key from
               `mail/dkim-private-key` onto EFS at the rspamd-expected
               path (`rspamd/dkim/s1.key`),
            2. hashes `mail/postmaster-password` plus each
               `mail/users/<name>` secret and writes them to
               `postfix-accounts.cf` (one row per `[mail].users` entry
               plus postmaster),
            3. writes a `postfix-main.cf` override adding the VPC CIDR
               to `mynetworks`, a `postfix-master.cf` override that
               re-adds `permit_mynetworks` to the submission service so
               in-VPC clients (Authentik, Vaultwarden) submit on 587
               without SASL, and a rspamd
               `worker-controller.inc` override that binds the rspamd
               web UI to `0.0.0.0:11334` and skips its built-in auth
               for VPC traffic (the internal ALB's Authentik OIDC
               action is the gate),
            4. issues / renews the Let's Encrypt cert via DNS-01 against
               Route53 using `lego` (renew if <30 days from expiry)
        - Rspamd web UI: internal ALB at `rspamd.<public_domain>`
          (private IP, public DNS so Tailscale clients can resolve it
          via Headscale's exit-node tunnel). HTTPS listener fronts the
          mail task's port 11334 behind an Authentik-OIDC action.
          Members of the `rspamd` Authentik group can review the spam
          quarantine and rspamd stats.
        - DKIM: a Custom Resource Lambda generates an RSA-2048 keypair
          on first deploy, stores the private key in
          `mail/dkim-private-key`, and emits the public key as a Route53
          TXT record at `s1._domainkey.<public_domain>`. Subsequent
          deploys are idempotent (re-derives the public key from the
          stored private key).
        - SES relay: a dedicated IAM SMTP user
          (`{mail.relay.iam_user_name}`) is created by the bootstrap
          script; the secret access key is converted to a SES SMTP
          password via the documented HMAC-SHA256 algorithm and stored
          at `mail/ses-relay`. The mail container reads it as
          `RELAY_USER` / `RELAY_PASSWORD` and Postfix relays all
          outbound through `email-smtp.<region>.amazonaws.com:587`.
        - Monthly EventBridge schedule → tiny
          `ecs:UpdateService --force-new-deployment` Lambda guarantees
          the init container (and therefore the Let's Encrypt renewal)
          runs at least once a month even if nothing else touches the
          stack.
        - DNS published in the public hosted zone:
            - `A smtp` aliased to the NLB,
            - `MX @` → `smtp.<public_domain>`,
            - `TXT @` SPF (`v=spf1 include:amazonses.com -all`),
            - `TXT _dmarc` (`p=quarantine`),
            - `TXT s1._domainkey` (DKIM, set by the Custom Resource),
            - `TXT _amazonses` and 3× `*._domainkey` CNAMEs for SES
              identity verification (set by `bin/bootstrap`).
    - Out-of-band steps that the bootstrap script can't automate are
      documented under [Post-Deploy Setup](#post-deploy-setup) → "Mail
      server" (SES production-access ticket; PTR records).
- [OpenClaw Stack](./infra/stacks/openclaw_stack.py)
    - Personal-assistant agent platform + the Matrix bot that talks
      to it over E2EE rooms.
    - Resources:
        - Service: single EC2 instance running the openclaw daemon
          (gateway listening on a WebSocket-RPC loopback, agents
          spawning subprocesses as needed) and the openclaw-matrix-bot
          systemd user unit alongside it
        - Storage: encrypted EFS mounted at `/data`, holding both
          openclaw agent state (sessions, memory, workspaces,
          auth-profiles) and the matrix bot's `cross-signing.json`.
          `RemovalPolicy.RETAIN` so stack destroys don't nuke any of
          that. Backup plan writes daily/weekly/monthly into the
          shared BackupVault.
        - Network: dedicated single-subnet public VPC (not the
          foundation VPC). No inbound rules on the instance SG; SSM
          Session Manager is the only operator access path.
        - User-data: bootstrap script lives at
          `assets/openclaw/user-data.sh.tmpl`; CDK substitutes
          paths + asset URIs + EFS id and hands the rendered string
          to `ec2.UserData.custom()`
        - Matrix bot: source under `assets/openclaw_bot/` is shipped
          as an S3 asset, downloaded + built (`pnpm install` pinned
          to 10.18.3 + `tsc`) on each fresh instance. The bot reads
          its access token from `matrix/openclaw-bot-token`, the
          control-room id from `/openclaw-matrix-bot/control-room-id`
          SSM parameter, and the openclaw gateway token from
          `gateway-token` (extracted from the daemon's state JSON by
          the prestart helper). Connects to Synapse as
          `@openclaw-bot:<public_domain>` and proxies messages from
          the allowlisted operator MXID to `openclaw agent --agent
          main` subprocess invocations.
    - Notes:
        - This service is high-trust by virtue of running an LLM
          agent that can hit the openclaw `exec` tool, so it lives
          in its own VPC and the operator-access path is SSM-only.
        - The matrix bot is provisioned by a one-shot Custom Resource
          in MatrixStack, not here -- see Matrix Stack above. The
          Lambda registers `@openclaw-bot` and writes its access
          token to Secrets Manager; this stack just consumes those.
- [Apex Edge Stack](./infra/stacks/apex_edge_stack.py)
    - Apex CloudFront distribution + S3 origin bucket + ACM cert +
      apex Route53 records. Pinned to `us-east-1` (CloudFront +
      ACM-for-CloudFront live there only).
    - Resources:
        - Storage: private S3 bucket served via Origin Access Control
        - CDN: one CloudFront distribution. The default behavior
          serves `assets/site/` from the bucket. Other behaviors are
          contributed declaratively by `app_builder.py` via an
          `ApexBehavior` list (currently:
          `/.well-known/webfinger*` → WebFinger API Gateway). Other
          static content is contributed via an `ApexContentDeployment`
          list (currently: Matrix's `.well-known/matrix/{server,client}`
          discovery JSON).
        - TLS: ACM cert validated via DNS against the public hosted
          zone, covering apex + `www.` SAN.
        - DNS: Route53 A-record aliases at apex and `www.`
    - Notes:
        - ApexEdgeStack itself does NOT know which services
          contribute behaviors / content; everything flows through
          app_builder's declarative lists. To add another apex-served
          service, add an entry to those lists in `app_builder.py`,
          not to this stack.
        - The point of this stack is to give `<public_domain>/`
          something real on the apex. AWS Support reviews the SES
          production-access request against the URL you supply on
          the form, and a bare apex that returns 404 reads as
          suspicious.
- [Vaultwarden Stack](./infra/stacks/vaultwarden_stack.py)
    - Self-hosted Bitwarden-compatible password vault with Authentik
      SSO.
    - Resources:
        - Storage: EFS `/data` (encrypted, RETAIN, with backup plan),
          dedicated logical DB on the shared RDS
        - Services: single Fargate task (`vaultwarden/server`) pulled
          via the Docker Hub pull-through cache
        - Network: public ALB at `vaultwarden.<public_domain>`
        - OIDC: SSO via Authentik. `SIGNUPS_ALLOWED=true` +
          `SSO_ONLY=true` -- any Authentik-authenticated user can
          register on first SSO login, password login + signup are
          fully disabled
        - `bin/db-sql vaultwarden` opens an ECS-Exec shell on the
          container with DB env vars in scope
- [Webmail Stack](./infra/stacks/webmail_stack.py)
    - Roundcube webmail at `mail.<public_domain>`.
    - Resources:
        - Services: single Fargate task (`roundcube/roundcubemail`)
          pulled from the Docker Hub pull-through cache, talking to
          MailStack on IMAPS:993 + STARTTLS:587 via the public NLB
        - Storage: a `RoundcubeAp` access point on the existing
          MailStack EFS, mounted at `/var/roundcube` for sqlite +
          config (covered by the same backup plan as the rest of the
          mail volume)
        - Network: public ALB with an Authentik-OIDC action gating
          all traffic; after SSO, the user signs into Roundcube with
          their IMAP password (`mail/users/<name>`). Two-step today;
          collapsing it into a single-step SSO is a tracked follow-up.
- Planned private AWS stacks:
    - searXNG
- Planned homelab hosting:
    - ownCloud / NextCloud
    - Gitea or Forgejo
