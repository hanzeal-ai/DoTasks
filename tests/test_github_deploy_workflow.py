from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GithubDeployWorkflowTest(unittest.TestCase):
    def test_main_push_publishes_immutable_ghcr_image_then_deploys(self) -> None:
        source = (ROOT / ".github" / "workflows" / "deploy-cloud.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("branches:\n      - main", source)
        self.assertIn("packages: write", source)
        self.assertIn('image_name="ghcr.io/${GITHUB_REPOSITORY,,}"', source)
        self.assertIn('echo "tag=$image_name:$GITHUB_SHA"', source)
        self.assertIn("platforms: linux/amd64", source)
        self.assertIn("push: true", source)
        self.assertIn("./scripts/test", source)
        self.assertIn("needs: build", source)
        self.assertIn("- self-hosted", source)
        self.assertIn("- Linux", source)
        self.assertIn("- X64", source)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", source)
        self.assertIn("actions/download-artifact@018cc2cf5baa6db3ef3c5f8a56943fffe632ef53", source)
        self.assertIn("reuse_env=(--reuse-env)", source)
        self.assertIn('"${reuse_env[@]}"', source)

    def test_deployment_needs_no_inbound_ssh_or_long_lived_ghcr_token(self) -> None:
        source = (ROOT / ".github" / "workflows" / "deploy-cloud.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("ECS_KNOWN_HOSTS", source)
        self.assertNotIn("ECS_SSH_PRIVATE_KEY", source)
        self.assertNotIn("ECS_HOST", source)
        self.assertNotIn("scp ", source)
        self.assertNotIn("ssh ", source)
        self.assertNotIn("StrictHostKeyChecking=no", source)
        self.assertIn("Log in to GHCR for deployment", source)
        self.assertIn("registry: ghcr.io", source)
        self.assertNotIn("GHCR_READ_TOKEN", source)
        self.assertNotIn("uses: actions/checkout@v", source)
        self.assertNotIn("uses: docker/login-action@v", source)


if __name__ == "__main__":
    unittest.main()
