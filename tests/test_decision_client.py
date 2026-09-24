from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from taskboard.decision_client import DecisionClient
from taskboard.obsidian import ObsidianAdapter


class DecisionClientTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "token").write_text("t" * 48)
        self.env = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                          if not key.startswith("DOTASKS_DECISION_")}, clear=True)
        self.env.start()
        self.response = {
            "schema_version": 1, "policy_version": "v1", "model_id": "laya",
            "model_revision": "pinned", "runtime_version": "0.3.20", "request_id": "req",
            "advisory_only": True, "truncated": False,
            "score_kind": "ordinal_0_1",
            "candidates": [{"id": "TASK-2", "relevance": 0.9}, {"id": "TASK-1", "relevance": 0.1}],
        }
        self.requests = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                parent.requests.append((self.path, self.headers.get("Authorization"),
                                        json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(parent.response).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.candidates = [{"task_id": "TASK-1", "summary": "缓存逻辑", "score": 20},
                           {"task_id": "TASK-2", "summary": "缓存缺陷", "score": 10}]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.env.stop()
        self.temp.cleanup()

    def client(self, mode="rank"):
        (self.home / "decision-service.json").write_text(json.dumps({
            "mode": mode, "url": f"http://127.0.0.1:{self.server.server_port}",
            "token_file": str(self.home / "token"), "timeout_seconds": 1,
        }))
        return DecisionClient(self.home)

    def test_real_http_ranking_preserves_membership_and_original_records(self):
        original = copy.deepcopy(self.candidates)
        result = self.client().rank_history("修复缓存", self.candidates)
        self.assertEqual(["TASK-2", "TASK-1"], [item["task_id"] for item in result])
        self.assertEqual([10, 20], [item["score"] for item in result])
        self.assertEqual(original, self.candidates)
        self.assertEqual("Bearer " + "t" * 48, self.requests[0][1])

    def test_shadow_retains_order_and_off_sends_no_request(self):
        result = self.client("shadow").rank_history("缓存", self.candidates)
        self.assertEqual(["TASK-1", "TASK-2"], [item["task_id"] for item in result])
        self.client("off").rank_history("缓存", self.candidates)
        self.assertEqual(1, len(self.requests))

    def test_invalid_missing_duplicate_or_nonfinite_scores_keep_original(self):
        client = self.client()
        invalid = [[], [{"id": "TASK-1", "relevance": 1}] * 2,
                   [{"id": "TASK-1", "relevance": float("nan")}, {"id": "TASK-2", "relevance": 0.1}],
                   [{"id": "OTHER", "relevance": 1}, {"id": "TASK-2", "relevance": 0.1}]]
        for values in invalid:
            self.response["candidates"] = values
            self.assertIs(self.candidates, client.rank_history("缓存", self.candidates))
        with patch.object(client, "_request", side_effect=TimeoutError):
            self.assertIs(self.candidates, client.rank_history("缓存", self.candidates))

    def test_invalid_config_disables_advice_and_long_input_does_not_truncate(self):
        client = self.client()
        self.assertIs(self.candidates, client.rank_history("a" * 2001, self.candidates))
        self.assertFalse(self.requests)
        with patch.dict(os.environ, {"DOTASKS_DECISION_URL": "http://public.example.com"}):
            self.assertTrue(DecisionClient(self.home).configuration_error)
        with patch.dict(os.environ, {"DOTASKS_DECISION_TIMEOUT": "nan"}):
            self.assertEqual("off", DecisionClient(self.home).mode)

    def test_failure_requires_complete_consistent_probabilities(self):
        client = self.client()
        self.response.update(category="environment", probabilities={
            "environment": 0.7, "implementation": 0.1, "project": 0.1, "unknown": 0.1})
        self.assertEqual("environment", client.classify_failure("日志")["category"])
        self.response["probabilities"]["environment"] = 0.2
        self.assertIsNone(client.classify_failure("日志"))

    def test_assignment_invalid_generation_membership_and_timeout_are_advisory_only(self):
        client=self.client()
        candidates=[{'id':'bob','modules':['api'],'active':0,'capacity':1}]
        self.response.update(task_revision=2,candidate_version='a'*64,candidates=[{'id':'bob','relevance':.5}])
        self.assertIsNotNone(client.recommend_assignment('接口',2,'a'*64,candidates))
        for updates in ({'task_revision':1},{'candidate_version':'b'*64},{'candidates':[{'id':'mallory','relevance':1}]}):
            original=copy.deepcopy(self.response);self.response.update(updates)
            self.assertIsNone(client.recommend_assignment('接口',2,'a'*64,candidates))
            self.response=original
        with patch.object(client,'_request',side_effect=TimeoutError):
            self.assertIsNone(client.recommend_assignment('接口',2,'a'*64,candidates))

    def test_dependency_search_keeps_ancestors_and_other_projects_out(self):
        adapter = ObsidianAdapter(self.home, decision_client=self.client())
        project = str(self.home / "project")
        root = adapter.project_root(project) / "Tasks"
        root.mkdir(parents=True)
        for task_id, text in [("TASK-1", "缓存 缓存 缓存"), ("TASK-2", "缓存"), ("TASK-0", "早期设计")]:
            (root / f"{task_id}.md").write_text(f"project: {project}\n{text}\n" + (
                "- [[TASK-0]] --continues_from--> [[TASK-2]]\n" if task_id == "TASK-2" else ""))
        other = adapter.project_root(str(self.home / "other")) / "Tasks"
        other.mkdir(parents=True)
        (other / "TASK-9.md").write_text("缓存 缓存 缓存")
        result = adapter.search_task_dependencies("缓存", "缓存", project=project)
        self.assertEqual(["TASK-2", "TASK-1", "TASK-0"], [item["task_id"] for item in result])
        self.assertTrue(result[-1]["lineage_expansion"])
        self.assertEqual(["TASK-1", "TASK-2"], [item["id"] for item in self.requests[0][2]["candidates"]])


if __name__ == "__main__":
    unittest.main()
