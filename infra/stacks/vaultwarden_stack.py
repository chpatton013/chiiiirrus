from dataclasses import dataclass

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.foundation_exports import FoundationExports
from ..models.data_exports import DataExports
from ..models.vaultwarden_config import VaultwardenConfig

VAULTWARDEN_HTTP_PORT = 80


@dataclass(frozen=True)
class VaultwardenImports:
    cfg: VaultwardenConfig
    foundation: FoundationExports
    data: DataExports
    authentik_issuer_base: str


class VaultwardenStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: VaultwardenImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        data = imports.data

        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"
        oidc_issuer = f"{imports.authentik_issuer_base}/vaultwarden/"

        ###
        # Secrets

        admin_token_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "AdminTokenSecret", "vaultwarden/admin-token"
        )
        db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DbSecret", cfg.db.secret_name
        )
        oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OidcSecret", "authentik/oidc/vaultwarden"
        )

        ###
        # EFS for /data

        efs_sg = ec2.SecurityGroup(
            self, "EfsSecurityGroup", vpc=foundation.vpc, allow_all_outbound=True
        )
        filesystem = efs.FileSystem(
            self,
            "DataFs",
            vpc=foundation.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_group=efs_sg,
            encrypted=True,
            removal_policy=RemovalPolicy.RETAIN,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=efs.ThroughputMode.BURSTING,
        )
        access_point = filesystem.add_access_point(
            "DataAccessPoint",
            path="/vaultwarden",
            create_acl=efs.Acl(owner_gid="1000", owner_uid="1000", permissions="750"),
            posix_user=efs.PosixUser(gid="1000", uid="1000"),
        )

        ###
        # Service

        environment = {
            "DOMAIN": f"https://{fqdn}",
            # SIGNUPS_ALLOWED=true + SSO_ONLY=true: any Authentik-authenticated
            # user can register on first SSO login; password login + signup are
            # fully disabled. The Authentik blueprint imposes no group filter,
            # so the gating is "must have an Authentik account."
            "SIGNUPS_ALLOWED": "true",
            "SSO_ONLY": "true",
            "INVITATIONS_ALLOWED": "true",
            "WEBSOCKET_ENABLED": "true",
            "DATA_FOLDER": "/data",
            "ROCKET_PORT": str(VAULTWARDEN_HTTP_PORT),
            "ROCKET_ADDRESS": "0.0.0.0",
            "SMTP_HOST": cfg.smtp.host,
            "SMTP_PORT": str(cfg.smtp.port),
            "SMTP_SECURITY": "starttls",
            "SMTP_FROM": cfg.smtp.from_email_address,
            "DB_HOST": data.database.instance.db_instance_endpoint_address,
            "DB_PORT": str(data.database.port),
            "DB_NAME": cfg.db.name,
            "LOG_LEVEL": "info",
            "SSO_ENABLED": "true",
            "SSO_AUTHORITY": oidc_issuer,
            "SSO_PKCE": "true",
            "SSO_SIGNUPS_MATCH_EMAIL": "true",
        }
        secrets = {
            "ADMIN_TOKEN": ecs.Secret.from_secrets_manager(
                admin_token_secret, "secret"
            ),
            "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
            "SSO_CLIENT_ID": ecs.Secret.from_secrets_manager(oidc_secret, "client_id"),
            "SSO_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                oidc_secret, "client_secret"
            ),
        }

        image = ecs.ContainerImage.from_registry(
            f"{foundation.dockerhub_mirror_base}/vaultwarden/server:{cfg.image_version}"
        )

        # Vaultwarden needs DATABASE_URL as a single string. ECS secrets cannot
        # be interpolated into other env vars, so assemble it in a shell wrapper
        # before exec'ing the image's default entrypoint.
        entry_command = [
            "sh",
            "-c",
            'export DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=require"; '
            "exec /start.sh",
        ]

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="vaultwarden",
            cpu=cfg.task.cpu,
            memory_limit_mib=cfg.task.memory_limit_mib,
            desired_count=cfg.task.desired_count,
            min_healthy_percent=cfg.task.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            container_kwargs=dict(
                image=image,
                port_mappings=[
                    ecs.PortMapping(
                        container_port=VAULTWARDEN_HTTP_PORT,
                        host_port=VAULTWARDEN_HTTP_PORT,
                    ),
                ],
                command=entry_command,
                environment=environment,
                secrets=secrets,
            ),
        )
        service.grant_pull_through_cache(foundation.dockerhub_mirror_namespace)

        ###
        # EFS mount

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
        filesystem.grant_read_write(service.task_defn.task_role)
        efs_sg.add_ingress_rule(
            service.security_group,
            ec2.Port.tcp(2049),
            "Vaultwarden task to EFS",
        )

        ###
        # ALB + routing

        alb = PublicHttpAlb(
            self,
            "PublicHttpAlb",
            fqdn=fqdn,
            a_record=cfg.subdomain,
            zone=foundation.public_zone,
            vpc=foundation.vpc,
        )

        alb.https_listener.add_targets(
            "Targets",
            port=VAULTWARDEN_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(path="/alive", healthy_http_codes="200"),
        )

        ###
        # Security group + DB wiring

        service.security_group.add_ingress_rule(
            alb.security_group,
            ec2.Port.tcp(VAULTWARDEN_HTTP_PORT),
            "ALB to Vaultwarden HTTP",
        )
        data.database.grant_connect(
            self,
            "VaultwardenDbIngress",
            peer=service.service,
            description="Vaultwarden to DB",
        )
