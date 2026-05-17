"""Forward a local TCP port to a port on a node via SSM.

Blocking. Ctrl-C ends the session.

Usage:
  bin/aws-node-portforward <label> <local-port>:<remote-port>
  bin/aws-node-portforward <label> 18789:18789

Local port may be omitted to match remote:
  bin/aws-node-portforward <label> 18789

Requires `session-manager-plugin` installed locally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from discover import find_nodes, print_listing, resolve

PORT_PAIR_RE = re.compile(r"^(?P<local>\d{1,5})(?::(?P<remote>\d{1,5}))?$")


def _parse_ports(spec: str) -> tuple[int, int]:
    m = PORT_PAIR_RE.match(spec)
    if not m:
        sys.exit(f"invalid port spec {spec!r}; expected 'PORT' or 'LOCAL:REMOTE'")
    local = int(m.group("local"))
    remote = int(m.group("remote") or local)
    if not (1 <= local <= 65535 and 1 <= remote <= 65535):
        sys.exit(f"port out of range in {spec!r}")
    return local, remote


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Forward a local TCP port to a port on a NodeExec-tagged "
            "instance via SSM Session Manager."
        ),
    )
    parser.add_argument(
        "label",
        nargs="?",
        help="NodeExecLabel of the node (omit to list)",
    )
    parser.add_argument(
        "ports",
        nargs="?",
        help="Port pair: 'LOCAL:REMOTE' or just 'PORT' (same on both sides)",
    )
    args = parser.parse_args()

    if not args.label:
        nodes = find_nodes()
        print("Nodes available (pass a label as the positional arg):", file=sys.stderr)
        print_listing(nodes, stream=sys.stderr)
        return 0

    if not args.ports:
        sys.exit("missing port spec; usage: node-portforward <label> LOCAL:REMOTE")

    local, remote = _parse_ports(args.ports)
    node = resolve(args.label)
    if node.state != "running":
        sys.exit(f"node {node.label!r} is not running (state={node.state})")

    params = json.dumps({"portNumber": [str(remote)], "localPortNumber": [str(local)]})

    print(
        f"# {node.label}: forwarding localhost:{local} -> "
        f"{node.instance_id}:{remote} (Ctrl-C to end)",
        file=sys.stderr,
    )
    os.execvp(
        "aws",
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            node.instance_id,
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            params,
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
