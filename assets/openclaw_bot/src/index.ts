import { execFileSync } from "node:child_process";
import * as fs from "node:fs";

import {
  ClientEvent,
  EventType,
  MsgType,
  Preset,
  RelationType,
  RoomEvent,
  RoomMemberEvent,
  SyncState,
  createClient,
  type MatrixClient,
  type MatrixEvent,
  type Room,
  type RoomMember,
} from "matrix-js-sdk";

import { ensureCrossSigning } from "./cross-signing-import.js";
import { forwardToGateway } from "./openclaw.js";
import { registerVerificationHandler } from "./verification.js";

interface Config {
  homeserverUrl: string;
  accessToken: string;
  userId: string;
  deviceId: string;
  controlRoomId: string | null;
  controlRoomParam: string;
  allowedSender: string;
  dataDir: string;
  gatewayUrl: string;
  gatewayToken: string;
  rateLimitMaxPerWindow: number;
  rateLimitWindowMs: number;
}

type Logger = (level: "info" | "warn" | "error", msg: string) => void;

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

function parseTokenFile(envName: string): {
  token: string;
  userId: string;
  deviceId: string;
} {
  const raw = readFileEnv(envName);
  // Token files written by the bootstrap CR contain a JSON object;
  // the bot's prestart helper only extracts the token field. We
  // also accept a single-line token string as a fallback so
  // operators can hand-rotate without re-deploying.
  if (raw.startsWith("{")) {
    const parsed = JSON.parse(raw) as {
      token: string;
      user_id: string;
      device_id: string;
    };
    return {
      token: parsed.token,
      userId: parsed.user_id,
      deviceId: parsed.device_id,
    };
  }
  return {
    token: raw,
    userId: requireEnv("BOT_USER_ID"),
    deviceId: requireEnv("BOT_DEVICE_ID"),
  };
}

