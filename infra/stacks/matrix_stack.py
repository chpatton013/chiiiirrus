from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_events as events,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from ..constructs.db_exec_tags import tag_for_db_exec
from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.input_hash import expand_globs, hash_inputs
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.asset_loader import AssetLoader
from ..models.data_exports import DataExports
from ..models.foundation_exports import FoundationExports
from ..models.matrix_config import MatrixConfig

# Synapse listens on 8008 inside the container; ALB terminates TLS
# and forwards plain HTTP to this port. Both the client-server API
# and federation traffic share this single listener via the
# `client, federation` resources list - no separate 8448.
SYNAPSE_HTTP_PORT = 8008

# The Synapse Docker image runs as uid/gid 991. EFS access point
# enforces the same POSIX identity for files written by the task.
SYNAPSE_UID = "991"
SYNAPSE_GID = "991"

# Matrix `server_name` is the apex (e.g. example.com), so MXIDs
# look like `@yourname:example.com`. The actual Synapse listener runs
# at matrix.<public_domain>; federating peers find it via the
# `.well-known/matrix/server` JSON served from the apex by SiteStack.


@dataclass(frozen=True)
class MatrixImports:
    cfg: MatrixConfig
    foundation: FoundationExports
    data: DataExports
    assets: AssetLoader
    authentik_issuer_base: str


class MatrixStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: MatrixImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        data = imports.data

        # Init + bot-bootstrap scripts live under assets/matrix/ so
        # shellcheck/shfmt + future editors can inspect them as real
        # files instead of as r-strings inside this Python module.
        init_script = imports.assets.read_text("matrix", "init.sh")
        bootstrap_script = imports.assets.read_text("matrix", "bootstrap.sh")

        # Matrix `server_name` is the apex; the listener lives at
        # matrix.<public_domain>. .well-known delegation in SiteStack
        # tells federating peers and clients to look here.
        server_name = foundation.public_domain
        listener_fqdn = f"{cfg.subdomain}.{foundation.public_domain}"
        oidc_issuer = f"{imports.authentik_issuer_base}/matrix/"

        ###
        # Secrets

        db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DbSecret", cfg.db.secret_name
        )
        oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OidcSecret", "authentik/oidc/matrix"
        )

        ###
        # EFS for /data (signing key, media store, log config,
        # registration shared secret, generated homeserver.yaml).

        # AGENT TODO: Encapsulate this SG, FS, and AP as a construct that handls
        # all that for us.
        # The following kwargs should use these default values:
        # - allow_all_outbound=True
        # - vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
        # - encrypted=True,
        # - removal_policy=RemovalPolicy.RETAIN,
        # - performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
        # - throughput_mode=efs.ThroughputMode.BURSTING,
        # The following kwargs need to be explicitly provided:
        # - vpc
        # - access_points
        # All other kwargs should be taken as a ** group and passed to efs.FileSystem
        # access_points should be a list of dataclass objects that we define
        # with the following properties:
        # - id, client_token, create_acl, path, posix_user (all optional except id)
        # The construct should assign the following member properties:
        # - security_group
        # - filesystem
        efs_sg = ec2.SecurityGroup(
            self, "EfsSecurityGroup", vpc=foundation.vpc, allow_all_outbound=True
        )
        filesystem = efs.FileSystem(
            self,
            "MatrixFs",
            vpc=foundation.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_group=efs_sg,
            encrypted=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
        )
        access_point = filesystem.add_access_point(
            "DataAccessPoint",
            path="/synapse",
            create_acl=efs.Acl(
                owner_uid=SYNAPSE_UID, owner_gid=SYNAPSE_GID, permissions="750"
            ),
            posix_user=efs.PosixUser(uid=SYNAPSE_UID, gid=SYNAPSE_GID),
        )

        ###
        # Service: one Fargate task with init + main containers
        # sharing /data via EFS.

        common_environment = {
            "SYNAPSE_SERVER_NAME": server_name,
            "PUBLIC_BASEURL": f"https://{listener_fqdn}/",
            "SYNAPSE_PORT": str(SYNAPSE_HTTP_PORT),
            "DB_HOST": data.database.instance.db_instance_endpoint_address,
            "DB_PORT": str(data.database.port),
            "DB_NAME": cfg.db.name,
            "OIDC_ISSUER": oidc_issuer,
            "REMOTE_MEDIA_LIFETIME": cfg.remote_media_lifetime,
        }
        # Init container env additions only it needs: DB_USER plain,
        # DB_PASSWORD and OIDC_CLIENT_ID/SECRET as ECS secrets.
        init_environment = {
            **common_environment,
        }
        init_secrets = {
            "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
            "OIDC_CLIENT_ID": ecs.Secret.from_secrets_manager(oidc_secret, "client_id"),
            "OIDC_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                oidc_secret, "client_secret"
            ),
        }

        synapse_image = ecs.ContainerImage.from_registry(
            f"{foundation.dockerhub_mirror_base}/matrixdotorg/synapse:{cfg.image_version}"
        )

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="synapse",
            cpu=cfg.task.cpu,
            memory_limit_mib=cfg.task.memory_limit_mib,
            desired_count=cfg.task.desired_count,
            min_healthy_percent=cfg.task.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            container_kwargs=dict(
                image=synapse_image,
                port_mappings=[
                    ecs.PortMapping(
                        container_port=SYNAPSE_HTTP_PORT,
                        host_port=SYNAPSE_HTTP_PORT,
                    ),
                ],
                # Main container reads /data/homeserver.yaml that the
                # init container rendered. Synapse's image
                # entrypoint (/start.py) honors SYNAPSE_CONFIG_PATH
                # and skips its own generate step when the file is
                # present.
                environment={
                    "SYNAPSE_CONFIG_PATH": "/data/homeserver.yaml",
                    "SYNAPSE_SERVER_NAME": server_name,
                    "SYNAPSE_REPORT_STATS": "no",
                    # Same DB connection vars as the init container,
                    # for bin/db-sql: lets an ECS-Exec'd shell run
                    # python3 + psycopg2 (already bundled in the
                    # Synapse image) against the matrix DB without
                    # plumbing master credentials.
                    "DB_HOST": data.database.instance.db_instance_endpoint_address,
                    "DB_PORT": str(data.database.port),
                    "DB_NAME": cfg.db.name,
                },
                secrets={
                    "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
                    "DB_PASSWORD": ecs.Secret.from_secrets_manager(
                        db_secret, "password"
                    ),
                },
            ),
            health_check_grace_period=Duration.seconds(120),
        )
        service.grant_pull_through_cache(foundation.dockerhub_mirror_namespace)

        # Init container: same image, overridden entrypoint runs the
        # bash script above. essential=False + SUCCESS dependency on
        # the main container means init runs once per task start,
        # exits 0, then Synapse starts.
        init_container = service.task_defn.add_container(
            "Init",
            image=synapse_image,
            essential=False,
            entry_point=["bash", "-c"],
            command=[init_script],
            environment=init_environment,
            secrets=init_secrets,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="synapse-init",
                log_group=service.log_group,
            ),
        )

        ###
        # Shared /data EFS volume mounted by both containers.

        service.task_defn.add_volume(
            name="data",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=filesystem.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )
        service.container.add_mount_points(
            ecs.MountPoint(
                source_volume="data", container_path="/data", read_only=False
            )
        )
        init_container.add_mount_points(
            ecs.MountPoint(
                source_volume="data", container_path="/data", read_only=False
            )
        )
        service.container.add_container_dependencies(
            ecs.ContainerDependency(
                container=init_container,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )
        filesystem.grant_read_write(service.task_defn.task_role)
        efs_sg.add_ingress_rule(
            service.security_group,
            ec2.Port.tcp(2049),
            "Synapse task to EFS",
        )

        ###
        # ALB at matrix.<public_domain>. Terminates TLS, forwards
        # plain HTTP to Synapse on port 8008. Same listener serves
        # both Client-Server and federation traffic.

        alb = PublicHttpAlb(
            self,
            "PublicHttpAlb",
            fqdn=listener_fqdn,
            a_record=cfg.subdomain,
            zone=foundation.public_zone,
            vpc=foundation.vpc,
        )
        alb.https_listener.add_targets(
            "Targets",
            port=SYNAPSE_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/health", healthy_http_codes="200"),
        )

        ###
        # SG + DB wiring

        service.security_group.add_ingress_rule(
            alb.security_group,
            ec2.Port.tcp(SYNAPSE_HTTP_PORT),
            "ALB to Synapse HTTP",
        )
        data.database.grant_connect(
            self,
            "MatrixDbIngress",
            peer=service.service,
            description="Synapse to DB",
        )
        # Discoverable by bin/db-sql for ad-hoc SQL against the matrix
        # DB. Uses the matrix user (not master) -- sufficient for DML
        # on Synapse's own tables; the matrix user owns the schema.
        tag_for_db_exec(service.service, label="matrix")

        ###
        # Backups (signing key + media + registration secret all
        # live on EFS; Postgres covered by RDS automated snapshots).

        backup_plan = backup.BackupPlan(
            self,
            "MatrixBackupPlan",
            backup_plan_name="matrix-efs-backups",
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

        ###
        # Bot account bootstrap (Custom Resource -> ECS one-shot task)
        #
        # On first deploy, registers `@openclaw-bot:<server_name>`
        # via Synapse's shared-secret nonce flow and writes the
        # resulting access token to `matrix/openclaw-bot-token`.
        # The token persists; the OpenClaw EC2 host's bot service
        # reads it at startup. Idempotent on the secret value: the
        # Lambda no-ops if the token is already populated.
        #
        # Pre-create the secret manually before first deploy:
        #   echo '{"token":"pending"}' | bin/aws-write-secret matrix/openclaw-bot-token -

        bot_token_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "BotTokenSecret", "matrix/openclaw-bot-token"
        )

        bootstrap_log_group = logs.LogGroup(self, "BootstrapLogGroup")
        bootstrap_task_defn = ecs.FargateTaskDefinition(
            self,
            "BootstrapTaskDefn",
            cpu=256,
            memory_limit_mib=512,
        )
        bootstrap_task_defn.add_volume(
            name="data",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=filesystem.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )
        filesystem.grant_read(bootstrap_task_defn.task_role)
        # Bootstrap task uses the same Synapse image (provides
        # `register_new_matrix_user`); pull-through-cache grant on
        # the execution role is needed for the image fetch.
        stack = Stack.of(self)
        bootstrap_exec_role = bootstrap_task_defn.obtain_execution_role()
        bootstrap_exec_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        bootstrap_exec_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:CreateRepository",
                    "ecr:BatchImportUpstreamImage",
                ],
                resources=[
                    f"arn:aws:ecr:{stack.region}:{stack.account}:repository/{foundation.dockerhub_mirror_namespace}/*"
                ],
            )
        )

        bootstrap_container = bootstrap_task_defn.add_container(
            "BotBootstrap",
            image=synapse_image,
            essential=True,
            entry_point=["bash", "-c"],
            command=[bootstrap_script],
            environment={"HOMESERVER_URL": f"https://{listener_fqdn}"},
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="matrix-bot-bootstrap",
                log_group=bootstrap_log_group,
            ),
        )
        bootstrap_container.add_mount_points(
            ecs.MountPoint(source_volume="data", container_path="/data", read_only=True)
        )
        # Re-use the main service's SG so the bootstrap task can
        # mount EFS (EFS SG already accepts NFS from this SG) and
        # reach matrix.<public_domain> for the registration HTTP
        # call (allow_all_outbound on the SG is the default).
        bootstrap_fn = lambda_python.PythonFunction(
            self,
            "BootstrapFn",
            entry=str(imports.assets.lambda_path("matrix_bot_account")),
            runtime=lambda_.Runtime.PYTHON_3_12,
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(10),
            environment={
                "CLUSTER_ARN": foundation.cluster.cluster_arn,
                "TASK_DEFINITION_ARN": bootstrap_task_defn.task_definition_arn,
                "SUBNET_IDS": ",".join(
                    s.subnet_id for s in foundation.vpc.private_subnets
                ),
                "SECURITY_GROUP_IDS": service.security_group.security_group_id,
                "SECRET_ID": bot_token_secret.secret_name,
                "CONTAINER_NAME": bootstrap_container.container_name,
                "LOG_GROUP": bootstrap_log_group.log_group_name,
                "LOG_STREAM_PREFIX": "matrix-bot-bootstrap",
            },
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[bootstrap_task_defn.task_definition_arn],
            )
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks", "logs:GetLogEvents"],
                resources=["*"],
            )
        )
        bootstrap_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    bootstrap_task_defn.task_role.role_arn,
                    bootstrap_exec_role.role_arn,
                ],
            )
        )
        bot_token_secret.grant_read(bootstrap_fn)
        bot_token_secret.grant_write(bootstrap_fn)

        bootstrap_provider = cr.Provider(
            self,
            "BootstrapProvider",
            on_event_handler=cast(lambda_.IFunction, bootstrap_fn),
        )
        # Re-fires the bootstrap CR whenever any input changes:
        # the Lambda code that runs the registration, the bootstrap
        # bash script (with the BOT_USERNAME constant embedded), or
        # the homeserver URL the script POSTs to.
        bootstrap_trigger = hash_inputs(
            files=expand_globs(
                imports.assets.lambda_path("matrix_bot_account"), "**/*"
            ),
            env={"HOMESERVER_URL": f"https://{listener_fqdn}"},
            extra=bootstrap_script,
        )
        bootstrap_resource = CustomResource(
            self,
            "BotAccountBootstrap",
            service_token=bootstrap_provider.service_token,
            properties={"Trigger": bootstrap_trigger},
        )
        # Synapse must be live before the bootstrap task can hit
        # `/_synapse/admin/v1/register` and `/_matrix/client/v3/login`.
        bootstrap_resource.node.add_dependency(service.service)
