from __future__ import annotations

import importlib.metadata
import sys
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.9.25"
TOMLI_VERSION = "2.4.1"
TOMLI_WHEEL_SHA256 = "0d85819802132122da43cb86656f8d1f8c6587d54ae7dcaf30e90533028b49fe"


class DependencyContractTest(unittest.TestCase):
    def test_locked_test_interpreter_is_exact(self) -> None:
        self.assertEqual(PYTHON_VERSION, (ROOT / ".python-version").read_text().strip())
        self.assertEqual((3, 9, 25), sys.version_info[:3])

    def test_project_declares_python_and_conditional_tomli(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(">=3.9", project["requires-python"])
        self.assertIn(
            "tomli==2.4.1; python_version < '3.11'",
            project["dependencies"],
        )

    def test_tomli_lock_uses_official_pure_python_wheel_hash(self) -> None:
        lock = (ROOT / "requirements.lock").read_text().strip()
        self.assertEqual(
            f"tomli=={TOMLI_VERSION} --hash=sha256:{TOMLI_WHEEL_SHA256}",
            lock,
        )
        self.assertEqual(TOMLI_VERSION, importlib.metadata.version("tomli"))

if __name__ == "__main__":
    unittest.main()
