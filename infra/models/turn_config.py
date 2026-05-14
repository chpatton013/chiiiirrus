from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class TurnConfig:
    # coturn at `<turn_subdomain>.<public_domain>` and livekit-server
    # at `<livekit_subdomain>.<public_domain>` share one EC2 host +
    # one TLS cert (multi-SAN). Both subdomains resolve to the same
    # Elastic IP via Route53 A records.
    turn_subdomain: str
    livekit_subdomain: str
    instance_type: str
    # coturn's TURN relay UDP port range. Each concurrent relay
    # allocates one port; 100 ports is plenty for a household and
    # keeps the security-group rule count manageable.
    relay_min_port: int
    relay_max_port: int
    # The Matrix client TTL on TURN credentials. Synapse re-signs
    # credentials on /voip/turnServer using turn_shared_secret;
    # longer lifetimes let calls survive without re-signing
    # mid-call. 86400 (24h) is the practical max coturn supports.
    turn_user_lifetime_seconds: int

    @classmethod
    def load(cls, data: dict[str, Any]) -> Self:
        return cls(
            turn_subdomain=data["turn_subdomain"],
            livekit_subdomain=data["livekit_subdomain"],
            instance_type=data["instance_type"],
            relay_min_port=data["relay_min_port"],
            relay_max_port=data["relay_max_port"],
            turn_user_lifetime_seconds=data["turn_user_lifetime_seconds"],
        )
