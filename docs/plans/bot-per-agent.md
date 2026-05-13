# Bot-per-agent: multiple Matrix-bot ↔ openclaw-agent pairs

## Context

Today there's one bot user (`@openclaw-bot`), one openclaw agent
(`main`), one systemd unit. The operator wants to grow this into
N distinct agents (Wadsworth, Sebastian, Binx, plus more later)
each appearing as their own Matrix user, with rooms partitioned
by who's allowed to talk to whom:

- Sebastian: Chris's private assistant; sees Chris-only rooms,
  can hold sensitive context (e.g. planning a partner surprise).
- Binx: same for Chelsea.
- Wadsworth: shared; both Chris and Chelsea can talk to him in
  shared rooms (game-night planning, etc.).
- Eventually 5–10 more agents for narrower roles.

The repo should set up the *building blocks*; specific rooms +
who-talks-to-whom topology is operator state, not infrastructure.
Concretely: adding a new agent should be a one-line config change
in `config.toml` + `cdk deploy`, not a code change.

## Strategy

**One bot user per agent, one systemd unit per agent, one
openclaw workspace per agent, all driven from a list in
config.toml.** Run them on the same EC2 host as today — N user
units cost ~100 MB RAM each at the matrix-bot-sdk's current
resting state. Element-Web sees N distinct verified MXIDs;
operator addresses each one normally (@-mention, DM, invite into
shared rooms).

Why not the Matrix appservice pattern: see
[matrix-appservice.md](./matrix-appservice.md). Summary: E2EE
support for appservices is still rough; bot users with their own
devices + cross-signing keys are the lowest-surprise path today.

## Building blocks

### 1. Config model

New `infra/models/openclaw_agent_config.py`:

```python
@dataclass(frozen=True)
class OpenClawAgentConfig:
    # Short id used everywhere: systemd unit name, openclaw
    # agent id, matrix bot localpart, EFS subdir.
    id: str
    # MXID localpart for this agent's bot user. Usually matches
    # `id` but kept separate for cosmetic flexibility
    # (e.g. id=`sebastian-v2`, localpart=`sebastian`).
    mxid_localpart: str
    # Operator MXIDs allowed to talk to this agent. The bot
    # refuses messages from anyone not on this list.
    allowed_senders: list[str]
    # Display name shown in member lists / @-mentions.
    display_name: str
```

`config.toml` gains `[[openclaw.agents]]` entries:

```toml
[[openclaw.agents]]
id = "wadsworth"
mxid_localpart = "wadsworth"
display_name = "Wadsworth"
allowed_senders = ["@chris:<domain>", "@chelsea:<domain>"]

[[openclaw.agents]]
id = "sebastian"
mxid_localpart = "sebastian"
display_name = "Sebastian"
allowed_senders = ["@chris:<domain>"]

[[openclaw.agents]]
id = "binx"
mxid_localpart = "binx"
display_name = "Binx"
allowed_senders = ["@chelsea:<domain>"]
```

### 2. Bootstrap CR (MatrixStack) loops over agents

Today `matrix_bot_account` Lambda registers exactly one bot.
Refactor to take an `agents` list in the CR property and:

- For each agent, register `@<localpart>:<domain>` via the
  shared-secret nonce flow.
- Mint an access token via `/login`.
- Write the per-agent token to `matrix/openclaw-bot/<id>-token`
  Secrets Manager.
- Set the display name (the bot can do this itself on first
  start; or the CR can do it via `/profile/<mxid>/displayname`).

The `Trigger` property already comes from `hash_inputs(...)` over
the bootstrap script + Lambda code + env. Add the agent list to
the hash so adding/removing an agent re-fires the CR.

Skip-if-already-registered logic: per-agent token lookup, same
`_current_token()` guard as today, just keyed on `<id>-token`.

### 3. OpenClaw provisioning (user-data) loops over agents

Today `user-data.sh.tmpl` provisions `main`. Add a per-agent
loop:

```sh
for AGENT_ID in @@AGENT_IDS@@; do
  sudo -iu ubuntu XDG_RUNTIME_DIR=... openclaw agents add "$AGENT_ID" \
    --workspace "/data/openclaw/workspaces/$AGENT_ID" \
    --auth-choice skip ...
done
```

`@@AGENT_IDS@@` substitution is just `" ".join(a.id for a in
agents)`.

Each agent gets its own workspace, auth-profile, session store,
memory — full isolation by openclaw's native pattern.

### 4. Systemd unit template, one per agent

Today's `openclaw-matrix-bot.service` becomes
`openclaw-matrix-bot@.service` (systemd's instantiated-unit
template syntax), or just N separately-rendered unit files. Each
unit:

- `Environment=OPENCLAW_AGENT_ID=<id>`
- `Environment=BOT_TOKEN_SECRET_ID=matrix/openclaw-bot/<id>-token`
- `Environment=CONTROL_ROOM_PARAM=/openclaw-matrix-bot/<id>/control-room-id`
- `Environment=MATRIX_BOT_DATA_DIR=/data/matrix-bot/<id>` (so
  each agent's E2E + crypto-store + cross-signing.json stay
  separate)
