"""Optional, bounded Laya advice. This adapter never mutates workflow state."""
from __future__ import annotations

import ipaddress
import http.client
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .http_client import NoRedirect


LOG = logging.getLogger(__name__)
CATEGORIES = {"environment", "project", "implementation", "unknown"}


def probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Invalid decision probability")
    return float(value)


class DecisionClient:
    def __init__(self, data_home: Path):
        self.mode, self.url, self.token, self.timeout = "off", "", "", 5.0
        self.configuration_error = False
        try:
            path = data_home / "decision-service.json"
            config = json.loads(path.read_text()) if path.is_file() else {}
            self.mode = os.environ.get("DOTASKS_DECISION_MODE", config.get("mode", "off"))
            if self.mode not in {"off", "shadow", "rank"}:
                raise ValueError("Unknown decision mode")
            if self.mode == "off":
                return
            self.url = os.environ.get("DOTASKS_DECISION_URL", config.get("url", "")).rstrip("/")
            parsed = urlsplit(self.url)
            host = parsed.hostname or ""
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path
                    or (parsed.scheme == "http" and not loopback)):
                raise ValueError("Decision URL must be loopback HTTP or HTTPS without credentials/path")
            token_path = os.environ.get("DOTASKS_DECISION_TOKEN_FILE", config.get("token_file", ""))
            self.token = Path(token_path).expanduser().read_text().strip()
            if len(self.token) < 32 or any(char.isspace() for char in self.token):
                raise ValueError("Invalid decision token")
            self.timeout = float(os.environ.get("DOTASKS_DECISION_TIMEOUT", config.get("timeout_seconds", 5)))
            if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 30:
                raise ValueError("Decision timeout must be between 0.1 and 30 seconds")
        except (OSError, ValueError, TypeError, AttributeError):
            self.mode, self.configuration_error = "off", True
            LOG.warning("laya decision configuration invalid; existing workflow retained")

    def _request(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(self.url + path, method="POST",
                                        data=json.dumps(payload, ensure_ascii=False).encode(),
                                        headers={"Authorization": "Bearer " + self.token,
                                                 "Content-Type": "application/json"})
        # Never forward the bearer token to a redirect or an implicit proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=self.timeout) as response:
            raw = response.read(96 * 1024 + 1)
        if len(raw) > 96 * 1024:
            raise ValueError("Decision response too large")
        result = json.loads(raw)
        if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
                or result["schema_version"] != 1 or result.get("advisory_only") is not True
                or result.get("truncated") is not False):
            raise ValueError("Unsupported decision response")
        for name in ("request_id", "policy_version", "model_id", "model_revision", "runtime_version"):
            if not isinstance(result.get(name), str) or not 1 <= len(result[name]) <= 200:
                raise ValueError("Missing decision provenance")
        return result

    def _call(self, path: str, payload: dict, validate) -> dict | None:
        if self.mode == "off":
            return None
        started = time.monotonic()
        try:
            result = self._request(path, payload)
            validate(result)
            LOG.info("laya decision %s", json.dumps({
                "operation": path, "mode": self.mode, "status": "ok",
                "request_id": result["request_id"], "model_revision": result["model_revision"],
                "policy_version": result["policy_version"],
                "latency_ms": int((time.monotonic() - started) * 1000),
            }))
            return result
        except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError):
            LOG.warning("laya decision unavailable or invalid for %s; existing workflow retained", path)
            return None

    def rank_history(self, query: str, candidates: list[dict]) -> list[dict]:
        if self.mode == "off" or not candidates:
            return candidates
        # Never trim evidence or drop candidates to satisfy the model's limits.
        if len(query) > 2000 or len(candidates) > 8:
            LOG.info("laya history skipped: input exceeds decision limits")
            return candidates
        items = [{"id": str(item["task_id"]), "text": str(item.get("summary") or "")}
                 for item in candidates]
        if any(not item["text"].strip() or len(item["text"]) > 2000 for item in items):
            return candidates
        ids = [item["id"] for item in items]

        def validate(result):
            if result.get("score_kind") != "ordinal_0_1":
                raise ValueError("Unsupported relevance score semantics")
            ranked = result["candidates"]
            if not isinstance(ranked, list) or len(ranked) != len(ids):
                raise ValueError("Incomplete candidate ranking")
            if sorted(item["id"] for item in ranked) != sorted(ids):
                raise ValueError("Candidate ids changed")
            for item in ranked:
                probability(item["relevance"])

        result = self._call("/v1/history/rank", {"query": query, "candidates": items}, validate)
        if result is None:
            return candidates
        scores = {item["id"]: item["relevance"] for item in result["candidates"]}
        enriched = [{**item, "decision_advice": {
            "relevance": scores[item["task_id"]], "score_kind": result["score_kind"], "mode": self.mode,
            "request_id": result["request_id"], "policy_version": result["policy_version"],
            "model_revision": result["model_revision"],
        }} for item in candidates]
        return sorted(enriched, key=lambda item: -scores[item["task_id"]]) if self.mode == "rank" else enriched

    def classify_failure(self, text: str) -> dict | None:
        if len(text) > 6000 or not text.strip():
            return None

        def validate(result):
            values = result["probabilities"]
            if result["category"] not in CATEGORIES or not isinstance(values, dict) or set(values) != CATEGORIES:
                raise ValueError("Invalid failure categories")
            if abs(sum(probability(value) for value in values.values()) - 1) > 0.002:
                raise ValueError("Invalid failure probability distribution")
            if values[result["category"]] < max(values.values()):
                raise ValueError("Failure category contradicts probabilities")

        return self._call("/v1/failure/classify", {"text": text}, validate)

    def recommend_assignment(self, query: str, revision: int, candidate_version: str, candidates: list[dict]) -> dict | None:
        if not candidates or len(candidates) > 8 or len(query) > 2000:
            return None
        items = [{'id':c['id'], 'text':json.dumps({k:c[k] for k in ('modules','active','capacity')}, ensure_ascii=False)} for c in candidates]
        if any(len(c['text']) > 2000 for c in items):
            return None
        ids = sorted(c['id'] for c in items)
        def validate(result):
            if result.get('task_revision') != revision or result.get('candidate_version') != candidate_version:
                raise ValueError('Stale assignment advice')
            if result.get('score_kind') != 'ordinal_0_1' or sorted(c['id'] for c in result['candidates']) != ids:
                raise ValueError('Invalid assignment candidate set')
            for candidate in result['candidates']:
                probability(candidate['relevance'])
        return self._call('/v1/assignment/recommend', {'query':query,'task_revision':revision,
            'candidate_version':candidate_version,'candidates':items},validate)
