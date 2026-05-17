"""Print summary info about a NodeExec-tagged EC2 instance.

Usage:
  bin/aws-node-info              # list known nodes
  bin/aws-node-info <label>      # detail one node
"""

from __future__ import annotations

import argparse
import sys

from discover import find_nodes, print_listing, resolve


def _print_detail(label: str) -> int:
    node = resolve(label)
    width = 16
    print(f"{'label':>{width}}: {node.label}")
    print(f"{'instance id':>{width}}: {node.instance_id}")
    print(f"{'state':>{width}}: {node.state}")
    print(f"{'instance type':>{width}}: {node.instance_type}")
    print(f"{'public ip':>{width}}: {node.public_ip or '-'}")
    print(f"{'public dns':>{width}}: {node.public_dns or '-'}")
    print(f"{'private ip':>{width}}: {node.private_ip or '-'}")
    if node.name:
        print(f"{'name tag':>{width}}: {node.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show info about a NodeExec-tagged EC2 instance.",
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

    return _print_detail(args.label)


if __name__ == "__main__":
    sys.exit(main())