- `Environment=ALLOWED_SENDERS=<comma-joined>` (multi-MXID
  allowlist)

User-data writes N unit files (one per agent), enables + starts
each.

### 5. Bot code generalization

`assets/openclaw_bot/src/index.ts`:

- Drop `ALLOWED_SENDER` (singular), read `ALLOWED_SENDERS`
  (comma-list).
- `forwardToGateway` already takes `agentId`; reads it from
  `OPENCLAW_AGENT_ID` env (today defaults to "main"; with the
  refactor, the env var becomes required).
- Membership check + sender check work as today, just iterate
  the allowlist instead of single-compare.
- Each bot's `dataDir` becomes `/data/matrix-bot/<id>/`.
  cross-signing.json + crypto-store live in there per agent.
- Control room: per-agent SSM param. First-start bootstrap
  creates the agent's own DM with the first allowed-sender,
  invites the other allowed-senders if any, persists the room
  id.

### 6. Verification per agent

`bin/matrix-bot-verify '@<localpart>:<domain>'` runs once per
agent post-deploy. Same operator-recovery-key path; just three
runs to verify all three.

## File changes summary

- New: `infra/models/openclaw_agent_config.py`
- Modified: `infra/models/openclaw_config.py` (or wherever
  `OpenClawConfig` lives) — add `agents: list[OpenClawAgentConfig]`
- Modified: `config.toml` — `[[openclaw.agents]]` entries
- Modified: `infra/stacks/matrix_stack.py` — pass agents into
  bootstrap CR properties; hash them in trigger inputs
- Modified: `assets/lambdas/matrix_bot_account/index.py` — loop
  over agents, write N secrets
- Modified: `assets/openclaw/user-data.sh.tmpl` — per-agent
  provisioning loop; N systemd unit files
- Modified: `infra/stacks/openclaw_stack.py` — render N units
  from a template, pass agent list as substitution
- Modified: `assets/openclaw_bot/src/index.ts` — multi-sender
  allowlist; OPENCLAW_AGENT_ID-driven config
- Modified: `bin/matrix-bot-verify` docs — note that it's run
  per-agent

## Open decisions

- **Shared rooms.** Wadsworth in a room with both Chris and
  Chelsea: when either speaks, Wadsworth replies. Easy: room is
  in his allowed set, members include both senders. But should
  Sebastian sometimes be in a room with Wadsworth (the user
  mentioned wanting to "transfer relevant context")? If so, the
  per-agent allowlist needs to include other agents' MXIDs too,
  not just human MXIDs. Wadsworth's allowlist becomes
  `[@chris, @chelsea, @sebastian, @binx]` etc.
- **Per-room session continuity.** Currently `openclaw agent
  --message ...` without `--session-id` creates a fresh session
  each invocation; agent has no per-room memory. Probably want
  to map `room_id → openclaw_session_id` and pass
  `--session-id` so each room is one continuous conversation.
  This is bot-code work, orthogonal to the per-agent
  multiplication.
- **Onboarding partner (Chelsea).** Operator said: "I'll create
  her account in Authentik by hand." Fine, no IaC change there.
  But Chelsea also needs to do the bot-verify dance per agent
  she's allowed to talk to (Wadsworth, Binx). Document this in
  the Post-Deploy section of the README.
- **Scaling ceiling.** Per-bot RAM at idle is ~100 MB
  (matrix-bot-sdk + rust-crypto). 10 agents = 1 GB. t3.small
  has 2 GB. Will probably need to bump to t3.medium or larger
  somewhere between 5–10 agents. Cheap; flag.

## Migration mechanics

When this lands:

1. Add new agent entries to config.toml.
2. `bin/cdk deploy MatrixStack OpenClawStack`.
3. Bootstrap CR registers new MXIDs, writes per-agent tokens.
4. User-data redeploys (instance replacement) and provisions N
   openclaw agents + N systemd units.
5. Each new agent's bot starts, generates fresh cross-signing
   keys (each writes a different `/data/matrix-bot/<id>/cross-signing.json`),
   uploads them to Synapse, creates its own DM with the first
   allowed sender.
6. Operator runs `bin/matrix-bot-verify` once per new agent
   (using their existing recovery key).

The existing `@openclaw-bot` user becomes one of the entries
(rename + retitle to e.g. `main` or `wadsworth`). Reusing its
access token avoids re-onboarding the bot account from scratch.

## Verification

- `cdk diff` shows N new bot-token secrets, one per agent.
- Each agent bot's logs show successful `cross-signing setup
  complete` on first start.
- Each agent appears as a distinct user in Element's member
  list when invited to a room.
- Allowlist enforcement: a message from a non-allowed MXID gets
  silently dropped; bot does not reply.

## Out of scope

- Cross-agent context bridging beyond what openclaw supports
  natively (see "shared rooms" open decision).
- Federated agents (agents from other Matrix homeservers
  participating in our rooms). Not relevant since federation
  is open by default — but no special infrastructure here.
- Replacing matrix-bot-sdk with matrix-rust-sdk. Separate
  migration; outside this plan.
