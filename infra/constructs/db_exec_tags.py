"""Tag an ECS service for discovery by bin/db-sql.

bin/db-sql lists services tagged DbExec=true and shells into the
one selected by DbExecLabel. The env-prefix / password-suffix tags
let the tool print the right hint for each service's image-specific
DB env conventions (Synapse uses DB_HOST, Authentik uses
AUTHENTIK_POSTGRESQL__HOST, Headscale uses
HEADSCALE_DATABASE_POSTGRES_HOST, etc).
"""

from aws_cdk import Tags, aws_ecs as ecs


def tag_for_db_exec(
    service: ecs.IBaseService,
    *,
    label: str,
    container: str = "Container",
    env_prefix: str = "DB_",
    env_password_suffix: str = "PASSWORD",
) -> None:
    tags = Tags.of(service)
    tags.add("DbExec", "true")
    tags.add("DbExecLabel", label)
    tags.add("DbExecContainer", container)
    tags.add("DbExecEnvPrefix", env_prefix)
    tags.add("DbExecEnvPasswordSuffix", env_password_suffix)
