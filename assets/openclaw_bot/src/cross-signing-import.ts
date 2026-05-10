import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

import { type MatrixClient } from "matrix-js-sdk";

// matrix-js-sdk's public `CryptoApi.bootstrapCrossSigning` only
// works through 4S (secret storage). We don't want to set up 4S
// for a single-user bot, so we generate the cross-signing key
// material ourselves, persist private seeds on disk, and:
//
// 1. Reach into the rust-crypto wasm binding's OlmMachine to
//    `importCrossSigningKeys(...)` so subsequent device-signing
//    works through matrix-js-sdk's native CryptoApi machinery.
// 2. On first deploy (no master key on Synapse), upload the
//    public keys to `/keys/device_signing/upload` ourselves. MSC3967
//    means the first upload doesn't need UIA. We do this with
//    canonical-JSON + ed25519 in Node and a raw `fetch` so we
//    don't depend on matrix-js-sdk's internal http path.
// 3. Call `CryptoApi.crossSignDevice(deviceId)` so the running
//    device's identity is signed by the self-signing key; that's
//    what Element checks to mark the device as trusted under the
//    user's master key.
//
// The on-disk file lives at <dataDir>/cross-signing.json and
// outlives instance replacements (it's on EFS). The bot's device
// IDs can rotate freely; the cross-signing identity stays put.

interface KeyPair {
  pub: Uint8Array;
  priv: Uint8Array;
}

interface CrossSigningKeys {
  master: KeyPair;
  selfSigning: KeyPair;
  userSigning: KeyPair;
}

// PKCS#8 ASN.1 prefix for an Ed25519 private key (RFC 8410); 32-byte
// seed appended forms a valid PKCS#8 v1 document.
const ED25519_PKCS8_PREFIX = Buffer.from(
  "302e020100300506032b657004220420",
  "hex",
);

function genKeyPair(): KeyPair {
  const kp = crypto.generateKeyPairSync("ed25519");
  const pubJwk = kp.publicKey.export({ format: "jwk" });
  const privJwk = kp.privateKey.export({ format: "jwk" });
  return {
    pub: Buffer.from(pubJwk.x as string, "base64url"),
    priv: Buffer.from(privJwk.d as string, "base64url"),
  };
}

function privKeyObject(seed: Uint8Array): crypto.KeyObject {
  return crypto.createPrivateKey({
    key: Buffer.concat([ED25519_PKCS8_PREFIX, Buffer.from(seed)]),
    format: "der",
    type: "pkcs8",
  });
}

function signEd25519(privSeed: Uint8Array, message: Buffer): Buffer {
  return crypto.sign(null, message, privKeyObject(privSeed));
}

// Matrix "unpadded base64": standard base64 with `=` padding
// stripped. NOT base64url.
function b64u(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64").replace(/=+$/, "");
}

// Canonical JSON: keys sorted lexically, no whitespace, UTF-8.
function canonicalJson(obj: unknown): string {
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj);
  if (Array.isArray(obj)) {
    return "[" + obj.map(canonicalJson).join(",") + "]";
  }
  const o = obj as Record<string, unknown>;
  const keys = Object.keys(o).sort();
  return (
    "{" +
    keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(o[k])).join(",") +
    "}"
  );
}

function signObject(obj: object, privSeed: Uint8Array): string {
  const o = obj as Record<string, unknown>;
  const { signatures: _s, unsigned: _u, ...rest } = o;
  const sig = signEd25519(privSeed, Buffer.from(canonicalJson(rest), "utf8"));
  return b64u(sig);
}

function persistKeys(dataDir: string, keys: CrossSigningKeys): void {
  const file = path.join(dataDir, "cross-signing.json");
  const json = {
    version: 1,
    master: { pub: b64u(keys.master.pub), priv: b64u(keys.master.priv) },
    self_signing: {
      pub: b64u(keys.selfSigning.pub),
      priv: b64u(keys.selfSigning.priv),
    },
    user_signing: {
      pub: b64u(keys.userSigning.pub),
      priv: b64u(keys.userSigning.priv),
    },
  };
  fs.writeFileSync(file, JSON.stringify(json), { mode: 0o600 });
}

function loadKeys(dataDir: string): CrossSigningKeys | null {
  const file = path.join(dataDir, "cross-signing.json");
  if (!fs.existsSync(file)) return null;
  const json = JSON.parse(fs.readFileSync(file, "utf8"));
  const decode = (s: string) => Buffer.from(s, "base64");
  return {
    master: { pub: decode(json.master.pub), priv: decode(json.master.priv) },
    selfSigning: {
      pub: decode(json.self_signing.pub),
      priv: decode(json.self_signing.priv),
    },
    userSigning: {
      pub: decode(json.user_signing.pub),
      priv: decode(json.user_signing.priv),
    },
  };
}

