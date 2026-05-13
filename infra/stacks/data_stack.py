from typing import cast

from dataclasses import dataclass

from aws_cdk import (
    CustomResource,
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from ..constructs.database_instance import PrivateIsolatedDatabaseInstance
from ..models.asset_loader import AssetLoader
from ..models.data_config import DataConfig
from ..models.data_exports import DataExports
from ..models.db_config import DbConfig
from ..models.foundation_exports import FoundationExports
from ..models.instance_type import INSTANCE_TYPES


@dataclass(frozen=True)
class DataImports:
    cfg: DataConfig
    foundation: FoundationExports
    databases: list[DbConfig]
    assets: AssetLoader


class DataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: DataImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        databases = imports.databases
        assets = imports.assets

        self.secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DatabaseSecret", cfg.master_secret_name
        )

        self.database = PrivateIsolatedDatabaseInstance(
            self,
            "Database",
            secret=self.secret,
            vpc=foundation.vpc,
            port=cfg.instance.port,
            instance_kwargs=dict(
                engine=rds.DatabaseInstanceEngine.postgres(
                    version=rds.PostgresEngineVersion.VER_16
                ),
                instance_type=INSTANCE_TYPES[cfg.instance.instance_type],
                allocated_storage=cfg.instance.allocated_storage_gib,
                max_allocated_storage=100,
                multi_az=False,
                storage_encrypted=True,
                publicly_accessible=False,
                # RDS automated snapshots are always-on. The CDK
                # default is a 1-day retention; bump to 14 to match
                # the EFS lifecycle policy + give us headroom on
                # ransomware / oops-deletes.
                backup_retention=Duration.days(14),
            ),
        )

        init_fn = lambda_python.PythonFunction(
            self,
            "DbInitFn",
            entry=str(assets.lambda_path("rds_logical_databases")),
            runtime=lambda_.Runtime.PYTHON_3_12,
            index="index.py",
            handler="handler",
            timeout=Duration.minutes(2),
            vpc=foundation.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )
        self.secret.grant_read(init_fn)
        self.database.instance.connections.allow_default_port_from(init_fn)

        database_entries = []
        for db_cfg in databases:
            db_secret = secretsmanager.Secret.from_secret_name_v2(
                self, f"DbSecret-{db_cfg.name}", db_cfg.secret_name
            )
            db_secret.grant_read(init_fn)
            entry = {
                "Name": db_cfg.name,
                "User": db_cfg.name,
                "SecretArn": db_secret.secret_arn,
            }
            if db_cfg.collation is not None:
                entry["Collation"] = db_cfg.collation
            if db_cfg.ctype is not None:
                entry["Ctype"] = db_cfg.ctype
            if db_cfg.template is not None:
                entry["Template"] = db_cfg.template
            database_entries.append(entry)

        provider = cr.Provider(
            self,
            "DbInitProvider",
            on_event_handler=cast(lambda_.IFunction, init_fn),
        )
        init = CustomResource(
            self,
            "DbInit",
            service_token=provider.service_token,
            properties={
                "Host": self.database.instance.db_instance_endpoint_address,
                "Port": self.database.instance.db_instance_endpoint_port,
                "MasterSecretArn": self.secret.secret_arn,
                "Databases": database_entries,
            },
        )
        init.node.add_dependency(self.database.instance)

        self.exports = DataExports(
            database=self.database,
            master_secret=self.secret,
        )
