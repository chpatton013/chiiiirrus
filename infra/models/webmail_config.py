from dataclasses import dataclass
from typing import Any, Self

from .fargate_task_config import FargateTaskConfig


@dataclass(frozen=True)
class WebmailConfig:
    subdomain: str
    image_version: str
    task: FargateTaskConfig

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            subdomain=data["subdomain"],
            image_version=data["image_version"],
            task=FargateTaskConfig.load(data["task"]),
        )