async function fetchSynapseMasterPub(
  client: MatrixClient,
  userId: string,
): Promise<string | null> {
  const resp = await fetch(`${client.baseUrl}/_matrix/client/v3/keys/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${client.getAccessToken()}`,
    },
    body: JSON.stringify({ device_keys: { [userId]: [] } }),
  });
  if (!resp.ok) {
    throw new Error(`/keys/query failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as {
    master_keys?: Record<string, { keys: Record<string, string> }>;
  };
  const entry = data.master_keys?.[userId];
  if (!entry) return null;
  return Object.values(entry.keys)[0] ?? null;
}

async function uploadPublicKeys(
  client: MatrixClient,
  userId: string,
  keys: CrossSigningKeys,
): Promise<void> {
  const masterPubB64 = b64u(keys.master.pub);
  const sskPubB64 = b64u(keys.selfSigning.pub);
  const uskPubB64 = b64u(keys.userSigning.pub);

  const masterKey = {
    user_id: userId,
    usage: ["master"],
    keys: { [`ed25519:${masterPubB64}`]: masterPubB64 },
  };
  const sskBase = {
    user_id: userId,
    usage: ["self_signing"],
    keys: { [`ed25519:${sskPubB64}`]: sskPubB64 },
  };
  const uskBase = {
    user_id: userId,
    usage: ["user_signing"],
    keys: { [`ed25519:${uskPubB64}`]: uskPubB64 },
  };
  const selfSigningKey = {
    ...sskBase,
    signatures: {
      [userId]: {
        [`ed25519:${masterPubB64}`]: signObject(sskBase, keys.master.priv),
      },
    },
  };
  const userSigningKey = {
    ...uskBase,
    signatures: {
      [userId]: {
        [`ed25519:${masterPubB64}`]: signObject(uskBase, keys.master.priv),
      },
    },
  };

  const resp = await fetch(
    `${client.baseUrl}/_matrix/client/v3/keys/device_signing/upload`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${client.getAccessToken()}`,
      },
      body: JSON.stringify({
        master_key: masterKey,
        self_signing_key: selfSigningKey,
        user_signing_key: userSigningKey,
      }),
    },
  );
  if (!resp.ok) {
    throw new Error(
      `device_signing/upload failed: ${resp.status} ${await resp.text()}`,
    );
  }
}

export async function ensureCrossSigning(
  client: MatrixClient,
  dataDir: string,
  log: (level: "info" | "warn" | "error", msg: string) => void,
): Promise<void> {
  const userId = client.getUserId();
  if (!userId) throw new Error("client has no userId");

  // 1. Load or generate cross-signing keys locally.
  let keys = loadKeys(dataDir);
  if (!keys) {
    log("info", "generating new cross-signing keys");
    keys = {
      master: genKeyPair(),
      selfSigning: genKeyPair(),
      userSigning: genKeyPair(),
    };
    persistKeys(dataDir, keys);
  }

  // 2. Compare against Synapse: have these keys ever been uploaded?
  const synapseMasterPub = await fetchSynapseMasterPub(client, userId);
  const ourMasterPubB64 = b64u(keys.master.pub);

  if (synapseMasterPub && synapseMasterPub !== ourMasterPubB64) {
    log(
      "warn",
      "Synapse master key differs from local cross-signing.json; not modifying state",
    );
    return;
  }

  if (!synapseMasterPub) {
    log("info", "uploading cross-signing public keys to Synapse");
    await uploadPublicKeys(client, userId, keys);
  }

  // 3. Import private keys into the OlmMachine so matrix-js-sdk
  //    can use them for device signing + outgoing flows. The
  //    `olmMachine` property is `private` on the TS surface but
  //    accessible at runtime.
  const olmMachine = (
    client.getCrypto() as unknown as {
      olmMachine: {
        importCrossSigningKeys: (
          master: string,
          ssk: string,
          usk: string,
        ) => Promise<unknown>;
      };
    }
  ).olmMachine;
  await olmMachine.importCrossSigningKeys(
    b64u(keys.master.priv),
    b64u(keys.selfSigning.priv),
    b64u(keys.userSigning.priv),
  );

  // 4. Sign this device with the self-signing key. Idempotent.
  const deviceId = client.getDeviceId();
  if (!deviceId) throw new Error("client has no deviceId");
  await client.getCrypto()!.crossSignDevice(deviceId);

  log("info", "cross-signing setup complete");
}
