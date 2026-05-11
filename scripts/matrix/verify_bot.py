"""Sign a Matrix bot's cross-signing master key with the operator's user_signing key.

Side-channel verification flow. Replaces Element's interactive SAS
verify-user dance for bots whose SDK doesn't implement the
receive side of SAS. Result: the bot's master key gains a
signature from your user_signing_key, which Element treats as a
fully-verified user identity (green shield).

Inputs:
  - Operator's homeserver access token (interactive prompt).
  - Operator's Element recovery key (interactive prompt). This is
    the random base58-encoded key Element prints once when
    "Set up secure backup". Decrypts the user_signing private
    seed out of m.cross_signing.user_signing in account data.
  - Bot's MXID (positional arg).

Operations (all via the Matrix Client-Server API; no server admin
needed):
  1. Validate the token by calling /account/whoami.
  2. Read m.secret_storage.default_key + m.secret_storage.key.* to
     learn the operator's secret storage config.
  3. Decode + validate the recovery key against the key info.
  4. Fetch encrypted m.cross_signing.user_signing account data and
     decrypt with the recovery key.
  5. Fetch the bot's master_key public via /keys/query.
  6. Strip signatures + unsigned, canonical-JSON the rest, ed25519
     sign with the user_signing private seed.
  7. POST the signature to /keys/signatures/upload.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request

import base58
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _http(
    method: str,
    homeserver: str,
    path: str,
    token: str,
    body: dict | None = None,
) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{homeserver}{path}", method=method, headers=headers, data=data
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} on {method} {path}: {body_text}")


def decode_recovery_key(recovery_key_str: str) -> bytes:
    """Decode a Matrix-format recovery key into its raw 32-byte secret storage key."""
    stripped = "".join(recovery_key_str.split())  # drop all whitespace
    decoded = base58.b58decode(stripped)
    if len(decoded) != 35:
        raise SystemExit(
            f"recovery key decoded length {len(decoded)} (expected 35); "
            "check the key value is correct"
        )
    if decoded[0] != 0x8B or decoded[1] != 0x01:
        raise SystemExit(
            f"recovery key has bad prefix {decoded[:2].hex()} (expected 8b01)"
        )
    parity = 0
    for byte in decoded:
        parity ^= byte
    if parity != 0:
        raise SystemExit("recovery key parity byte mismatch")
    return decoded[2:34]


def _hkdf(raw_key: bytes, info: bytes) -> tuple[bytes, bytes]:
    """Matrix SSSS HKDF: salt of 32 zero bytes, sha256, 64-byte output split into (aes_key, mac_key)."""
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=b"\x00" * 32,
        info=info,
    ).derive(raw_key)
    return derived[:32], derived[32:]


def validate_recovery_key(raw_key: bytes, key_info: dict) -> None:
    """Verify the recovery key matches the operator's secret storage key."""
    iv_b64 = key_info.get("iv")
    mac_b64 = key_info.get("mac")
    if not iv_b64 or not mac_b64:
        # No iv/mac on the key info means there's nothing to verify against;
        # the key may have been created by an older client.
        print(
            "warning: secret storage key has no iv/mac; skipping validation",
            file=sys.stderr,
        )
        return
    aes_key, mac_key = _hkdf(raw_key, info=b"")
    iv = base64.b64decode(iv_b64)
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(b"\x00" * 32) + encryptor.finalize()
    computed_mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(computed_mac, base64.b64decode(mac_b64)):
        raise SystemExit(
            "recovery key did not match the operator's secret storage key "
            "(HMAC mismatch). Either the recovery key is wrong, or the "
            "default secret storage key has been rotated."
        )


