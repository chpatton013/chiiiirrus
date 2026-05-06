import aws_cdk as cdk

from .models.app_config import AppConfig
from .models.asset_loader import AssetLoader
from .stacks.authentik_stack import AuthentikImports, AuthentikStack
from .stacks.data_stack import DataImports, DataStack
from .stacks.foundation_stack import FoundationImports, FoundationStack
from .stacks.headscale_stack import HeadscaleImports, HeadscaleStack
from .stacks.mail_stack import MailImports, MailStack
from .stacks.openclaw_stack import OpenClawImports, OpenClawStack
from .stacks.site_stack import SiteImports, SiteStack
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

    # CloudFront / ACM-for-CloudFront only live in us-east-1, so SiteStack
    # is pinned there. Everything else stays in the app's primary region;
    # cross-stack references between regions are explicitly enabled.
    site_env = cdk.Environment(account=env.account, region="us-east-1")

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
            rspamd_redirect_uri=rspamd_redirect_uri,
            roundcube_redirect_uri=roundcube_redirect_uri,
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
    SiteStack(
        app,
        "SiteStack",
        imports=SiteImports(
            cfg=cfg.site,
            foundation=foundation,
            assets=assets,
            webfinger_api_domain=webfinger.api_invoke_domain,
        ),
        env=site_env,
        cross_region_references=True,
    )
    OpenClawStack(
        app,
        "OpenClawStack",
        imports=OpenClawImports(foundation=foundation),
        env=env,
    )
