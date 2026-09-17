from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-cloud-ip"


class DeployCloudIpScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="DoTasksCloudDeploy")
        self.project = Path(self.temp.name) / "project"
        self.bin_dir = Path(self.temp.name) / "bin"
        self.project.mkdir()
        self.bin_dir.mkdir()
        self.log = Path(self.temp.name) / "docker.log"
        self._executable(
            "docker",
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOTASKS_TEST_DOCKER_LOG\"\nexit 0\n",
        )
        self._executable("curl", "#!/bin/sh\nprintf '{\"ok\": false, \"mode\": \"cloud_relay\"}\\n'\n")
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "DOTASKS_DEPLOY_ROOT": str(self.project),
                "DOTASKS_TEST_DOCKER_LOG": str(self.log),
                "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _executable(self, name: str, source: str) -> None:
        path = self.bin_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, *extra: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/sh",
                str(SCRIPT),
                "--public-ip",
                "203.0.113.10",
                "--image",
                "registry.example.com/team/dotasks:2026.09.03",
                "--http-password",
                "browser-password-123456",
                "--agent-token",
                "agent-token-12345678901234567890",
                *extra,
            ],
            cwd=self.project,
            env=self.environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_creates_private_env_builds_and_checks_health(self) -> None:
        result = self.run_script()

        env_file = self.project / ".env"
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(
            env_file.read_text(encoding="utf-8"),
            "DOTASKS_IMAGE=registry.example.com/team/dotasks:2026.09.03\n"
            "DOTASKS_PUBLIC_URL=http://203.0.113.10:8765\n"
            "DOTASKS_HTTP_USER=dotasks\n"
            "DOTASKS_HTTP_PASSWORD=browser-password-123456\n"
            "DOTASKS_AGENT_ID=default\n"
            "DOTASKS_AGENT_TOKEN=agent-token-12345678901234567890\n"
            "DOTASKS_BIND_ADDRESS=0.0.0.0\n"
            "DOTASKS_BIND_PORT=8765\n",
        )
        docker_calls = self.log.read_text(encoding="utf-8")
        self.assertIn("compose version", docker_calls)
        self.assertIn("config", docker_calls)
        self.assertIn("pull", docker_calls)
        self.assertIn("up -d --remove-orphans", docker_calls)
        self.assertNotIn("--build", docker_calls)
        compose = self.project / ".dotasks-cloud" / "compose.yaml"
        self.assertIn("image: ${DOTASKS_IMAGE:?set DOTASKS_IMAGE}", compose.read_text())
        self.assertIn("DoTasks Cloud is running: http://203.0.113.10:8765", result.stdout)
        self.assertIn("allow inbound TCP 8765", result.stdout)

    def test_accepts_eight_character_password_and_reuses_it(self) -> None:
        self.run_script("--http-password", "pass1234")
        self.assertIn(
            "DOTASKS_HTTP_PASSWORD=pass1234\n",
            (self.project / ".env").read_text(encoding="utf-8"),
        )
        self.run_script("--reuse-env")

    def test_rejects_password_shorter_than_eight_characters(self) -> None:
        result = self.run_script("--http-password", "pass123", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOTASKS_HTTP_PASSWORD must contain at least 8 characters", result.stderr)
        self.assertFalse((self.project / ".env").exists())

    def test_https_url_binds_backend_to_loopback_for_reverse_proxy(self) -> None:
        result = subprocess.run(
            [
                "/bin/sh",
                str(SCRIPT),
                "--public-url",
                "https://tasks.example.com",
                "--image",
                "registry.example.com/team/dotasks:2026.09.03",
                "--http-password",
                "browser-password-123456",
                "--agent-token",
                "agent-token-12345678901234567890",
            ],
            cwd=self.project,
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
        )

        environment = (self.project / ".env").read_text(encoding="utf-8")
        self.assertIn("DOTASKS_PUBLIC_URL=https://tasks.example.com", environment)
        self.assertIn("DOTASKS_BIND_ADDRESS=127.0.0.1", environment)
        self.assertIn("allow inbound TCP 80 and 443", result.stdout)
        self.assertIn("with WebSocket support", result.stdout)

    def test_requires_explicit_choice_for_existing_env(self) -> None:
        self.run_script()

        result = self.run_script(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--reuse-env", result.stderr)

        reused = self.run_script("--reuse-env")
        self.assertIn("Reused credentials", reused.stdout)

    def test_reuse_can_update_only_the_image(self) -> None:
        self.run_script()

        result = self.run_script(
            "--reuse-env",
            "--image",
            "registry.example.com/team/dotasks:2026.09.04",
        )

        environment = (self.project / ".env").read_text(encoding="utf-8")
        self.assertIn("DOTASKS_IMAGE=registry.example.com/team/dotasks:2026.09.04", environment)
        self.assertIn("DOTASKS_HTTP_PASSWORD=browser-password-123456", environment)
        self.assertIn("DOTASKS_AGENT_TOKEN=agent-token-12345678901234567890", environment)
        self.assertIn("Image: registry.example.com/team/dotasks:2026.09.04", result.stdout)

    def test_https_image_update_preserves_existing_listener(self) -> None:
        self.run_script()
        env_file = self.project / ".env"
        previous = env_file.read_text().replace("DOTASKS_BIND_ADDRESS=0.0.0.0", "DOTASKS_BIND_ADDRESS=172.17.0.1")
        # Keep an existing private listener used by a containerized reverse proxy.
        lines = previous.splitlines()
        previous = "\n".join("DOTASKS_PUBLIC_URL=https://tasks.example.com" if line.startswith("DOTASKS_PUBLIC_URL=") else line for line in lines) + "\n"
        env_file.write_text(previous)
        result = subprocess.run([
            "/bin/sh", str(SCRIPT), "--public-url", "https://tasks.example.com", "--reuse-env",
            "--image", "registry.example.com/team/dotasks:new", "--no-print-secrets",
        ], cwd=self.project, env=self.environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        current = env_file.read_text()
        self.assertIn("DOTASKS_BIND_ADDRESS=172.17.0.1", current)
        self.assertIn("DOTASKS_HTTP_PASSWORD=browser-password-123456", current)
        self.assertIn("DOTASKS_AGENT_TOKEN=agent-token-12345678901234567890", current)
        self.assertIn("DOTASKS_IMAGE=registry.example.com/team/dotasks:new", current)

    def test_ci_mode_keeps_credentials_out_of_output(self) -> None:
        result = self.run_script("--no-print-secrets")

        self.assertNotIn("browser-password-123456", result.stdout)
        self.assertNotIn("agent-token-12345678901234567890", result.stdout)
        self.assertIn("Credentials remain only in", result.stdout)

    def test_rejects_mismatched_reused_env(self) -> None:
        (self.project / ".env").write_text(
            "DOTASKS_IMAGE=registry.example.com/team/dotasks:2026.09.03\n"
            "DOTASKS_PUBLIC_URL=http://198.51.100.20:8765\n"
            "DOTASKS_HTTP_USER=dotasks\n"
            "DOTASKS_HTTP_PASSWORD=browser-password-123456\n"
            "DOTASKS_AGENT_ID=default\n"
            "DOTASKS_AGENT_TOKEN=agent-token-12345678901234567890\n"
            "DOTASKS_BIND_ADDRESS=0.0.0.0\n"
            "DOTASKS_BIND_PORT=8765\n",
            encoding="utf-8",
        )

        result = self.run_script("--reuse-env", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_never_removes_the_compose_volume(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("down -v", source)


if __name__ == "__main__":
    unittest.main()
