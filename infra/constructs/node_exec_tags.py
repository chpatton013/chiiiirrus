"""Tag an EC2 instance for discovery by `bin/node-*` helpers.

The node-* tools list EC2 instances tagged `NodeExec=true` and
dispatch by `NodeExecLabel`. Mirrors the `db_exec_tags`
mechanism for ECS services. Apply this to any instance you'd
like to manage via SSM Session Manager from the operator
machine (openclaw, turn, etc.).
"""

from aws_cdk import Tags, aws_ec2 as ec2


def tag_for_node_exec(instance: ec2.IInstance, *, label: str) -> None:
    tags = Tags.of(instance)
    tags.add("NodeExec", "true")
    tags.add("NodeExecLabel", label)
