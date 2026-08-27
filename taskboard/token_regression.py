from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from core.dispatcher import TaskDispatcher
from core.service import TaskboardService


TERMINAL_TASK_STATUSES = {
    "done", "failed", "blocked", "waiting_confirmation", "acceptance_blocked", "cancelled",
}
LIFECYCLE_COMPLETION_TOOLS = {
    "transition_task", "report_run_blocked", "submit_task_delivery", "review_code",
    "run_acceptance_checks", "accept_task", "review_task",
}


def _git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *arguments], check=True,
        capture_output=True, text=True,
    )


def _function_calls(paths: Iterable[Path]) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            if payload.get("type") not in {
                "function_call", "custom_tool_call", "local_shell_call", "mcp_tool_call",
            }:
                continue
            calls.append({
                "name": str(
                    payload.get("name") or payload.get("tool_name") or payload.get("tool")
                    or payload.get("type") or ""
                ),
                "arguments": str(payload.get("arguments") or payload.get("input") or payload.get("command") or ""),
            })
    return calls


def analyze_rollouts(paths: Iterable[Path]) -> dict[str, Any]:
    calls = _function_calls(paths)
    lifecycle_calls = [
        call for call in calls
        if any(call["name"].endswith(name) for name in LIFECYCLE_COMPLETION_TOOLS)
    ]
    context_reads = [call for call in calls if call["name"].endswith("get_task_context")]
    directory_searches = []
    for call in calls:
        searchable = f"{call['name']} {call['arguments']}".lower()
        registry_tool = any(term in call["name"].lower() for term in (
            "tool_search", "list_mcp_resources", "list_mcp_resource_templates",
        ))
        shell_registry_scan = (
            any(term in searchable for term in (".codex/skills", "/skills/", "tool registry", "工具目录"))
            and any(term in searchable for term in ("rg ", "find ", "ls ", "grep "))
        )
        if registry_tool or shell_registry_scan:
            directory_searches.append(call)
    return {
        "function_call_count": len(calls),
        "lifecycle_calls": lifecycle_calls,
        "context_reads": context_reads,
        "tool_directory_searches": directory_searches,
    }


def evaluate_report(
    report: dict[str, Any], *, raw_min: int = 20_000, raw_max: int = 40_000,
    effective_min: int = 10_000, effective_max: int = 20_000,
    lifecycle_max: int = 3,
) -> dict[str, bool]:
    observations = report.get("observations") or {}
    raw = int(report.get("raw_token_used") or 0)
    effective = int(report.get("effective_token_used") or 0)
    return {
        "quality_preserved": bool(report.get("quality_preserved")),
        "task_completed": report.get("status") == "done",
        "no_tool_directory_search": not observations.get("tool_directory_searches"),
        "no_context_refetch": not observations.get("context_reads"),
        "lifecycle_calls_within_limit": len(observations.get("lifecycle_calls") or []) <= lifecycle_max,
        "raw_tokens_in_target": raw_min <= raw <= raw_max,
        "effective_tokens_in_target": effective_min <= effective <= effective_max,
    }


def _create_benchmark_task(service: TaskboardService, project: Path) -> dict[str, Any]:
    target = {"file": "src/HomeTitle.tsx", "symbols": ["HomeTitle"]}
    acceptance = [{
        "criterion": "首页标题显示快乐智慧园且不再包含智慧幼儿园",
        "file": target["file"], "symbol": "HomeTitle",
        "method": "执行精确字面量检查",
        "command": "rg -q '快乐智慧园' src/HomeTitle.tsx && ! rg -q '智慧幼儿园' src/HomeTitle.tsx",
        "expected": "只保留快乐智慧园", "check_type": "automated",
    }]
    implementation = {
        "targets": [target],
        "ordered_steps": [{
            "file": target["file"], "symbol": "HomeTitle",
            "action": "将智慧幼儿园替换为快乐智慧园",
            "before": "智慧幼儿园", "after": "快乐智慧园",
        }],
    }
    review = {"checks": ["只修改 HomeTitle 的标题字面量并保留函数结构"]}
    project_value = str(project)
    evidence = {
        "tool": "codegraph_explore", "files": [target["file"]], "symbols": ["HomeTitle"],
    }
    analysis = service.prepare_location_analysis({
        "title": "智慧幼儿园改为快乐智慧园", "project": project_value,
        "goal": "将首页标题中的智慧幼儿园改为快乐智慧园", "modules": ["home-title"],
    })
    result = service.finalize_task_intake({
        "analysis_id": analysis["analysis_id"],
        "title": "智慧幼儿园改为快乐智慧园", "project": project_value,
        "goal": "将首页标题中的智慧幼儿园改为快乐智慧园",
        "scope": ["仅替换 HomeTitle 标题字面量"],
        "out_of_scope": ["不修改函数结构、样式或其他文件"],
        "modules": ["home-title"], "location_evidence": evidence,
        "targets": [target], "ordered_steps": implementation["ordered_steps"],
        "review_checks": review["checks"], "acceptance_plan": acceptance,
        "dependency_analysis": {"decision": "independent"},
    })
    return service.get_task(result["task_id"])


