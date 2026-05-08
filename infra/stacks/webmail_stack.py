from dataclasses import dataclass

from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.webmail_config import WebmailConfig
from .mail_stack import MailExports

ROUNDCUBE_HTTP_PORT = 80
ROUNDCUBE_DATA_DIR = "/var/roundcube"


@dataclass(frozen=True)
class WebmailImports:
    cfg: WebmailConfig
    foundation: FoundationExports
    mail: MailExports
    assets: AssetLoader
    # Mail server hostname (smtp.<public_domain>) - Roundcube talks to
    # IMAPS:993 + STARTTLS:587 through the public NLB.
    mail_fqdn: str
    # Authentik issuer base URL. Roundcube composes its OAuth2
    # authorize/token/userinfo URLs from this and runs the OAuth flow
    # itself (no ALB-OIDC gate), exchanging codes for an access token
    # used as XOAUTH2 SASL credentials for IMAP and SMTP.
    authentik_issuer_base: str


class WebmailStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: WebmailImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        mail = imports.mail
        assets = imports.assets
        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OidcSecret", "authentik/oidc/roundcube"
        )

        image = ecs.ContainerImage.from_registry(
            f"{foundation.dockerhub_mirror_base}/roundcube/roundcubemail"
            f":{cfg.image_version}"
        )

        environment = {
            # IMAPS for inbound; ssl:// prefix tells Roundcube to use
            # implicit TLS instead of STARTTLS.
            "ROUNDCUBEMAIL_DEFAULT_HOST": f"ssl://{imports.mail_fqdn}",
            "ROUNDCUBEMAIL_DEFAULT_PORT": "993",
            # STARTTLS submission for outbound. tls:// = STARTTLS.
            "ROUNDCUBEMAIL_SMTP_SERVER": f"tls://{imports.mail_fqdn}",
            "ROUNDCUBEMAIL_SMTP_PORT": "587",
            # Auto-append @<public_domain> so users type just the
            # localpart at the IMAP login.
            "ROUNDCUBEMAIL_USERNAME_DOMAIN": foundation.public_domain,
            "ROUNDCUBEMAIL_DB_TYPE": "sqlite",
            "ROUNDCUBEMAIL_DB_DIR": f"{ROUNDCUBE_DATA_DIR}/db",
            "ROUNDCUBEMAIL_PLUGINS": "archive,zipdownload,managesieve",
        }
        # Init-only env: Authentik issuer base. The OIDC client_id /
        # client_secret are injected by ECS as task-launch secrets
        # (see init_secrets below) so the init container can template
        # them directly without an AWS API call.
        init_environment = {
            "AUTHENTIK_ISSUER_BASE": imports.authentik_issuer_base,
        }
        init_secrets = {
            "OAUTH_CLIENT_ID": ecs.Secret.from_secrets_manager(
                oidc_secret, "client_id"
            ),
            "OAUTH_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                oidc_secret, "client_secret"
            ),
        }

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="webmail",
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
                        container_port=ROUNDCUBE_HTTP_PORT,
                        host_port=ROUNDCUBE_HTTP_PORT,
                    ),
                ],
                environment=environment,
                # Image default CMD (apache2-foreground) is left as-is.
                # The upstream entrypoint (docker-entrypoint.sh) only
                # runs its config-generation logic when $1 starts with
                # apache2 / php-fpm / bin, so any CMD override breaks
                # config setup. The entrypoint already includes any
                # `/var/roundcube/config/*.php` files in the generated
                # config.docker.inc.php, which is exactly where our
                # init container writes oauth.inc.php.
            ),
        )
        service.grant_pull_through_cache(foundation.dockerhub_mirror_namespace)

        ###
        # EFS volume mount: Roundcube state (sqlite + override config)
        # lives on the existing MailStack EFS access point so backups
        # come along for free.

        service.task_defn.add_volume(
            name="roundcube",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=mail.efs_filesystem.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=mail.roundcube_access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )
        service.container.add_mount_points(
            ecs.MountPoint(
                source_volume="roundcube",
                container_path=ROUNDCUBE_DATA_DIR,
                read_only=False,
            )
        )
        mail.efs_filesystem.grant_read_write(service.task_defn.task_role)
        # CfnSecurityGroupIngress (rather than `add_ingress_rule` on the
        # imported mail SG) so the ingress resource lives in this
        # stack. Adding via the L2 method would put the rule in
        # MailStack with a back-reference to WebmailStack's SG, which
        # cycles since WebmailStack already depends on MailStack for
        # the EFS file system.
        ec2.CfnSecurityGroupIngress(
            self,
            "MailEfsIngress",
            group_id=mail.efs_security_group.security_group_id,
            source_security_group_id=service.security_group.security_group_id,
            ip_protocol="tcp",
            from_port=2049,
            to_port=2049,
            description="Webmail task to mail EFS",
        )

        ###
        # Init container - idempotently bootstraps the sqlite schema +
        # writes the Roundcube oauth.inc.php so the main Roundcube
        # container can run the OAuth flow against Authentik on first
        # request. Logic lives in assets/webmail_init/init.sh; this
        # block just wires the env vars + secrets the script reads.

        # Strip the docker image tag suffix (e.g. "-apache", "-fpm")
        # to recover the bare upstream source version, which is the
        # tag used in the roundcubemail GitHub repo and what the
        # Dockerfile uses to fetch sqlite.initial.sql.
        roundcube_source_version = cfg.image_version.split("-", 1)[0]
        init_image = ecs.ContainerImage.from_docker_image_asset(
            ecr_assets.DockerImageAsset(
                self,
                "RoundcubeInitImage",
                directory=str(assets.docker_path("webmail_init")),
                build_args={"ROUNDCUBE_SOURCE_VERSION": roundcube_source_version},
                platform=ecr_assets.Platform.LINUX_AMD64,
            )
        )
        init_log_group = logs.LogGroup(self, "InitLogGroup")
        init_container = service.task_defn.add_container(
            "RoundcubeInit",
            image=init_image,
            essential=False,
            environment={
                **init_environment,
                "ROUNDCUBE_DATA_DIR": ROUNDCUBE_DATA_DIR,
            },
            secrets=init_secrets,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="roundcube-init",
                log_group=init_log_group,
            ),
        )
        init_container.add_mount_points(
            ecs.MountPoint(
                source_volume="roundcube",
                container_path=ROUNDCUBE_DATA_DIR,
                read_only=False,
            )
        )
        service.container.add_container_dependencies(
            ecs.ContainerDependency(
                container=init_container,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )

        ###
        # Public ALB at mail.<public_domain> with Authentik OIDC gate.

        alb = PublicHttpAlb(
            self,
            "Alb",
            fqdn=fqdn,
            a_record=cfg.subdomain,
            zone=foundation.public_zone,
            vpc=foundation.vpc,
        )
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "TargetGroup",
            vpc=foundation.vpc,
            port=ROUNDCUBE_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.HTTP,
                path="/",
                healthy_http_codes="200,302",
            ),
            targets=[
                service.service.load_balancer_target(
                    container_name=service.container.container_name,
                    container_port=ROUNDCUBE_HTTP_PORT,
                    protocol=ecs.Protocol.TCP,
                ),
            ],
        )
        service.security_group.add_ingress_rule(
            alb.security_group,
            ec2.Port.tcp(ROUNDCUBE_HTTP_PORT),
            "ALB to webmail",
        )
        # Plain forward; Roundcube's oauth2 plugin runs the OAuth flow
        # itself end-to-end so anonymous users hitting the apex are
        # redirected to Authentik by Roundcube, not by the ALB.
        alb.https_listener.add_action(
            "Forward",
            action=elbv2.ListenerAction.forward([target_group]),
        )
