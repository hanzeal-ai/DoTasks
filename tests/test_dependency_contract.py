from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.14"


class DependencyContractTest(unittest.TestCase):
    def test_locked_test_interpreter_uses_python_314(self) -> None:
        self.assertEqual(PYTHON_VERSION, (ROOT / ".python-version").read_text().strip())
        self.assertEqual((3, 14), sys.version_info[:2])

    def test_project_uses_the_current_python_runtime_without_compatibility_dependencies(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(">=3.14", project["requires-python"])
        self.assertEqual([], project["dependencies"])
        self.assertFalse((ROOT / "requirements.lock").exists())

if __name__ == "__main__":
    unittest.main()
