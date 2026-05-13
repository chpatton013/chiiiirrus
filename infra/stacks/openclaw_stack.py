import pathlib
from dataclasses import dataclass

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Stack,
    Fn,
    RemovalPolicy,
    Stack,
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_efs as efs,
    aws_events as events,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from constructs import Construct

from ..constructs.shared_efs_volume import EfsAccessPointSpec, SharedEfsVolume
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports


@dataclass(frozen=True)
class OpenClawImports:
    foundation: FoundationExports
    assets: AssetLoader
    matrix_homeserver_url: str
    allowed_sender: str


EFS_MOUNTPOINT_DIR = pathlib.Path("/data")
OPENCLAW_ROOT_DIR = EFS_MOUNTPOINT_DIR / "openclaw"
OPENCLAW_HOME_DIR = OPENCLAW_ROOT_DIR / "home"
OPENCLAW_STATE_DIR = OPENCLAW_ROOT_DIR / "state"
OPENCLAW_WORKSPACES_DIR = OPENCLAW_ROOT_DIR / "workspaces"
MATRIX_BOT_DIR = EFS_MOUNTPOINT_DIR / "matrix-bot"
MATRIX_BOT_INSTALL_DIR = pathlib.Path("/opt/openclaw-matrix-bot")
MATRIX_BOT_RUNTIME_DIR = pathlib.Path("/run/openclaw-matrix-bot")
MATRIX_BOT_TOKEN_SECRET = "matrix/openclaw-bot-token"
MATRIX_BOT_CONTROL_ROOM_PARAM = "/openclaw-matrix-bot/control-room-id"
# OpenClaw stores its gateway auth token inside its main state JSON
# file at `gateway.auth.token` rather than as a standalone file.
# The bot's prestart helper reads this state file and extracts the
# token into a sibling runtime file the bot consumes.
OPENCLAW_STATE_FILE = OPENCLAW_STATE_DIR / "openclaw.json"
NODESOURCE_KEY_URI = "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
NODESOURCE_REPO_URI = "https://deb.nodesource.com/node_24.x"
NODESOURCE_KEY_PATH = pathlib.Path("/etc/apt/keyrings/nodesource.gpg")
NODESOURCE_REPO_PATH = pathlib.Path("/etc/apt/sources.list.d/nodesource.list")
EFS_UTILS_INSTALLER_URI = "https://amazon-efs-utils.aws.com/efs-utils-installer.sh"
HOMEBREW_INSTALLER_URI = (
    "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
)
PNPM_INSTALLER_URI = "https://get.pnpm.io/install.sh"
# Pin pnpm. v11 changed install behavior such that
# `@matrix-org/matrix-sdk-crypto-nodejs/download-lib.js` (which we
# run by hand after `pnpm install --ignore-scripts`) can no longer
# resolve its `https-proxy-agent` transitive dep, breaking user-data
# build on every fresh instance.
PNPM_VERSION = "10.18.3"
BUILD_DEPENDENCIES = [
    "build-essential",
    "curl",
    "ca-certificates",
    "gcc",
    "git",
    "gnupg",
    "jq",
    "nfs-common",
    "nodejs",
    "openjdk-21-jre-headless",
    "trash-cli",
]
OPENCLAW_HOOKS = [
    "boot-md",
    "command-logger",
    "session-memory",
]


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
        # (sessions, memory, auth-profiles, workspaces) plus the
        # matrix bot's cross-signing keys. We do NOT want a
        # `cdk destroy OpenClawStack` to nuke that material -- instance
        # replacement leaves the EFS in place regardless of policy, but
        # a manual stack destroy without RETAIN would.
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
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
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

        # Matrix bot resources the EC2 instance pulls at runtime: the
        # bot's access token from Secrets Manager and the control
        # room ID from SSM Parameter Store. The control-room
        # parameter is created manually post-deploy after I invite
        # the bot to a fresh DM room.
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{Aws.REGION}:{Aws.ACCOUNT_ID}:secret:{MATRIX_BOT_TOKEN_SECRET}-*"
                ],
            )
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:PutParameter"],
                resources=[
                    f"arn:aws:ssm:{Aws.REGION}:{Aws.ACCOUNT_ID}:parameter{MATRIX_BOT_CONTROL_ROOM_PARAM}"
                ],
            )
        )

        # Upload the bot source as an S3 asset; user-data fetches and
        # installs it locally on the instance. node_modules + dist
        # are .gitignore'd and rebuilt on the host so the deploy
        # bundle stays small.
        # AGENT TODO: I wanted to start small with the scope of the bot for the
        # sake of getting things working, but now I want to share the bigger
        # picture of how I want this to work.
        # - First, focusing on the setup of the human users. Most of this
        # deployment has been built to automatically set up my own user account,
        # but I also want to add a user account for my partner. I don't think I
        # need to include her account in the authentik blueprints, since I plan
        # on making it for her in authentik by hand. Trying to automate that
        # would complicate the setup we have now with env-var injection; as long
        # as her account isn't lost during redeploys then I'm fine with setting
        # it up manually this one time.
        # - Second, onto agents. I want to have multiple openclaw agents, each
        # tailored for a specific purpose, and I want to use different matrix
        # rooms to talk to them. I want each of those agents to appear as their
        # own matrix username, with separate identities I can address in
        # messages. For the sake of giving practical examples, let's assign some
        # names. My name is Chris, my partner's name is Chelsea, and I have
        # three different AI agents in mind: Wadsworth, Sebastian, and Binx.
        # Each of these three agents are assistants to help keep our daily lives
        # humming along smoothly, but they have different roles.
        #   - Sebastian is my personal assistant, whom I can share sensitive
        #   information with (like planning a surprise for my partner without
        #   worrying about her finding out), assign all sorts of tasks to (like
        #   clean up my email inbox), etc.
        #   - Binx plays that same role for my partner.
        #   - Wadsworth is a shared assistant that both myself and my partner
        #   can talk to and assign tasks to that we don't mind the other
        #   learning about. For example, "plan a game night with the neighbors
        #   that works with our schedules".
        # The rationale behind splitting these roles across agents is to
        # partition information, personas, memory, etc so there's no chance of
        # cross-contamination. My partner and I can build Wadsworth together,
        # but we each retain our own private assistants.
        # I'm worried about the security implications of exposing the control ui
        # for openclaw on the internet (and I won't have much luck getting my
        # partner to connect to tailscale to access it), so I want to use Matrix
        # as the "safe" way to direct these agents (since E2EE in matrix rooms
        # gives me confidence that an external attacker hasn't hacked my matrix
        # server either).
        # I'd like to be able to use matrix rooms in the same way that ai chat
        # interfaces uses separate chats. The agent still has all the project
        # context (the other chats) for reference, but the primary context comes
        # from the chat history in that room specifically. Those rooms can have
        # multiple humans and/or multiple agents in them. For example, I may
        # make a room where I invite both Wadsworth and Sebastian so Sebastian
        # can distill and transfer some bit of relevant information to
        # Wadsworth. Or both Chelsea and I may join a room with Wadsworth where
        # we plan an event.
        # Eventually I'll make many more agents for different purposes, and I'll
        # want to make rooms mixing and matching many of them together. I don't
        # know the full set of agents and rooms I'll want, so I don't think that
        # information belongs encoded in this repo's IAC. Instead, I want the
        # code here to set up the building blocks that can enable this use
        # pattern. Let's brainstorm about what we would need to do to set that
        # up.
        bot_asset = s3_assets.Asset(
            self,
            "MatrixBotAsset",
            path=str(imports.assets.openclaw_bot_path()),
        )
        bot_asset.grant_read(role)
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

        hook_commands = "\n".join(
            f'sudo -iu ubuntu XDG_RUNTIME_DIR="/run/user/$(id -u ubuntu)" openclaw hooks enable {h}'
            for h in OPENCLAW_HOOKS
        )
        user_data_template = imports.assets.read_text("openclaw", "user-data.sh.tmpl")
        substitutions = {
            "NODESOURCE_KEY_URI": NODESOURCE_KEY_URI,
            "NODESOURCE_KEY_PATH": str(NODESOURCE_KEY_PATH),
            "NODESOURCE_REPO_URI": NODESOURCE_REPO_URI,
            "NODESOURCE_REPO_PATH": str(NODESOURCE_REPO_PATH),
            "BUILD_DEPENDENCIES": " ".join(BUILD_DEPENDENCIES),
            "EFS_UTILS_INSTALLER_URI": EFS_UTILS_INSTALLER_URI,
            "EFS_MOUNTPOINT_DIR": str(EFS_MOUNTPOINT_DIR),
            "EFS_FSTAB": efs_fstab,
            "OPENCLAW_HOME_DIR": str(OPENCLAW_HOME_DIR),
            "OPENCLAW_STATE_DIR": str(OPENCLAW_STATE_DIR),
            "OPENCLAW_WORKSPACES_DIR": str(OPENCLAW_WORKSPACES_DIR),
            "OPENCLAW_MAIN_WORKSPACE": str(OPENCLAW_WORKSPACES_DIR / "main"),
            "OPENCLAW_HOOK_COMMANDS": hook_commands,
            "HOMEBREW_INSTALLER_URI": HOMEBREW_INSTALLER_URI,
            "PNPM_INSTALLER_URI": PNPM_INSTALLER_URI,
            "PNPM_VERSION": PNPM_VERSION,
            "MATRIX_BOT_INSTALL_DIR": str(MATRIX_BOT_INSTALL_DIR),
            "MATRIX_BOT_DIR": str(MATRIX_BOT_DIR),
            "BOT_ASSET_S3_URI": f"s3://{bot_asset.s3_bucket_name}/{bot_asset.s3_object_key}",
            "HOMESERVER_URL": imports.matrix_homeserver_url,
            "ALLOWED_SENDER": imports.allowed_sender,
            "MATRIX_BOT_TOKEN_SECRET": MATRIX_BOT_TOKEN_SECRET,
            "MATRIX_BOT_CONTROL_ROOM_PARAM": MATRIX_BOT_CONTROL_ROOM_PARAM,
            "OPENCLAW_STATE_FILE": str(OPENCLAW_STATE_FILE),
        }
        rendered = user_data_template
        for key, value in substitutions.items():
            rendered = rendered.replace(f"@@{key}@@", value)
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

        backup_plan = backup.BackupPlan(
            self,
            "OpenClawBackupPlan",
            backup_plan_name="openclaw-efs-backups",
            backup_vault=foundation.backup_vault,
        )

        backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="daily-10-days",
                schedule_expression=events.Schedule.cron(minute="0", hour="5"),
                delete_after=Duration.days(10),
            )
        )
        backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="weekly-4-weeks",
                schedule_expression=events.Schedule.cron(
                    minute="0", hour="6", week_day="SUN"
                ),
                delete_after=Duration.days(28),
            )
        )
        backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="monthly-3-months",
                schedule_expression=events.Schedule.cron(minute="0", hour="7", day="1"),
                delete_after=Duration.days(90),
            )
        )
        backup_plan.add_selection(
            "EfsSelection",
            resources=[backup.BackupResource.from_efs_file_system(filesystem)],
        )

        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "InstancePublicIp", value=instance.instance_public_ip)
        CfnOutput(self, "EfsId", value=filesystem.file_system_id)
        CfnOutput(
            self,
            "SsmSessionExample",
            value=f"aws ssm start-session --target {instance.instance_id}",
        )
        CfnOutput(
            self,
            "SsmPortForwardExample",
            value=(
                f"aws ssm start-session --target {instance.instance_id} "
                "--document-name AWS-StartPortForwardingSession "
                '--parameters \'{"portNumber":["18789"],"localPortNumber":["18789"]}\''
            ),
        )