def run_benchmark(timeout_seconds: int = 600) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-taskboard-token-regression-") as temporary:
        root = Path(temporary)
        project = root / "project"
        source = project / "src" / "HomeTitle.tsx"
        source.parent.mkdir(parents=True)
        lines = [f"// fixture line {index}" for index in range(1, 56)]
        lines.append("export function HomeTitle() { return '智慧幼儿园' }")
        lines.extend(f"// fixture line {index}" for index in range(57, 121))
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _git(project, "init", "-q")
        _git(project, "config", "user.email", "token-regression@example.com")
        _git(project, "config", "user.name", "Token Regression")
        _git(project, "add", ".")
        _git(project, "commit", "-qm", "initial benchmark")

        previous = {
            key: os.environ.get(key)
            for key in ("CODEX_TASKBOARD_CODEX_HOME", "CODEX_TASKBOARD_OBSIDIAN_VAULT")
        }
        os.environ["CODEX_TASKBOARD_CODEX_HOME"] = str(root / "data" / "codex-home")
        os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = str(root / "vault")
        service = TaskboardService(root / "data")
        dispatcher = TaskDispatcher(service, interval_seconds=0.2)
        task: dict[str, Any] = {}
        try:
            service.set_dispatcher_enabled(True)
            task = _create_benchmark_task(service, project)
            deadline = time.monotonic() + max(60, int(timeout_seconds))
            while time.monotonic() < deadline:
                dispatcher.dispatch_once(limit=1)
                task = service.get_task(task["id"])
                if task["status"] in TERMINAL_TASK_STATUSES:
                    break
                time.sleep(0.25)
            if task.get("status") == "done":
                drain_deadline = time.monotonic() + 20
                while dispatcher._active_clients and time.monotonic() < drain_deadline:
                    dispatcher.dispatch_once(limit=1)
                    time.sleep(0.2)
            task = service.get_task(task["id"])
        finally:
            dispatcher.stop()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        content = source.read_text(encoding="utf-8")
        changed = subprocess.run(
            ["git", "-C", str(project), "diff", "--name-only", "HEAD", "--"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        runs = service.list_runs(task["id"])
        execution = next((item for item in runs if item["run_type"] == "execution"), {})
        observations = analyze_rollouts(service.codex_home.rglob("*.jsonl"))
        quality_preserved = (
            content.count("快乐智慧园") == 1
            and "智慧幼儿园" not in content
            and changed == ["src/HomeTitle.tsx"]
        )
        report: dict[str, Any] = {
            "benchmark": "智慧幼儿园 -> 快乐智慧园",
            "task_id": task["id"], "status": task["status"],
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "raw_token_used": int(task.get("token_used") or 0),
            "effective_token_used": int(task.get("effective_token_used") or 0),
            "quality_preserved": quality_preserved,
            "changed_files": changed,
            "execution_profile": (execution.get("context_snapshot") or {}).get("execution_profile") or {},
            "runs": [{
                key: item.get(key)
                for key in (
                    "id", "run_type", "status", "attempt", "token_used",
                    "effective_token_used", "input_tokens", "cached_input_tokens",
                    "output_tokens", "reasoning_output_tokens",
                )
            } for item in runs],
            "observations": observations,
            "dispatcher_error": dispatcher.last_error,
        }
        report["gates"] = evaluate_report(report)
        report["passed"] = all(report["gates"].values())
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real Codex Taskboard token regression benchmark")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run_benchmark(arguments.timeout)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output:
        arguments.output.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
