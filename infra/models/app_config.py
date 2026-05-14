import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Self

from .apex_edge_config import ApexEdgeConfig
from .authentik_config import AuthentikConfig
from .data_config import DataConfig
from .element_call_config import ElementCallConfig
from .element_web_config import ElementWebConfig
from .foundation_config import FoundationConfig
from .headscale_config import HeadscaleConfig
from .lk_jwt_config import LkJwtConfig
from .mail_config import MailConfig
from .matrix_config import MatrixConfig
from .turn_config import TurnConfig
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
    turn: TurnConfig
    lk_jwt: LkJwtConfig
    element_call: ElementCallConfig

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
            turn=TurnConfig.load(data["turn"]),
            lk_jwt=LkJwtConfig.load(data["lk_jwt"]),
            element_call=ElementCallConfig.load(data["element_call"]),
        )


def load_config(path: pathlib.Path) -> AppConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.load(data)
