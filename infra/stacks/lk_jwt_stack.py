"""lk-jwt-service: mints LiveKit JWTs for Element-Call clients.

Element-Call clients call this service to exchange a Matrix
OpenID token for a LiveKit access token. lk-jwt-service is
stateless: one Fargate task behind PublicHttpAlb on
`lk-jwt.<public_domain>`, fronted by ACM TLS. Image is the
upstream GHCR build (ghcr.io/element-hq/lk-jwt-service) pulled
through the existing GHCR mirror.
"""

from dataclasses import dataclass

from aws_cdk import (
    Duration,
    Stack,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
)
from constructs import Construct

from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.public_http_alb import PublicHttpAlb
from ..models.foundation_exports import FoundationExports
from ..models.lk_jwt_config import LkJwtConfig
from ..models.turn_exports import TurnExports

LK_JWT_HTTP_PORT = 8080


@dataclass(frozen=True)
class LkJwtImports:
    cfg: LkJwtConfig
    foundation: FoundationExports
    turn: TurnExports


class LkJwtStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: LkJwtImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        turn = imports.turn

        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        image = ecs.ContainerImage.from_registry(
            f"{foundation.ghcr_mirror_base}/element-hq/lk-jwt-service:{cfg.image_version}"
        )

        service = PrivateEgressFargateService(
            self,
            "Service",
            stream_prefix="lk-jwt",
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
                        container_port=LK_JWT_HTTP_PORT,
                        host_port=LK_JWT_HTTP_PORT,
                    ),
                ],
                environment={
                    "LIVEKIT_URL": turn.livekit_url,
                    "LK_JWT_PORT": str(LK_JWT_HTTP_PORT),
                },
                secrets={
                    "LIVEKIT_KEY": ecs.Secret.from_secrets_manager(
                        turn.livekit_api_key_secret, "secret"
                    ),
                    "LIVEKIT_SECRET": ecs.Secret.from_secrets_manager(
                        turn.livekit_api_secret_secret, "secret"
                    ),
                },
            ),
        )
        service.grant_pull_through_cache(foundation.ghcr_mirror_namespace)

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
            port=LK_JWT_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                # lk-jwt-service doesn't ship a dedicated health
                # endpoint; the JWT-issuance route requires a
                # valid Matrix token. Hit `/` and accept the
                # service's default 404 as proof of liveness.
                path="/healthz",
                healthy_http_codes="200,404",
            ),
        )
