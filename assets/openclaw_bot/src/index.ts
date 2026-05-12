import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

import {
  MatrixClient,
  RustSdkCryptoStorageProvider,
  RustSdkCryptoStoreType,
  SimpleFsStorageProvider,
} from "matrix-bot-sdk";

import { ensureCrossSigning } from "./crosssigning.js";
import { forwardToGateway } from "./openclaw.js";

interface Config {
  homeserverUrl: string;
  accessToken: string;
  controlRoomId: string | null;
  controlRoomParam: string;
  allowedSender: string;
  dataDir: string;
  agentId: string;
  agentTimeoutSeconds: number;
  rateLimitMaxPerWindow: number;
  rateLimitWindowMs: number;
}

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(`missing required env: ${name}`);
  }
  return v;
}

function readFileEnv(envName: string): string {
  const filePath = requireEnv(envName);
  if (!fs.existsSync(filePath)) {
    throw new Error(`${envName} points at missing file: ${filePath}`);
  }
  return fs.readFileSync(filePath, "utf8").trim();
}

function loadConfig(): Config {
  const dataDir = requireEnv("MATRIX_BOT_DATA_DIR");
  fs.mkdirSync(dataDir, { recursive: true });
  const controlRoomIdRaw = (process.env.CONTROL_ROOM_ID ?? "").trim();
  return {
    homeserverUrl: requireEnv("HOMESERVER_URL"),
    accessToken: readFileEnv("BOT_ACCESS_TOKEN_FILE"),
    controlRoomId: controlRoomIdRaw || null,
    controlRoomParam: requireEnv("CONTROL_ROOM_PARAM"),
    allowedSender: requireEnv("ALLOWED_SENDER"),
    dataDir,
    agentId: process.env.OPENCLAW_AGENT_ID?.trim() || "main",
    agentTimeoutSeconds: Number(
      process.env.OPENCLAW_AGENT_TIMEOUT_SECONDS ?? "120",
    ),
    rateLimitMaxPerWindow: 6,
    rateLimitWindowMs: 60_000,
  };
}

async function bootstrapControlRoom(
  client: MatrixClient,
  cfg: Config,
  log: (level: "info" | "warn" | "error", msg: string) => void,
): Promise<string> {
  log("info", `bootstrapping control room; inviting ${cfg.allowedSender}`);
  const roomId = await client.createRoom({
    preset: "trusted_private_chat",
    invite: [cfg.allowedSender],
    is_direct: true,
    name: "OpenClaw control",
    topic:
      "Messages here are forwarded to the OpenClaw loopback gateway on the EC2 host.",
    initial_state: [
      {
        type: "m.room.encryption",
        state_key: "",
        content: { algorithm: "m.megolm.v1.aes-sha2" },
      },
    ],
  });
  log("info", `created ${roomId}; persisting to SSM ${cfg.controlRoomParam}`);
  execFileSync(
    "aws",
    [
      "ssm",
      "put-parameter",
      "--name",
      cfg.controlRoomParam,
      "--value",
      roomId,
      "--type",
      "String",
      "--overwrite",
    ],
    { stdio: "pipe" },
  );
  return roomId;
}

class SlidingWindowLimiter {
  private timestamps: number[] = [];

  constructor(
    private readonly maxPerWindow: number,
    private readonly windowMs: number,
  ) {}

  tryAcquire(): boolean {
    const now = Date.now();
    this.timestamps = this.timestamps.filter((t) => now - t < this.windowMs);
    if (this.timestamps.length >= this.maxPerWindow) return false;
    this.timestamps.push(now);
    return true;
  }
}

