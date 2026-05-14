import aws_cdk as cdk
from aws_cdk import (
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3deploy,
)

from .models.app_config import AppConfig
from .models.asset_loader import AssetLoader
from .stacks.apex_edge_stack import (
    ApexBehavior,
    ApexContentDeployment,
    ApexEdgeImports,
    ApexEdgeStack,
)
from .stacks.authentik_stack import AuthentikImports, AuthentikStack
from .stacks.data_stack import DataImports, DataStack
from .stacks.element_call_stack import ElementCallImports, ElementCallStack
from .stacks.element_web_stack import ElementWebImports, ElementWebStack
from .stacks.foundation_stack import FoundationImports, FoundationStack
from .stacks.headscale_stack import HeadscaleImports, HeadscaleStack
from .stacks.lk_jwt_stack import LkJwtImports, LkJwtStack
from .stacks.mail_stack import MailImports, MailStack
from .stacks.matrix_stack import MatrixImports, MatrixStack
from .stacks.openclaw_stack import OpenClawImports, OpenClawStack
from .stacks.turn_stack import TurnImports, TurnStack
from .stacks.vaultwarden_stack import VaultwardenImports, VaultwardenStack
from .stacks.webfinger_stack import WebFingerImports, WebFingerStack
from .stacks.webmail_stack import WebmailImports, WebmailStack


