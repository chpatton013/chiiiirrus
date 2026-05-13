"""An EFS file system + its security group + N access points, packaged
as a single construct.

Defaults match the Matrix reference: encrypted at rest, RETAIN on
removal, general-purpose performance, bursting throughput,
private-with-egress subnets, SG outbound open. All overridable via
the explicit kwargs.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from aws_cdk import (
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_efs as efs,
)
from constructs import Construct


@dataclass(frozen=True)
class EfsAccessPointSpec:
    """Spec for one access point on the shared file system.

    `id` is the construct id used when CDK adds the AP to the
    file system AND the key under which the resulting `efs.AccessPoint`
    is exposed via `SharedEfsVolume.access_points`. Other fields map
    one-to-one onto `FileSystem.add_access_point` kwargs and are all
    optional (omit for EFS defaults).
    """

    id: str
    client_token: str | None = None
    create_acl: efs.Acl | None = None
    path: str | None = None
    posix_user: efs.PosixUser | None = None


class SharedEfsVolume(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        access_points: Sequence[EfsAccessPointSpec],
        vpc_subnets: ec2.SubnetSelection | None = None,
        encrypted: bool = True,
        removal_policy: RemovalPolicy = RemovalPolicy.RETAIN,
        performance_mode: efs.PerformanceMode = efs.PerformanceMode.GENERAL_PURPOSE,
        throughput_mode: efs.ThroughputMode = efs.ThroughputMode.BURSTING,
        lifecycle_policy: efs.LifecyclePolicy | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        if vpc_subnets is None:
            vpc_subnets = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )

        self.security_group = ec2.SecurityGroup(
            self, "SecurityGroup", vpc=vpc, allow_all_outbound=True
        )
        self.filesystem = efs.FileSystem(
            self,
            "FileSystem",
            vpc=vpc,
            vpc_subnets=vpc_subnets,
            security_group=self.security_group,
            encrypted=encrypted,
            removal_policy=removal_policy,
            performance_mode=performance_mode,
            throughput_mode=throughput_mode,
            lifecycle_policy=lifecycle_policy,
        )

        self.access_points: dict[str, efs.AccessPoint] = {}
        for spec in access_points:
            self.access_points[spec.id] = self.filesystem.add_access_point(
                spec.id,
                client_token=spec.client_token,
                create_acl=spec.create_acl,
                path=spec.path,
                posix_user=spec.posix_user,
            )
