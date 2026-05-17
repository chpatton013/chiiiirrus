"""Register a new Matrix user via Synapse's register_new_matrix_user.

ECS-exec's into the running Synapse Fargate task (which has
/data/homeserver.yaml with the registration_shared_secret and
`python -m synapse._scripts.register_new_matrix_user` available),
creates the user with a random password, then logs in to obtain
an access token. Prints the credentials as JSON to stdout.

Intended for one-shot operator use -- e.g. creating
@openclaw-wadsworth, @openclaw-sebastian, @openclaw-binx so the
tokens can be pasted into the openclaw matrix-channel wizard.
The script does NOT persist tokens to Secrets Manager; that's the
operator's responsibility.

Idempotency: re-running with the same username fails at the
register step (Synapse returns "User ID already taken"). If you
need to rotate a password / re-mint a token for an existing user,
use Synapse's admin API instead.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys

import boto3

STACK_NAME = "MatrixStack"
# Per the repo's shell-into-service convention, every Fargate
# service's main container is named "Container".
CONTAINER_NAME = "Container"
# Sentinel prefix the in-container script writes around its
# single JSON output line, so we can fish it back out of the
# session-manager-plugin's noisy stdout.
SENTINEL = "MATRIX_USER_JSON:"

USERNAME_RE = re.compile(r"^[a-z0-9._=\-/+]+$")


# Runs inside the synapse container. Receives username as $1
# and the admin flag (--admin / --no-admin) as $2. Talks to
# Synapse on http://127.0.0.1:8008 -- the main container doesn't
# have $HOMESERVER_URL in its env (that's only on the one-shot
# bootstrap task), and going out through the public ALB would
# bounce back into this same container anyway. Logs the
# register step to stderr; emits one stdout line:
#   MATRIX_USER_JSON: {"token": "...", "user_id": "...", "device_id": "..."}
IN_CONTAINER_SCRIPT = r"""
set -euo pipefail
USER=$1
ADMIN_FLAG=$2
URL=http://127.0.0.1:8008
PASS=$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')
python -m synapse._scripts.register_new_matrix_user \
  -c /data/homeserver.yaml \
  -u "$USER" -p "$PASS" "$ADMIN_FLAG" \
  "$URL" >&2
RESP=$(curl -fsS -X POST "$URL/_matrix/client/v3/login" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"m.login.password\",\"user\":\"$USER\",\"password\":\"$PASS\",\"initial_device_display_name\":\"$USER\"}")
echo "MATRIX_USER_JSON: $(echo "$RESP" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(json.dumps({"token": r["access_token"], "user_id": r["user_id"], "device_id": r.get("device_id", "")}))')"
"""


def _service_arn(cfn) -> str:
    paginator = cfn.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=STACK_NAME):
        for r in page["StackResourceSummaries"]:
            if r["ResourceType"] == "AWS::ECS::Service":
                return r["PhysicalResourceId"]
    raise RuntimeError(f"no ECS::Service in stack {STACK_NAME}")


def _cluster_service(service_arn: str) -> tuple[str, str]:
    # arn:aws:ecs:<region>:<acct>:service/<cluster>/<service>
    tail = service_arn.split(":", 5)[-1]
    parts = tail.split("/")
    if len(parts) != 3 or parts[0] != "service":
        raise RuntimeError(f"unexpected service ARN format: {service_arn}")
    return parts[1], parts[2]


def _running_task(ecs, cluster: str, service: str) -> str:
    res = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")
    tasks = res.get("taskArns") or []
    if not tasks:
        raise RuntimeError(f"no running tasks for service {service}")
    return tasks[0]


def _extract_credentials(output: str) -> dict:
    for line in output.splitlines():
        idx = line.find(SENTINEL)
        if idx < 0:
            continue
        payload = line[idx + len(SENTINEL) :].strip()
        # The session-manager-plugin may append CR or escape codes;
        # find the JSON object by braces.
        start = payload.find("{")
        end = payload.rfind("}")
        if start < 0 or end < 0:
            continue
        try:
            return json.loads(payload[start : end + 1])
        except json.JSONDecodeError:
            continue
    raise RuntimeError(
        f"could not extract credentials from execute-command output:\n{output}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register a Matrix user and print its access token."
    )
    parser.add_argument(
        "username",
        help="Matrix localpart (lowercase), e.g. 'openclaw-wadsworth'",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Grant Synapse-admin privilege (default: regular user)",
    )
    args = parser.parse_args()

    if not USERNAME_RE.match(args.username):
        sys.stderr.write(
            f"invalid username {args.username!r}; must be a lowercase Matrix localpart\n"
        )
        return 2

    cfn = boto3.client("cloudformation")
    ecs = boto3.client("ecs")

    service_arn = _service_arn(cfn)
    cluster, service = _cluster_service(service_arn)
    task_arn = _running_task(ecs, cluster, service)
    task_id = task_arn.rsplit("/", 1)[-1]

    admin_flag = "--admin" if args.admin else "--no-admin"
    # bash -c <script> <argv0> <argv1...> binds positional args: $0=argv0, $1=argv1...
    inner = (
        "bash -c "
        + shlex.quote(IN_CONTAINER_SCRIPT)
        + " register-user "
        + shlex.quote(args.username)
        + " "
        + admin_flag
    )

    sys.stderr.write(
        f"registering {args.username} on cluster={cluster} service={service} task={task_id}\n"
    )

    proc = subprocess.run(
        [
            "aws",
            "ecs",
            "execute-command",
            "--cluster",
            cluster,
            "--task",
            task_arn,
            "--container",
            CONTAINER_NAME,
            "--interactive",
            "--command",
            inner,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        sys.stderr.write(
            "execute-command failed:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
        return proc.returncode

    try:
        creds = _extract_credentials(proc.stdout)
    except RuntimeError as e:
        sys.stderr.write(f"{e}\n--- stderr ---\n{proc.stderr}\n")
        return 3

    json.dump(creds, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
