from dataclasses import dataclass

from aws_cdk import (
    Duration,
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
from ..models.webmail_config import WebmailConfig
from .mail_stack import MailExports

ROUNDCUBE_HTTP_PORT = 80
ROUNDCUBE_DATA_DIR = "/var/roundcube"


@dataclass(frozen=True)
class WebmailImports:
    cfg: WebmailConfig
    foundation: FoundationExports
    mail: MailExports
    # Mail server hostname (smtp.<public_domain>) - Roundcube talks to
    # IMAPS:993 + STARTTLS:587 through the public NLB.
    mail_fqdn: str
    # Authentik issuer base URL + ALB-OIDC redirect URI for Roundcube.
    authentik_issuer_base: str
    roundcube_redirect_uri: str


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
        alb.https_listener.add_action(
            "OidcGate",
            action=elbv2.ListenerAction.authenticate_oidc(
                authorization_endpoint=(
                    f"{imports.authentik_issuer_base}/roundcube/authorize/"
                ),
                token_endpoint=f"{imports.authentik_issuer_base}/roundcube/token/",
                user_info_endpoint=(
                    f"{imports.authentik_issuer_base}/roundcube/userinfo/"
                ),
                issuer=f"{imports.authentik_issuer_base}/roundcube/",
                client_id=oidc_secret.secret_value_from_json(
                    "client_id"
                ).unsafe_unwrap(),
                client_secret=oidc_secret.secret_value_from_json("client_secret"),
                next=elbv2.ListenerAction.forward([target_group]),
            ),
        )
