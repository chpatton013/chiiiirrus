from dataclasses import dataclass
from typing import Any, Self

from .fargate_task_config import FargateTaskConfig


@dataclass(frozen=True)
class SynapseAdminConfig:
    subdomain: str
    # Ketesa release tag (formerly synapse-admin; etkecc/ketesa on GitHub).
    # Pin to a specific `vX.Y.Z`; the Dockerfile fetches the matching
    # ketesa.tar.gz from the GitHub release.
    version: str
    task: FargateTaskConfig

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            subdomain=data["subdomain"],
            version=data["version"],
            task=FargateTaskConfig.load(data["task"]),
        )