def decrypt_secret(raw_key: bytes, secret_name: str, encrypted_entry: dict) -> bytes:
    """Decrypt one entry of an encrypted account_data secret."""
    aes_key, mac_key = _hkdf(raw_key, info=secret_name.encode())
    iv = base64.b64decode(encrypted_entry["iv"])
    ciphertext = base64.b64decode(encrypted_entry["ciphertext"])
    expected_mac = base64.b64decode(encrypted_entry["mac"])
    computed_mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(computed_mac, expected_mac):
        raise SystemExit(f"HMAC mismatch decrypting {secret_name}")
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def canonical_json(obj) -> bytes:
    """Matrix canonical JSON encoding."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def b64_unpadded(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign a bot's master cross-signing key with your own user_signing_key, "
        "marking the bot as verified in Element without going through SAS."
    )
    parser.add_argument(
        "bot_user_id",
        help="The bot's MXID, e.g. @openclaw-bot:chiiiirs.com",
    )
    parser.add_argument(
        "--homeserver-url",
        default="https://matrix.chiiiirs.com",
        help="Matrix homeserver base URL",
    )
    parser.add_argument(
        "--token",
        help="Operator's access token; prompts if not given. "
        "Extract from Element Web Settings > Help & About > Advanced > Access Token.",
    )
    parser.add_argument(
        "--recovery-key",
        help="Operator's 4S recovery key (the base58 string Element gave you when you set up "
        "secure backup); prompts if not given.",
    )
    args = parser.parse_args()

    token = args.token or getpass.getpass("Element access token: ").strip()
    if not token:
        raise SystemExit("no access token supplied")
    recovery_key_str = (
        args.recovery_key
        or getpass.getpass("Recovery key (base58, spaces ignored): ").strip()
    )
    if not recovery_key_str:
        raise SystemExit("no recovery key supplied")

    sys.stderr.write("validating access token...\n")
    whoami = _http(
        "GET", args.homeserver_url, "/_matrix/client/v3/account/whoami", token
    )
    user_id = whoami["user_id"]
    sys.stderr.write(f"authenticated as {user_id}\n")

    raw_key = decode_recovery_key(recovery_key_str)

    sys.stderr.write("fetching secret storage configuration...\n")
    default_key = _http(
        "GET",
        args.homeserver_url,
        f"/_matrix/client/v3/user/{user_id}/account_data/m.secret_storage.default_key",
        token,
    )
    default_key_id = default_key["key"]
    key_info = _http(
        "GET",
        args.homeserver_url,
        f"/_matrix/client/v3/user/{user_id}/account_data/m.secret_storage.key.{default_key_id}",
        token,
    )
    sys.stderr.write(f"  default key: {default_key_id}\n")
    sys.stderr.write(f"  algorithm:   {key_info.get('algorithm')}\n")

    validate_recovery_key(raw_key, key_info)
    sys.stderr.write("recovery key verified.\n")

    sys.stderr.write("decrypting user_signing private key...\n")
    encrypted_secret = _http(
        "GET",
        args.homeserver_url,
        f"/_matrix/client/v3/user/{user_id}/account_data/m.cross_signing.user_signing",
        token,
    )
    encrypted_entry = encrypted_secret["encrypted"][default_key_id]
    plaintext = decrypt_secret(raw_key, "m.cross_signing.user_signing", encrypted_entry)
    # The secret content is the unpadded base64 of the 32-byte
    # ed25519 seed (Matrix stores cross-signing private keys this
    # way). Pad to a multiple of 4 chars before decoding.
    encoded = plaintext.decode("ascii").strip()
    padded = encoded + "=" * (-len(encoded) % 4)
    user_signing_seed = base64.b64decode(padded)
    if len(user_signing_seed) != 32:
        raise SystemExit(
            f"recovered user_signing seed has wrong length {len(user_signing_seed)} (expected 32)"
        )

    sys.stderr.write(f"fetching bot keys for {args.bot_user_id}...\n")
    keys = _http(
        "POST",
        args.homeserver_url,
        "/_matrix/client/v3/keys/query",
        token,
        body={"device_keys": {args.bot_user_id: []}},
    )
    bot_master = keys.get("master_keys", {}).get(args.bot_user_id)
    if not bot_master:
        raise SystemExit(
            f"{args.bot_user_id} has no master cross-signing key on the server"
        )
    bot_master_pub_b64 = next(iter(bot_master["keys"].values()))
    sys.stderr.write(f"  bot master key: ed25519:{bot_master_pub_b64}\n")

    # Build the master key object to sign: strip signatures + unsigned,
    # canonical-JSON the rest, ed25519-sign with our user_signing_priv.
    to_sign = {
        k: v for k, v in bot_master.items() if k not in ("signatures", "unsigned")
    }
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(user_signing_seed)
    user_signing_pub = private_key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    user_signing_pub_b64 = b64_unpadded(user_signing_pub)
    signature = private_key.sign(canonical_json(to_sign))
    signature_b64 = b64_unpadded(signature)

    # Already-signed check: if Synapse already has this signature, no-op.
    existing = bot_master.get("signatures", {}).get(user_id, {})
    if existing.get(f"ed25519:{user_signing_pub_b64}") == signature_b64:
        sys.stderr.write(
            "bot is already signed by our user_signing_key; nothing to upload.\n"
        )
        return 0

    sys.stderr.write("uploading signature...\n")
    signed_master = {
        **to_sign,
        "signatures": {
            user_id: {f"ed25519:{user_signing_pub_b64}": signature_b64},
        },
    }
    resp = _http(
        "POST",
        args.homeserver_url,
        "/_matrix/client/v3/keys/signatures/upload",
        token,
        body={args.bot_user_id: {bot_master_pub_b64: signed_master}},
    )
    failures = resp.get("failures") or {}
    if failures:
        raise SystemExit(f"signature upload had failures: {failures}")
    sys.stderr.write(
        f"signed {args.bot_user_id}'s master key. Reload Element to see the "
        "verified-user shield.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
