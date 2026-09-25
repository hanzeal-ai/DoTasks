from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taskboard.project_guard import ProjectWorkspaceGuard


class ProjectWorkspaceGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        self.file = self.home / "file.txt"
        self.file.write_text("not a directory", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_accepts_existing_absolute_directory(self) -> None:
        self.assertEqual(
            str(self.project),
            ProjectWorkspaceGuard.require_project_directory(self.project),
        )

    def test_rejects_relative_missing_and_non_directory_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            ProjectWorkspaceGuard.require_project_directory("relative/project")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ProjectWorkspaceGuard.require_project_directory(self.home / "missing")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            ProjectWorkspaceGuard.require_project_directory(self.file)


