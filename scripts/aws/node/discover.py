"""Shared discovery for `bin/aws-node-*` helpers.

Lists EC2 instances tagged `NodeExec=true` (see
`infra/constructs/node_exec_tags.py`) and dispatches by
`NodeExecLabel`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import boto3


@dataclass(frozen=True)
class Node:
    label: str
    instance_id: str
    state: str
    instance_type: str
    public_ip: str | None
    public_dns: str | None
    private_ip: str | None
    name: str | None  # Name tag, if any


def _tags(instance: dict) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in instance.get("Tags", []) or []}


def find_nodes(ec2_client=None) -> list[Node]:
    ec2_client = ec2_client or boto3.client("ec2")
    paginator = ec2_client.get_paginator("describe_instances")
    nodes: list[Node] = []
    for page in paginator.paginate(
        Filters=[{"Name": "tag:NodeExec", "Values": ["true"]}],
    ):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = _tags(inst)
                label = tags.get("NodeExecLabel") or inst["InstanceId"]
                nodes.append(
                    Node(
                        label=label,
                        instance_id=inst["InstanceId"],
                        state=inst["State"]["Name"],
                        instance_type=inst["InstanceType"],
                        public_ip=inst.get("PublicIpAddress") or None,
                        public_dns=inst.get("PublicDnsName") or None,
                        private_ip=inst.get("PrivateIpAddress") or None,
                        name=tags.get("Name"),
                    )
                )
    return nodes


def resolve(label: str, ec2_client=None) -> Node:
    nodes = find_nodes(ec2_client)
    matches = [n for n in nodes if n.label == label and n.state != "terminated"]
    if not matches:
        labels = sorted({n.label for n in nodes if n.state != "terminated"})
        sys.exit(
            f"no node with NodeExecLabel={label!r}; "
            f"available: {labels if labels else '(none)'}"
        )
    if len(matches) > 1:
        # Multiple non-terminated matches -- ambiguous.
        ids = [n.instance_id for n in matches]
        sys.exit(
            f"multiple nodes with NodeExecLabel={label!r}: {ids}; "
            "narrow down (e.g. terminate the stale instance)"
        )
    return matches[0]


def print_listing(nodes: list[Node], stream=sys.stderr) -> None:
    if not nodes:
        print("no NodeExec-tagged EC2 instances found", file=stream)
        return
    for n in sorted(nodes, key=lambda x: (x.label, x.state)):
        pub = n.public_ip or "-"
        print(
            f"  {n.label:20s}  id={n.instance_id} state={n.state} "
            f"type={n.instance_type} public_ip={pub}",
            file=stream,
        )
