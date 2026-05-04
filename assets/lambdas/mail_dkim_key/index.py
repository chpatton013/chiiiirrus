"""Custom Resource Lambda that manages a DKIM RSA-2048 keypair for the
mail server.

On every Create/Update event:

- Read the current value of the `mail/dkim-private-key` secret. If the
  value is missing, empty, or the placeholder string ``"pending"``,
  generate a fresh keypair and write the PEM-encoded private key back
  into the secret.
- Compute the corresponding DKIM TXT record value and return it via
  the Custom Resource ``Data`` map under key ``PublicKeyTxt``. The
  parent stack references this with ``cr.get_att_string("PublicKeyTxt")``
  to populate a Route53 TxtRecord at ``s1._domainkey.<domain>``.

Idempotent: once the secret has a real key, every subsequent event just
re-derives the public key from it, so the DKIM TXT value is stable
across deploys.
"""

import base64
import json
import os

import boto3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sm = boto3.client("secretsmanager")

SECRET_ID = os.environ["SECRET_ID"]
PLACEHOLDER = "pending"


def _read_stored_pem() -> str:
    try:
        raw = sm.get_secret_value(SecretId=SECRET_ID).get("SecretString", "")
    except sm.exceptions.ResourceNotFoundException:
        return ""
    try:
        value = json.loads(raw).get("secret", "")
    except (json.JSONDecodeError, AttributeError):
        return ""
    return "" if value == PLACEHOLDER else value


def _generate_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


def _public_key_txt(pem: str) -> str:
    private_key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    public_key = private_key.public_key()
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    b64 = base64.b64encode(der).decode("ascii")
    return f"v=DKIM1; k=rsa; p={b64}"


def handler(event, _ctx):
    request_type = event["RequestType"]
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "mail-dkim-key")}

    pem = _read_stored_pem()
    if not pem:
        pem = _generate_pem()
        sm.put_secret_value(
            SecretId=SECRET_ID,
            SecretString=json.dumps({"secret": pem}),
        )

    return {
        "PhysicalResourceId": "mail-dkim-key",
        "Data": {"PublicKeyTxt": _public_key_txt(pem)},
    }
