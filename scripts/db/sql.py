"""Drop into an ECS-Exec shell on a service tagged for DB access.

Discovers ECS services tagged `DbExec=true` across every cluster
in the current account/region, lists them by `DbExecLabel`, and
`aws ecs execute-command`s an interactive shell into one. The shell
inherits the container's DB connection env (host/port/name/user/
password) so the operator can run psql or python from inside.

The tool is intentionally agnostic about which DB client lives in
the container -- Synapse / Authentik images ship psycopg2, others
ship psql. The hint printed before the session lists the actual env
var names the service is using (since they differ by image, e.g.
AUTHENTIK_POSTGRESQL__HOST vs HEADSCALE_DATABASE_POSTGRES_HOST vs
the plain DB_HOST default).

Tags read off each service:
  DbExec=true                        opt-in marker
  DbExecLabel=<short-name>           CLI arg ("matrix", "authentik", ...)
  DbExecContainer=<name>             container in the task to exec into
  DbExecEnvPrefix=<prefix>           e.g. "DB_" or "AUTHENTIK_POSTGRESQL__"
  DbExecEnvPasswordSuffix=<suffix>   PASSWORD by default; PASS for Headscale

Usage:
  bin/db-sql              # list known services
  bin/db-sql <label>      # interactive shell into that service
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3


def _tags_of(svc: dict) -> dict[str, str]:
    return {t["key"]: t["value"] for t in svc.get("tags", []) or []}


def _list_clusters(ecs) -> list[str]:
    out: list[str] = []
    for page in ecs.get_paginator("list_clusters").paginate():
        out.extend(page["clusterArns"])
    return out


def _list_services(ecs, cluster: str) -> list[str]:
    out: list[str] = []
    for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
        out.extend(page["serviceArns"])
    return out


def find_db_exec_services(ecs) -> list[dict]:
    found: list[dict] = []
    for cluster in _list_clusters(ecs):
        arns = _list_services(ecs, cluster)
        # describe_services accepts up to 10 at a time.
        for start in range(0, len(arns), 10):
            batch = arns[start : start + 10]
            described = ecs.describe_services(
                cluster=cluster, services=batch, include=["TAGS"]
            )
            for svc in described["services"]:
                tags = _tags_of(svc)
                if tags.get("DbExec") != "true":
                    continue
                found.append(
                    {
                        "label": tags.get("DbExecLabel") or svc["serviceName"],
                        "cluster": cluster,
                        "service_name": svc["serviceName"],
                        "container": tags.get("DbExecContainer", "Container"),
                        "env_prefix": tags.get("DbExecEnvPrefix", "DB_"),
                        "env_password_suffix": tags.get(
                            "DbExecEnvPasswordSuffix", "PASSWORD"
                        ),
                    }
                )
    return found


def _env_names(svc: dict) -> dict[str, str]:
    p = svc["env_prefix"]
    return {
        "host": f"{p}HOST",
        "port": f"{p}PORT",
        "name": f"{p}NAME",
        "user": f"{p}USER",
        "password": f"{p}{svc['env_password_suffix']}",
    }


def _print_hint(svc: dict) -> None:
    e = _env_names(svc)
    print(f"# {svc['label']}: container env names", file=sys.stderr)
    for k, v in e.items():
        print(f"#   {k:>9s} = ${v}", file=sys.stderr)
    print("#", file=sys.stderr)
    print(
        f"# psql:    PGPASSWORD=\"${e['password']}\" "
        f"psql -h \"${e['host']}\" -p \"${e['port']}\" "
        f"-U \"${e['user']}\" -d \"${e['name']}\"",
        file=sys.stderr,
    )
    print(
        "# python:  python3 -c 'import os, psycopg2; "
        "c=psycopg2.connect("
        f'host=os.environ["{e["host"]}"], '
        f'port=int(os.environ["{e["port"]}"]), '
        f'dbname=os.environ["{e["name"]}"], '
        f'user=os.environ["{e["user"]}"], '
        f'password=os.environ["{e["password"]}"], '
        'sslmode="require")\'',
        file=sys.stderr,
    )
    print("", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ECS-Exec into a service that has DB access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "label",
        nargs="?",
        help="DbExecLabel of the service to exec into (omit to list)",
    )
    parser.add_argument(
        "--command",
        default="/bin/sh",
        help="Command to run inside the container (default: /bin/sh)",
    )
    args = parser.parse_args()

    ecs = boto3.client("ecs")
    services = find_db_exec_services(ecs)
    if not services:
        sys.exit("no ECS services tagged DbExec=true found")

    if not args.label:
        print("Services available (pass a label as the positional arg):")
        for s in services:
            cluster = s["cluster"].rsplit("/", 1)[-1]
            print(
                f"  {s['label']:20s}  cluster={cluster} "
                f"container={s['container']} prefix={s['env_prefix']}"
            )
        return 0

    matches = [s for s in services if s["label"] == args.label]
    if not matches:
        sys.exit(
            f"no service with DbExecLabel={args.label!r}; "
            f"available: {[s['label'] for s in services]}"
        )
    if len(matches) > 1:
        sys.exit(f"multiple services with DbExecLabel={args.label!r}; ambiguous")
    svc = matches[0]

    tasks = ecs.list_tasks(
        cluster=svc["cluster"],
        serviceName=svc["service_name"],
        desiredStatus="RUNNING",
    )
    task_arns = tasks.get("taskArns") or []
    if not task_arns:
        sys.exit(f"no running tasks for service {svc['service_name']}")

    _print_hint(svc)
    os.execvp(
        "aws",
        [
            "aws",
            "ecs",
            "execute-command",
            "--cluster",
            svc["cluster"],
            "--task",
            task_arns[0],
            "--container",
            svc["container"],
            "--interactive",
            "--command",
            args.command,
        ],
    )


if __name__ == "__main__":
    sys.exit(main() or 0)
