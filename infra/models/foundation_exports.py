from dataclasses import dataclass

from aws_cdk import (
    aws_backup as backup,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_route53 as route53,
)


@dataclass(frozen=True)
class FoundationExports:
    public_domain: str
    public_zone: route53.IHostedZone
    private_domain: str
    private_zone: route53.IHostedZone
    vpc: ec2.IVpc
    cluster: ecs.ICluster
    ghcr_mirror_base: str
    ghcr_mirror_namespace: str
    dockerhub_mirror_base: str
    dockerhub_mirror_namespace: str
    backup_vault: backup.IBackupVault
