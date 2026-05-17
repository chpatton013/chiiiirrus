"""Run a non-interactive shell command on a node via SSM RunCommand.

Submits a one-shot `AWS-RunShellScript` invocation, polls for
completion, and prints stdout / stderr. Exit code mirrors the
remote command's exit code (so `bin/node-exec openclaw false` exits
non-zero locally).

Usage:
  bin/aws-node-exec <label> <command>
  bin/aws-node-exec <label> --user ubuntu <command>

The command is passed to `bash -c`, so quoting / pipelines /
redirects all work as expected. Output truncates at SSM's 24 KB
per-stream cap; for larger output, pipe to a file on the node and
fetch it separately.
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3

from discover import resolve

POLL_INTERVAL_SECONDS = 2
TIMEOUT_SECONDS = 600


def _wait_for_command(ssm, command_id: str, instance_id: str) -> dict:
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if inv["Status"] in ("Pending", "InProgress", "Delayed"):
            continue
        return inv
    raise RuntimeError(
        f"timed out after {TIMEOUT_SECONDS}s waiting for SSM command {command_id}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a shell command on a NodeExec-tagged EC2 instance.",
    )
    parser.add_argument(
        "label",
        help="NodeExecLabel of the node to run on",
    )
    parser.add_argument(
        "command",
        help="Shell command (passed to `bash -c` on the remote)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Run as this user via sudo -iu (default: root, the SSM default)",
    )
    args = parser.parse_args()

    node = resolve(args.label)
    if node.state != "running":
        sys.exit(f"node {node.label!r} is not running (state={node.state})")

    if args.user:
        # `sudo -iu <user> bash -c '<cmd>'` runs in a login shell so
        # /etc/profile.d/* (XDG_RUNTIME_DIR, openclaw env) gets loaded.
        remote = f"sudo -iu {args.user} bash -c {args.command!r}"
    else:
        remote = args.command

    ssm = boto3.client("ssm")
    response = ssm.send_command(
        InstanceIds=[node.instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [remote]},
    )
    command_id = response["Command"]["CommandId"]

    inv = _wait_for_command(ssm, command_id, node.instance_id)

    stdout = inv.get("StandardOutputContent", "")
    stderr = inv.get("StandardErrorContent", "")
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")

    if inv["Status"] != "Success":
        sys.stderr.write(
            f"# command status={inv['Status']} "
            f"response_code={inv.get('ResponseCode', '?')}\n"
        )
    # Mirror the remote exit code (0 on Success, ResponseCode otherwise).
    return inv.get("ResponseCode", 1) if inv["Status"] != "Success" else 0


if __name__ == "__main__":
    sys.exit(main())
