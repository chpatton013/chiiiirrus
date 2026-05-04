"""Tiny EventBridge-triggered Lambda that calls
``ecs:UpdateService(forceNewDeployment=True)`` on the mail Fargate
service so the init container re-runs at least once a month.

The init container does Let's Encrypt cert renewal at task start; lego
no-ops when the cert is fresh and renews when it's <30 days from
expiry. ECS would otherwise only replace the task on stack updates,
which could let the cert lapse silently. This Lambda fires on a monthly
EventBridge schedule to guarantee cadence.
"""

import os

import boto3

ecs = boto3.client("ecs")

CLUSTER_ARN = os.environ["CLUSTER_ARN"]
SERVICE_ARN = os.environ["SERVICE_ARN"]


def handler(_event, _ctx):
    response = ecs.update_service(
        cluster=CLUSTER_ARN,
        service=SERVICE_ARN,
        forceNewDeployment=True,
    )
    deployment = (response.get("service") or {}).get("deployments", [{}])[0]
    return {
        "status": deployment.get("status"),
        "rollout": deployment.get("rolloutState"),
    }
