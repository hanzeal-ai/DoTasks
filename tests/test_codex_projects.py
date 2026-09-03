from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taskboard.codex_projects import discover_codex_projects, sanitize_codex_projects


class CodexProjectsTest(unittest.TestCase):
    def test_discovers_existing_local_projects_in_codex_sidebar_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            first = home / "first"
            second = home / "second"
            first.mkdir()
            second.mkdir()
            (home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "local-projects": {
                            "project-a": {
                                "id": "project-a",
                                "name": "First",
                                "rootPaths": [str(first)],
                            },
                            "project-b": {
                                "id": "project-b",
                                "name": "Second",
                                "rootPaths": [str(second), str(home / "missing")],
                            },
                        },
                        "project-order": ["project-b", "project-a"],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                [
                    {"id": "project-b:0", "name": "Second", "path": str(second.resolve())},
                    {"id": "project-a", "name": "First", "path": str(first.resolve())},
                ],
                discover_codex_projects(home),
            )

    def test_missing_or_invalid_codex_state_returns_no_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual([], discover_codex_projects(home))
            (home / ".codex-global-state.json").write_text("not json", encoding="utf-8")
            self.assertEqual([], discover_codex_projects(home))

    def test_sanitizer_rejects_relative_duplicate_and_malformed_records(self) -> None:
        self.assertEqual(
            [{"id": "one", "name": "Project", "path": "/Users/example/project"}],
            sanitize_codex_projects(
                [
                    {"id": "one", "name": "Project", "path": "/Users/example/project"},
                    {"id": "two", "name": "Duplicate", "path": "/Users/example/project"},
                    {"id": "three", "name": "Relative", "path": "relative/project"},
                    "invalid",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
