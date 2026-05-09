from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class FoundationConfig:
    project_name: str
    public_domain: str
    private_domain: str

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            project_name=data["project_name"],
            public_domain=data["public_domain"],
            private_domain=data["private_domain"],
        )
