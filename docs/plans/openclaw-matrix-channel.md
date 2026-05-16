# Replace Matrix appservice + matrix-bot with openclaw's native Matrix channel

## Context

We just shipped the matrix-appservice work (`docs/plans/matrix-appservice.md`,
Phases A + B). End-to-end signal flow works: operator -> Element -> Synapse ->
AS -> `openclaw agent --agent <id>` -> reply -> back to the room. The
unencrypted-rooms-only constraint of that approach made it explicitly a
"decide before you build pantalaimon" tool.

While creating the three agents on the openclaw EC2, the wizard surfaced
that openclaw already ships a first-class Matrix channel plugin
(`https://docs.openclaw.ai/channels/matrix`). It uses `matrix-js-sdk` with
Rust crypto, supports multiple accounts per gateway, and natively handles:

- E2EE per account (no pantalaimon needed)
- Threads, reactions, streaming replies, mention gating
- DM / group room policies, allowlists, per-room overrides
- Exec approvals via Matrix reactions
- Stable cross-device credential + crypto storage

The AS approach we built is **functionally subsumed** by this. Keeping it
means maintaining ~500 lines of code that reimplements (poorly) what
openclaw provides natively, missing E2EE, and accepting the operational
cost of pantalaimon later. The plan now is to migrate to openclaw's native
channel and decommission both the AS and the existing matrix-bot.

The intended outcome: matrix-side openclaw integration runs entirely
inside the openclaw daemon. No separate Node processes, no pantalaimon, no
AS YAML, no separate matrix-bot. Three (or more) bot accounts each with
their own real Matrix identity, encryption on by default, configured
once via CDK at deploy time.

## Goal

- Stand up three openclaw accounts -- `@openclaw-wadsworth`,
  `@openclaw-sebastian`, `@openclaw-binx` (see Open Decisions) -- with
  E2EE enabled, allowlisted to the operator's MXID.
- Tear down the matrix-appservice infrastructure (`OpenClawStack`'s
  AS ALB, the `assets/openclaw_appservice/` bundle, the Synapse-side
  AS YAML registration, the two AS-token secrets).
- Decide whether the existing matrix-bot (`@openclaw-bot`) stays as the
  "main" agent or also migrates / gets retired (see Open Decisions).

After the migration: `grep -r appservice infra/ assets/` returns nothing
matrix-related; openclaw's gateway is the only Matrix-facing process on
the EC2; one MXID per agent.

## Open decisions

### D1. Agent MXID naming

The existing convention is `@openclaw-bot`. Extending naturally gives
`@openclaw-wadsworth`, etc. Alternatives: pure agent names (`@wadsworth`)
look cleaner but risk collision with future human accounts and don't read
as "this is a bot."

**Leading candidate: `@openclaw-<agent>`** -- matches the bot pattern, no
risk of collision, immediately recognizable in Element. The lowercase-agent
restriction (Matrix localparts can't have uppercase) is fine since our
agent ids are already lowercase.

### D2. Fate of `@openclaw-bot` + the existing matrix-bot

Options:

| Option | Pros | Cons |
|---|---|---|
| **Keep both**: matrix-bot runs on @openclaw-bot for "main"; openclaw native runs the new three | Zero disruption to existing usage; both code paths get exercised | Two parallel matrix integrations to keep alive |
| **Migrate @openclaw-bot to openclaw native** under the same MXID | Single integration; consistent UX across all agents | Existing matrix-bot's device + crypto state becomes orphan; rooms re-verify the new device |
| **Retire @openclaw-bot entirely**; "main" becomes `@openclaw-main` under the native channel | Clean slate, uniform naming | Existing DMs with the operator go silent under the old MXID; new DM under new MXID |

**Leading candidate: keep both during eval, decide after**. Phase 1-3 of
this plan leaves the matrix-bot untouched. Phase 4 picks a path once the
operator has used both side-by-side for a bit.

### D3. Account registration

Three accounts means three Synapse `register_new_matrix_user` calls. The
existing `assets/lambdas/matrix_bot_account/` Custom Resource does this
for one user. Generalize that Lambda to accept a list of usernames and
register/login each (writing one secret per account), or write a new
Lambda?

**Leading candidate: generalize the existing Lambda**. Same shape
(shared-secret nonce -> register -> login -> persist token), just a loop
over a list. The Custom Resource trigger fires on agent-list change. One
managed-by-CDK secret per account: `matrix/openclaw-account-<agent>-token`.

### D4. Config rendering

openclaw's channel config lives in JSON5 (`~/.openclaw/config.json5`).
The wizard writes interactively; we'd render it at deploy time via
user-data. Each account also accepts env-var overrides
(`MATRIX_<ID>_HOMESERVER`, `MATRIX_<ID>_ACCESS_TOKEN`, etc.) but
policy fields (`dm.allowFrom`, etc.) likely require the config file.

**Leading candidate: CDK renders the full `channels.matrix` section into
the openclaw config file**, populated from a single
`channels.matrix.json5.tmpl` asset. Access tokens come in as runtime
file paths (read by openclaw via `MATRIX_<ID>_ACCESS_TOKEN_FILE`-style
env var, with the file staged by a systemd ExecStartPre helper that
fetches each token from Secrets Manager). This keeps tokens out of
disk-resident config files.

## Building blocks

### 1. Account registration Lambda

`assets/lambdas/matrix_openclaw_accounts/` (or rename
`matrix_bot_account` to handle both). On Create / Update:

- Reads `AGENTS` env var (comma-separated list).
- For each agent, computes the username (`openclaw-<agent>`), checks the
  matching Secrets Manager secret (`matrix/openclaw-account-<agent>-token`).
- If the secret has the placeholder `{"token":"pending"}`, register the
  user via Synapse's shared-secret nonce flow (same code path the
  existing Lambda uses) and persist the access token.
