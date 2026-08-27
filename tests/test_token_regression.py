from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taskboard.token_regression import analyze_rollouts, evaluate_report


class TokenRegressionHarnessTest(unittest.TestCase):
    def test_rollout_analysis_detects_refetch_search_and_lifecycle_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rollout = Path(temporary) / "rollout.jsonl"
            items = [
                {"type": "response_item", "payload": {"type": "function_call", "name": "mcp__codex_taskboard__get_task_context", "arguments": "{}"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"find ~/.codex/skills\"}"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "mcp__codex_taskboard__submit_task_delivery", "arguments": "{}"}},
                {"type": "response_item", "payload": {"type": "mcp_tool_call", "server": "codex-taskboard", "tool": "review_code", "arguments": "{}"}},
            ]
            rollout.write_text("\n".join(json.dumps(item) for item in items), encoding="utf-8")

            result = analyze_rollouts([rollout])

        self.assertEqual(1, len(result["context_reads"]))
        self.assertEqual(1, len(result["tool_directory_searches"]))
        self.assertEqual(2, len(result["lifecycle_calls"]))

    def test_gate_evaluation_keeps_quality_and_token_targets_explicit(self) -> None:
        report = {
            "status": "done", "quality_preserved": True,
            "raw_token_used": 30_000, "effective_token_used": 15_000,
            "observations": {
                "tool_directory_searches": [], "context_reads": [],
                "lifecycle_calls": [{}, {}],
            },
        }
        self.assertTrue(all(evaluate_report(report).values()))


if __name__ == "__main__":
    unittest.main()
