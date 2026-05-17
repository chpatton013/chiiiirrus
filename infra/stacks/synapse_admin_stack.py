from dataclasses import dataclass

from aws_cdk import (
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.synapse_admin_config import SynapseAdminConfig

# nginx in the container listens on 8080; ALB target group sends
# traffic here. No EFS, no DB; the SPA is fully static and the
# admin API calls go from the browser straight to Synapse.
SYNAPSE_ADMIN_PORT = 8080


@dataclass(frozen=True)
class SynapseAdminImports:
    cfg: SynapseAdminConfig
    foundation: FoundationExports
    assets: AssetLoader
    matrix_fqdn: str
    authentik_issuer_base: str


class SynapseAdminStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: SynapseAdminImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation

        listener_fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        # Build the nginx+ketesa image inline. The Dockerfile fetches
        # the synapse-admin (now "ketesa") release tarball at build
        # time, so the deploy bundle stays small (only the Dockerfile
        # + config templates ship as the build context).
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(imports.assets.docker_path("synapse-admin")),
            build_args={"SYNAPSE_ADMIN_VERSION": cfg.version},
            # Fargate runs linux/amd64; pin the platform so buildx
            # on an Apple-Silicon operator machine emits a matching
            # image (default is the host arch, which gives arm64
            # binaries that Fargate refuses with "exec format error").
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="synapse-admin",
            cpu=cfg.task.cpu,
            memory_limit_mib=cfg.task.memory_limit_mib,
            desired_count=cfg.task.desired_count,
            min_healthy_percent=cfg.task.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            container_kwargs=dict(
                image=ecs.ContainerImage.from_docker_image_asset(image_asset),
                port_mappings=[
                    ecs.PortMapping(
                        container_port=SYNAPSE_ADMIN_PORT,
                        host_port=SYNAPSE_ADMIN_PORT,
                    ),
                ],
                # entrypoint.sh reads MATRIX_FQDN to render config.json
                # at container start (envsubst, dropped into nginx's
                # /docker-entrypoint.d/).
                environment={"MATRIX_FQDN": imports.matrix_fqdn},
            ),
        )

        # OIDC secret bootstrapped manually before first deploy via
        # `bin/aws-write-secret authentik/oidc/synapse-admin -`.
        # Same pattern as every other Authentik-gated app.
        oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OidcSecret", "authentik/oidc/synapse-admin"
        )

        alb = PublicHttpAlb(
            self,
            "PublicHttpAlb",
            fqdn=listener_fqdn,
            a_record=cfg.subdomain,
            zone=foundation.public_zone,
            vpc=foundation.vpc,
        )
        service.security_group.add_ingress_rule(
            alb.security_group,
            ec2.Port.tcp(SYNAPSE_ADMIN_PORT),
            "ALB to synapse-admin nginx",
        )

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "TargetGroup",
            vpc=foundation.vpc,
            port=SYNAPSE_ADMIN_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                protocol=elbv2.Protocol.HTTP,
                path="/healthz",
                healthy_http_codes="200",
            ),
            targets=[
                service.service.load_balancer_target(
                    container_name=service.container.container_name,
                    container_port=SYNAPSE_ADMIN_PORT,
                    protocol=ecs.Protocol.TCP,
                ),
            ],
        )
        # Authentik exposes a single OAuth2 endpoint family and
        # disambiguates by client_id; the issuer is the per-app URL
        # (carries the slug). Mirrors the rspamd-OIDC gate in
        # MailStack.
        alb.https_listener.add_action(
            "OidcGate",
            action=elbv2.ListenerAction.authenticate_oidc(
                authorization_endpoint=f"{imports.authentik_issuer_base}/authorize/",
                token_endpoint=f"{imports.authentik_issuer_base}/token/",
                user_info_endpoint=f"{imports.authentik_issuer_base}/userinfo/",
                issuer=f"{imports.authentik_issuer_base}/synapse-admin/",
                client_id=oidc_secret.secret_value_from_json(
                    "client_id"
                ).unsafe_unwrap(),
                client_secret=oidc_secret.secret_value_from_json("client_secret"),
                next=elbv2.ListenerAction.forward([target_group]),
            ),
        )
