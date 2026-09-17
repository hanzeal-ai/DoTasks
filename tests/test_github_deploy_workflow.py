from __future__ import annotations

import os
import subprocess
import tempfile
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
        self.assertIn(
            "CONFIGURED_PUBLIC_URL: ${{ secrets.DOTASKS_PUBLIC_URL }}", source
        )
        self.assertIn('if [[ -n "$CONFIGURED_PUBLIC_URL" ]]', source)
        self.assertNotIn(
            "\n          DOTASKS_PUBLIC_URL: ${{ secrets.DOTASKS_PUBLIC_URL }}",
            source,
        )

    def test_configuration_repair_is_bounded_and_preserves_secrets(self):
        source = (ROOT / ".github/workflows/deploy-cloud.yml").read_text()
        repair = source.split("          runner_uid=$(id -u)", 1)[1].split("          install -m 755", 1)[0]
        repair = "runner_uid=$(id -u)" + repair
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            (commands / "stat").write_text(
                '#!/bin/sh\ncase "$3" in .env) echo "$TEST_CONFIG_OWNER";; *) id -u;; esac\n'
            )
            (commands / "sudo").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SUDO_LOG"\n')
            for command in commands.iterdir():
                command.chmod(0o755)
            credential = root / ".env"
            credential.write_text("existing-secret-unchanged")
            log = root / "sudo.log"
            environment = {**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"],
                           "deploy_root": str(root), "TEST_SUDO_LOG": str(log), "TEST_CONFIG_OWNER": "0"}
            run = lambda: subprocess.run(["bash", "-eu", "-c", repair], cwd=root, env=environment,
                                         capture_output=True, text=True)
            result = run()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("existing-secret-unchanged", credential.read_text())
            self.assertEqual(0o600, credential.stat().st_mode & 0o777)
            if os.getuid() != 0:
                self.assertEqual(f"-n chown {os.getuid()}:{os.getgid()} -- {credential}\n", log.read_text())
            environment["TEST_CONFIG_OWNER"] = "999999"
            result = run()
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Unexpected deployment configuration owner", result.stderr)
            credential.unlink()
            target = root / "untouched"
            target.write_text("untouched")
            target.chmod(0o644)
            credential.symlink_to(target)
            result = run()
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Refusing symlink", result.stderr)
            self.assertEqual(0o644, target.stat().st_mode & 0o777)
            credential.unlink()
            credential.mkdir()
            result = run()
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Unexpected deployment configuration type", result.stderr)

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
