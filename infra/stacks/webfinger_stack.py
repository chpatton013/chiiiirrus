from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    Aws,
    Duration,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_lambda as lambda_,
)
from constructs import Construct

from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.webfinger_config import WebFingerConfig


@dataclass(frozen=True)
class WebFingerImports:
    cfg: WebFingerConfig
    foundation: FoundationExports
    assets: AssetLoader
    authentik_issuer_base: str


@dataclass(frozen=True)
class WebFingerExports:
    # Regional invoke domain for the HTTP API (default stage, no path
    # prefix). app_builder.py wraps this in an `ApexBehavior` so
    # ApexEdgeStack's CloudFront distribution routes
    # `/.well-known/webfinger*` here.
    api_invoke_domain: str


class WebFingerStack(Stack):
    @property
    def exports(self) -> WebFingerExports:
        return self._exports

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: WebFingerImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        oidc_issuer_url = (
            f"{imports.authentik_issuer_base}/{cfg.oidc_issuer_application}/"
        )

        handler = lambda_.Function(
            self,
            "Handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(5),
            code=lambda_.Code.from_asset(str(imports.assets.lambda_path("webfinger"))),
            environment={
                "WEBFINGER_SUBJECT": cfg.subject,
                "WEBFINGER_ISSUER_URL": oidc_issuer_url,
            },
        )

        api = apigwv2.HttpApi(self, "HttpApi")
        api.add_routes(
            path="/.well-known/webfinger",
            methods=[apigwv2.HttpMethod.GET],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "Integration", cast(lambda_.IFunction, handler)
            ),
        )

        self._exports = WebFingerExports(
            api_invoke_domain=f"{api.api_id}.execute-api.{Aws.REGION}.amazonaws.com",
        )
