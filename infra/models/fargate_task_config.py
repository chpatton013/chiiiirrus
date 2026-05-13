from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class FargateTaskConfig:
    """Per-service ECS Fargate task sizing + rollout policy.

    cpu / memory_limit_mib:
      vCPU units (1024 = 1 vCPU) and MiB of RAM the task definition
      reserves on the Fargate fleet. Fargate enforces fixed CPU+memory
      pairings -- see AWS docs for valid combos.

    desired_count:
      How many copies of the task ECS keeps running. Defaults to 1.
      Increase to >=2 for stateless services that want zero-downtime
      rolling deploys (combined with min_healthy_percent=100, see
      below).

    min_healthy_percent:
      During a deploy, the minimum fraction of desired_count that ECS
      must keep healthy while it replaces tasks. AWS pairs this with
      a max_healthy_percent (CDK defaults to 200) to bound the rollout:

      - 100 = no downtime. ECS must keep all old tasks running until
        a new task replacement is healthy, then terminate the old
        one. With desired_count=1 that means a brief overlap where 2
        tasks run simultaneously -- fine for stateless services, but
        bad for tasks that share exclusive resources (EFS file locks,
        single-instance-only database migrations, port bindings on
        the same private IP).

      - 0 = serial replace. ECS may terminate the old task BEFORE the
        new one is healthy. With desired_count=1 this produces a
        brief outage window (~30-90s depending on image cold-start)
        but guarantees no overlap. Used by single-task services that
        share state on EFS (mail, matrix, webmail) where running two
        copies in parallel risks corrupting shared state or fighting
        over locks.

      We default to 100 because it's the safer choice for stateless
      services; services with EFS-shared state explicitly override to
      0 in config.toml.
    """

    cpu: int
    memory_limit_mib: int
    desired_count: int
    min_healthy_percent: int

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            cpu=data["cpu"],
            memory_limit_mib=data["memory_limit_mib"],
            desired_count=data.get("desired_count", 1),
            min_healthy_percent=data.get("min_healthy_percent", 100),
        )
