from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish-cloud-image"


class PublishCloudImageScriptTest(unittest.TestCase):
    def test_builds_versioned_linux_amd64_image_and_pushes_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DoTasksImagePublish") as temporary:
            temp = Path(temporary)
            log = temp / "docker.log"
            docker = temp / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOTASKS_TEST_DOCKER_LOG\"\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "DOTASKS_TEST_DOCKER_LOG": str(log),
                    "PATH": f"{temp}:/usr/bin:/bin",
                }
            )

            result = subprocess.run(
                [
                    "/bin/sh",
                    str(SCRIPT),
                    "--image",
                    "registry.example.com/team/dotasks:2026.09.03",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            calls = log.read_text(encoding="utf-8")
            self.assertIn("buildx version", calls)
            self.assertIn("buildx build --platform linux/amd64", calls)
            self.assertIn("--tag registry.example.com/team/dotasks:2026.09.03", calls)
            self.assertIn("--push", calls)
            self.assertIn("Published: registry.example.com/team/dotasks:2026.09.03", result.stdout)

    def test_requires_explicit_image_version(self) -> None:
        result = subprocess.run(
            [
                "/bin/sh",
                str(SCRIPT),
                "--image",
                "registry.example.com/team/dotasks",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit version tag", result.stderr)


if __name__ == "__main__":
    unittest.main()
