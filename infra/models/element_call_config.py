from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class ElementCallConfig:
    subdomain: str
    # Either "latest" (resolves at synth time via GitHub API; new
    # Element-Call releases auto-deploy on next cdk diff/deploy) or
    # a pinned release tag like "livekit-v0.13.0" (skips the API
    # call, never auto-updates -- escape hatch when a release is
    # bad).
    version: str

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            subdomain=data["subdomain"],
            version=data.get("version", "latest"),
        )
