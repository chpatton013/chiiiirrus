from dataclasses import dataclass

from aws_cdk import (
    aws_ec2 as ec2,
    aws_efs as efs,
)


@dataclass(frozen=True)
class MailExports:
    # Mail's EFS file system + the access point reserved for Roundcube
    # state (sqlite + config). WebmailStack mounts this access point so
    # Roundcube state lives on the same EFS as the mail server,
    # bringing it under the same backup plan.
    efs_filesystem: efs.IFileSystem
    efs_security_group: ec2.ISecurityGroup
    roundcube_access_point: efs.IAccessPoint
