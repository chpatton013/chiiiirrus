import pathlib
from dataclasses import dataclass

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_efs as efs,
    aws_iam as iam,
)
from constructs import Construct

from ..constructs.node_exec_tags import tag_for_node_exec
from ..constructs.shared_efs_volume import EfsAccessPointSpec, SharedEfsVolume
from ..constructs.standard_backup_plan import StandardBackupPlan
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports


@dataclass(frozen=True)
class OpenClawImports:
    foundation: FoundationExports
    assets: AssetLoader


EFS_MOUNTPOINT_DIR = pathlib.Path("/data")
OPENCLAW_ROOT_DIR = EFS_MOUNTPOINT_DIR / "openclaw"
OPENCLAW_HOME_DIR = OPENCLAW_ROOT_DIR / "home"
OPENCLAW_STATE_DIR = OPENCLAW_ROOT_DIR / "state"
OPENCLAW_WORKSPACES_DIR = OPENCLAW_ROOT_DIR / "workspaces"


def parse_bool(s: str) -> bool:
    return s.strip().lower() == "true"


class OpenClawStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: OpenClawImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        foundation = imports.foundation
        user_data_replace = parse_bool(
            self.node.try_get_context("userDataReplace") or "false"
        )

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        instance_sg = ec2.SecurityGroup(
            self,
            "InstanceSecurityGroup",
            vpc=vpc,
            allow_all_outbound=True,
            description="OpenClaw instance security group",
        )

        # OpenClaw's VPC is single-tier public (no NAT, no isolated
        # subnets), so override the construct's PRIVATE_WITH_EGRESS
        # default. RETAIN: this EFS holds openclaw agent state
        # (sessions, memory, auth-profiles, workspaces, matrix
        # crypto stores). We do NOT want a `cdk destroy OpenClawStack`
        # to nuke that material -- instance replacement leaves the EFS
        # in place regardless of policy, but a manual stack destroy
        # without RETAIN would.
        efs_volume = SharedEfsVolume(
            self,
            "OpenClawEfs",
            vpc=vpc,
            access_points=[
                EfsAccessPointSpec(
                    id="OpenClawAccessPoint",
                    path="/openclaw",
                    create_acl=efs.Acl(
                        owner_gid="1000", owner_uid="1000", permissions="750"
                    ),
                    posix_user=efs.PosixUser(gid="1000", uid="1000"),
                ),
            ],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        efs_sg = efs_volume.security_group
        efs_sg.add_ingress_rule(
            instance_sg, ec2.Port.tcp(2049), "NFS from OpenClaw instance"
        )
        filesystem = efs_volume.filesystem
        access_point = efs_volume.access_points["OpenClawAccessPoint"]

        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal(
                "ec2.amazonaws.com"
            ),  # pyright: ignore[reportArgumentType]
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonElasticFileSystemClientFullAccess"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
            ],
        )
        filesystem.grant_read_write(role)
        # efs-utils' mount path resolves `fs-xxx.efs.<region>` via the
        # VPC's Amazon-provided DNS resolver; if those records aren't
        # yet propagated (typical right after mount-target recreation)
        # it falls back to looking up MT IPs through the EC2 API.
        # Grant the API read so that fallback succeeds.
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeAvailabilityZones"],
                resources=["*"],
            )
        )

        efs_fstab = " ".join(
            [
                f"{filesystem.file_system_id}:/",
                str(EFS_MOUNTPOINT_DIR),
                "efs",
                f"_netdev,tls,iam,accesspoint={access_point.access_point_id}",
                "0",
                "0",
            ]
        )

        ubuntu_ami = ec2.MachineImage.from_ssm_parameter(
            "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
            os=ec2.OperatingSystemType.LINUX,
        )

        rendered = imports.assets.render_template(
            "openclaw",
            "user-data.sh.tmpl",
            substitutions={
                "EFS_MOUNTPOINT_DIR": str(EFS_MOUNTPOINT_DIR),
                "EFS_FSTAB": efs_fstab,
                "OPENCLAW_HOME_DIR": str(OPENCLAW_HOME_DIR),
                "OPENCLAW_STATE_DIR": str(OPENCLAW_STATE_DIR),
                "OPENCLAW_WORKSPACES_DIR": str(OPENCLAW_WORKSPACES_DIR),
                "OPENCLAW_MAIN_WORKSPACE": str(OPENCLAW_WORKSPACES_DIR / "main"),
            },
        )
        user_data = ec2.UserData.custom(rendered)

        instance = ec2.Instance(
            self,
            "OpenClawInstance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=instance_sg,
            role=role,  # pyright: ignore[reportArgumentType]
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ubuntu_ami,
            user_data=user_data,
            user_data_causes_replacement=user_data_replace,
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size=20,
                        encrypted=True,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        delete_on_termination=True,
                    ),
                ),
            ],
        )

        backup_plan = StandardBackupPlan(
            self,
            "OpenClawBackupPlan",
            backup_plan_name="openclaw-efs-backups",
            backup_vault=foundation.backup_vault,
        )
        backup_plan.backup_plan.add_selection(
            "EfsSelection",
            resources=[backup.BackupResource.from_efs_file_system(filesystem)],
        )

        # Discoverable by bin/node-* helpers (info/shell/exec/portforward).
        tag_for_node_exec(instance, label="openclaw")
