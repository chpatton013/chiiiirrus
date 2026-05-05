import argparse
import base64
import getpass
import hashlib
import hmac
import json
import pathlib
import secrets
import string
import subprocess
import sys
from collections.abc import Callable

import boto3


def find_repo_root(start: pathlib.Path) -> pathlib.Path:
    output = subprocess.run(
        ["git", "-C", start, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )

    return pathlib.Path(output.stdout.strip())


HERE = pathlib.Path(__file__).parent
REPO_ROOT = find_repo_root(HERE)
BIN_DIR = REPO_ROOT / "bin"
CONFIG_PATH = REPO_ROOT / "config.toml"
CREATE_HOSTED_ZONE = BIN_DIR / "aws-create-hosted-zone"
WRITE_SECRET = BIN_DIR / "aws-write-secret"

sys.path.insert(0, str(REPO_ROOT))
from infra.models.app_config import load_config  # noqa: E402


def resolve_arg(value: str | None) -> str | None:
    """Resolve a flag value, handling the `@path` file-reference convention."""
    if value is None:
        return None
    if value.startswith("@"):
        with open(value[1:]) as f:
            return f.read().rstrip("\r\n")
    return value


def prompt_required(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.", file=sys.stderr)


def prompt_password_or_default(label: str) -> str | None:
    """Ask for a password twice. Empty input means 'use the auto-generated default'."""
    while True:
        first = getpass.getpass(f"{label} (leave blank to auto-generate): ")
        if first == "":
            return None
        second = getpass.getpass(f"{label} (confirm): ")
        if first == second:
            return first
        print("Passwords did not match; try again.", file=sys.stderr)


def generate_oidc_client_id() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(40))


def resolve_required(
    args: argparse.Namespace,
    flag: str,
    label: str,
    *,
    default: str | None = None,
) -> str:
    return resolve_arg(getattr(args, flag)) or prompt_required(label, default=default)


def resolve_secret(args: argparse.Namespace, flag: str, label: str) -> str:
    return resolve_arg(getattr(args, flag)) or getpass.getpass(f"{label}: ")


def resolve_optional_password(
    args: argparse.Namespace, flag: str, label: str
) -> str | None:
    # Re-prompt only when the raw flag is None, so `--flag @empty.file` still
    # produces "" without interactively prompting.
    if getattr(args, flag) is None:
        return prompt_password_or_default(label)
    return resolve_arg(getattr(args, flag))


def resolve_or_generate(
    args: argparse.Namespace, flag: str, gen: Callable[[], str]
) -> str:
    return resolve_arg(getattr(args, flag)) or gen()


def fetch_existing_secrets() -> set[str]:
    client = boto3.client("secretsmanager")
    paginator = client.get_paginator("list_secrets")
    names: set[str] = set()
    for page in paginator.paginate():
        for entry in page.get("SecretList", []):
            names.add(entry["Name"])
    return names


def needs_write(name: str, existing: set[str]) -> bool:
    """True if the secret is missing. If it exists, log the skip and return False."""
    if name in existing:
        print(f"secret '{name}' already exists; skipping", file=sys.stderr)
        return False
    return True


def run(cmd: list[str], label: str, stdin_value: str | None = None) -> None:
    result = subprocess.run(
        cmd,
        input=stdin_value if stdin_value is not None else None,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(f"Failed: {label}\n")
        sys.exit(result.returncode)


def _write_secret_cmd(
    secret_name: str,
    *,
    template: dict | None = None,
    key: str | None = None,
    length: int | None = None,
    bytes_: int | None = None,
    exclude_punctuation: bool = False,
    use_stdin: bool = False,
) -> list[str]:
    cmd = [str(WRITE_SECRET), secret_name, "--skip-if-exists"]
    if template is not None:
        assert key is not None
        cmd.extend(["--template", json.dumps(template), "--key", key])
    if exclude_punctuation:
        cmd.append("--exclude-punctuation")
    if use_stdin:
        cmd.append("-")
    elif bytes_ is not None:
        cmd.extend([f"--bytes={bytes_}"])
    elif length is not None:
        cmd.extend([f"--length={length}"])
    return cmd


SES_SMTP_VERSION = 0x04


def derive_ses_smtp_password(secret_access_key: str, region: str) -> str:
    """Derive an SES SMTP password from an IAM secret access key.

    Implements the documented HMAC-SHA256 algorithm:
    https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html#smtp-credentials-convert
    """

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    signature = _sign(("AWS4" + secret_access_key).encode("utf-8"), "11111111")
    signature = _sign(signature, region)
    signature = _sign(signature, "ses")
    signature = _sign(signature, "aws4_request")
    signature = _sign(signature, "SendRawEmail")
    return base64.b64encode(bytes([SES_SMTP_VERSION]) + signature).decode("ascii")


def find_hosted_zone_id(domain: str) -> str:
    r53 = boto3.client("route53")
    target = domain.rstrip(".") + "."
    # list_hosted_zones_by_name doesn't support boto3 pagination; it
    # returns zones alphabetically starting from DNSName. Asking for
    # exactly our target finds the matching zone immediately.
    response = r53.list_hosted_zones_by_name(DNSName=target, MaxItems="10")
    for zone in response.get("HostedZones", []):
        if zone["Name"] == target and not zone["Config"].get("PrivateZone"):
            return zone["Id"].split("/")[-1]
    raise RuntimeError(f"public hosted zone not found for domain {domain!r}")


def upsert_route53_record(
    zone_id: str, name: str, type_: str, values: list[str], ttl: int
) -> None:
    r53 = boto3.client("route53")
    r53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": name.rstrip(".") + ".",
                        "Type": type_,
                        "TTL": ttl,
                        "ResourceRecords": [{"Value": v} for v in values],
                    },
                }
            ]
        },
    )


