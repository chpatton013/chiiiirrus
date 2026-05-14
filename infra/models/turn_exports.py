from dataclasses import dataclass

from aws_cdk import aws_secretsmanager as secretsmanager


@dataclass(frozen=True)
class TurnExports:
    # FQDNs for the shared coturn + livekit EC2 host.
    turn_fqdn: str
    livekit_fqdn: str
    # Synapse signs ephemeral TURN credentials using this shared
    # secret; coturn validates them with the same key. Imported as
    # an ECS secret into the Synapse init container.
    turn_shared_secret: secretsmanager.ISecret
    # Element-Call clients connect to this signaling URL after
    # they've been handed a LiveKit JWT by lk-jwt-service.
    livekit_url: str
    # lk-jwt-service uses these to mint LiveKit access tokens.
    livekit_api_key_secret: secretsmanager.ISecret
    livekit_api_secret_secret: secretsmanager.ISecret
    # Pre-rendered list of `turn_uris:` entries for Synapse:
    # turn:..., turn:...?transport=tcp, turns:...?transport=tcp.
    # Joined with newlines + indentation as Synapse expects them.
    turn_uris: list[str]
