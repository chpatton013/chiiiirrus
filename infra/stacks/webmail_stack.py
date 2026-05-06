from dataclasses import dataclass

from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
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
        # Init container - idempotently load the Roundcube sqlite schema
        # before the main container starts. The upstream image's
        # entrypoint only runs schema init when sqlite.db is absent;
        # on a fresh EFS access point the file gets touched (or is
        # left empty by a previous failed boot), the entrypoint sees
        # something there, skips schema load, and the main container
        # then crashes on every request with `no such table: session`.
        # This init owns schema (re-)loading + perms so a first deploy
        # converges on its own.

        init_script = (
            "set -eu\n"
            f"mkdir -p {ROUNDCUBE_DATA_DIR}/db {ROUNDCUBE_DATA_DIR}/config\n"
            # 1. Re-create the DB if it's absent or doesn't have a
            # `session` table. Otherwise leave it alone (preserves
            # accumulated user prefs, contacts, etc.).
            "php -r '\n"
            f'  $f = "{ROUNDCUBE_DATA_DIR}/db/sqlite.db";\n'
            "  $need_init = !file_exists($f);\n"
            "  if (!$need_init) {\n"
            "    try {\n"
            '      $db = new PDO("sqlite:" . $f);\n'
            "      $r = $db->query(\n"
            '        "SELECT name FROM sqlite_master "\n'
            '        . "WHERE type=\\"table\\" AND name=\\"session\\""\n'
            "      )->fetch();\n"
            "      $need_init = !$r;\n"
            "    } catch (Throwable $e) {\n"
            "      $need_init = true;\n"
            "    }\n"
            "  }\n"
            "  if ($need_init) {\n"
            "    @unlink($f);\n"
            '    $db = new PDO("sqlite:" . $f);\n'
            "    $db->exec(\n"
            '      file_get_contents("/var/www/html/SQL/sqlite.initial.sql")\n'
            "    );\n"
            '    echo "schema loaded\\n";\n'
            "  } else {\n"
            '    echo "schema present\\n";\n'
            "  }\n"
            "'\n"
            # 2. Templated oauth2 config so Roundcube can run the
            # OAuth flow itself against Authentik. ECS injects the
            # client_id/secret as task-launch secrets (init_secrets)
            # so they never cross CFN parameters. The leading "\\$"
            # in the heredoc tells the shell to emit a literal "$" -
            # PHP needs "$config[...]" to survive.
            f"cat > {ROUNDCUBE_DATA_DIR}/config/oauth.inc.php <<PHP\n"
            "<?php\n"
            "\\$config['oauth_provider'] = 'generic';\n"
            "\\$config['oauth_provider_name'] = 'Authentik';\n"
            "\\$config['oauth_client_id'] = '$OAUTH_CLIENT_ID';\n"
            "\\$config['oauth_client_secret'] = '$OAUTH_CLIENT_SECRET';\n"
            "\\$config['oauth_auth_uri'] = '$AUTHENTIK_ISSUER_BASE/authorize/';\n"
            "\\$config['oauth_token_uri'] = '$AUTHENTIK_ISSUER_BASE/token/';\n"
            "\\$config['oauth_identity_uri'] = '$AUTHENTIK_ISSUER_BASE/userinfo/';\n"
            "\\$config['oauth_scope'] = 'openid profile email offline_access';\n"
            "\\$config['oauth_pkce'] = 'S256';\n"
            "\\$config['oauth_identity_fields'] = ['email'];\n"
            "\\$config['imap_auth_type'] = 'XOAUTH2';\n"
            "\\$config['smtp_auth_type'] = 'XOAUTH2';\n"
            "\\$config['login_autocomplete'] = 0;\n"
            "PHP\n"
            f"chown -R www-data:www-data {ROUNDCUBE_DATA_DIR}\n"
            f"chmod 0640 {ROUNDCUBE_DATA_DIR}/db/sqlite.db\n"
            f"chmod 0640 {ROUNDCUBE_DATA_DIR}/config/oauth.inc.php\n"
        )
        init_log_group = logs.LogGroup(self, "InitLogGroup")
        init_container = service.task_defn.add_container(
            "RoundcubeInit",
            image=image,
            essential=False,
            entry_point=["sh", "-c"],
            command=[init_script],
            environment=init_environment,
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