function loadConfig(): Config {
  const dataDir = requireEnv("MATRIX_BOT_DATA_DIR");
  fs.mkdirSync(dataDir, { recursive: true });
  const controlRoomIdRaw = (process.env.CONTROL_ROOM_ID ?? "").trim();
  const tokenInfo = parseTokenFile("BOT_ACCESS_TOKEN_FILE");
  return {
    homeserverUrl: requireEnv("HOMESERVER_URL"),
    accessToken: tokenInfo.token,
    userId: tokenInfo.userId,
    deviceId: tokenInfo.deviceId,
    controlRoomId: controlRoomIdRaw || null,
    controlRoomParam: requireEnv("CONTROL_ROOM_PARAM"),
    allowedSender: requireEnv("ALLOWED_SENDER"),
    dataDir,
    gatewayUrl: requireEnv("OPENCLAW_GATEWAY_URL"),
    gatewayToken: readFileEnv("OPENCLAW_GATEWAY_TOKEN_FILE"),
    rateLimitMaxPerWindow: 6,
    rateLimitWindowMs: 60_000,
  };
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

async function waitForPrepared(client: MatrixClient): Promise<void> {
  if (client.getSyncState() === SyncState.Prepared) return;
  return new Promise((resolve, reject) => {
    const onSync = (state: SyncState) => {
      if (state === SyncState.Prepared || state === SyncState.Syncing) {
        client.off(ClientEvent.Sync, onSync);
        resolve();
      } else if (state === SyncState.Error) {
        client.off(ClientEvent.Sync, onSync);
        reject(new Error("sync entered ERROR state"));
      }
    };
    client.on(ClientEvent.Sync, onSync);
  });
}

async function bootstrapControlRoom(
  client: MatrixClient,
  cfg: Config,
  log: Logger,
): Promise<string> {
  log("info", `bootstrapping control room; inviting ${cfg.allowedSender}`);
  const result = await client.createRoom({
    preset: Preset.TrustedPrivateChat,
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
  const roomId = result.room_id;
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

function registerInviteHandler(
  client: MatrixClient,
  controlRoomId: string,
  cfg: Config,
  log: Logger,
): void {
  client.on(
    RoomMemberEvent.Membership,
    async (event: MatrixEvent, member: RoomMember) => {
      if (member.userId !== cfg.userId) return;
      if (member.membership !== "invite") return;
      const roomId = event.getRoomId();
      const sender = event.getSender();
      if (!roomId) return;
      if (roomId !== controlRoomId) {
        log("warn", `rejecting invite to non-allowlisted room ${roomId}`);
        try {
          await client.leave(roomId);
        } catch (e) {
          log("warn", `leave failed for ${roomId}: ${(e as Error).message}`);
        }
        return;
      }
      if (sender !== cfg.allowedSender) {
        log("warn", `rejecting invite from non-allowlisted sender ${sender}`);
        try {
          await client.leave(roomId);
        } catch (e) {
          log("warn", `leave failed for ${roomId}: ${(e as Error).message}`);
        }
        return;
      }
      try {
        await client.joinRoom(roomId);
        log("info", `joined control room ${roomId}`);
      } catch (e) {
        log("error", `join failed for ${roomId}: ${(e as Error).message}`);
      }
    },
  );
}

function registerMessageHandler(
  client: MatrixClient,
  controlRoomId: string,
  cfg: Config,
  log: Logger,
): void {
  const limiter = new SlidingWindowLimiter(
    cfg.rateLimitMaxPerWindow,
    cfg.rateLimitWindowMs,
  );

  client.on(
    RoomEvent.Timeline,
    async (
      event: MatrixEvent,
      _room: Room | undefined,
      toStartOfTimeline: boolean | undefined,
    ) => {
      if (toStartOfTimeline) return;
      // Decrypt-then-emit means we should wait for decryption if
      // the event is still encrypted at the time of dispatch.
      if (event.isEncrypted() && event.isDecryptionFailure()) return;
      if (event.getType() !== EventType.RoomMessage) return;
      const roomId = event.getRoomId();
      if (roomId !== controlRoomId) return;
      const sender = event.getSender();
      if (sender !== cfg.allowedSender) {
        log(
          "warn",
          `ignoring message from non-allowlisted sender ${sender ?? "<unknown>"}`,
        );
        return;
      }

      const content = event.getContent();
      if (content.msgtype !== MsgType.Text) return;

      const crypto = client.getCrypto();
      if (!crypto || !(await crypto.isEncryptionEnabledInRoom(roomId))) {
        log("warn", `refusing message in unencrypted room ${roomId}`);
        await sendThreadReply(
          client,
          roomId,
          event,
          "rejected: this control room must be end-to-end encrypted.",
        );
        return;
      }

      if (!limiter.tryAcquire()) {
        log("warn", `rate limit exceeded; dropping ${event.getId()}`);
        await sendThreadReply(
          client,
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
          gatewayUrl: cfg.gatewayUrl,
          gatewayToken: cfg.gatewayToken,
          prompt,
        });
        await sendThreadReply(client, roomId, event, response);
      } catch (e) {
        const msg = (e as Error).message ?? String(e);
        log("error", `gateway error: ${msg}`);
        await sendThreadReply(client, roomId, event, `gateway error: ${msg}`);
      }
    },
  );
}

async function sendThreadReply(
  client: MatrixClient,
  roomId: string,
  rootEvent: MatrixEvent,
  body: string,
): Promise<void> {
  const rootId = rootEvent.getId();
  if (!rootId) {
    await client.sendEvent(roomId, EventType.RoomMessage, {
      msgtype: MsgType.Text,
      body,
    });
    return;
  }
  await client.sendEvent(roomId, EventType.RoomMessage, {
    msgtype: MsgType.Text,
    body,
    "m.relates_to": {
      rel_type: RelationType.Thread,
      event_id: rootId,
    },
  });
}

async function main(): Promise<void> {
  const cfg = loadConfig();
  const log: Logger = (level, msg) => {
    console.log(JSON.stringify({ ts: new Date().toISOString(), level, msg }));
  };

  const client = createClient({
    baseUrl: cfg.homeserverUrl,
    accessToken: cfg.accessToken,
    userId: cfg.userId,
    deviceId: cfg.deviceId,
  });

  registerVerificationHandler(client, cfg.allowedSender, log);

  await client.initRustCrypto({ useIndexedDB: false });

  await client.startClient();
  await waitForPrepared(client);
  log("info", `sync prepared; user_id=${cfg.userId} device_id=${cfg.deviceId}`);

  await ensureCrossSigning(client, cfg.dataDir, log);

  // Resolve the control room AFTER cross-signing is set up so the
  // very first message exchange is cross-signed-eligible.
  const controlRoomId =
    cfg.controlRoomId ?? (await bootstrapControlRoom(client, cfg, log));

  const crypto = client.getCrypto();
  if (!crypto) throw new Error("no crypto API");
  if (!(await crypto.isEncryptionEnabledInRoom(controlRoomId))) {
    throw new Error(
      `control room ${controlRoomId} is not encrypted; refusing to run`,
    );
  }

  registerInviteHandler(client, controlRoomId, cfg, log);
  registerMessageHandler(client, controlRoomId, cfg, log);

  log("info", `bot running in ${controlRoomId}`);
}

main().catch((e) => {
  console.error("fatal:", e);
  process.exit(1);
});