async function main(): Promise<void> {
  const cfg = loadConfig();
  const log = (level: "info" | "warn" | "error", msg: string) => {
    console.log(JSON.stringify({ ts: new Date().toISOString(), level, msg }));
  };

  const storage = new SimpleFsStorageProvider(
    path.join(cfg.dataDir, "bot-storage.json"),
  );
  const cryptoStorage = new RustSdkCryptoStorageProvider(
    path.join(cfg.dataDir, "crypto-store"),
    RustSdkCryptoStoreType.Sqlite,
  );

  const client = new MatrixClient(
    cfg.homeserverUrl,
    cfg.accessToken,
    storage,
    cryptoStorage,
  );

  // First-run bootstrap: if SSM hasn't been seeded with a control
  // room ID, create one ourselves with the allowed sender invited
  // and an `m.room.encryption` state event so the room is E2EE
  // from the first message. The new ID is written back to SSM so
  // subsequent restarts pick it up via the prestart helper.
  const controlRoomId =
    cfg.controlRoomId ?? (await bootstrapControlRoom(client, cfg, log));
  log("info", `starting bot for ${cfg.allowedSender} in ${controlRoomId}`);

  // Auto-join the control room only. Refuse all other invites.
  client.on("room.invite", async (roomId: string, invite) => {
    if (roomId !== controlRoomId) {
      log("warn", `rejecting invite to non-allowlisted room ${roomId}`);
      try {
        await client.leaveRoom(roomId);
      } catch (e) {
        log("warn", `leave failed for ${roomId}: ${(e as Error).message}`);
      }
      return;
    }
    if (invite?.sender !== cfg.allowedSender) {
      log(
        "warn",
        `rejecting invite from non-allowlisted sender ${invite?.sender}`,
      );
      try {
        await client.leaveRoom(roomId);
      } catch (e) {
        log("warn", `leave failed for ${roomId}: ${(e as Error).message}`);
      }
      return;
    }
    await client.joinRoom(roomId);
    log("info", `joined control room ${roomId}`);
  });

  const limiter = new SlidingWindowLimiter(
    cfg.rateLimitMaxPerWindow,
    cfg.rateLimitWindowMs,
  );

  client.on("room.message", async (roomId: string, event: any) => {
    // Allowlist: room
    if (roomId !== controlRoomId) return;
    // Allowlist: sender
    if (event?.sender !== cfg.allowedSender) {
      log(
        "warn",
        `ignoring message from non-allowlisted sender ${event?.sender}`,
      );
      return;
    }
    // Skip our own messages
    const me = await client.getUserId();
    if (event.sender === me) return;

    const content = event.content;
    if (!content || content.msgtype !== "m.text") return;

    // E2E enforcement: matrix-bot-sdk doesn't carry a per-event
    // "this was encrypted" flag on decrypted room.message events,
    // so we anchor on the room's encryption state instead. We
    // assert at startup that the control room IS encrypted; if
    // someone later disables encryption on the room, this re-check
    // catches it.
    if (!(await client.crypto.isRoomEncrypted(roomId))) {
      log("warn", `refusing message in unencrypted room ${roomId}`);
      await client.replyText(
        roomId,
        event,
        "rejected: this control room must be end-to-end encrypted.",
      );
      return;
    }

    if (!limiter.tryAcquire()) {
      log("warn", `rate limit exceeded; dropping ${event.event_id}`);
      await client.replyText(
        roomId,
        event,
        "rate limited; try again in a minute.",
      );
      return;
    }

    const prompt = (content.body as string | undefined)?.trim();
    if (!prompt) return;
    log("info", `forwarding prompt of length ${prompt.length}`);

    try {
      const response = await forwardToGateway({
        agentId: cfg.agentId,
        prompt,
        timeoutSeconds: cfg.agentTimeoutSeconds,
      });
      await client.replyText(roomId, event, response);
    } catch (e) {
      const msg = (e as Error).message ?? String(e);
      log("error", `gateway error: ${msg}`);
      await client.replyText(roomId, event, `gateway error: ${msg}`);
    }
  });

  await client.start();
  if (!(await client.crypto.isRoomEncrypted(controlRoomId))) {
    throw new Error(
      `control room ${controlRoomId} is not encrypted; refusing to run`,
    );
  }
  await ensureCrossSigning(client, cfg.dataDir, log);
  log("info", `bot running; user_id=${await client.getUserId()}`);
}

main().catch((e) => {
  console.error("fatal:", e);
  process.exit(1);
});
