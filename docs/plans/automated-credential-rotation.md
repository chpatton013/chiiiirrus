# Automated credential rotation

## Context

Every long-lived credential the stack uses today was set once at
bootstrap and never rotates. The repo has ~20 secrets in Secrets
Manager spanning:

- **DB passwords** (5: RDS master + four per-service users:
  authentik / headscale / vaultwarden / matrix).
- **OIDC client secrets** (7, all under `authentik/oidc/<app>/`).
- **Service-internal admin tokens** (vaultwarden admin token,
  authentik bootstrap-secret, headplane cookie-secret).
- **Lambda-managed API keys** (headscale admin API key,
  headscale exit-node preauthkey, matrix openclaw-bot token).
- **Cryptographic identity material** (DKIM private key, headscale
  noise private key) — rotation has external consequences (DNS,
  federation handshakes).
- **Derived credentials** (SES SMTP password = HMAC over an IAM
  user's access key).

None of this is high-risk-of-exposure today (everything's in
Secrets Manager, IAM-grant scoped, never logged), but "never
rotate" is a weak posture. Compliance-style frameworks (SOC 2,
HIPAA) call for periodic rotation; pragmatically, the credential
most at risk of exposure-via-misconfiguration is the RDS master
password (one accidental `cdk diff` paste into a logged channel
and it's out).

The repo's existing TODO entry (in the README) calls this out
with the right outline:

> **Secrets Manager hosted rotation**
> (`secret.add_rotation_schedule(..., hosted_rotation=...)`)
> with an EventBridge rule on `secretsmanager:RotationSucceeded`
> firing `ecs:UpdateService --force-new-deployment` so tasks
> recycle.

This plan expands that outline into something buildable.

## Scope

**v1**: DB passwords only. Five secrets, one mechanism, one CDK
construct, one EventBridge consumer.

**v2** (deferred): OIDC client secrets via Authentik's rotation
API + blueprint re-apply.

**v3** (deferred): Lambda-managed API keys + admin tokens. Most
of these already have a "rotation by re-running the managing CR"
path; v3 turns that into a schedule.

**Out of scope**: cryptographic identity (DKIM, noise) — rotation
has external coordination cost (DNS publish for DKIM, federation
re-handshake for noise) that outweighs the benefit for a personal
deployment. Document a manual procedure instead.

## v1 Strategy: DB password rotation

### Mechanism

Secrets Manager has a built-in **hosted rotation** template
for Postgres. CDK exposes it as:

```python
secret.add_rotation_schedule(
    "Rotation",
    hosted_rotation=secretsmanager.HostedRotation.postgres_single_user(
        vpc=foundation.vpc,
    ),
    automatically_after=Duration.days(30),
)
```

The hosted-rotation Lambda is AWS-managed; CDK provisions it in
the specified VPC, gives it ingress to RDS, and wires it to the
secret. Every 30 days the Lambda:

1. Generates a new random password.
2. Updates the Postgres user with `ALTER USER ... PASSWORD`.
3. Writes the new password back to the secret as the current
   value (the previous value becomes `AWSPREVIOUS`).

Both `single_user` and `multi_user` flavors exist:

- **`single_user`** rotates the same role's password. Brief
  window where new sessions get the new password but old in-
  flight sessions hold the old. Services that read the password
  *at task boot* (every ECS task here) keep the old password
  until the next deploy, and connect attempts after rotation
  fail until the task recycles.

- **`multi_user`** alternates between two Postgres roles
  (`<user>_a` / `<user>_b`); only one is the "current" at any
  time. Applications retry-on-auth-fail and pick up the new
  current role. Zero downtime but requires app-side cooperation.

None of the services in this repo retry-on-auth-fail. Going
with **`single_user` + force-new-deployment on rotation** is the
right call. ECS task recycle takes ~30-90s per service; that's
the trade we already accept on every deploy.

### EventBridge → force-new-deployment wiring

When hosted rotation succeeds, Secrets Manager fires a
CloudWatch event:

```json
{
  "source": "aws.secretsmanager",
  "detail-type": "AWS API Call via CloudTrail",
  "detail": {
    "eventName": "RotationSucceeded",
    "additionalEventData": {"SecretId": "..."}
  }
}
```

A small Lambda (mirror of `assets/lambdas/mail_force_redeploy/`)
matches the event and calls `ecs:UpdateService(...,
forceNewDeployment=True)` on the consumer service.

### Construct shape

New `infra/constructs/rotating_db_secret.py`:

```python
class RotatingDbSecret(Construct):
    """Wires hosted Postgres rotation + EventBridge → force-new-
    deployment so consumer ECS services pick up the new password
    automatically after rotation."""

    def __init__(
        self, scope, construct_id, *,
        secret: secretsmanager.ISecret,
        vpc: ec2.IVpc,
        db_security_group: ec2.ISecurityGroup,
        rotate_every: Duration = Duration.days(30),
        force_deploy_services: list[ecs.IBaseService] | None = None,
    ) -> None: ...
```

The construct:

1. Calls `secret.add_rotation_schedule(...)` with the hosted
   template. The rotation Lambda lands in `vpc` (private-with-
   egress subnet) and gets ingress to `db_security_group` on the
   DB port.
2. If `force_deploy_services` is non-empty: provisions a small
   Python Lambda (asset under `assets/lambdas/secret_force_redeploy/`),
   an EventBridge rule keyed on `RotationSucceeded` for this
   specific secret arn, and IAM granting `ecs:UpdateService` on
   each listed service.

Caller pattern in DataStack:

```python
for db_cfg in databases:
    db_secret = secretsmanager.Secret.from_secret_name_v2(...)
    RotatingDbSecret(
        self, f"Rotate-{db_cfg.name}",
        secret=db_secret,
        vpc=foundation.vpc,
        db_security_group=self.database.security_group,
        # Consumer services come from the relevant downstream
        # stack — see "consumer plumbing" below.
        force_deploy_services=...,
    )
```

### Consumer plumbing

DataStack doesn't know which ECS services consume which DB
secret (that's set up in AuthentikStack, MatrixStack, etc.). Two
options:

1. **Centralize in DataStack via cross-stack refs.** Each
   consumer stack exports its service ARN; DataStack imports
   them and wires the EventBridge rule. Complex; introduces a
   cycle (DataStack → consumer stacks today, would also need
   consumer → DataStack for service export).

2. **Decentralize: each consumer stack instantiates its own
   `SecretConsumerForceDeploy` construct** pointing at the
   db_secret it consumes + its own service. The rotation
   schedule still lives in DataStack (single source of truth for
   the secret); each consumer is responsible for redeploying
   itself.

   ```python
   # in AuthentikStack:
   SecretConsumerForceDeploy(
       self, "DbSecretConsumer",
       secret=db_secret,  # the same one DataStack rotates
       services=[server_service.service, worker_service.service],
   )
   ```

**Option 2 is the natural fit.** EventBridge rules can live in
the same stack as their target service; CloudTrail's rotation
event is global, so any rule can listen for it.

The construct split becomes:

- `RotatingDbSecret` (in DataStack): owns the rotation schedule.
- `SecretConsumerForceDeploy` (in each consumer stack): owns
  the EventBridge rule + redeploy Lambda for its own services.

### RDS master secret

DataStack's `data/database` master secret rotates the same way.
No consumer services to redeploy — it's only read at provision
time by the DbInit Lambda. Just `RotatingDbSecret` with empty
`force_deploy_services`.

## v2 (deferred): OIDC client secrets

Authentik is the source of truth for OIDC client secrets. The
Secrets Manager entries (`authentik/oidc/<app>`) are seeded
from `bin/aws-write-secret` at bootstrap; Authentik picks them up
via `!Env AK_BP_<APP>_CLIENT_SECRET` placeholders in blueprints.

Rotation has three pieces:

1. **Generate a new client secret.** Either Authentik's admin API
   (`/api/v3/providers/oauth2/<id>/`) or by editing the blueprint
   and re-applying.
2. **Write the new value to Secrets Manager.** Custom Lambda.
3. **Force the consumer service to re-read.** Same
   force-new-deployment Lambda as v1.

But Authentik's blueprint reconciler is one-way: the blueprint is
the source of truth, the provider's actual client_secret in
Authentik mirrors it. If we want rotation, the blueprint env-var
value has to change, which means changing the secret first, then
re-rendering. The `_stamp_blueprints` hashing already triggers a
worker reapply when the env value changes — so the loop is:

- Lambda generates new random value, writes to
  `authentik/oidc/<app>`.
- AuthentikStack's next CDK deploy (or worker tick) picks up the
  new value via the blueprint env, updates the provider.
- Consumer service force-redeploys to pick up the new value from
  its own secret read.

Two of those steps are synchronous (Secret update, force-deploy);
one is asynchronous (Authentik blueprint reconciliation runs on
its own cadence). Easiest: don't auto-rotate OIDC secrets; add a
`bin/rotate-oidc-secret <app>` script that does all three steps
in order and exits. Manual cadence, but reproducible.

## v3 (deferred): Lambda-managed admin tokens

Three secrets here:

- `headscale/admin-api-key` (lambda-managed by headscale_stack's
  bootstrap CR)
- `headscale/exit-node/preauthkey` (lambda-managed by another CR)
- `matrix/openclaw-bot-token` (lambda-managed by
  matrix_bot_account CR)

Each has a "rotate by setting the secret to `pending` and
re-firing the managing CR" path (the same one used during the
ghost-account cleanup earlier in this project). v3 would wrap
that with a schedule:

```
EventBridge schedule (monthly) →
  Lambda that:
    1. UpdateSecretValue(SecretId=X, SecretString='{"secret":"pending"}')
    2. Invokes the managing CR's Lambda directly with Create/Update
       event payload
    3. Verifies the secret value was re-populated
```

The existing `reissue-custom-resource` skill in the repo
already documents this manual sequence. v3 just schedules it.

Per-token nuances:

- **headscale admin API key**: rotation is cheap (just a new
  random token), no consumer impact (only Headplane reads it,
  and only at task start).
- **exit-node preauthkey**: rotating invalidates the in-use key.
  The aws-exit Fargate service re-registers with the new key on
  next start, but in-flight Tailscale clients connected to the
  exit node aren't affected (they auth via a different
  mechanism).
