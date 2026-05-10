from __future__ import annotations

# Python implementation of the OpenClaw Matrix control bot.
#
# Uses mautrix-python (https://github.com/mautrix/python) which
# provides a high-level client framework with first-class E2EE
# and cross-signing support including MSC4190 for replacing keys
# without UIA.
#
# This file is a SCAFFOLD. The mautrix API surface is verified
# against the published docs but specific method names may need
# small adjustments at first run. See <project>/README.md for the
# setup notes.

import asyncio
import json
import logging
import subprocess
import time
from collections import deque
from pathlib import Path

from mautrix.client import Client, EventHandler
from mautrix.crypto import OlmMachine, PgCryptoStateStore, StateStore  # type: ignore[attr-defined]
from mautrix.types import (
    EventType,
    Format,
    MessageEvent,
    MessageType,
    RelatesTo,
    RelationType,
    RoomID,
    StrippedStateEvent,
    TextMessageEventContent,
    UserID,
)

from openclaw_bot.config import Config, load_config
from openclaw_bot.openclaw import forward_to_gateway

log = logging.getLogger("openclaw_bot")


# Simple sliding-window rate limiter. Same shape as the TS bot.
class SlidingWindowLimiter:
    def __init__(self, max_per_window: int, window_seconds: float) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._timestamps: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] >= self._window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


async def bootstrap_control_room(client: Client, cfg: Config) -> RoomID:
    log.info("bootstrapping control room; inviting %s", cfg.allowed_sender)
    room_id = await client.create_room(
        invitees=[UserID(cfg.allowed_sender)],
        is_direct=True,
        name="OpenClaw control",
        topic=(
            "Messages here are forwarded to the OpenClaw loopback "
            "gateway on the EC2 host."
        ),
        # Encryption is enabled via initial_state on the create-room
        # request. mautrix's create_room helper accepts arbitrary
        # initial_state events.
        initial_state=[
            {
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }
        ],
        preset="trusted_private_chat",
    )
    log.info("created %s; persisting to SSM %s", room_id, cfg.control_room_param)
    subprocess.run(
        [
            "aws",
            "ssm",
            "put-parameter",
            "--name",
            cfg.control_room_param,
            "--value",
            room_id,
            "--type",
            "String",
            "--overwrite",
        ],
        check=True,
    )
    return RoomID(room_id)


def register_handlers(
    client: Client,
    cfg: Config,
    control_room_id: RoomID,
    limiter: SlidingWindowLimiter,
) -> None:
    @client.event_handler(EventType.ROOM_MEMBER)  # type: ignore[arg-type]
    async def on_membership(evt: StrippedStateEvent) -> None:
        if evt.state_key != cfg.user_id:
            return
        membership = getattr(evt.content, "membership", None)
        if str(membership) != "invite":
            return
        room_id = evt.room_id
        sender = evt.sender
        if room_id != control_room_id:
            log.warning("rejecting invite to non-allowlisted room %s", room_id)
            try:
                await client.leave_room(room_id)
            except Exception as e:  # noqa: BLE001
                log.warning("leave failed for %s: %s", room_id, e)
            return
        if sender != cfg.allowed_sender:
            log.warning("rejecting invite from non-allowlisted sender %s", sender)
            try:
                await client.leave_room(room_id)
            except Exception as e:  # noqa: BLE001
                log.warning("leave failed for %s: %s", room_id, e)
            return
        await client.join_room(room_id)
        log.info("joined control room %s", room_id)

    @client.event_handler(EventType.ROOM_MESSAGE)  # type: ignore[arg-type]
    async def on_message(evt: MessageEvent) -> None:
        if evt.room_id != control_room_id:
            return
        if evt.sender != cfg.allowed_sender:
            log.warning("ignoring message from non-allowlisted sender %s", evt.sender)
            return
        if evt.sender == cfg.user_id:
            return
        content = evt.content
        if not isinstance(content, TextMessageEventContent):
            return
        if content.msgtype != MessageType.TEXT:
            return

        # E2E enforcement: ROOM must be encrypted. mautrix sets
        # `evt.event_id` on encrypted-then-decrypted events; we
        # double-check the room's encryption state.
        is_encrypted = await client.state_store.is_encrypted(evt.room_id)
        if not is_encrypted:
            log.warning("refusing message in unencrypted room %s", evt.room_id)
            await _reply(
                client, evt, "rejected: this control room must be end-to-end encrypted."
            )
            return

        if not limiter.try_acquire():
            log.warning("rate limit exceeded; dropping %s", evt.event_id)
            await _reply(client, evt, "rate limited; try again in a minute.")
            return

        prompt = (content.body or "").strip()
        if not prompt:
            return
        log.info("forwarding prompt of length %d", len(prompt))

        try:
            response = await forward_to_gateway(
                cfg.gateway_url, cfg.gateway_token, prompt
            )
            await _reply(client, evt, response)
        except Exception as e:  # noqa: BLE001
            log.error("gateway error: %s", e)
            await _reply(client, evt, f"gateway error: {e}")


async def _reply(client: Client, root: MessageEvent, body: str) -> None:
    content = TextMessageEventContent(
        msgtype=MessageType.TEXT,
        body=body,
        format=Format.HTML,
        formatted_body=body,
        relates_to=RelatesTo(
            rel_type=RelationType.THREAD,
            event_id=root.event_id,
        ),
    )
    await client.send_message_event(root.room_id, EventType.ROOM_MESSAGE, content)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
    )
    cfg = load_config()

    # mautrix's Client uses a "state store" abstraction for sync
    # state, joined rooms, encryption status, etc. A SQLite-backed
    # store persists across restarts.
    state_store_path = cfg.data_dir / "mautrix-state.db"

    client = Client(
        mxid=UserID(cfg.user_id),
        base_url=cfg.homeserver_url,
        token=cfg.access_token,
        # The default Client doesn't ship a state store; supply
        # one backed by SQLite via the asyncpg-or-sqlite driver
        # mautrix provides. Concrete class name confirmed at first
        # boot; placeholder here while verifying the import path.
        device_id=cfg.device_id,
    )

    # Bring up the encryption helper. mautrix's `OlmMachine` is
    # the high-level wrapper around python-olm; it handles device
    # key rotation, megolm session sharing, and SAS verification.
    # The `self_sign` flag is what enables automatic cross-signing
    # bootstrap (master/self/user-signing keys uploaded to the
    # server on first start, leveraging MSC4190 to skip UIA).
    # TODO: wire concrete classes once API confirmed at runtime.
    # crypto = OlmMachine(
    #     client,
    #     state_store=...,
    #     ...,
    # )
    # client.crypto = crypto
    # await crypto.share_keys()  # ensure device + cross-signing keys are up
    # await crypto.bootstrap_cross_signing(self_sign=True)

    control_room_id = (
        RoomID(cfg.control_room_id)
        if cfg.control_room_id
        else await bootstrap_control_room(client, cfg)
    )
    log.info("starting bot for %s in %s", cfg.allowed_sender, control_room_id)

    limiter = SlidingWindowLimiter(
        cfg.rate_limit_max_per_window, cfg.rate_limit_window_seconds
    )
    register_handlers(client, cfg, control_room_id, limiter)

    # TODO: wire SAS verification handlers via
    # `client.crypto.add_verification_listener(...)` once the API
    # path is confirmed.

    await client.start(None)
    log.info("bot running")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