- If the secret already holds a real token (`syt_...`), skip.
- On Delete: no-op (don't deactivate accounts on stack teardown; that's
  destructive).

Idempotent on per-account secret state, like the existing single-account
flow.

### 2. Per-account Secrets Manager secrets

`MatrixStack` (the producer of bot identity in this repo) creates one
secret per agent: `matrix/openclaw-account-<agent>-token`. Pre-seeded
with `{"token":"pending"}` (same bootstrap mechanic as
`matrix/openclaw-bot-token`).

### 3. openclaw config rendering

New asset `assets/openclaw/channels.matrix.json5.tmpl`. Rendered by
user-data's existing `render_template` pipeline. Skeleton:

```json5
{
  channels: {
    matrix: {
      homeserver: "@@MATRIX_HOMESERVER_URL@@",
      encryption: true,
      dm: {
        policy: "allowlist",
        allowFrom: ["@@OPERATOR_MXID@@"],
        sessionScope: "per-user",
      },
      groupPolicy: "allowlist",
      groups: {},
      accounts: {
        wadsworth: { userId: "@openclaw-wadsworth:@@HOMESERVER_NAME@@" },
        sebastian: { userId: "@openclaw-sebastian:@@HOMESERVER_NAME@@" },
        binx: { userId: "@openclaw-binx:@@HOMESERVER_NAME@@" },
      },
      defaultAccount: "wadsworth",
    },
  },
}
```

Access tokens get supplied via env vars at openclaw daemon startup, not
baked into the config file:

```
MATRIX_WADSWORTH_ACCESS_TOKEN=<read from Secrets Manager via prestart>
MATRIX_SEBASTIAN_ACCESS_TOKEN=<...>
MATRIX_BINX_ACCESS_TOKEN=<...>
```

(Verify the exact env-var naming matches what openclaw's matrix channel
recognizes -- the docs mention `MATRIX_<ID>_*` for named accounts. If
the channel can read `*_ACCESS_TOKEN_FILE` paths the way other openclaw
integrations do, prefer the file form so tokens never enter the process
environment.)

### 4. openclaw daemon prestart

New systemd ExecStartPre helper for the openclaw daemon (the one started
by `openclaw onboard ... --install-daemon` already), or extend the
existing helper. Pulls each per-account token from Secrets Manager into
a runtime file, then exports the corresponding env var pointing at that
file.

### 5. CDK shape

- `infra/stacks/matrix_stack.py`: N secrets via the same `_random_token`
  / pre-seeded pattern; one Custom Resource invoking the generalized
  Lambda; trigger hashed on the agent list.
- `infra/stacks/openclaw_stack.py`: pass agent list + per-account
  secret names into the user-data substitution map; grant the instance
  role `secretsmanager:GetSecretValue` on each
  `matrix/openclaw-account-*-token`.

## Sequencing

Five phases, each independently revertable. Phases 3-5 are
destructive; do them only after Phase 2 confirms the native channel
delivers what the AS did plus E2EE.

### Phase 1: scaffolding + one agent

- Generalize the registration Lambda; pre-seed the three secrets.
- Render `channels.matrix.json5` with all three accounts but only
  enable `wadsworth` to start (`accounts.wadsworth`); leave the
  other two stubs commented or absent.
- Deploy MatrixStack -> the Lambda registers `@openclaw-wadsworth` +
  populates its secret.
- Deploy OpenClawStack -> openclaw daemon restarts with the new config;
  matrix channel boots one account.
- Verify: invite `@openclaw-wadsworth:chiiiirs.com` to an **encrypted**
  DM from Element. Confirm the agent responds; confirm Element shows
  the device as verified (or that the verify flow works once).

### Phase 2: add the other two agents

