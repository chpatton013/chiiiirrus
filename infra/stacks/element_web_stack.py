"""Self-host Element-Web from S3 + CloudFront pointed at our Synapse.

Element-Web is a static React SPA. We pin a release version (or
let it auto-update to "latest"), fetch the upstream tarball at
CDK synth time, drop in our own config.json, and deploy the
result to an apex-edge-style S3 + CloudFront pair.

Fetching happens at synth time inside `__init__`, with a
filesystem cache keyed on the resolved version so repeated `cdk
synth` runs don't re-download. The cache lives under
`assets/element-web/cache/` (gitignored).
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
from ..models.element_web_config import ElementWebConfig
from ..models.foundation_exports import FoundationExports

_ELEMENT_RELEASES_API = (
    "https://api.github.com/repos/element-hq/element-web/releases/latest"
)
_ELEMENT_DOWNLOAD_BASE = "https://github.com/element-hq/element-web/releases/download"


def _resolve_version(version_spec: str) -> str:
    """Return a concrete release tag.

    `"latest"` triggers a GitHub API lookup; anything else is
    returned verbatim. Pinned versions never auto-update; the
    "latest" mode re-resolves on every synth, which is how
    auto-updates land (a new release flips the CDK asset hash,
    next deploy picks it up).
    """
    if version_spec != "latest":
        return version_spec
    req = urllib.request.Request(
        _ELEMENT_RELEASES_API,
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)["tag_name"]


def _fetch_bundle(version: str, cache_dir: pathlib.Path) -> pathlib.Path:
    """Download + extract Element-Web's tarball; return the dist dir.

    Cached by version. The marker file (`index.html`) lets a
    half-finished extract get retried cleanly on the next synth.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / f"element-{version}.tar.gz"
    extract_dir = cache_dir / f"dist-{version}"

    if not (extract_dir.is_dir() and (extract_dir / "index.html").is_file()):
        if not tarball.is_file():
            url = f"{_ELEMENT_DOWNLOAD_BASE}/{version}/element-{version}.tar.gz"
            urllib.request.urlretrieve(url, tarball)

        if extract_dir.is_dir():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir()
        with tarfile.open(tarball, "r:gz") as t:
            t.extractall(extract_dir)
        # Element's tarball extracts to a single `element-vX.Y.Z/`
        # subdir. Flatten so `extract_dir/index.html` lands directly.
        children = list(extract_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():
            inner = children[0]
            for child in inner.iterdir():
                child.rename(extract_dir / child.name)
            inner.rmdir()
    # Strip source maps every time -- they don't regenerate, but
    # an older cache may predate this scrub. Element ships ~70 MB
    # of .map files; serving them to users gives away the source
    # tree and blows out the BucketDeployment Lambda's tmpfs.
    for m in extract_dir.rglob("*.map"):
        m.unlink()
    return extract_dir


@dataclass(frozen=True)
class ElementWebImports:
    cfg: ElementWebConfig
    foundation: FoundationExports
    assets: AssetLoader
    # Element points itself at this Synapse FQDN. Comes from
    # app_builder so this stack stays unaware of how matrix_fqdn
    # is composed.
    matrix_fqdn: str


class ElementWebStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        imports: ElementWebImports,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = imports.cfg
        foundation = imports.foundation
        fqdn = f"{cfg.subdomain}.{foundation.public_domain}"

        # Fetch + materialize the Element bundle at synth time.
        version = _resolve_version(cfg.version)
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        cache_dir = repo_root / "assets" / "element-web" / "cache"
        bundle_dir = _fetch_bundle(version, cache_dir)

        # Render config.json on top of the upstream bundle.
        config_template = imports.assets.read_text("element-web", "config.json.tmpl")
        config = config_template.replace(
            "@@MATRIX_FQDN@@", imports.matrix_fqdn
        ).replace("@@SERVER_NAME@@", foundation.public_domain)
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
            # Element-Web is an SPA; route every "not found" back
            # to index.html so client-side routing handles the URL.
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
            # Element ships ~70 MB across ~1000 files. The default
            # 128 MB / 512 MB tmpfs / 15-min Lambda config can't
            # finish the upload before timing out.
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
