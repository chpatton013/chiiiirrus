from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

from ..models.asset_loader import AssetLoader
from ..models.foundation_exports import FoundationExports
from ..models.site_config import SiteConfig


@dataclass(frozen=True)
class SiteImports:
    cfg: SiteConfig
    foundation: FoundationExports
    assets: AssetLoader
    # WebFinger's regional API invoke domain
    # (e.g. "abc.execute-api.us-west-2.amazonaws.com"). CloudFront
    # forwards `/.well-known/webfinger*` here so the apex hostname can
    # serve a static landing page from S3 by default while preserving
    # WebFinger discovery for Tailscale OIDC.
    webfinger_api_domain: str


class SiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: SiteImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        cfg = imports.cfg
        foundation = imports.foundation
        apex_fqdn = foundation.public_domain
        names = [apex_fqdn]
        if cfg.www_subdomain:
            names.append(f"{cfg.www_subdomain}.{apex_fqdn}")

        # Private S3 bucket; CloudFront reads via Origin Access Control.
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        cert = acm.Certificate(
            self,
            "Certificate",
            domain_name=apex_fqdn,
            subject_alternative_names=names[1:],
            validation=acm.CertificateValidation.from_dns(foundation.public_zone),
        )

        webfinger_origin = origins.HttpOrigin(
            imports.webfinger_api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        )

        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                # WebFinger responses are dynamic JSON keyed on a query
                # parameter; bypass the cache and forward query strings.
                "/.well-known/webfinger*": cloudfront.BehaviorOptions(
                    origin=webfinger_origin,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            },
            domain_names=names,
            certificate=cert,
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(60),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=404,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(60),
                ),
            ],
        )

        s3deploy.BucketDeployment(
            self,
            "SiteContent",
            sources=[s3deploy.Source.asset(str(imports.assets.site_path()))],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        target = route53.RecordTarget.from_alias(
            cast(
                route53.IAliasRecordTarget,
                route53_targets.CloudFrontTarget(distribution),
            )
        )
        route53.ARecord(
            self,
            "ApexA",
            zone=foundation.public_zone,
            record_name=apex_fqdn,
            target=target,
        )
        if cfg.www_subdomain:
            route53.ARecord(
                self,
                "WwwA",
                zone=foundation.public_zone,
                record_name=f"{cfg.www_subdomain}.{apex_fqdn}",
                target=target,
            )
