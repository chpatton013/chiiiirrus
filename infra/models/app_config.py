import pathlib
import tomllib
from dataclasses import dataclass
from typing import Any, Self

from .authentik_config import AuthentikConfig
from .data_config import DataConfig
from .foundation_config import FoundationConfig
from .headscale_config import HeadscaleConfig
from .mail_config import MailConfig
from .vaultwarden_config import VaultwardenConfig
from .webfinger_config import WebFingerConfig


@dataclass(frozen=True)
class AppConfig:
    foundation: FoundationConfig
    data: DataConfig
    authentik: AuthentikConfig
    webfinger: WebFingerConfig
    headscale: HeadscaleConfig
    vaultwarden: VaultwardenConfig
    mail: MailConfig

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
        )


def load_config(path: pathlib.Path) -> AppConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.load(data)
