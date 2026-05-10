import { type MatrixClient } from "matrix-js-sdk";
import { CryptoEvent } from "matrix-js-sdk/lib/crypto-api/index.js";
import {
  VerificationPhase,
  VerificationRequestEvent,
  type VerificationRequest,
  VerifierEvent,
  type Verifier,
  type ShowSasCallbacks,
} from "matrix-js-sdk/lib/crypto-api/verification.js";

// Wires up an auto-accept handler for SAS verification requests
// from the allowed sender. Element initiates "Verify user"; bot
// accepts; bot starts SAS; bot auto-confirms the emoji match.
//
// Auto-confirm is safe in this deployment: the bot's allowlist is
// already restricted to a single MXID; if that MXID is requesting
// verification, the bot trusts them by definition.

type Logger = (level: "info" | "warn" | "error", msg: string) => void;

export function registerVerificationHandler(
  client: MatrixClient,
  allowedSender: string,
  log: Logger,
): void {
  client.on(CryptoEvent.VerificationRequestReceived, (request) => {
    handleRequest(request, allowedSender, log).catch((e) => {
      log("error", `verification handler crashed: ${(e as Error).message}`);
    });
  });
}

async function handleRequest(
  request: VerificationRequest,
  allowedSender: string,
  log: Logger,
): Promise<void> {
  if (request.otherUserId !== allowedSender) {
    log(
      "warn",
      `ignoring verification request from non-allowlisted ${request.otherUserId}`,
    );
    try {
      await request.cancel();
    } catch (e) {
      log("warn", `cancel failed: ${(e as Error).message}`);
    }
    return;
  }

  log(
    "info",
    `accepting verification request from ${request.otherUserId} device=${request.otherDeviceId ?? "<in-room>"}`,
  );

  try {
    await request.accept();
  } catch (e) {
    log("error", `accept failed: ${(e as Error).message}`);
    return;
  }

  const verifier = await waitForStart(request);
  if (!verifier) {
    log("warn", "could not start verifier; request likely cancelled");
    return;
  }

  verifier.on(VerifierEvent.ShowSas, (sas: ShowSasCallbacks) => {
    autoConfirmSas(sas, log).catch((e) => {
      log("error", `confirm failed: ${(e as Error).message}`);
    });
  });

  verifier.on(VerifierEvent.Cancel, (err: unknown) => {
    log(
      "warn",
      `verifier cancelled: ${err instanceof Error ? err.message : String(err)}`,
    );
  });
}

async function waitForStart(
  request: VerificationRequest,
): Promise<Verifier | undefined> {
  return new Promise((resolve) => {
    const tryStart = async () => {
      try {
        const verifier = await request.startVerification("m.sas.v1");
        resolve(verifier);
      } catch {
        // Not Ready yet (or already done) - wait for the next change.
      }
    };

    if (request.phase >= VerificationPhase.Ready) {
      void tryStart();
      return;
    }

    const onChange = () => {
      if (request.phase === VerificationPhase.Cancelled) {
        request.off(VerificationRequestEvent.Change, onChange);
        resolve(undefined);
        return;
      }
      if (request.phase >= VerificationPhase.Ready) {
        request.off(VerificationRequestEvent.Change, onChange);
        void tryStart();
      }
    };
    request.on(VerificationRequestEvent.Change, onChange);
  });
}

async function autoConfirmSas(
  sas: ShowSasCallbacks,
  log: Logger,
): Promise<void> {
  const emoji = sas.sas?.emoji;
  if (emoji) {
    const labels = emoji.map((e) => (e as [string, string])[1]).join(" ");
    log("info", `auto-confirming SAS emoji: ${labels}`);
  } else {
    log("info", "auto-confirming SAS (no emoji array surfaced)");
  }
  await sas.confirm();
}
