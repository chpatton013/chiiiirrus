# OpenClaw Matrix bot — Python implementation

**Parallel to `assets/openclaw_bot/`** (TypeScript). The Python
version exists to evaluate whether [mautrix-python](https://github.com/mautrix/python)
gives us cleaner cross-signing + verification handling than
matrix-bot-sdk's bundled rust-crypto-nodejs binding.

## Why Python?

matrix-bot-sdk's rust-crypto-nodejs adapter (v0.4.0) **does not
expose any interactive verification API** — no `acceptVerification`,
no SAS surface. Element's "Verify user" flow hangs against the
bot. We tried migrating to matrix-js-sdk but its Node-side crypto
persistence is officially in-memory only; every restart hits an
OTK-collision wall.

mautrix-python is libolm-based with file-backed persistence
(SQLite). MSC4190 support (added in 0.20.8, 2025-06) lets the
bot replace cross-signing keys without UIA on subsequent uploads.
Synapse v1.152 (our deployment) supports MSC4190.

## Current state: SCAFFOLD

This directory is a structural placeholder. The mautrix-python
import paths and concrete class names need first-boot verification
against the installed package; this README will be updated once
the scaffold is fleshed out and running.

## Scope (when fleshed out)

Same env contract as the TypeScript bot, with one distinction:
this bot uses a **separate** SSM control-room param and Secrets
Manager entry so both bots can run side-by-side during evaluation:

- `BOT_TOKEN_SECRET_ID=matrix/openclaw-bot-py-token`
- `CONTROL_ROOM_PARAM=/openclaw-matrix-bot-py/control-room-id`
- `BOT_USER_ID=@openclaw-bot-py:<public_domain>`

Same external observables:

- Auto-create encrypted DM with the allowed sender, persist room
  ID to SSM.
- Allowlist: single sender, single room.
- Sliding-window rate limit (6 msgs / 60 s).
- Forward decrypted messages to the OpenClaw loopback gateway.
- Reply in-thread.
- Cross-signing identity uploaded on first start (mautrix native).
- SAS verification handler (auto-accept + auto-confirm from
  allowed sender).

## System dependencies

Ubuntu 24.04:

```sh
apt-get install -y libolm3 libolm-dev python3-dev build-essential
```

## Install

```sh
pip install -e . --no-build-isolation
```

(or `uv pip install -e .`)

## Run (locally, for development)

```sh
HOMESERVER_URL=https://matrix.chiiiirs.com \
BOT_ACCESS_TOKEN_FILE=./.token \
BOT_USER_ID='@openclaw-bot-py:chiiiirs.com' \
BOT_DEVICE_ID=ABCDEFGHIJ \
CONTROL_ROOM_PARAM=/openclaw-matrix-bot-py/control-room-id \
ALLOWED_SENDER='@chris:chiiiirs.com' \
MATRIX_BOT_DATA_DIR=./.bot-data \
OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789 \
OPENCLAW_GATEWAY_TOKEN_FILE=./.gateway-token \
python -m openclaw_bot.main
```

## TODO before deploying

- Verify mautrix import paths and class names against the installed
  0.21.x package; correct any drift.
- Wire OlmMachine + state store; confirm `bootstrap_cross_signing(self_sign=True)` is the right call.
- Add verification flow handlers (SAS auto-accept + auto-confirm
  from allowlisted sender).
- Add a bootstrap CR Lambda task for the Python bot (mirrors
  `assets/lambdas/matrix_bot_account/` but registers
  `@openclaw-bot-py`).
- Add an OpenClawStack systemd user unit for the Python bot
  alongside the existing TypeScript one. Different
  `RuntimeDirectory`, different env, different SSM/Secrets
  references.
- Add `libolm3` to `BUILD_DEPENDENCIES` in `openclaw_stack.py`.