- **matrix openclaw-bot token**: rotation = delete the bot's
  current access token + re-register via shared-secret nonce.
  The bot service has to restart with the new token. Doable but
  fiddly; defer.

## Out of scope (manual procedures)

Document under README Operations:

- **DKIM key rotation**. Generate new RSA-2048 keypair, store
  private key in `mail/dkim-private-key`, publish new public key
  as TXT record at `s2._domainkey.<public_domain>` (incrementing
  selector), update Postfix config to sign with `s2`, leave `s1`
  TXT record up for 7 days so receivers verifying old mail
  still validate, then remove `s1`. Per-rotation cadence: ~12
  months unless compromise suspected.

- **Headscale noise private key rotation**. Means every
  existing Tailscale client must re-register. Effectively a
  rebuild of the tailnet. Don't rotate; treat as permanent.

- **SES SMTP password rotation**. Requires rotating the
  underlying IAM access key. AWS rotates IAM access keys via
  `aws iam create-access-key` + delete-the-old; the SMTP
  password is then derived from the new access key via HMAC. A
  bin script + a `bin/aws-write-secret mail/ses-relay` followup
  documents it.

## File changes for v1

- New: `infra/constructs/rotating_db_secret.py`
- New: `infra/constructs/secret_consumer_force_deploy.py`
- New: `assets/lambdas/secret_force_redeploy/index.py` (the
  EventBridge-triggered Lambda; mirrors `mail_force_redeploy/`)
