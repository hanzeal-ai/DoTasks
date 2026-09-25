"""Shared JSON transport and redirect policy for authenticated HTTP clients."""
from __future__ import annotations

import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(url: str, payload: dict | None = None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("服务返回了无效响应")
    return result
