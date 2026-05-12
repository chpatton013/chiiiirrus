import pathlib
import tempfile
from dataclasses import dataclass

from aws_cdk import (
    Aws,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from ..constructs.db_exec_tags import tag_for_db_exec
from ..constructs.fargate_service import PrivateEgressFargateService
from ..constructs.input_hash import hash_inputs
from ..constructs.public_http_alb import PublicHttpAlb
from ..constructs.shared_volume_init import SharedVolumeInit
from ..models.asset_loader import AssetLoader
from ..models.authentik_config import AuthentikConfig
from ..models.data_exports import DataExports
from ..models.foundation_exports import FoundationExports


@dataclass(frozen=True)
class AuthentikImports:
    cfg: AuthentikConfig
    foundation: FoundationExports
    data: DataExports
    assets: AssetLoader
    tailscale_redirect_uri: str
    headscale_redirect_uri: str
    headplane_redirect_uri: str
    headplane_launch_url: str
    vaultwarden_redirect_uri: str
    rspamd_redirect_uri: str
    roundcube_redirect_uri: str
    matrix_redirect_uri: str


AUTHENTIK_HTTP_PORT = 9000
BLUEPRINTS_VOLUME = "authentik-blueprints"
BLUEPRINTS_MOUNT_PATH = "/blueprints/custom"
PRIVATE_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]


def _stamp_blueprints(
    src: pathlib.Path,
    env: dict[str, str],
) -> tuple[pathlib.Path, str]:
    """Stamp a hash comment into each blueprint YAML so its body hash
    changes whenever any input does, and return (stamped_dir, hash).

    Authentik's blueprint reconciliation skips reapply when the YAML
    body hash matches the stored hash. Values that flow in via
    `!Env` placeholders aren't part of that hash, so an env-var-only
    change (e.g. flipping a redirect URI) leaves the on-disk YAML
    looking unchanged and the worker silently does nothing. We make
    those inputs visible to the hash by appending a deterministic
    `# blueprint-inputs-hash:` comment computed over the YAML
    contents AND the AK_BP_* env values - any change anywhere flips
    the hash, the YAML body changes, the worker reapplies.

    The same hash is also used as `AUTHENTIK_BLUEPRINT_SYNC_VERSION`
    so YAML-only edits (no env-var change) still flip a task-def env
    var and force the worker to roll.
    """
    yamls = sorted(src.glob("*.yaml"))
    digest = hash_inputs(files=yamls, env=env)

    stamped = pathlib.Path(tempfile.mkdtemp(prefix="ak-blueprints-"))
    for p in yamls:
        (stamped / p.name).write_text(
            p.read_text() + f"\n# blueprint-inputs-hash: {digest}\n"
        )
    return stamped, digest


class AuthentikStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: AuthentikImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        data = imports.data
        assets = imports.assets

        ###
        # Secrets

        secret_key = secretsmanager.Secret.from_secret_name_v2(
            self, "SecretKey", "authentik/secret-key"
        )
        bootstrap_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "BootstrapSecret", "authentik/bootstrap"
        )
        db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DbSecret", cfg.db.secret_name
        )
        tailscale_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "TailscaleOidcSecret", "authentik/oidc/tailscale"
        )
        headscale_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "HeadscaleOidcSecret", "authentik/oidc/headscale"
        )
        headplane_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "HeadplaneOidcSecret", "authentik/oidc/headplane"
        )
        vaultwarden_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "VaultwardenOidcSecret", "authentik/oidc/vaultwarden"
        )
        rspamd_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "RspamdOidcSecret", "authentik/oidc/rspamd"
        )
        roundcube_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "RoundcubeOidcSecret", "authentik/oidc/roundcube"
        )
        matrix_oidc_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "MatrixOidcSecret", "authentik/oidc/matrix"
        )

        ###
        # Storage

        bucket = s3.Bucket(
            self,
            "Bucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )

        blueprints_bucket = s3.Bucket(
            self,
            "BlueprintsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )
        # Inputs the blueprints reference via `!Env`. Collected here so
        # the stamping helper can hash them alongside the YAML bodies;
        # any change here flips the hash and forces a worker reapply.
        blueprint_env = {
            "AK_BP_USER_USERNAME": cfg.user.username,
            "AK_BP_USER_DISPLAY_NAME": cfg.user.display_name,
            "AK_BP_USER_EMAIL": f"{cfg.user.username}@{foundation.public_domain}",
            "AK_BP_TAILSCALE_REDIRECT_URI": imports.tailscale_redirect_uri,
            "AK_BP_HEADSCALE_REDIRECT_URI": imports.headscale_redirect_uri,
            "AK_BP_HEADPLANE_REDIRECT_URI": imports.headplane_redirect_uri,
            "AK_BP_HEADPLANE_LAUNCH_URL": imports.headplane_launch_url,
            "AK_BP_VAULTWARDEN_REDIRECT_URI": imports.vaultwarden_redirect_uri,
            "AK_BP_RSPAMD_REDIRECT_URI": imports.rspamd_redirect_uri,
            "AK_BP_ROUNDCUBE_REDIRECT_URI": imports.roundcube_redirect_uri,
            "AK_BP_MATRIX_REDIRECT_URI": imports.matrix_redirect_uri,
        }
        stamped_blueprints, blueprint_inputs_hash = _stamp_blueprints(
            pathlib.Path(assets.blueprints_path("authentik")),
            blueprint_env,
        )
        s3deploy.BucketDeployment(
            self,
            "BlueprintsDeployment",
            sources=[s3deploy.Source.asset(str(stamped_blueprints))],
            destination_bucket=blueprints_bucket,
            prune=True,
        )

        ###
        # Services

        common_env = {
            "AUTHENTIK_DISABLE_UPDATE_CHECK": "true",
            "AUTHENTIK_EMAIL__FROM": cfg.smtp.from_email_address,
            "AUTHENTIK_EMAIL__HOST": cfg.smtp.host,
            "AUTHENTIK_EMAIL__PORT": str(cfg.smtp.port),
            "AUTHENTIK_EMAIL__USE_TLS": "true",
            "AUTHENTIK_EMAIL__USE_SSL": "false",
            "AUTHENTIK_LISTEN__HTTP": f"0.0.0.0:{AUTHENTIK_HTTP_PORT}",
            "AUTHENTIK_POSTGRESQL__HOST": data.database.instance.db_instance_endpoint_address,
            "AUTHENTIK_POSTGRESQL__NAME": cfg.db.name,
            "AUTHENTIK_POSTGRESQL__PORT": str(data.database.port),
            # NOTE: Without this setting, the stack will never reach a healthy
            # deployed state. The server and worker containers will endlessly
            # fail to connect to the database and restart.
            "AUTHENTIK_POSTGRESQL__SSLMODE": "require",
            "AUTHENTIK_STORAGE__BACKEND": "s3",
            "AUTHENTIK_STORAGE__S3__BUCKET_NAME": bucket.bucket_name,
            "AUTHENTIK_STORAGE__S3__REGION": Aws.REGION,
            "AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS": ",".join(PRIVATE_CIDRS),
            **blueprint_env,
            # Auto-derived from the blueprint YAMLs + `blueprint_env`
            # above. Bumps automatically on any change, so the worker
            # task definition diffs and rolls without anyone having to
            # remember a manual version bump.
            "AUTHENTIK_BLUEPRINT_SYNC_VERSION": blueprint_inputs_hash,
        }

        common_secrets = {
            "AUTHENTIK_POSTGRESQL__PASSWORD": ecs.Secret.from_secrets_manager(
                db_secret, "password"
            ),
            "AUTHENTIK_POSTGRESQL__USER": ecs.Secret.from_secrets_manager(
                db_secret, "username"
            ),
            "AUTHENTIK_SECRET_KEY": ecs.Secret.from_secrets_manager(
                secret_key, "secret"
            ),
            "AK_BP_TAILSCALE_CLIENT_ID": ecs.Secret.from_secrets_manager(
                tailscale_oidc_secret, "client_id"
            ),
            "AK_BP_TAILSCALE_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                tailscale_oidc_secret, "client_secret"
            ),
            "AK_BP_HEADSCALE_CLIENT_ID": ecs.Secret.from_secrets_manager(
                headscale_oidc_secret, "client_id"
            ),
            "AK_BP_HEADSCALE_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                headscale_oidc_secret, "client_secret"
            ),
            "AK_BP_HEADPLANE_CLIENT_ID": ecs.Secret.from_secrets_manager(
                headplane_oidc_secret, "client_id"
            ),
            "AK_BP_HEADPLANE_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                headplane_oidc_secret, "client_secret"
            ),
            "AK_BP_VAULTWARDEN_CLIENT_ID": ecs.Secret.from_secrets_manager(
                vaultwarden_oidc_secret, "client_id"
            ),
            "AK_BP_VAULTWARDEN_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                vaultwarden_oidc_secret, "client_secret"
            ),
            "AK_BP_RSPAMD_CLIENT_ID": ecs.Secret.from_secrets_manager(
                rspamd_oidc_secret, "client_id"
            ),
            "AK_BP_RSPAMD_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                rspamd_oidc_secret, "client_secret"
            ),
            "AK_BP_ROUNDCUBE_CLIENT_ID": ecs.Secret.from_secrets_manager(
                roundcube_oidc_secret, "client_id"
            ),
            "AK_BP_ROUNDCUBE_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                roundcube_oidc_secret, "client_secret"
            ),
            "AK_BP_MATRIX_CLIENT_ID": ecs.Secret.from_secrets_manager(
                matrix_oidc_secret, "client_id"
            ),
            "AK_BP_MATRIX_CLIENT_SECRET": ecs.Secret.from_secrets_manager(
                matrix_oidc_secret, "client_secret"
            ),
        }

        app_image = ecs.ContainerImage.from_registry(
            f"{foundation.ghcr_mirror_base}/goauthentik/server:{cfg.image_version}"
        )
        health_check_path = "/-/health/live/"

        server_service = PrivateEgressFargateService(
            self,
            "ServerService",
            stream_prefix="authentik-server",
            cpu=cfg.server.cpu,
            memory_limit_mib=cfg.server.memory_limit_mib,
            desired_count=cfg.server.desired_count,
            min_healthy_percent=cfg.server.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            container_kwargs=dict(
                image=app_image,
                port_mappings=[
                    ecs.PortMapping(
                        container_port=AUTHENTIK_HTTP_PORT,
                        host_port=AUTHENTIK_HTTP_PORT,
                    ),
                ],
                command=["server"],
                environment=common_env,
                secrets=common_secrets,
                health_check=ecs.HealthCheck(
                    command=["CMD", "ak", "healthcheck"],
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(5),
                    retries=3,
                    start_period=Duration.seconds(300),
                ),
            ),
        )
        worker_service = PrivateEgressFargateService(
            self,
            "WorkerService",
            stream_prefix="authentik-worker",
            cpu=cfg.worker.cpu,
            memory_limit_mib=cfg.worker.memory_limit_mib,
            desired_count=cfg.worker.desired_count,
            min_healthy_percent=cfg.worker.min_healthy_percent,
            vpc=foundation.vpc,
            cluster=foundation.cluster,
            container_kwargs=dict(
                image=app_image,
                command=["worker"],
                environment=common_env,
                secrets={
                    **common_secrets,
                    "AUTHENTIK_BOOTSTRAP_EMAIL": ecs.Secret.from_secrets_manager(
                        bootstrap_secret, "email"
                    ),
                    "AUTHENTIK_BOOTSTRAP_PASSWORD": ecs.Secret.from_secrets_manager(
                        bootstrap_secret, "password"
                    ),
                },
            ),
        )

        server_service.grant_pull_through_cache(foundation.ghcr_mirror_namespace)
        worker_service.grant_pull_through_cache(foundation.ghcr_mirror_namespace)

        for svc, init_id, init_prefix in [
            (
                server_service,
                "ServerBlueprintsInit",
                "authentik-server-blueprints-init",
            ),
            (
                worker_service,
                "WorkerBlueprintsInit",
                "authentik-worker-blueprints-init",
            ),
        ]:
            SharedVolumeInit(
                self,
                init_id,
                service=svc,
                volume_name=BLUEPRINTS_VOLUME,
                mount_path=BLUEPRINTS_MOUNT_PATH,
                shell_commands=[
                    f'aws s3 sync "s3://${{BLUEPRINTS_BUCKET}}/" "{BLUEPRINTS_MOUNT_PATH}/"',
                ],
                environment={"BLUEPRINTS_BUCKET": blueprints_bucket.bucket_name},
                stream_prefix=init_prefix,
            )
            blueprints_bucket.grant_read(svc.task_defn.task_role)

        alb = PublicHttpAlb(
            self,
            "PublicHttpAlb",
            fqdn=f"{cfg.subdomain}.{foundation.public_domain}",
            a_record=cfg.subdomain,
            zone=foundation.public_zone,
            vpc=foundation.vpc,
        )

        alb.https_listener.add_targets(
            "AuthentikTargets",
            port=AUTHENTIK_HTTP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[server_service.service],
            deregistration_delay=Duration.seconds(30),
            health_check=elbv2.HealthCheck(
                path=health_check_path,
                healthy_http_codes="200",
            ),
        )

        ###
        # Permissions

        # Give server and worker services full access to S3 bucket.
        bucket_access_policy_statement = iam.PolicyStatement(
            actions=[
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
            ],
            resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/*"],
        )
        server_service.task_defn.add_to_task_role_policy(bucket_access_policy_statement)
        worker_service.task_defn.add_to_task_role_policy(bucket_access_policy_statement)

        # Allow server and worker services to connect to DB instance.
        data.database.grant_connect(
            self,
            "ServerDbIngress",
            peer=server_service.service,
            description="Authentik server to DB",
        )
        data.database.grant_connect(
            self,
            "WorkerDbIngress",
            peer=worker_service.service,
            description="Authentik worker to DB",
        )
        # Both services share the same DB; expose the server task to
        # bin/db-sql since it's the primary, but the worker would
        # work identically.
        tag_for_db_exec(
            server_service.service,
            label="authentik",
            env_prefix="AUTHENTIK_POSTGRESQL__",
        )

        # Allow ALB to connect to server service HTTP port.
        server_service.security_group.add_ingress_rule(
            alb.security_group,
            ec2.Port.tcp(AUTHENTIK_HTTP_PORT),
            "ALB to Authentik HTTP",
        )
