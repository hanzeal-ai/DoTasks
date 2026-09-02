from __future__ import annotations

import json
import unittest

from core.run_context import group_verification_checks, prompt_context


class RunContextContractTest(unittest.TestCase):
    def test_review_prompt_exposes_only_git_diff_inputs(self):
        compacted = json.loads(prompt_context({
            "stage": "code_review",
            "task": {"id": "TASK-0001", "title": "质量审查", "goal": "不要暴露"},
            "diff_scope": {
                "base_revision": "abc123",
                "changed_files": ["src/App.tsx"],
                "workspace_path": "/tmp/review-worktree",
                "artifact_path": "/tmp/delivery.patch",
                "artifact_sha256": "secret-hash",
            },
            "review_checks": [{"id": "code-quality", "description": "代码质量"}],
        }))

        self.assertEqual(
            {"base_revision", "changed_files", "workspace_path"},
            set(compacted["diff_scope"]),
        )
        self.assertNotIn("goal", compacted["task"])
        self.assertNotIn("artifact_path", json.dumps(compacted))
        self.assertNotIn("artifact_sha256", json.dumps(compacted))

    def test_execution_prompt_has_one_requirement_source(self):
        compacted = json.loads(prompt_context({
            "stage": "execution",
            "task": {
                "id": "TASK-0001",
                "title": "唯一任务说明",
                "goal": "只从自然语言任务说明读取需求",
            },
            "constraints": {"scope": ["唯一范围"]},
            "execution_environment": "worktree",
            "targets": [{
                "file": "src/App.tsx",
                "mode": "modify",
                "symbols": ["App"],
                "reason": "内部定位原因",
                "tasks": [{"symbol": "App", "action": "重复的实现说明"}],
            }],
            "verify": [{"command": "npm test"}],
            "tasks": [{
                "id": "TASK-0001", "title": "重复标题", "goal": "重复目标",
            }],
            "base_revision": "abc123",
        }))

        self.assertEqual(
            {"execution_environment", "targets", "verify", "tasks"},
            set(compacted),
        )
        self.assertEqual(
            {"file": "src/App.tsx", "mode": "modify", "symbols": ["App"]},
            compacted["targets"][0],
        )
        self.assertEqual([{"id": "TASK-0001"}], compacted["tasks"])
        self.assertNotIn("task", compacted)
        self.assertNotIn("constraints", compacted)
        self.assertNotIn("base_revision", compacted)

    def test_compaction_preserves_criterion_location_method_and_required(self):
        compacted = group_verification_checks([
            {
                "criterion": "取消范围可选",
                "file": "web/Booking.tsx",
                "symbol": "CancelScope",
                "method": "manual browser verification",
                "expected": "显示三个范围选项",
                "check_type": "manual_runtime",
                "required": True,
                "artifact_refs": ["artifact://intake/LOC-1/reference.png"],
                "failure_category": "environment",
                "repair_command": "uv sync --frozen",
                "repair_timeout_seconds": 600,
            }
        ])

        self.assertEqual(1, len(compacted))
        criterion = compacted[0]["criteria"][0]
        self.assertEqual("web/Booking.tsx", criterion["file"])
        self.assertEqual("CancelScope", criterion["symbol"])
        self.assertEqual("manual browser verification", criterion["method"])
        self.assertTrue(criterion["required"])
        self.assertEqual(
            ["artifact://intake/LOC-1/reference.png"], criterion["artifact_refs"]
        )
        self.assertEqual("environment", compacted[0]["failure_category"])
        self.assertEqual("uv sync --frozen", compacted[0]["repair_command"])
        self.assertEqual(600, compacted[0]["repair_timeout_seconds"])


if __name__ == "__main__":
    unittest.main()