- Add `sebastian` and `binx` to the config + the agent list in
  app_builder.
- Redeploy MatrixStack (Lambda registers the two new accounts) +
  OpenClawStack (config refresh).
- Verify each new MXID by DM as in Phase 1.

### Phase 3: decommission the AS

- Remove from `infra/stacks/openclaw_stack.py`: `AppserviceAlb`, the
  9000 SG ingress, the `MatrixAppserviceAsset` upload, the AS
  systemd unit block in user-data, the per-AS env vars + secrets.
- Remove from `infra/stacks/matrix_stack.py`: the two
  `AppserviceAsToken` / `AppserviceHsToken` secrets, the
  `APPSERVICE_*` env entries + secret refs in `init_environment` /
  `init_secrets`, the `appservice_openclaw_tmpl` read.
- Remove `assets/matrix/appservice-openclaw.yaml.tmpl` and the
  `app_service_config_files` block in `assets/matrix/homeserver.yaml.tmpl`.
- Remove `assets/matrix/init.sh`'s AS-rendering step.
- Remove `assets/openclaw_appservice/` entirely.
- Remove `openclaw-as.<public_domain>` ALB + cert (handled by stack
  destroy) + the Route53 alias (same).
- Deploy MatrixStack + OpenClawStack. The matrix-appservice work
  becomes purely historical.

### Phase 4: pick a fate for `@openclaw-bot`

(See D2. Decision point gated on Phase 1-3 success.)

If migrating: re-register openclaw-bot under the openclaw native
channel as another account; the existing matrix-bot's access token
keeps working until openclaw-bot's secret is rotated. Stop the
matrix-bot systemd unit; remove the bot install dir; openclaw native
handles the existing rooms.

If retiring: similar but skip the rename. Existing DMs go silent.

If keeping both: leave matrix-bot alone; document that as the long-term
state.

### Phase 5: cleanup the bot code path (only if Phase 4 retired/migrated)

- Remove `assets/openclaw_bot/`.
- Remove the bot install + systemd unit blocks in
  `assets/openclaw/user-data.sh.tmpl`.
- Remove the matrix-bot Lambda (`assets/lambdas/matrix_bot_account/`)
  if its functionality is fully subsumed by the new accounts Lambda.
- Remove the legacy MATRIX_BOT_* substitutions and constants in
  `infra/stacks/openclaw_stack.py`.

## Verification

Per phase:

- **Phase 1**: `@openclaw-wadsworth` responds to an encrypted DM,
  Element shows the conversation as encrypted with a verified device.
  `openclaw matrix profile set --account wadsworth --name "Wadsworth"`
  succeeds. Agent's reply lands in the same encrypted thread.
- **Phase 2**: Same for `sebastian` and `binx`. Send a message in a
  group room that mentions two of them; only the mentioned ones
  reply (verifies `requireMention` gating works).
- **Phase 3**: `bin/cdk diff --all` clean.
  `grep -r appservice infra/ assets/matrix/ assets/openclaw_appservice/`
  returns nothing (the asset dir is gone). The AS ALB no longer exists
  in AWS; `openclaw-as.<public_domain>` resolves NXDOMAIN.
- **Phase 4/5**: depending on the chosen path,
  `systemctl --user is-active openclaw-matrix-bot` returns `inactive`
  (or the unit no longer exists), and the legacy bot is no longer
  running.

## Rollback

Phases 1-2 are additive. If the openclaw native channel doesn't work
out, just back out the config -> openclaw stops attempting to log in
those accounts. The Synapse accounts persist (idempotent-on-creation
secrets are easy to clean up manually).

Phase 3 is the irreversible step in the sequence. If the AS turns out
to be needed after all, restore the appservice work from git -- it's
all preserved in history (the matrix-appservice Phase A + Phase B
commits).

Phase 4/5: only proceed once the new path has been validated. The
matrix-bot's access token in Secrets Manager survives a code-only
revert.

## Out of scope

- Group rooms with multiple agents simultaneously, or mixed-agent
  conversations. The native channel supports this via mention gating;
  exercising it can wait until the basic 1:1 path is solid.
- ACP workspace bindings (`/acp spawn --bind here`). Powerful, but
  separate from the migration scope.
- Pantalaimon (was Phase C of `matrix-appservice.md`). The native
  channel makes pantalaimon entirely unnecessary; that whole concern
  goes away when the AS does.
- Federation behavior for the new MXIDs. Synapse's federation is open
  by default; agents become reachable from other Matrix servers if
  any peer initiates a DM. Operator-controlled allowlist policies
  (`dm.policy: "allowlist"`) prevent that from being a problem in
  practice.
- README / docs/plans updates. The matrix-appservice plan should stay
  as-is for historical context; this plan supersedes its execution
  path.
