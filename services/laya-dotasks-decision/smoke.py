"""Real HTTP deployment check; tiny diagnostic cases, not a quality benchmark."""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Smoke endpoint must not redirect credentials")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8791")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    endpoint = urlsplit(args.url)
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise SystemExit("Use HTTPS or loopback HTTP")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}:
        raise SystemExit("Use an origin URL without credentials, query or path")
    token = args.token_file.expanduser().read_text().strip()
    results, timings = [], []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(path, payload):
        started = time.monotonic()
        request = urllib.request.Request(args.url.rstrip("/") + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            result = json.load(response)
        timings.append(round((time.monotonic() - started) * 1000))
        assert result["advisory_only"] is True and result["truncated"] is False
        return result

    cases = [
        ("ModuleNotFoundError: No module named 'httpx'，测试尚未开始。", "environment"),
        ("npm ERR! Missing script: test，项目的 package.json 没有测试脚本。", "project"),
        ("AssertionError: expected 2, got 3。依赖安装成功，业务函数返回错误结果。", "implementation"),
        ("任务没有完成，原因不详，没有提供日志。", "unknown"),
    ]
    for text, expected in cases:
        result = call("/v1/failure/classify", {"text": text})
        assert set(result["probabilities"]) == {"environment", "project", "implementation", "unknown"}
        results.append({"expected": expected, "actual": result["category"],
                        "probabilities": result["probabilities"]})
    rank_cases = [
        ("修复缓存没有失效，导致任务看板仍显示旧状态的问题", "调整登录页的颜色和 logo 大小", "任务看板缓存失效策略，状态变更后清理缓存并刷新看板"),
        ("数据库连接没有释放，运行一段时间后连接池耗尽", "调整导出文件的列顺序", "修复异常分支未归还数据库连接的问题"),
        ("上传中文文件名失败，修复路径编码", "修复上传路径中 Unicode 字符的编码", "优化任务优先级排序"),
        ("有效 Token 预算需要对缓存输入打折计算", "调整设置页面的字体", "实现缓存输入 Token 的加权预算统计"),
    ]
    rankings = []
    for index, (query, first, second) in enumerate(rank_cases):
        expected = "first" if index == 2 else "second"
        ranking = call("/v1/history/rank", {"query": query, "candidates": [
            {"id": "first", "text": first}, {"id": "second", "text": second}]})
        assert {item["id"] for item in ranking["candidates"]} == {"first", "second"}
        assert ranking["score_kind"] == "ordinal_0_1"
        rankings.append({"expected_first": expected, "result": ranking})
    print(json.dumps({"failure_cases": results, "history_cases": rankings,
        "matched_failure_cases": sum(item["actual"] == item["expected"] for item in results),
        "total_failure_cases": len(cases), "latency_ms": timings,
        "matched_history_cases": sum(item["result"]["candidates"][0]["id"] == item["expected_first"] for item in rankings),
        "total_history_cases": len(rankings),
        "median_latency_ms": statistics.median(timings)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
