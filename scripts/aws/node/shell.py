"""Open an interactive SSM Session Manager shell on a node.

Usage:
  bin/aws-node-shell              # list known nodes
  bin/aws-node-shell <label>      # open interactive session on <label>

Requires `session-manager-plugin` installed locally (the AWS CLI
shells out to it for the wire protocol).
"""

from __future__ import annotations

import argparse
import os
import sys

from discover import find_nodes, print_listing, resolve


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open an SSM Session Manager shell on a node.",
    )
    parser.add_argument(
        "label",
        nargs="?",
        help="NodeExecLabel of the node (omit to list)",
    )
    args = parser.parse_args()

    if not args.label:
        nodes = find_nodes()
        print("Nodes available (pass a label as the positional arg):", file=sys.stderr)
        print_listing(nodes, stream=sys.stderr)
        return 0

    node = resolve(args.label)
    if node.state != "running":
        sys.exit(f"node {node.label!r} is not running (state={node.state})")

    print(
        f"# {node.label}: starting SSM session on {node.instance_id} "
        f"(public_ip={node.public_ip or '-'})",
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
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
