from dataclasses import dataclass
from typing import Any, Self

from .fargate_task_config import FargateTaskConfig


@dataclass(frozen=True)
class MailRelayConfig:
    port: int
    iam_user_name: str
    secret_name: str

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            port=data.get("port", 587),
            iam_user_name=data["iam_user_name"],
            secret_name=data["secret_name"],
        )


@dataclass(frozen=True)
class MailConfig:
    subdomain: str
    postmaster_address: str
    image_version: str
    task: FargateTaskConfig
    relay: MailRelayConfig
    enable_smtps: bool
    enable_clamav: bool
    users: tuple[str, ...]

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            subdomain=data["subdomain"],
            postmaster_address=data["postmaster_address"],
            image_version=data["image_version"],
            task=FargateTaskConfig.load(data["task"]),
            relay=MailRelayConfig.load(data["relay"]),
            enable_smtps=bool(data.get("enable_smtps", False)),
            enable_clamav=bool(data.get("enable_clamav", True)),
            users=tuple(data.get("users", ())),
        )