def domain_ses_verified(ses, domain: str) -> bool:
    response = ses.get_identity_verification_attributes(Identities=[domain])
    status = (
        response.get("VerificationAttributes", {})
        .get(domain, {})
        .get("VerificationStatus")
    )
    return status in ("Pending", "Success")


def domain_ses_dkim_enabled(ses, domain: str) -> bool:
    response = ses.get_identity_dkim_attributes(Identities=[domain])
    attrs = response.get("DkimAttributes", {}).get(domain, {})
    return bool(attrs.get("DkimEnabled")) and bool(attrs.get("DkimTokens"))


def iam_user_exists(iam, name: str) -> bool:
    try:
        iam.get_user(UserName=name)
        return True
    except iam.exceptions.NoSuchEntityException:
        return False


def bootstrap_ses_smtp_relay(iam_user_name: str) -> tuple[str, str]:
    """Create or reuse the SES SMTP IAM user, mint an access key, and
    derive the SMTP password. Returns (access_key_id, smtp_password).

    The IAM user name is operator-controlled via [mail.relay].iam_user_name."""
    region = boto3.Session().region_name or "us-west-2"
    iam = boto3.client("iam")
    if not iam_user_exists(iam, iam_user_name):
        iam.create_user(UserName=iam_user_name)
        iam.attach_user_policy(
            UserName=iam_user_name,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSESFullAccess",
        )
    access_key = iam.create_access_key(UserName=iam_user_name)["AccessKey"]
    smtp_password = derive_ses_smtp_password(access_key["SecretAccessKey"], region)
    return access_key["AccessKeyId"], smtp_password


def bootstrap_ses_domain(public_domain: str) -> None:
    """Verify the public domain in SES (incl. DKIM) and publish the
    required Route53 TXT/CNAMEs. Idempotent."""
    ses = boto3.client("ses")
    zone_id = find_hosted_zone_id(public_domain)
    if not domain_ses_verified(ses, public_domain):
        token = ses.verify_domain_identity(Domain=public_domain)["VerificationToken"]
        upsert_route53_record(
            zone_id,
            f"_amazonses.{public_domain}",
            "TXT",
            [f'"{token}"'],
            1800,
        )
        print(f"SES domain verification record published for {public_domain}")
    else:
        print(f"SES domain {public_domain!r} already verified; skipping")
    if not domain_ses_dkim_enabled(ses, public_domain):
        tokens = ses.verify_domain_dkim(Domain=public_domain)["DkimTokens"]
        for token in tokens:
            upsert_route53_record(
                zone_id,
                f"{token}._domainkey.{public_domain}",
                "CNAME",
                [f"{token}.dkim.amazonses.com"],
                1800,
            )
        print(f"SES DKIM CNAMEs ({len(tokens)}) published for {public_domain}")
    else:
        print(f"SES DKIM for {public_domain!r} already enabled; skipping")


