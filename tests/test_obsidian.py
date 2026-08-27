from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.obsidian import ObsidianAdapter


class ObsidianCacheTest(unittest.TestCase):
    def test_search_reuses_unchanged_markdown_content(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"CODEX_TASKBOARD_OBSIDIAN_VAULT": temporary}, clear=False,
        ):
            adapter = ObsidianAdapter(Path(temporary))
            note = adapter.root / "Tasks" / "TASK-1 缓存.md"
            note.parent.mkdir(parents=True)
            note.write_text("# 缓存\n\n复用任务数据。\n", encoding="utf-8")
            self.assertEqual(1, len(adapter.search("缓存")))

            with patch.object(Path, "read_text", side_effect=AssertionError("unchanged note was reread")):
                self.assertEqual(1, len(adapter.search("缓存")))

            note.write_text("# 缓存\n\n复用任务数据与工具状态。\n", encoding="utf-8")
            self.assertIn("工具状态", adapter.search("工具状态")[0]["summary"])


if __name__ == "__main__":
    unittest.main()
