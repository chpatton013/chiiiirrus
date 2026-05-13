"""An EFS file system + its security group + N access points, packaged
as a single construct so per-stack EFS mounts don't have to repeat the
8-line `SecurityGroup` + `FileSystem` + `add_access_point` recipe.

Defaults match the Matrix reference pattern (encrypted at rest, RETAIN
on removal, general-purpose performance, bursting throughput, private
subnets with egress, SG outbound open). Anything else can be overridden
via `**kwargs`, which are forwarded to `efs.FileSystem`.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True)
class SharedEfsVolumeAccessPoints:
    """Helper container giving callers dict-like access to the
    construct's access points by spec id."""

    by_id: dict[str, efs.AccessPoint] = field(default_factory=dict)

    def __getitem__(self, key: str) -> efs.AccessPoint:
        return self.by_id[key]


class SharedEfsVolume(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        access_points: Sequence[EfsAccessPointSpec],
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id)

        self.security_group = ec2.SecurityGroup(
            self, "SecurityGroup", vpc=vpc, allow_all_outbound=True
        )

        fs_kwargs: dict[str, Any] = dict(
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_group=self.security_group,
            encrypted=True,
            removal_policy=RemovalPolicy.RETAIN,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
        )
        fs_kwargs.update(kwargs)
        self.filesystem = efs.FileSystem(self, "FileSystem", **fs_kwargs)

        self.access_points: dict[str, efs.AccessPoint] = {}
        for spec in access_points:
            ap_kwargs: dict[str, Any] = {}
            if spec.client_token is not None:
                ap_kwargs["client_token"] = spec.client_token
            if spec.create_acl is not None:
                ap_kwargs["create_acl"] = spec.create_acl
            if spec.path is not None:
                ap_kwargs["path"] = spec.path
            if spec.posix_user is not None:
                ap_kwargs["posix_user"] = spec.posix_user
            self.access_points[spec.id] = self.filesystem.add_access_point(
                spec.id, **ap_kwargs
            )
