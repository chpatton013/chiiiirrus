from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    Aws,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.mail_config import MailConfig

DKIM_SELECTOR = "s1"
CONFIG_MOUNT = "/tmp/docker-mailserver"
MAIL_MOUNT = "/var/mail"
CLAMAV_MOUNT = "/var/lib/clamav"
LE_DIR = f"{CONFIG_MOUNT}/letsencrypt"

# rspamd's worker-controller HTTP UI. Listens inside the container; the
# init-container override (worker-controller.inc) binds it to 0.0.0.0
# and grants the VPC CIDR `secure_ip` so the ALB-OIDC frontend doesn't
# need rspamd's own auth.
RSPAMD_UI_PORT = 11334

MAIL_PORTS: list[tuple[str, int]] = [
    ("smtp", 25),  # incoming MX + in-VPC submission via mynetworks
    ("smtps", 465),  # implicit-TLS submission
    ("submission", 587),  # STARTTLS submission (SASL required)
    ("imaps", 993),  # implicit-TLS IMAP
]


@dataclass(frozen=True)
class MailImports:
    cfg: MailConfig
    foundation: FoundationExports
    assets: AssetLoader
    # Authentik issuer base URL (e.g.
    # "https://auth.<public_domain>/application/o") and the rspamd
    # ALB-OIDC redirect URI
    # ("https://rspamd.<public_domain>/oauth2/idpresponse"). Both flow
    # in from `app_builder.py` so MailStack can compose the OIDC
    # authorize/token/userinfo URLs and reference the OIDC secret.
    authentik_issuer_base: str
    rspamd_redirect_uri: str


@dataclass(frozen=True)
class MailExports:
    # Mail's EFS file system + the access point reserved for Roundcube
    # state (sqlite + config). WebmailStack mounts this access point so
    # Roundcube state lives on the same EFS as the mail server,
    # bringing it under the same backup plan.
    efs_filesystem: efs.IFileSystem
    efs_security_group: ec2.ISecurityGroup
    roundcube_access_point: efs.IAccessPoint


class MailStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: MailImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        assets = imports.assets

        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        ###
        # Secrets

        ses_relay_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "SesRelaySecret", cfg.relay.secret_name
        )
        postmaster_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "PostmasterSecret", "mail/postmaster-password"
        )
        dkim_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DkimSecret", "mail/dkim-private-key"
        )

        ###
        # DKIM key Custom Resource: generates the keypair on first deploy,
        # stores the private key in Secrets Manager, returns the public key
        # in DKIM TXT format. Idempotent on subsequent runs.

        dkim_fn = lambda_python.PythonFunction(
            self,
            "DkimKeyFn",
            entry=str(assets.lambda_path("mail_dkim_key")),
            runtime=lambda_.Runtime.PYTHON_3_12,
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(2),
            environment={"SECRET_ID": "mail/dkim-private-key"},
        )
        dkim_secret.grant_read(dkim_fn)
        dkim_secret.grant_write(dkim_fn)

        dkim_provider = cr.Provider(
            self,
            "DkimKeyProvider",
            on_event_handler=cast(lambda_.IFunction, dkim_fn),
        )
        dkim_resource = CustomResource(
            self,
            "DkimKey",
            service_token=dkim_provider.service_token,
            properties={"Trigger": "v1"},
        )

        ###
        # EFS - one filesystem, three access points (mail / config / clamav).

        efs_sg = ec2.SecurityGroup(
            self, "EfsSecurityGroup", vpc=foundation.vpc, allow_all_outbound=True
        )
        filesystem = efs.FileSystem(
            self,
            "MailFs",
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

        def _ap(name: str, path: str) -> efs.IAccessPoint:
            return filesystem.add_access_point(
                name,
                path=path,
                create_acl=efs.Acl(owner_uid="0", owner_gid="0", permissions="0750"),
                posix_user=efs.PosixUser(uid="0", gid="0"),
            )

        ap_mail = _ap("MailAp", "/dms/mail")
        ap_config = _ap("ConfigAp", "/dms/config")
        ap_clamav = _ap("ClamavAp", "/dms/clamav")
        ap_roundcube = _ap("RoundcubeAp", "/dms/roundcube")

        ###
        # NLB + Fargate service.
        #
        # Auto-assigned IPs (no static EIPs). Rationale: the account's
        # EIP quota is tight, CFN's EIP delete on rollback has been
        # flaky, and the originally-cited reason for static EIPs (PTR
        # records) was already optional in the plan since outbound
        # mail goes through SES (which has its own clean PTR).
        # NLB-assigned IPs are stable for the lifetime of the NLB; the
        # A record is an alias to the NLB DNS name, so IP changes on
        # NLB recreation are absorbed automatically.

        nlb_sg = ec2.SecurityGroup(
            self, "NlbSecurityGroup", vpc=foundation.vpc, allow_all_outbound=True
        )
        for _, port in MAIL_PORTS:
            nlb_sg.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(port),
                f"public to NLB tcp/{port}",
            )

        nlb = elbv2.NetworkLoadBalancer(
            self,
            "Nlb",
            vpc=foundation.vpc,
            internet_facing=True,
            cross_zone_enabled=True,
            security_groups=[nlb_sg],
        )

        # docker-mailserver image from the dockerhub pull-through cache.
        image = ecs.ContainerImage.from_registry(
            f"{foundation.dockerhub_mirror_base}/mailserver/docker-mailserver"
            f":{cfg.image_version}"
        )

        environment = {
            "OVERRIDE_HOSTNAME": fqdn,
            "POSTMASTER_ADDRESS": cfg.postmaster_address,
            "PERMIT_DOCKER": "none",
            "TZ": "UTC",
            "ACCOUNT_PROVISIONER": "FILE",
            "RELAY_HOST": f"email-smtp.{Aws.REGION}.amazonaws.com",
            "RELAY_PORT": str(cfg.relay.port),
            "ENABLE_RSPAMD": "1",
            "ENABLE_OPENDKIM": "0",  # rspamd handles DKIM signing
            "ENABLE_OPENDMARC": "0",
            "ENABLE_POLICYD_SPF": "1",
            "ENABLE_AMAVIS": "0",  # rspamd replaces amavis
            "ENABLE_CLAMAV": "1" if cfg.enable_clamav else "0",
            "ENABLE_FAIL2BAN": "0",  # Fargate disallows NET_ADMIN
            "SSL_TYPE": "manual",
            "SSL_CERT_PATH": f"{LE_DIR}/certificates/{fqdn}.crt",
            "SSL_KEY_PATH": f"{LE_DIR}/certificates/{fqdn}.key",
            "RSPAMD_DKIM_SELECTOR": DKIM_SELECTOR,
            "LOG_LEVEL": "info",
        }
        secrets = {
            "RELAY_USER": ecs.Secret.from_secrets_manager(ses_relay_secret, "username"),
            "RELAY_PASSWORD": ecs.Secret.from_secrets_manager(
                ses_relay_secret, "password"
            ),
        }

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="mail",
            cpu=cfg.task.cpu,
            memory_limit_mib=cfg.task.memory_limit_mib,
            desired_count=cfg.task.desired_count,
            min_healthy_percent=cfg.task.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            # docker-mailserver brings up postfix, dovecot, rspamd,
            # and clamav serially; first-boot can take ~90s before
            # rspamd's :11334 binds. The ELB default of 60s before
            # marking unhealthy is tighter than first-boot needs and
            # was tripping the deployment circuit breaker on every
            # roll. 3 minutes is comfortably wider than the observed
            # cold-start.
            health_check_grace_period=Duration.minutes(3),
            container_kwargs=dict(
                image=image,
                port_mappings=[
                    *(
                        ecs.PortMapping(container_port=p, host_port=p)
                        for _, p in MAIL_PORTS
                    ),
                    ecs.PortMapping(
                        container_port=RSPAMD_UI_PORT,
                        host_port=RSPAMD_UI_PORT,
                    ),
                ],
                environment=environment,
                secrets=secrets,
            ),
        )
        service.grant_pull_through_cache(foundation.dockerhub_mirror_namespace)

        ###
        # EFS volumes + mounts (3 volumes for mail / config / clamav).

        for vol_name, mount_path, ap in (
            ("mail", MAIL_MOUNT, ap_mail),
            ("config", CONFIG_MOUNT, ap_config),
            ("clamav", CLAMAV_MOUNT, ap_clamav),
        ):
            service.task_defn.add_volume(
                name=vol_name,
                efs_volume_configuration=ecs.EfsVolumeConfiguration(
                    file_system_id=filesystem.file_system_id,
                    transit_encryption="ENABLED",
                    authorization_config=ecs.AuthorizationConfig(
                        access_point_id=ap.access_point_id,
                        iam="ENABLED",
                    ),
                ),
            )
            service.container.add_mount_points(
                ecs.MountPoint(
                    source_volume=vol_name,
                    container_path=mount_path,
                    read_only=False,
                )
            )
        filesystem.grant_read_write(service.task_defn.task_role)
        efs_sg.add_ingress_rule(
            service.security_group, ec2.Port.tcp(2049), "Mail task to EFS"
        )

        ###
        # Init container - DKIM key materialization, postmaster mailbox,
        # mynetworks override, and Let's Encrypt cert issuance/renewal.

        init_image = ecs.ContainerImage.from_docker_image_asset(
            ecr_assets.DockerImageAsset(
                self,
                "MailInitImage",
                directory=str(assets.docker_path("mail_init")),
                platform=ecr_assets.Platform.LINUX_AMD64,
            )
        )

        init_script_lines = [
            "set -eu",
            f"mkdir -p {CONFIG_MOUNT}/rspamd/dkim {CONFIG_MOUNT}/rspamd/override.d {LE_DIR}",
            # 1. DKIM private key (selector s1)
            (
                f'aws secretsmanager get-secret-value --secret-id "$DKIM_SECRET" '
                f"--query SecretString --output text | jq -r .secret "
                f"> {CONFIG_MOUNT}/rspamd/dkim/{DKIM_SELECTOR}.key"
            ),
            f"chmod 0600 {CONFIG_MOUNT}/rspamd/dkim/{DKIM_SELECTOR}.key",
            # 2. postfix-accounts.cf - postmaster + per-user mailboxes.
            # Each line: <full-address>|{SHA512-CRYPT}<dovecot hash>
            (
                "{ "
                "pm=$(aws secretsmanager get-secret-value "
                '--secret-id "$POSTMASTER_SECRET" '
                "--query SecretString --output text | jq -r .secret); "
                'echo "$POSTMASTER_ADDRESS|{SHA512-CRYPT}$(openssl passwd -6 "$pm")"; '
                "for user in $MAIL_USERS; do "
                "pw=$(aws secretsmanager get-secret-value "
                '--secret-id "mail/users/$user" '
                "--query SecretString --output text | jq -r .secret); "
                'echo "$user@$MAIL_DOMAIN|{SHA512-CRYPT}$(openssl passwd -6 "$pw")"; '
                "done; "
                f"}} > {CONFIG_MOUNT}/postfix-accounts.cf"
            ),
            # 3a. mynetworks override so VPC traffic submits without SASL.
            (
                f"printf 'mynetworks = 127.0.0.1/32 [::1]/128 %s\\n' \"$VPC_CIDR\" "
                f"> {CONFIG_MOUNT}/postfix-main.cf"
            ),
            # 3b. master.cf override: re-add permit_mynetworks to the
            # submission (587) service's client + relay + recipient
            # restrictions so VPC clients submit on 587 without SASL.
            # All three default to permit_sasl_authenticated,reject in
            # DMS; missing any one rejects internal services.
            (
                "{ "
                "printf 'submission/inet/smtpd_client_restrictions="
                "permit_mynetworks,permit_sasl_authenticated,reject\\n'; "
                "printf 'submission/inet/smtpd_relay_restrictions="
                "permit_mynetworks,permit_sasl_authenticated,reject\\n'; "
                "printf 'submission/inet/smtpd_recipient_restrictions="
                "permit_mynetworks,permit_sasl_authenticated,reject\\n'; "
                f"}} > {CONFIG_MOUNT}/postfix-master.cf"
            ),
            # 3c. rspamd worker-controller override: bind the HTTP UI to
            # 0.0.0.0:11334 (default is 127.0.0.1) and skip rspamd's own
            # password check for VPC traffic. The internal ALB's OIDC
            # action is the gate.
            (
                "{ "
                "printf 'bind_socket = \"*:%s\";\\n' "
                f'"{RSPAMD_UI_PORT}"; '
                'printf \'secure_ip = "%s";\\n\' "$VPC_CIDR"; '
                f"}} > {CONFIG_MOUNT}/rspamd/override.d/worker-controller.inc"
            ),
            # 3d. Dovecot OAUTHBEARER passdb: lets Roundcube authenticate
            # IMAP/SMTP-submission with an Authentik-issued access
            # token, no password ever templated. Additive to the
            # existing passwd-file passdb so mutt / Apple Mail keep
            # working with passwords. Dovecot validates tokens
            # locally against Authentik's JWKS - no per-login
            # round-trip back to Authentik.
            f"mkdir -p {CONFIG_MOUNT}/jwks",
            (
                "curl -sf "
                '"$AUTHENTIK_ISSUER_BASE/roundcube/jwks/" '
                f'> "{CONFIG_MOUNT}/jwks/keys.json"'
            ),
            # dovecot.cf - single-quoted printf so the literal
            # `$auth_mechanisms` (a Dovecot variable, not shell)
            # survives unmangled.
            (
                "printf '"
                "auth_mechanisms = $auth_mechanisms oauthbearer xoauth2\\n"
                "passdb {\\n"
                "  driver = oauth2\\n"
                "  mechanisms = oauthbearer xoauth2\\n"
                f"  args = {CONFIG_MOUNT}/dovecot-oauth2.conf.ext\\n"
                "}\\n' "
                f"> {CONFIG_MOUNT}/dovecot.cf"
            ),
            # dovecot-oauth2.conf.ext - %s placeholders get filled
            # from shell-expanded args so $AUTHENTIK_ISSUER_BASE works,
            # and %%Lu becomes %Lu (Dovecot's lowercase-full-username
            # format spec). active_attribute + active_value must both
            # be empty for local-only JWT validation; setting one
            # without the other fails Dovecot's startup config check.
            (
                "printf '"
                "local_validation_key_dict = fs:posix:prefix=%s/jwks/\\n"
                "introspection_mode = local\\n"
                "issuers = %s/roundcube/\\n"
                "username_attribute = email\\n"
                "username_format = %%Lu\\n"
                "active_attribute =\\n"
                "active_value =\\n' "
                f'"{CONFIG_MOUNT}" "$AUTHENTIK_ISSUER_BASE" '
                f"> {CONFIG_MOUNT}/dovecot-oauth2.conf.ext"
            ),
            # 4. Let's Encrypt cert (issue once, renew if <30 days from expiry).
            f'export LEGO_PATH="{LE_DIR}"',
            (
                f'if [ ! -f "$LEGO_PATH/certificates/{fqdn}.crt" ]; then '
                f'lego --path="$LEGO_PATH" --email="$POSTMASTER_ADDRESS" '
                f'--domains="$MAIL_FQDN" --dns=route53 --accept-tos run; '
                "else "
                f'lego --path="$LEGO_PATH" --email="$POSTMASTER_ADDRESS" '
                f'--domains="$MAIL_FQDN" --dns=route53 renew --days=30 || true; '
                "fi"
            ),
        ]

        init_log_group = logs.LogGroup(self, "InitLogGroup")
        init_container = service.task_defn.add_container(
            "MailInit",
            image=init_image,
            essential=False,
            entry_point=["sh", "-c"],
            command=["; ".join(init_script_lines)],
            environment={
                "POSTMASTER_ADDRESS": cfg.postmaster_address,
                "VPC_CIDR": foundation.vpc.vpc_cidr_block,
                "MAIL_FQDN": fqdn,
                "MAIL_DOMAIN": foundation.public_domain,
                "MAIL_USERS": " ".join(cfg.users),
                "DKIM_SECRET": "mail/dkim-private-key",
                "POSTMASTER_SECRET": "mail/postmaster-password",
                # Used by the Dovecot OAUTHBEARER passdb config: the
                # init script fetches Authentik's JWKS for this app
                # so token signatures can be verified locally.
                "AUTHENTIK_ISSUER_BASE": imports.authentik_issuer_base,
                # lego picks up these from the standard AWS env
                "AWS_REGION": Aws.REGION,
            },
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="mail-init",
                log_group=init_log_group,
            ),
        )
        init_container.add_mount_points(
            ecs.MountPoint(
                source_volume="config",
                container_path=CONFIG_MOUNT,
                read_only=False,
            )
        )
        # Init grants: secrets + Route53 (for lego DNS-01)
        dkim_secret.grant_read(service.task_defn.task_role)
        postmaster_secret.grant_read(service.task_defn.task_role)
        if cfg.users:
            service.task_defn.task_role.add_to_principal_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{Aws.REGION}:{Aws.ACCOUNT_ID}:secret:mail/users/*"
                    ],
                )
            )
        service.task_defn.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "route53:ListHostedZonesByName",
                    "route53:ListResourceRecordSets",
                    "route53:GetChange",
                    "route53:GetHostedZone",
                ],
                resources=["*"],
            )
        )
        service.task_defn.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["route53:ChangeResourceRecordSets"],
                resources=[
                    f"arn:aws:route53:::hostedzone/{foundation.public_zone.hosted_zone_id}"
                ],
            )
        )
        # Main container starts only after init completes.
        service.container.add_container_dependencies(
            ecs.ContainerDependency(
                container=init_container,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )

        ###
        # Listeners (one per port). L2 `add_listener` returns a listener
        # that wires its own target group to the service with the
        # correct ordering, so no manual `add_dependency` is needed.

        for name, port in MAIL_PORTS:
            listener = nlb.add_listener(
                f"Listener{port}",
                port=port,
                protocol=elbv2.Protocol.TCP,
            )
            target = service.service.load_balancer_target(
                container_name=service.container.container_name,
                container_port=port,
                protocol=ecs.Protocol.TCP,
            )
            listener.add_targets(
                f"Tg{port}",
                port=port,
                protocol=elbv2.Protocol.TCP,
                targets=[target],
                deregistration_delay=Duration.seconds(30),
                preserve_client_ip=False,
                health_check=elbv2.HealthCheck(protocol=elbv2.Protocol.TCP),
            )
            service.security_group.add_ingress_rule(
                nlb_sg,
                ec2.Port.tcp(port),
                f"NLB to mail tcp/{port}",
            )

        ###
        # Internal ALB for the rspamd web UI. Public DNS -> private IP;
        # Tailscale clients reach it via the headscale exit-node which
        # routes the VPC CIDR. The ALB enforces Authentik OIDC; the
        # rspamd worker-controller's own auth is bypassed for VPC
        # traffic via the `secure_ip` override the init container
        # writes alongside the bind_socket override.

        rspamd_fqdn = f"rspamd.{foundation.public_domain}"
        rspamd_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "RspamdOidcSecret", "authentik/oidc/rspamd"
        )
        rspamd_alb = PublicHttpAlb(
            self,
            "RspamdAlb",
            fqdn=rspamd_fqdn,
            a_record="rspamd",
            zone=foundation.public_zone,
            vpc=foundation.vpc,
            internet_facing=False,
        )
        service.security_group.add_ingress_rule(
            rspamd_alb.security_group,
            ec2.Port.tcp(RSPAMD_UI_PORT),
            "Rspamd ALB to mail rspamd UI",
        )
        rspamd_target_group = elbv2.ApplicationTargetGroup(
            self,
            "RspamdTargetGroup",
            vpc=foundation.vpc,
            port=RSPAMD_UI_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.HTTP,
                path="/",
                healthy_http_codes="200,401",
            ),
            targets=[
                service.service.load_balancer_target(
                    container_name=service.container.container_name,
                    container_port=RSPAMD_UI_PORT,
                    protocol=ecs.Protocol.TCP,
                ),
            ],
        )
        rspamd_alb.https_listener.add_action(
            "RspamdOidcGate",
            action=elbv2.ListenerAction.authenticate_oidc(
                # Authentik exposes one shared OAuth2 endpoint per
                # category and disambiguates by client_id; the issuer
                # is the per-application URL (carries the slug).
                authorization_endpoint=f"{imports.authentik_issuer_base}/authorize/",
                token_endpoint=f"{imports.authentik_issuer_base}/token/",
                user_info_endpoint=f"{imports.authentik_issuer_base}/userinfo/",
                issuer=f"{imports.authentik_issuer_base}/rspamd/",
                client_id=rspamd_oidc_secret.secret_value_from_json(
                    "client_id"
                ).unsafe_unwrap(),
                client_secret=rspamd_oidc_secret.secret_value_from_json(
                    "client_secret"
                ),
                next=elbv2.ListenerAction.forward([rspamd_target_group]),
            ),
        )

        ###
        # Monthly EventBridge schedule -> Lambda -> ecs:UpdateService(force).
        # Guarantees the init container (and lego renewal) run at least
        # once a month.

        force_redeploy_fn = lambda_python.PythonFunction(
            self,
            "ForceRedeployFn",
            entry=str(assets.lambda_path("mail_force_redeploy")),
            runtime=lambda_.Runtime.PYTHON_3_12,
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(1),
            environment={
                "CLUSTER_ARN": foundation.cluster.cluster_arn,
                "SERVICE_ARN": service.service.service_arn,
            },
        )
        force_redeploy_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[service.service.service_arn],
            )
        )
        events.Rule(
            self,
            "MonthlyRedeploy",
            schedule=events.Schedule.cron(minute="0", hour="4", day="1"),
            targets=[
                cast(
                    events.IRuleTarget,
                    events_targets.LambdaFunction(
                        cast(lambda_.IFunction, force_redeploy_fn)
                    ),
                ),
            ],
        )

        ###
        # Route53: A (smtp -> EIPs), MX, SPF, DMARC, DKIM.

        route53.ARecord(
            self,
            "MailA",
            zone=foundation.public_zone,
            record_name=cfg.subdomain,
            target=route53.RecordTarget.from_alias(
                cast(
                    route53.IAliasRecordTarget,
                    route53_targets.LoadBalancerTarget(nlb),
                )
            ),
        )
        route53.MxRecord(
            self,
            "MailMx",
            zone=foundation.public_zone,
            record_name="",
            values=[route53.MxRecordValue(host_name=fqdn, priority=10)],
            ttl=Duration.minutes(5),
        )
        route53.TxtRecord(
            self,
            "MailSpf",
            zone=foundation.public_zone,
            record_name="",
            # Outbound mail relays through SES so include:amazonses.com is
            # all that's needed. We don't list ip4: entries because the
            # NLB-assigned IPs aren't stable across LB recreation.
            values=["v=spf1 include:amazonses.com -all"],
        )
        route53.TxtRecord(
            self,
            "MailDmarc",
            zone=foundation.public_zone,
            record_name="_dmarc",
            values=[
                "v=DMARC1; p=quarantine; "
                f"rua=mailto:{cfg.postmaster_address}; "
                f"ruf=mailto:{cfg.postmaster_address}; fo=1"
            ],
        )
        # CfnRecordSet (L1) instead of TxtRecord (L2): the DKIM payload
        # is a CFN Token and exceeds 255 bytes. The Lambda returns it
        # pre-split into quoted character-strings; CfnRecordSet passes
        # them to Route53 verbatim, while TxtRecord would re-wrap the
        # whole thing in another set of quotes and trip the
        # CharacterStringTooLong check.
        route53.CfnRecordSet(
            self,
            "MailDkim",
            hosted_zone_id=foundation.public_zone.hosted_zone_id,
            name=f"{DKIM_SELECTOR}._domainkey.{foundation.public_domain}.",
            type="TXT",
            ttl="1800",
            resource_records=[dkim_resource.get_att_string("PublicKeyTxt")],
        )

        ###
        # AWS Backup: daily + weekly snapshots of the mail EFS into the
        # shared FoundationStack vault. Mail volumes are RETAIN, but
        # RETAIN doesn't protect against software bugs deleting files.

        backup_plan = backup.BackupPlan(
            self,
            "MailBackupPlan",
            backup_plan_name="mail-efs-backups",
            backup_vault=foundation.backup_vault,
        )
        backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="daily-7-days",
                schedule_expression=events.Schedule.cron(minute="0", hour="5"),
                delete_after=Duration.days(7),
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
        backup_plan.add_selection(
            "MailEfsSelection",
            resources=[backup.BackupResource.from_efs_file_system(filesystem)],
        )

        self.exports = MailExports(
            efs_filesystem=filesystem,
            efs_security_group=efs_sg,
            roundcube_access_point=ap_roundcube,
        )
