from typing import Any

from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class PrivateEgressFargateService(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stream_prefix: str,
        cpu: int,
        memory_limit_mib: int,
        desired_count: int,
        min_healthy_percent: int,
        vpc: ec2.IVpc,
        cluster: ecs.ICluster,
        container_kwargs: dict[str, Any],
        health_check_grace_period: Duration | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.security_group = ec2.SecurityGroup(
            self, "SecurityGroup", vpc=vpc, allow_all_outbound=True
        )
        self.log_group = logs.LogGroup(self, "LogGroup")
        self.task_defn = ecs.FargateTaskDefinition(
            self,
            "TaskDefn",
            cpu=cpu,
            memory_limit_mib=memory_limit_mib,
        )
        self.container = self.task_defn.add_container(
            "Container",
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix=stream_prefix, log_group=self.log_group
            ),
            **container_kwargs,
        )
        service_kwargs: dict[str, Any] = dict(
            cluster=cluster,
            task_definition=self.task_defn,
            desired_count=desired_count,
            min_healthy_percent=min_healthy_percent,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False),
            assign_public_ip=False,
            security_groups=[self.security_group],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            enable_execute_command=True,
        )
        if health_check_grace_period is not None:
            # Suppresses ELB health-check failures for the first N
            # seconds of a fresh task. Useful when the container is a
            # large bundle (e.g. docker-mailserver bringing up
            # postfix/dovecot/rspamd/clamav serially) where the ELB
            # default of 60s before "unhealthy" is tighter than first-
            # boot needs.
            service_kwargs["health_check_grace_period"] = health_check_grace_period
        self.service = ecs.FargateService(
            self,
            "Service",
            **service_kwargs,
        )
        self.task_defn.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                resources=["*"],
            )
        )

    def grant_pull_through_cache(self, namespace: str) -> None:
        stack = Stack.of(self)
        execution_role = self.task_defn.obtain_execution_role()
        repo_arn = (
            f"arn:aws:ecr:{stack.region}:{stack.account}:repository/{namespace}/*"
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:CreateRepository",
                    "ecr:BatchImportUpstreamImage",
                ],
                resources=[repo_arn],
            )
        )
