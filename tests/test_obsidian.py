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
            os.environ, {"DOTASKS_OBSIDIAN_VAULT": temporary}, clear=False,
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

    def test_task_projection_is_project_scoped_and_search_does_not_cross_projects(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"DOTASKS_OBSIDIAN_VAULT": temporary}, clear=False,
        ):
            adapter = ObsidianAdapter(Path(temporary))
            first_project = str(Path(temporary) / "one" / "same-name")
            second_project = str(Path(temporary) / "two" / "same-name")
            base = {
                "requirement_id": "", "status": "done", "priority": "P2",
                "codex_thread_id": "", "modules": [], "scope": [],
                "acceptance_criteria": [],
                "dependency_analysis": {
                    "decision": "independent", "depends_tasks": [],
                    "conflicts_tasks": [], "history_tasks": [],
                },
                "implementation_contract": {"targets": []},
            }
            first_path = Path(adapter.sync_task({
                **base, "id": "TASK-0001", "title": "共享关键词一",
                "goal": "共享关键词 alpha", "project": first_project,
            }, []))
            second_path = Path(adapter.sync_task({
                **base, "id": "TASK-0002", "title": "共享关键词二",
                "goal": "共享关键词 beta", "project": second_project,
            }, []))

            self.assertNotEqual(first_path.parent.parent, second_path.parent.parent)
            first_results = adapter.search_task_dependencies(
                "共享关键词", "alpha", project=first_project,
            )
            self.assertEqual(["TASK-0001"], [item["task_id"] for item in first_results])

            continuation_path = Path(adapter.sync_task({
                **base, "id": "TASK-0003", "title": "共享关键词延续",
                "goal": "共享关键词 alpha 延续", "project": first_project,
            }, [{
                "source_task_id": "TASK-0003", "target_task_id": "TASK-0001",
                "relation_type": "continues_from",
            }]))
            self.assertTrue(continuation_path.is_file())
            continuation = next(
                item for item in adapter.search_task_dependencies(
                    "共享关键词", "延续", project=first_project,
                ) if item["task_id"] == "TASK-0003"
            )
            self.assertEqual(
                [{"from": "TASK-0001", "to": "TASK-0003", "type": "continues_from"}],
                continuation["history_edges"],
            )

    def test_dependency_search_expands_the_full_matched_ancestor_chain(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"DOTASKS_OBSIDIAN_VAULT": temporary}, clear=False,
        ):
            adapter = ObsidianAdapter(Path(temporary))
            project = str(Path(temporary) / "project")
            base = {
                "requirement_id": "", "status": "done", "priority": "P2",
                "codex_thread_id": "", "modules": [], "scope": [],
                "acceptance_criteria": [],
                "dependency_analysis": {
                    "decision": "independent", "depends_tasks": [],
                    "conflicts_tasks": [], "history_tasks": [], "history_edges": [],
                },
                "implementation_contract": {"targets": []},
            }
            adapter.sync_task({
                **base, "id": "TASK-0101", "title": "历史起点",
                "goal": "建立基础能力", "project": project,
            }, [])
            adapter.sync_task({
                **base, "id": "TASK-0102", "title": "历史中段",
                "goal": "扩展基础能力", "project": project,
            }, [{
                "source_task_id": "TASK-0102", "target_task_id": "TASK-0101",
                "relation_type": "continues_from",
            }])
            adapter.sync_task({
                **base, "id": "TASK-0103", "title": "终点火花",
                "goal": "交付最终能力", "project": project,
            }, [{
                "source_task_id": "TASK-0103", "target_task_id": "TASK-0102",
                "relation_type": "continues_from",
            }])

            results = adapter.search_task_dependencies(
                "终点火花", "", project=project,
            )

            self.assertEqual(
                {"TASK-0101", "TASK-0102", "TASK-0103"},
                {item["task_id"] for item in results},
            )
            self.assertTrue(next(
                item for item in results if item["task_id"] == "TASK-0101"
            )["lineage_expansion"])


if __name__ == "__main__":
    unittest.main()
