"""Self-host Element-Call from S3 + CloudFront pointed at our lk-jwt.

Element-Call is a static React SPA, same shape as Element-Web.
We pin a release version (or let it auto-update to "latest"),
fetch the upstream tarball at CDK synth time, render our own
config.json, and deploy the result to S3 + CloudFront.

Element-Call's release artifact naming differs from Element-Web:
the tag is `vX.Y.Z` but the tarball is `element-call-X.Y.Z.tar.gz`
(no `v` in the filename). The release tag is also the version the
GitHub API returns.
"""

import json
import pathlib
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass
from typing import cast

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Size,
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
from ..models.element_call_config import ElementCallConfig
from ..models.foundation_exports import FoundationExports

_ELEMENT_CALL_RELEASES_API = (
    "https://api.github.com/repos/element-hq/element-call/releases/latest"
)
_ELEMENT_CALL_DOWNLOAD_BASE = (
    "https://github.com/element-hq/element-call/releases/download"
)


def _resolve_version(version_spec: str) -> str:
    """Return a concrete release tag (e.g. `v0.19.3`).

    `"latest"` triggers a GitHub API lookup; anything else is
    returned verbatim. Pinned versions never auto-update; the
    "latest" mode re-resolves on every synth.
    """
    if version_spec != "latest":
        return version_spec
    req = urllib.request.Request(
        _ELEMENT_CALL_RELEASES_API,
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["tag_name"]


def _fetch_bundle(version: str, cache_dir: pathlib.Path) -> pathlib.Path:
    """Download + extract Element-Call's tarball; return the dist dir.

    Cached by version. `index.html` marks a complete extract.
    Tarball filename is `element-call-<bare>.tar.gz`, where
    `<bare>` is the tag with any leading `v` stripped.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    bare_version = version.lstrip("v")
    tarball = cache_dir / f"element-call-{bare_version}.tar.gz"
    extract_dir = cache_dir / f"dist-{version}"

    if not (extract_dir.is_dir() and (extract_dir / "index.html").is_file()):
        if not tarball.is_file():
            url = f"{_ELEMENT_CALL_DOWNLOAD_BASE}/{version}/element-call-{bare_version}.tar.gz"
            urllib.request.urlretrieve(url, tarball)

        if extract_dir.is_dir():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir()
        with tarfile.open(tarball, "r:gz") as t:
            t.extractall(extract_dir)
        # Element-Call's tarball flattens to either a single
        # versioned subdir or directly to the SPA files. Handle
        # both: if there's exactly one inner dir, hoist its
        # contents one level up.
        children = list(extract_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():
            inner = children[0]
            for child in inner.iterdir():
                child.rename(extract_dir / child.name)
            inner.rmdir()
    # Strip source maps -- same rationale as ElementWebStack.
    for m in extract_dir.rglob("*.map"):
        m.unlink()
    return extract_dir


@dataclass(frozen=True)
class ElementCallImports:
    cfg: ElementCallConfig
    foundation: FoundationExports
    assets: AssetLoader
    # Element-Call points itself at our Synapse + lk-jwt. Comes
    # from app_builder so this stack stays unaware of how the
    # other FQDNs are composed.
    matrix_fqdn: str
    lk_jwt_fqdn: str


class ElementCallStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: ElementCallImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        version = _resolve_version(cfg.version)
        bundle_dir = _fetch_bundle(version, imports.assets.element_call_cache_path())

        config = imports.assets.render_template(
            "element-call",
            "config.json.tmpl",
            substitutions={
                "MATRIX_FQDN": imports.matrix_fqdn,
                "SERVER_NAME": foundation.public_domain,
                "LK_JWT_FQDN": imports.lk_jwt_fqdn,
            },
        )
        (bundle_dir / "config.json").write_text(config)

        bucket = s3.Bucket(
            self,
            "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        cert = acm.Certificate(
            self,
            "Certificate",
            domain_name=fqdn,
            validation=acm.CertificateValidation.from_dns(foundation.public_zone),
        )

        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            domain_names=[fqdn],
            certificate=cert,
            default_root_object="index.html",
            # SPA: route every "not found" back to index.html so
            # client-side routing handles the URL.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(60),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(60),
                ),
            ],
        )

        s3deploy.BucketDeployment(
            self,
            "Content",
            sources=[s3deploy.Source.asset(str(bundle_dir))],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            memory_limit=1024,
            ephemeral_storage_size=Size.mebibytes(1024),
        )

        target = route53.RecordTarget.from_alias(
            cast(
                route53.IAliasRecordTarget,
                route53_targets.CloudFrontTarget(distribution),
            )
        )
        route53.ARecord(
            self,
            "A",
            zone=foundation.public_zone,
            record_name=fqdn,
            target=target,
        )