def write_secret(
    name: str,
    *,
    template: dict | None = None,
    key: str | None = None,
    provided: str | None = None,
    length: int | None = None,
    bytes_: int | None = None,
    exclude_punctuation: bool = False,
) -> None:
    """
    Write a Secrets Manager secret.

    If `provided` is not None, that value is written via stdin (including
    empty string). Otherwise the secret is generated using `length` / `bytes_`
    / `exclude_punctuation`, which act as a fallback spec and are ignored when
    `provided` is set.
    """
    print(f"writing secret: {name}")
    label = f"write-secret {name}"
    if provided is not None:
        cmd = _write_secret_cmd(name, template=template, key=key, use_stdin=True)
        run(cmd, label, stdin_value=provided)
        return
    cmd = _write_secret_cmd(
        name,
        template=template,
        key=key,
        length=length,
        bytes_=bytes_,
        exclude_punctuation=exclude_punctuation,
    )
    run(cmd, label)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive bootstrap: creates the hosted zone and seeds Authentik secrets. "
            "Domain is read from config.toml. Flag values prefixed with '@' are read "
            "from the named file. Pass --skip-if-exists to avoid prompting for inputs "
            "whose secrets already exist in Secrets Manager."
        )
    )
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help="Skip prompting and writing for secrets that already exist.",
    )
    parser.add_argument("--ghcr-username")
    parser.add_argument("--ghcr-access-token")
    parser.add_argument("--dockerhub-username")
    parser.add_argument("--dockerhub-access-token")
    parser.add_argument("--data-database-username")
    parser.add_argument("--data-database-password")
    parser.add_argument("--authentik-secret-key")
    parser.add_argument("--authentik-bootstrap-email")
    parser.add_argument("--authentik-bootstrap-password")
    parser.add_argument("--tailscale-oidc-client-id")
    parser.add_argument("--tailscale-oidc-client-secret")
    parser.add_argument("--headscale-oidc-client-id")
    parser.add_argument("--headscale-oidc-client-secret")
    parser.add_argument("--headplane-oidc-client-id")
    parser.add_argument("--headplane-oidc-client-secret")
    parser.add_argument("--vaultwarden-admin-token")
    parser.add_argument("--vaultwarden-oidc-client-id")
    parser.add_argument("--vaultwarden-oidc-client-secret")
    parser.add_argument("--mail-postmaster-password")
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)
    existing = fetch_existing_secrets() if args.skip_if_exists else set()

    run(
        [str(CREATE_HOSTED_ZONE), cfg.foundation.public_domain],
        "create-hosted-zone (public)",
    )
    run(
        [str(CREATE_HOSTED_ZONE), cfg.foundation.private_domain],
        "create-hosted-zone (private)",
    )

    if needs_write("ecr-pullthroughcache/ghcr", existing):
        write_secret(
            "ecr-pullthroughcache/ghcr",
            template={
                "username": resolve_required(
                    args,
                    "ghcr_username",
                    "GitHub username (for ghcr.io pull-through cache)",
                )
            },
            key="accessToken",
            provided=resolve_secret(
                args, "ghcr_access_token", "GitHub PAT with read:packages scope"
            ),
        )

    if needs_write("ecr-pullthroughcache/dockerhub", existing):
        write_secret(
            "ecr-pullthroughcache/dockerhub",
            template={
                "username": resolve_required(
                    args,
                    "dockerhub_username",
                    "Docker Hub username (for docker.io pull-through cache)",
                )
            },
            key="accessToken",
            provided=resolve_secret(args, "dockerhub_access_token", "Docker Hub PAT"),
        )

    if needs_write("data/database", existing):
        write_secret(
            "data/database",
            template={
                "username": resolve_required(
                    args,
                    "data_database_username",
                    "Data database master username",
                    default="postgres",
                )
            },
            key="password",
            provided=resolve_optional_password(
                args, "data_database_password", "Data database master password"
            ),
            length=32,
            exclude_punctuation=True,
        )

    for service in ("authentik", "headscale", "vaultwarden"):
        name = f"{service}/database"
        if needs_write(name, existing):
            write_secret(
                name,
                template={"username": service},
                key="password",
                length=32,
                exclude_punctuation=True,
            )

    if needs_write("authentik/secret-key", existing):
        write_secret(
            "authentik/secret-key",
            template={},
            key="secret",
            provided=resolve_optional_password(
                args, "authentik_secret_key", "Authentik secret key"
            ),
            length=50,
            exclude_punctuation=True,
        )

    if needs_write("authentik/bootstrap", existing):
        write_secret(
            "authentik/bootstrap",
            template={
                "email": resolve_required(
                    args, "authentik_bootstrap_email", "Authentik bootstrap email"
                ),
                "username": "akadmin",
            },
            key="password",
            provided=resolve_optional_password(
                args, "authentik_bootstrap_password", "Authentik bootstrap password"
            ),
            length=32,
        )

    if needs_write("authentik/oidc/tailscale", existing):
        write_secret(
            "authentik/oidc/tailscale",
            template={
                "client_id": resolve_required(
                    args,
                    "tailscale_oidc_client_id",
                    "Tailscale OIDC client ID (from Authentik)",
                )
            },
            key="client_secret",
            provided=resolve_secret(
                args,
                "tailscale_oidc_client_secret",
                "Tailscale OIDC client secret (from Authentik)",
            ),
        )

    for slug in ("headscale", "headplane", "vaultwarden"):
        name = f"authentik/oidc/{slug}"
        if needs_write(name, existing):
            write_secret(
                name,
                template={
                    "client_id": resolve_or_generate(
                        args, f"{slug}_oidc_client_id", generate_oidc_client_id
                    )
                },
                key="client_secret",
                provided=resolve_arg(getattr(args, f"{slug}_oidc_client_secret")),
                length=128,
                exclude_punctuation=True,
            )

    if needs_write("headplane/cookie-secret", existing):
        write_secret(
            "headplane/cookie-secret",
            template={},
            key="secret",
            length=32,
            exclude_punctuation=True,
        )
    if needs_write("headscale/noise-private-key", existing):
        write_secret(
            "headscale/noise-private-key", template={}, key="secret", bytes_=32
        )
    # Placeholder - the HeadscaleStack custom resource replaces this with the
    # real API key after Headscale is up. Secrets Manager rejects empty
    # strings, so we write a sentinel that the lambda recognizes.
    if needs_write("headscale/exit-node/preauthkey", existing):
        write_secret(
            "headscale/exit-node/preauthkey",
            template={},
            key="secret",
            provided="pending",
        )
    if needs_write("headscale/admin-api-key", existing):
        write_secret(
            "headscale/admin-api-key", template={}, key="secret", provided="pending"
        )

    if needs_write("vaultwarden/admin-token", existing):
        write_secret(
            "vaultwarden/admin-token",
            template={},
            key="secret",
            provided=resolve_optional_password(
                args, "vaultwarden_admin_token", "Vaultwarden admin token"
            ),
            length=64,
            exclude_punctuation=True,
        )

    # MailStack: postmaster mailbox password (used by the init container to
    # populate /tmp/docker-mailserver/postfix-accounts.cf on every task start).
    if needs_write("mail/postmaster-password", existing):
        write_secret(
            "mail/postmaster-password",
            template={},
            key="secret",
            provided=resolve_optional_password(
                args, "mail_postmaster_password", "Mail postmaster password"
            ),
            length=32,
            exclude_punctuation=True,
        )
    # Placeholder - the MailStack DKIM Custom Resource Lambda generates
    # the keypair on first deploy and rotates this in-place.
    if needs_write("mail/dkim-private-key", existing):
        write_secret(
            "mail/dkim-private-key", template={}, key="secret", provided="pending"
        )
    # SES SMTP relay credentials. Creates an IAM user + access key,
    # derives the SES SMTP password, and stores both in the secret.
    if needs_write(cfg.mail.relay.secret_name, existing):
        access_key_id, smtp_password = bootstrap_ses_smtp_relay(
            cfg.mail.relay.iam_user_name
        )
        write_secret(
            cfg.mail.relay.secret_name,
            template={"username": access_key_id},
            key="password",
            provided=smtp_password,
        )

    # MailStack SES domain setup: verify the public domain + publish DKIM CNAMEs.
    bootstrap_ses_domain(cfg.foundation.public_domain)

    print()
    print("=" * 60)
    print("Bootstrap complete. Manual steps that AWS does not allow scripting:")
    print("  - Move SES out of sandbox: AWS Support -> Service Quota Increase ->")
    print("    SES -> 'Production Access'. Required for sending to non-verified")
    print("    addresses.")
    print("  - (Optional) Reverse DNS (PTR) on the mail server EIPs after")
    print("    MailStack deploys: AWS Support case 'Reverse DNS for EIP'.")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