- Modified: `infra/stacks/data_stack.py` — wrap each per-DB
  secret with `RotatingDbSecret`; rotate the master separately.
- Modified: consumer stacks (`authentik_stack.py`,
  `headscale_stack.py`, `vaultwarden_stack.py`,
  `matrix_stack.py`) — instantiate
  `SecretConsumerForceDeploy` for their DB secret + their
  service(s).
- Modified: README Operations section — document manual paths
  for DKIM, noise, SES.

## Verification (v1)

- After deploy: AWS Console → Secrets Manager → each
  `<service>/database` shows a "Rotation enabled" status with a
  30-day automatic schedule.
- Force a rotation: `aws secretsmanager rotate-secret
  --secret-id <service>/database`. Wait ~30s.
  - Old `AWSCURRENT` value moves to `AWSPREVIOUS`.
  - New `AWSCURRENT` is a different random string.
  - The Postgres role's password is the new value (verify via
    `bin/db-sql <service>`).
- Within ~60s of rotation: the consumer ECS service's
  CloudWatch logs show a fresh task start; that task picks up
  the new password from its secrets injection and connects
  successfully.
- 30-day automatic: visually check after deploy that the next
  scheduled rotation is ~30 days out.

## Open decisions

- **Rotation interval.** 30 days is the AWS default for hosted
  rotation. 90 days is laxer; weekly is paranoid. Pick 30.
- **Force-redeploy delay.** Multiple services per secret recycle
  in parallel when the EventBridge rule fires. For services
  with `desired_count=1` + `min_healthy_percent=0` that's a
  ~30-90s outage. Acceptable for a personal deployment;
  document.
- **Master secret rotation cadence.** No consumer services to
  recycle, so rotation is essentially free. Could rotate every
  7 days. Probably overkill — match the per-DB cadence.

## What this plan is NOT

- Not a "rotate everything" plan. Cryptographic identity stays
  manual. v1 is just the DB passwords; v2 and v3 are recorded
  but deferred.
- Not a compliance checkpoint. No specific framework is being
  targeted; this is "stop accepting an obvious smell" rather
  than "satisfy SOC 2."
- Not a key-management overhaul. Secrets Manager stays the
  store; we're just turning on its built-in rotation features.
