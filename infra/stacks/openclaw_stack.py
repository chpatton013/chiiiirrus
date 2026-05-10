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

        efs_sg = ec2.SecurityGroup(
            self,
            "EfsSecurityGroup",
            vpc=vpc,
            allow_all_outbound=True,
            description="EFS security group",
        )
        efs_sg.add_ingress_rule(
            instance_sg, ec2.Port.tcp(2049), "NFS from OpenClaw instance"
        )

        filesystem = efs.FileSystem(
            self,
            "OpenClawEfs",
            vpc=vpc,
            security_group=efs_sg,
            encrypted=True,
            removal_policy=RemovalPolicy.DESTROY,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
        )

        access_point = filesystem.add_access_point(
            "OpenClawAccessPoint",
            path="/openclaw",
            create_acl=efs.Acl(owner_gid="1000", owner_uid="1000", permissions="750"),
            posix_user=efs.PosixUser(gid="1000", uid="1000"),
        )

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

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1",
            "set -euxo pipefail",
            "trap 'echo USERDATA FAILED on line $LINENO' ERR",
            "export DEBIAN_FRONTEND=noninteractive",
            # Add nodesource apt repo.
            "install -d -m 0755 /etc/apt/keyrings",
            f"curl -fsSL {NODESOURCE_KEY_URI} | gpg --dearmor -o {NODESOURCE_KEY_PATH!s}",
            f'echo "deb [signed-by={NODESOURCE_KEY_PATH!s}] {NODESOURCE_REPO_URI} nodistro main" >{NODESOURCE_REPO_PATH!s}',
            # Install build dependencies from apt repos.
            "apt-get update",
            f"apt-get install -y {' '.join(BUILD_DEPENDENCIES)}",
            # Enable service lingering for ubuntu user. This allows the openclaw
            # gateway service to run as a user service instead of a system
            # service.
            "loginctl enable-linger ubuntu",
            # Install EFS Utils from official installer.
            f"curl -fsSL {EFS_UTILS_INSTALLER_URI} | sh -s -- --install",
            # Set up EFS mount point.
            f"mkdir -p {EFS_MOUNTPOINT_DIR!s}",
            f'echo "{efs_fstab}" >>/etc/fstab',
            "mount -a",
            f"mountpoint -q {EFS_MOUNTPOINT_DIR!s}",
            # Install OpenClaw globally from npm.
            f"mkdir -p {OPENCLAW_HOME_DIR!s} {OPENCLAW_STATE_DIR!s} {OPENCLAW_WORKSPACES_DIR!s}",
            f"chown -R ubuntu:ubuntu {OPENCLAW_HOME_DIR!s} {OPENCLAW_STATE_DIR!s} {OPENCLAW_WORKSPACES_DIR!s}",
            "npm install -g openclaw@latest",
            "\n".join(
                [
                    "cat >/etc/profile.d/openclaw.sh <<'EOF'",
                    f"export OPENCLAW_HOME={OPENCLAW_HOME_DIR!s}",
                    f"export OPENCLAW_STATE_DIR={OPENCLAW_STATE_DIR!s}",
                    "EOF",
                ]
            ),
            "chmod 0644 /etc/profile.d/openclaw.sh",
            # Install Linux Homebrew as the ssm-user user from the official
            # installer.
            # NOTE: Homebrew installer refuses to run as root, but does need to
            # use sudo for root permissions. `brew shellenv` returns nothing for
            # root user, so it must be run as a non-privileged user.
            # ssm-user is normally created lazily by SSM on first interactive
            # session; on a freshly-replaced instance it doesn't exist yet, so
            # create it explicitly before brew tries to `sudo -iu` into it.
            "id ssm-user >/dev/null 2>&1 || useradd -m -s /bin/bash ssm-user",
            'install -d -m 0755 /etc/sudoers.d && echo "ssm-user ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/ssm-user',
            f"curl -fsSL {HOMEBREW_INSTALLER_URI} | sudo -iu ssm-user /bin/bash",
            " ".join(
                [
                    "sudo -u ssm-user /home/linuxbrew/.linuxbrew/bin/brew shellenv",
                    "| tee /etc/profile.d/homebrew.sh >/dev/null",
                ]
            ),
            "chmod 0644 /etc/profile.d/homebrew.sh",
            # Install pnpm for the ubuntu user from the official installer.
            f"curl -fsSL {PNPM_INSTALLER_URI} | sudo -iu ubuntu /bin/bash",
            # Configure openclaw onboarding.
            # NOTE: We both use `sudo -i` and set XDG_RUNTIME_DIR to allow
            # systemctl to enable user services without a reboot or login cycle.
            " ".join(
                [
                    'sudo -iu ubuntu XDG_RUNTIME_DIR="/run/user/$(id -u ubuntu)"',
                    "openclaw onboard",
                    "--non-interactive --accept-risk",
                    "--mode local --tailscale serve",
                    "--install-daemon --gateway-auth token --gateway-bind loopback",
                    "--daemon-runtime node --node-manager pnpm",
                    f"--workspace {(OPENCLAW_WORKSPACES_DIR / "main")!s}",
                    "--auth-choice skip --skip-channels --skip-search --skip-skills",
                    "--json",
                ]
            ),
            *(
                " ".join(
                    [
                        'sudo -iu ubuntu XDG_RUNTIME_DIR="/run/user/$(id -u ubuntu)"',
                        f"openclaw hooks enable {hook}",
                    ]
                )
                for hook in OPENCLAW_HOOKS
            ),
            # TODO: Enable after https://github.com/openclaw/openclaw/pull/63679 merges.
            # " ".join([
            #     "sudo -iu ubuntu XDG_RUNTIME_DIR=\"/run/user/$(id -u ubuntu)\"",
            #     "openclaw completion --install",
            # ]),
            # Matrix bot install: fetch the bot source bundle from
            # S3, unpack into /opt, install deps + build, then drop a
            # systemd user unit that runs it as ubuntu. The bot's
            # E2E + sync state lives under /data/matrix-bot on EFS so
            # device verification persists across instance
            # replacements.
            "apt-get install -y unzip python3",
            # AWS CLI v2 from the official installer (Ubuntu 24.04
            # dropped the `awscli` apt package in favor of the snap;
            # the standalone installer is the lighter dependency).
            'curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip',
            "unzip -oq /tmp/awscliv2.zip -d /tmp",
            "/tmp/aws/install --update",
            f"mkdir -p {MATRIX_BOT_INSTALL_DIR!s} {MATRIX_BOT_DIR!s}",
            f"chown -R ubuntu:ubuntu {MATRIX_BOT_DIR!s}",
            f"aws s3 cp s3://{bot_asset.s3_bucket_name}/{bot_asset.s3_object_key} /tmp/openclaw_bot.zip",
            f"unzip -oq /tmp/openclaw_bot.zip -d {MATRIX_BOT_INSTALL_DIR!s}",
            f"chown -R ubuntu:ubuntu {MATRIX_BOT_INSTALL_DIR!s}",
            f"chmod +x {MATRIX_BOT_INSTALL_DIR!s}/scripts/prestart",
            # pnpm is user-installed at ~/.local/share/pnpm/bin; the
            # PATH export lives in ~/.bashrc which non-interactive
            # shells skip via the early `*i*) ;; *) return ;;`
            # guard, so call the binary by absolute path.
            #
            # `--ignore-scripts` keeps pnpm v11 from blowing up
            # under --frozen-lockfile when an indirect dep has a
            # build script (matrix-js-sdk pulls
            # matrix-sdk-crypto-wasm, which has none, but other
            # transitive deps might).
            #
            # `tsc` is invoked directly rather than via `pnpm run
            # build` because `pnpm run` does an extra dep-status
            # round-trip that crashes against the readonly
            # node_modules layout pnpm leaves on disk in some
            # configurations.
            "sudo -iu ubuntu bash -c '"
            + " && ".join(
                [
                    f"cd {MATRIX_BOT_INSTALL_DIR!s}",
                    "~/.local/share/pnpm/bin/pnpm install --frozen-lockfile --ignore-scripts",
                    "./node_modules/.bin/tsc",
                ]
            )
            + "'",
            "install -d -m 0755 /home/ubuntu/.config/systemd/user",
            "\n".join(
                [
                    "cat >/home/ubuntu/.config/systemd/user/openclaw-matrix-bot.service <<'EOF'",
                    "[Unit]",
                    "Description=OpenClaw Matrix bot",
                    "After=network-online.target",
                    "Wants=network-online.target",
                    "",
                    "[Service]",
                    "Type=simple",
                    f"WorkingDirectory={MATRIX_BOT_INSTALL_DIR!s}",
                    "RuntimeDirectory=openclaw-matrix-bot",
                    "RuntimeDirectoryMode=0700",
                    f"Environment=HOMESERVER_URL={imports.matrix_homeserver_url}",
                    f"Environment=ALLOWED_SENDER={imports.allowed_sender}",
                    f"Environment=MATRIX_BOT_DATA_DIR={MATRIX_BOT_DIR!s}",
                    "Environment=OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789",
                    "Environment=OPENCLAW_GATEWAY_TOKEN_FILE=%t/openclaw-matrix-bot/gateway-token",
                    "Environment=BOT_ACCESS_TOKEN_FILE=%t/openclaw-matrix-bot/access-token",
                    f"Environment=BOT_TOKEN_SECRET_ID={MATRIX_BOT_TOKEN_SECRET}",
                    f"Environment=CONTROL_ROOM_PARAM={MATRIX_BOT_CONTROL_ROOM_PARAM}",
                    f"Environment=OPENCLAW_STATE_FILE={OPENCLAW_STATE_FILE!s}",
                    "EnvironmentFile=-%t/openclaw-matrix-bot/env",
                    f"ExecStartPre={MATRIX_BOT_INSTALL_DIR!s}/scripts/prestart",
                    f"ExecStart=/usr/bin/node {MATRIX_BOT_INSTALL_DIR!s}/dist/index.js",
                    "Restart=on-failure",
                    "RestartSec=10s",
                    "",
                    "[Install]",
                    "WantedBy=default.target",
                    "EOF",
                ]
            ),
            "chown ubuntu:ubuntu /home/ubuntu/.config/systemd/user/openclaw-matrix-bot.service",
            "chmod 0644 /home/ubuntu/.config/systemd/user/openclaw-matrix-bot.service",
            # Enable + start. Will fail loudly if the SSM parameter
            # isn't yet populated; that's expected on first deploy
            # before I create the control room. `restart` after
            # populating the param is the manual recovery.
            " ".join(
                [
                    'sudo -iu ubuntu XDG_RUNTIME_DIR="/run/user/$(id -u ubuntu)"',
                    "systemctl --user daemon-reload",
                ]
            ),
            " ".join(
                [
                    'sudo -iu ubuntu XDG_RUNTIME_DIR="/run/user/$(id -u ubuntu)"',
                    "systemctl --user enable openclaw-matrix-bot.service",
                ]
            ),
            "",
        )

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
