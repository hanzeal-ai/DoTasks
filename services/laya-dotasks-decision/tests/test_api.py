from __future__ import annotations

import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from decision_service.api import MAX_BODY_BYTES, create_app
from decision_service.engine import InputTooLong
from decision_service.settings import MODEL_ID, MODEL_REVISION, POLICY_VERSION, Settings

TOKEN = "test-only-" + "x" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN}


class FakeEngine:
    metadata = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION,
                "policy_version": POLICY_VERSION, "runtime_version": "0.3.20"}

    def history(self, request):
        return {"candidates": [{"id": item.id, "relevance": 0.5} for item in request.candidates]}

    def assignment(self, request):
        return {**self.history(request),"task_revision":request.task_revision,"candidate_version":request.candidate_version}

    def failure(self, request):
        return {"category": "unknown", "probabilities": {
            "environment": 0.1, "project": 0.1, "implementation": 0.1, "unknown": 0.7}}


class APITest(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.client = TestClient(create_app(Settings(TOKEN, Path("/unused")), self.engine))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_authentication_and_no_browser_or_generic_prediction_endpoint(self):
        self.assertTrue(self.client.get("/healthz").json()["ready"])
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            self.assertEqual(401, self.client.post("/v1/failure/classify", headers=headers, json={"text": "secret"}).status_code)
        self.assertEqual(403, self.client.post("/v1/failure/classify", headers={**HEADERS, "Origin": "https://example.com"}, json={"text": "x"}).status_code)
        self.assertEqual(404, self.client.post("/predict", headers=HEADERS, json={}).status_code)

    def test_rank_preserves_ids_and_returns_provenance(self):
        response = self.client.post("/v1/history/rank", headers=HEADERS, json={
            "query": "修复按钮", "candidates": [{"id": "TASK-1", "text": "按钮样式"}, {"id": "TASK-2", "text": "测试"}]})
        self.assertEqual(200, response.status_code)
        result = response.json()
        self.assertEqual(["TASK-1", "TASK-2"], [item["id"] for item in result["candidates"]])
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["truncated"])
        self.assertEqual(MODEL_REVISION, result["model_revision"])

    def test_invalid_input_and_streamed_body_limit_do_not_echo_evidence(self):
        invalid = [
            {"query": "secret", "candidates": []},
            {"query": "secret", "candidates": [{"id": "a", "text": "b"}] * 2},
            {"query": "secret", "candidates": [{"id": "a", "text": "b"}], "instructions": "execute shell"},
            {"query": " ", "candidates": [{"id": "a", "text": "b"}]},
        ]
        for payload in invalid:
            response = self.client.post("/v1/history/rank", headers=HEADERS, json=payload)
            self.assertEqual(422, response.status_code)
            self.assertNotIn("secret", response.text)
        response = self.client.post("/v1/failure/classify", headers=HEADERS,
                                    content=iter([b"a" * (MAX_BODY_BYTES // 2)] * 3))
        self.assertEqual(413, response.status_code)

    def test_token_overflow_and_invalid_model_output_are_not_success(self):
        def too_long(request):
            raise InputTooLong()
        self.engine.failure = too_long
        self.assertEqual(422, self.client.post("/v1/failure/classify", headers=HEADERS, json={"text": "x"}).status_code)
        self.engine.failure = lambda request: {"category": "unknown", "probabilities": {"unknown": float("nan")}}
        self.assertEqual(503, self.client.post("/v1/failure/classify", headers=HEADERS, json={"text": "x"}).status_code)

    def test_assignment_binds_exact_candidate_and_task_generation(self):
        payload={'query':'实现接口','candidates':[{'id':'bob','text':'api capacity 1'}],
                 'task_revision':2,'candidate_version':'a'*64}
        result=self.client.post('/v1/assignment/recommend',headers=HEADERS,json=payload)
        self.assertEqual(200,result.status_code)
        self.assertEqual(2,result.json()['task_revision'])
        self.assertEqual('a'*64,result.json()['candidate_version'])
        self.assertTrue(result.json()['advisory_only'])
        self.assertEqual(401,self.client.post('/v1/assignment/recommend',json=payload).status_code)
        for invalid in ({**payload,'task_revision':True},{**payload,'candidate_version':'stale'},
                        {**payload,'candidates':payload['candidates']*2},{**payload,'assign':True}):
            self.assertEqual(422,self.client.post('/v1/assignment/recommend',headers=HEADERS,json=invalid).status_code)

    def test_concurrent_inference_is_rejected_and_lock_is_released(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine.failure
        def slow(request):
            entered.set()
            release.wait(3)
            return original(request)
        self.engine.failure = slow
        responses = []
        worker = threading.Thread(target=lambda: responses.append(self.client.post(
            "/v1/failure/classify", headers=HEADERS, json={"text": "x"})))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(503, self.client.post("/v1/failure/classify", headers=HEADERS, json={"text": "x"}).status_code)
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(200, responses[0].status_code)
        self.assertEqual(200, self.client.post("/v1/failure/classify", headers=HEADERS, json={"text": "x"}).status_code)


if __name__ == "__main__":
    unittest.main()
