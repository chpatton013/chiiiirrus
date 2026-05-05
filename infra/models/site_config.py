from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class SiteConfig:
    www_subdomain: str | None

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(www_subdomain=data.get("www_subdomain"))
