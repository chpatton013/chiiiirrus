from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    Aws,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
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
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.mail_config import MailConfig

DKIM_SELECTOR = "s1"
CONFIG_MOUNT = "/tmp/docker-mailserver"
MAIL_MOUNT = "/var/mail"
CLAMAV_MOUNT = "/var/lib/clamav"
LE_DIR = f"{CONFIG_MOUNT}/letsencrypt"

MAIL_PORTS: list[tuple[str, int]] = [
    ("smtp", 25),  # incoming MX + in-VPC submission via mynetworks
    ("smtps", 465),  # implicit-TLS submission
    ("submission", 587),  # STARTTLS submission (SASL required)
    ("imap", 143),  # STARTTLS IMAP
    ("imaps", 993),  # implicit-TLS IMAP
]


@dataclass(frozen=True)
class MailImports:
    cfg: MailConfig
    foundation: FoundationExports
    assets: AssetLoader


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

        ###
        # Static EIPs (one per AZ) so the public hostname (and PTR if
        # ever set up) doesn't churn.

        eip1 = ec2.CfnEIP(self, "MailEip1", domain="vpc")
        eip2 = ec2.CfnEIP(self, "MailEip2", domain="vpc")

        ###
        # NLB + Fargate service.

        nlb_sg = ec2.SecurityGroup(
            self, "NlbSecurityGroup", vpc=foundation.vpc, allow_all_outbound=True
        )
        for _, port in MAIL_PORTS:
            nlb_sg.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(port),
                f"public to NLB tcp/{port}",
            )

        public_subnets = foundation.vpc.public_subnets
        nlb = elbv2.CfnLoadBalancer(
            self,
            "Nlb",
            type="network",
            scheme="internet-facing",
            ip_address_type="ipv4",
            subnet_mappings=[
                elbv2.CfnLoadBalancer.SubnetMappingProperty(
                    subnet_id=public_subnets[0].subnet_id,
                    allocation_id=eip1.attr_allocation_id,
                ),
                elbv2.CfnLoadBalancer.SubnetMappingProperty(
                    subnet_id=public_subnets[1].subnet_id,
                    allocation_id=eip2.attr_allocation_id,
                ),
            ],
            security_groups=[nlb_sg.security_group_id],
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
            container_kwargs=dict(
                image=image,
                port_mappings=[
                    ecs.PortMapping(container_port=p, host_port=p)
                    for _, p in MAIL_PORTS
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
            f"mkdir -p {CONFIG_MOUNT}/rspamd/dkim {LE_DIR}",
            # 1. DKIM private key (selector s1)
            (
                f'aws secretsmanager get-secret-value --secret-id "$DKIM_SECRET" '
                f"--query SecretString --output text | jq -r .secret "
                f"> {CONFIG_MOUNT}/rspamd/dkim/{DKIM_SELECTOR}.key"
            ),
            f"chmod 0600 {CONFIG_MOUNT}/rspamd/dkim/{DKIM_SELECTOR}.key",
            # 2. Postmaster mailbox (SHA512-CRYPT for Dovecot)
            (
                'pm=$(aws secretsmanager get-secret-value --secret-id "$POSTMASTER_SECRET" '
                "--query SecretString --output text | jq -r .secret)"
            ),
            (
                f'echo "$POSTMASTER_ADDRESS|{{SHA512-CRYPT}}$(openssl passwd -6 "$pm")" '
                f"> {CONFIG_MOUNT}/postfix-accounts.cf"
            ),
            # 3. mynetworks override so VPC traffic skips SASL on port 25
            (
                f"printf 'mynetworks = 127.0.0.1/32 [::1]/128 %s\\n' \"$VPC_CIDR\" "
                f"> {CONFIG_MOUNT}/postfix-main.cf"
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
                "DKIM_SECRET": "mail/dkim-private-key",
                "POSTMASTER_SECRET": "mail/postmaster-password",
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
        service.task_defn.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "route53:ListHostedZonesByName",
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
        # Target groups + listeners (one per port). Use L1 because the L2
        # NetworkLoadBalancer requires its own NLB construct (we built the
        # NLB at L1 to attach EIPs).

        for name, port in MAIL_PORTS:
            tg = elbv2.CfnTargetGroup(
                self,
                f"Tg{port}",
                name=f"mail-{name}",
                protocol="TCP",
                port=port,
                target_type="ip",
                vpc_id=foundation.vpc.vpc_id,
                health_check_protocol="TCP",
                health_check_interval_seconds=30,
                target_group_attributes=[
                    elbv2.CfnTargetGroup.TargetGroupAttributeProperty(
                        key="deregistration_delay.timeout_seconds",
                        value="30",
                    ),
                ],
            )
            elbv2.CfnListener(
                self,
                f"Listener{port}",
                load_balancer_arn=nlb.ref,
                protocol="TCP",
                port=port,
                default_actions=[
                    elbv2.CfnListener.ActionProperty(
                        type="forward",
                        target_group_arn=tg.ref,
                    ),
                ],
            )
            service.service.attach_to_network_target_group(
                elbv2.NetworkTargetGroup.from_target_group_attributes(
                    self,
                    f"TgImport{port}",
                    target_group_arn=tg.ref,
                    load_balancer_arns=nlb.ref,
                )
            )
            service.security_group.add_ingress_rule(
                nlb_sg,
                ec2.Port.tcp(port),
                f"NLB to mail tcp/{port}",
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
            target=route53.RecordTarget.from_values(eip1.ref, eip2.ref),
            ttl=Duration.minutes(5),
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
            values=[f"v=spf1 include:amazonses.com ip4:{eip1.ref} ip4:{eip2.ref} -all"],
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
        route53.TxtRecord(
            self,
            "MailDkim",
            zone=foundation.public_zone,
            record_name=f"{DKIM_SELECTOR}._domainkey",
            values=[dkim_resource.get_att_string("PublicKeyTxt")],
        )
