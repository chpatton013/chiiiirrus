import pathlib
import tempfile
import unittest
from typing import Any

import aws_cdk as cdk

from infra.app_builder import build_app
from infra.models.app_config import load_config
from infra.models.asset_loader import AssetLoader

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestSynth(unittest.TestCase):
    _tmpdir: tempfile.TemporaryDirectory
    _assembly: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        app = cdk.App(
            outdir=cls._tmpdir.name,
            context={
                # Skip asset bundling. We only care about template synthesis +
                # CDK validation (cross-stack refs, listener rules, etc.), not
                # the Docker-bundled Lambda payload.
                "aws:cdk:bundling-stacks": [],
            },
        )
        build_app(
            app,
            cfg=load_config(REPO_ROOT / "config.toml"),
            assets=AssetLoader(REPO_ROOT),
            # Placeholder env. HostedZone.from_lookup falls back to a dummy
            # value when context isn't cached and no resolver is attached,
            # which is fine for synth validation (we're not deploying).
            env=cdk.Environment(account="000000000000", region="us-east-1"),
        )
        cls._assembly = app.synth()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def _template(self, stack_name: str) -> dict:
        return self._assembly.get_stack_by_name(stack_name).template

    def test_foundation_stack(self) -> None:
        self.assertIn("Resources", self._template("FoundationStack"))

    def test_data_stack(self) -> None:
        self.assertIn("Resources", self._template("DataStack"))

    def test_authentik_stack(self) -> None:
        self.assertIn("Resources", self._template("AuthentikStack"))

    def test_webfinger_stack(self) -> None:
        self.assertIn("Resources", self._template("WebFingerStack"))

    def test_headscale_stack(self) -> None:
        self.assertIn("Resources", self._template("HeadscaleStack"))

    def test_vaultwarden_stack(self) -> None:
        self.assertIn("Resources", self._template("VaultwardenStack"))

    def test_mail_stack(self) -> None:
        self.assertIn("Resources", self._template("MailStack"))

    def test_site_stack(self) -> None:
        self.assertIn("Resources", self._template("SiteStack"))

    def test_webmail_stack(self) -> None:
        self.assertIn("Resources", self._template("WebmailStack"))

    def test_openclaw_stack(self) -> None:
        self.assertIn("Resources", self._template("OpenClawStack"))


if __name__ == "__main__":
    unittest.main()
