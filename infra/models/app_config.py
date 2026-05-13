import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Self

from .apex_edge_config import ApexEdgeConfig
from .authentik_config import AuthentikConfig
from .data_config import DataConfig
from .element_web_config import ElementWebConfig
from .foundation_config import FoundationConfig
from .headscale_config import HeadscaleConfig
from .mail_config import MailConfig
from .matrix_config import MatrixConfig
from .vaultwarden_config import VaultwardenConfig
from .webfinger_config import WebFingerConfig
from .webmail_config import WebmailConfig


@dataclass(frozen=True)
class AppConfig:
    foundation: FoundationConfig
    data: DataConfig
    authentik: AuthentikConfig
    webfinger: WebFingerConfig
    headscale: HeadscaleConfig
    vaultwarden: VaultwardenConfig
    mail: MailConfig
    matrix: MatrixConfig
    apex_edge: ApexEdgeConfig
    webmail: WebmailConfig
    element_web: ElementWebConfig

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            foundation=FoundationConfig.load(data["foundation"]),
            data=DataConfig.load(data["data"]),
            authentik=AuthentikConfig.load(data["authentik"]),
            webfinger=WebFingerConfig.load(data["webfinger"]),
            headscale=HeadscaleConfig.load(data["headscale"]),
            vaultwarden=VaultwardenConfig.load(data["vaultwarden"]),
            mail=MailConfig.load(data["mail"]),
            matrix=MatrixConfig.load(data["matrix"]),
            apex_edge=ApexEdgeConfig.load(data["apex_edge"]),
            webmail=WebmailConfig.load(data["webmail"]),
            element_web=ElementWebConfig.load(data["element_web"]),
        )


def load_config(path: pathlib.Path) -> AppConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.load(data)