def build_app(
    app: cdk.App,
    *,
    cfg: AppConfig,
    assets: AssetLoader,
    env: cdk.Environment,
) -> None:
    authentik_issuer_base = f"https://{cfg.authentik.subdomain}.{cfg.foundation.public_domain}/application/o"
    headscale_fqdn = (
        f"{cfg.headscale.headscale_subdomain}.{cfg.foundation.public_domain}"
    )
    headplane_fqdn = (
        f"{cfg.headscale.headplane_subdomain}.{cfg.foundation.public_domain}"
    )
    vaultwarden_fqdn = f"{cfg.vaultwarden.subdomain}.{cfg.foundation.public_domain}"
    rspamd_fqdn = f"rspamd.{cfg.foundation.public_domain}"
    rspamd_redirect_uri = f"https://{rspamd_fqdn}/oauth2/idpresponse"
    mail_fqdn = f"{cfg.mail.subdomain}.{cfg.foundation.public_domain}"
    roundcube_fqdn = f"{cfg.webmail.subdomain}.{cfg.foundation.public_domain}"
    # Roundcube 1.6's oauth2 plugin expects the IDP to redirect back to
    # the bare Roundcube root with `code` + `state` query params.
    roundcube_redirect_uri = f"https://{roundcube_fqdn}/index.php/login/oauth"
    matrix_fqdn = f"{cfg.matrix.subdomain}.{cfg.foundation.public_domain}"
    matrix_redirect_uri = f"https://{matrix_fqdn}/_synapse/client/oidc/callback"
    element_web_fqdn = f"{cfg.element_web.subdomain}.{cfg.foundation.public_domain}"
    element_web_base_url = f"https://{element_web_fqdn}/"
    lk_jwt_fqdn = f"{cfg.lk_jwt.subdomain}.{cfg.foundation.public_domain}"
    element_call_fqdn = f"{cfg.element_call.subdomain}.{cfg.foundation.public_domain}"
    element_call_base_url = f"https://{element_call_fqdn}/"
    # Element-Web embeds the Element-Call widget by URL; no
    # trailing slash inside the widget code's URL composition.
    element_call_url = element_call_base_url.rstrip("/")

    # CloudFront / ACM-for-CloudFront only live in us-east-1, so
    # ApexEdgeStack is pinned there. Everything else stays in the
    # app's primary region; cross-stack references between regions
    # are explicitly enabled.
    apex_edge_env = cdk.Environment(account=env.account, region="us-east-1")

    foundation = FoundationStack(
        app,
        "FoundationStack",
        imports=FoundationImports(cfg=cfg.foundation),
        env=env,
        cross_region_references=True,
    ).exports
    data = DataStack(
        app,
        "DataStack",
        imports=DataImports(
            cfg=cfg.data,
            foundation=foundation,
            databases=[
                cfg.authentik.db,
                cfg.headscale.db,
                cfg.vaultwarden.db,
                cfg.matrix.db,
            ],
            assets=assets,
        ),
        env=env,
    ).exports
    AuthentikStack(
        app,
        "AuthentikStack",
        imports=AuthentikImports(
            cfg=cfg.authentik,
            foundation=foundation,
            data=data,
            assets=assets,
            tailscale_redirect_uri="https://login.tailscale.com/a/oauth_response",
            headscale_redirect_uri=f"https://{headscale_fqdn}/oidc/callback",
            headplane_redirect_uri=f"https://{headplane_fqdn}/admin/oidc/callback",
            headplane_launch_url=f"https://{headplane_fqdn}/admin",
            vaultwarden_redirect_uri=f"https://{vaultwarden_fqdn}/identity/connect/oidc-signin",
            rspamd_redirect_uri=rspamd_redirect_uri,
            roundcube_redirect_uri=roundcube_redirect_uri,
            matrix_redirect_uri=matrix_redirect_uri,
        ),
        env=env,
    )
    webfinger = WebFingerStack(
        app,
        "WebFingerStack",
        imports=WebFingerImports(
            cfg=cfg.webfinger,
            foundation=foundation,
            assets=assets,
            authentik_issuer_base=authentik_issuer_base,
        ),
        env=env,
        cross_region_references=True,
    ).exports
    HeadscaleStack(
        app,
        "HeadscaleStack",
        imports=HeadscaleImports(
            cfg=cfg.headscale,
            foundation=foundation,
            data=data,
            assets=assets,
            authentik_issuer_base=authentik_issuer_base,
        ),
        env=env,
    )
    VaultwardenStack(
        app,
        "VaultwardenStack",
        imports=VaultwardenImports(
            cfg=cfg.vaultwarden,
            foundation=foundation,
            data=data,
            authentik_issuer_base=authentik_issuer_base,
        ),
        env=env,
    )
    mail = MailStack(
        app,
        "MailStack",
        imports=MailImports(
            cfg=cfg.mail,
            foundation=foundation,
            assets=assets,
            authentik_issuer_base=authentik_issuer_base,
            rspamd_redirect_uri=rspamd_redirect_uri,
        ),
        env=env,
    ).exports
    WebmailStack(
        app,
        "WebmailStack",
        imports=WebmailImports(
            cfg=cfg.webmail,
            foundation=foundation,
            mail=mail,
            assets=assets,
            mail_fqdn=mail_fqdn,
            authentik_issuer_base=authentik_issuer_base,
        ),
        env=env,
    )
    turn = TurnStack(
        app,
        "TurnStack",
        imports=TurnImports(
            cfg=cfg.turn,
            foundation=foundation,
            assets=assets,
        ),
        env=env,
    ).exports
    LkJwtStack(
        app,
        "LkJwtStack",
        imports=LkJwtImports(
            cfg=cfg.lk_jwt,
            foundation=foundation,
            turn=turn,
        ),
        env=env,
    )
    MatrixStack(
        app,
        "MatrixStack",
        imports=MatrixImports(
            cfg=cfg.matrix,
            foundation=foundation,
            data=data,
            assets=assets,
            authentik_issuer_base=authentik_issuer_base,
            element_web_base_url=element_web_base_url,
            element_call_base_url=element_call_base_url,
            turn_shared_secret=turn.turn_shared_secret,
            turn_uris=turn.turn_uris,
            turn_user_lifetime_seconds=cfg.turn.turn_user_lifetime_seconds,
        ),
        env=env,
    )
    ApexEdgeStack(
        app,
        "ApexEdgeStack",
        imports=ApexEdgeImports(
            cfg=cfg.apex_edge,
            foundation=foundation,
            assets=assets,
            behaviors=[
                # WebFinger responses are dynamic JSON keyed on a
                # query parameter; bypass the cache and forward
                # query strings.
                ApexBehavior(
                    path_pattern="/.well-known/webfinger*",
                    options=cloudfront.BehaviorOptions(
                        origin=origins.HttpOrigin(
                            webfinger.api_invoke_domain,
                            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                        ),
                        allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                        origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    ),
                ),
            ],
            content_deployments=[
                # Matrix federation/client discovery served from the
                # apex. Synapse lives at matrix.<public_domain>;
                # these JSON files tell Matrix federation peers and
                # clients to look there. `prune=False` avoids
                # fighting the apex site-content deployment over
                # object retention.
                ApexContentDeployment(
                    construct_id="MatrixWellKnown",
                    sources=[
                        s3deploy.Source.json_data(
                            ".well-known/matrix/server",
                            {"m.server": f"{matrix_fqdn}:443"},
                        ),
                        s3deploy.Source.json_data(
                            ".well-known/matrix/client",
                            {
                                "m.homeserver": {
                                    "base_url": f"https://{matrix_fqdn}",
                                },
                                # Element-Web reads this and uses
                                # our Element-Call deploy instead
                                # of the default call.element.io.
                                "io.element.call": {
                                    "preferred_domain": element_call_base_url,
                                },
                            },
                        ),
                    ],
                    content_type="application/json",
                    distribution_paths=["/.well-known/matrix/*"],
                    prune=False,
                ),
            ],
        ),
        env=apex_edge_env,
        cross_region_references=True,
    )
    ElementWebStack(
        app,
        "ElementWebStack",
        imports=ElementWebImports(
            cfg=cfg.element_web,
            foundation=foundation,
            assets=assets,
            matrix_fqdn=matrix_fqdn,
            element_call_url=element_call_url,
        ),
        env=apex_edge_env,
        cross_region_references=True,
    )
    ElementCallStack(
        app,
        "ElementCallStack",
        imports=ElementCallImports(
            cfg=cfg.element_call,
            foundation=foundation,
            assets=assets,
            matrix_fqdn=matrix_fqdn,
            lk_jwt_fqdn=lk_jwt_fqdn,
        ),
        env=apex_edge_env,
        cross_region_references=True,
    )
    OpenClawStack(
        app,
        "OpenClawStack",
        imports=OpenClawImports(
            foundation=foundation,
            assets=assets,
            matrix_homeserver_url=f"https://{matrix_fqdn}",
            allowed_sender=f"@{cfg.authentik.user.username}:{cfg.foundation.public_domain}",
        ),
        env=env,
    )
