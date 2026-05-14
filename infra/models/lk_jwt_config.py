from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class LkJwtTaskConfig:
    cpu: int
    memory_limit_mib: int
    desired_count: int
    min_healthy_percent: int

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            cpu=data["cpu"],
            memory_limit_mib=data["memory_limit_mib"],
            desired_count=data["desired_count"],
            min_healthy_percent=data["min_healthy_percent"],
        )


@dataclass(frozen=True)
class LkJwtConfig:
    # Mints short-lived LiveKit JWTs for Element-Call clients after
    # verifying their Matrix OpenID token against Synapse. Stateless
    # HTTP service, fronted by PublicHttpAlb at this subdomain.
    subdomain: str
    image_version: str
    task: LkJwtTaskConfig

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            subdomain=data["subdomain"],
            image_version=data["image_version"],
            task=LkJwtTaskConfig.load(data["task"]),
        )
