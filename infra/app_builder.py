import aws_cdk as cdk

from .models.app_config import AppConfig
from .models.asset_loader import AssetLoader
from .stacks.authentik_stack import AuthentikImports, AuthentikStack
from .stacks.data_stack import DataImports, DataStack
from .stacks.foundation_stack import FoundationImports, FoundationStack
from .stacks.headscale_stack import HeadscaleImports, HeadscaleStack
from .stacks.mail_stack import MailImports, MailStack
from .stacks.openclaw_stack import OpenClawStack
from .stacks.vaultwarden_stack import VaultwardenImports, VaultwardenStack
from .stacks.webfinger_stack import WebFingerImports, WebFingerStack


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

    foundation = FoundationStack(
        app,
        "FoundationStack",
        imports=FoundationImports(cfg=cfg.foundation),
        env=env,
    ).exports
    data = DataStack(
        app,
        "DataStack",
        imports=DataImports(
            cfg=cfg.data,
            foundation=foundation,
            databases=[cfg.authentik.db, cfg.headscale.db, cfg.vaultwarden.db],
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
        ),
        env=env,
    )
    WebFingerStack(
        app,
        "WebFingerStack",
        imports=WebFingerImports(
            cfg=cfg.webfinger,
            foundation=foundation,
            assets=assets,
            authentik_issuer_base=authentik_issuer_base,
        ),
        env=env,
    )
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
    MailStack(
        app,
        "MailStack",
        imports=MailImports(
            cfg=cfg.mail,
            foundation=foundation,
            assets=assets,
        ),
        env=env,
    )
    OpenClawStack(app, "OpenClawStack", env=env)
