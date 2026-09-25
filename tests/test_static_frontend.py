from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "web"
WEB_SOURCE = WEB_ROOT / "src"


class StaticFrontendTest(unittest.TestCase):
    def run_module_script(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f"""
import fs from "node:fs";
const source = fs.readFileSync("web/src/ui-core.js", "utf8");
const module = await import(`data:text/javascript;charset=utf-8,${{encodeURIComponent(source)}}`);
{body}
"""
        return subprocess.run(
            [os.environ.get("DOTASKS_NODE_BIN", "node"), "--input-type=module", "-e", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_header_does_not_show_integration_statuses(self):
        css = (WEB_SOURCE / "styles.css").read_text(encoding="utf-8")
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        self.assertNotIn('id="health"', html)
        self.assertNotIn('className="integration-statuses"', html)

    def test_project_chat_controls_are_removed_from_the_rendered_app(self):
        index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")

        self.assertNotIn("chatkit.js", index)
        self.assertNotIn('id="new-chat"', html)
        self.assertNotIn('className="projects-section"', html)
        self.assertNotIn("项目 / 聊天", html)
        self.assertNotIn("loadProjects();", app.split("async function load(", 1)[1].split("function scheduleBoardRefresh", 1)[0])

    def test_dispatch_waiting_reason_is_visible_on_cards_and_details(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")

        self.assertIn("dispatch_blockers", app)
        self.assertIn("等待调度", app)
        self.assertIn("调度等待原因", app)
        self.assertNotIn("project_blockers", app)
        self.assertNotIn("项目级阻塞", app)

    def test_metric_token_count_uses_compact_english_units(self):
        completed = self.run_module_script("""
const cases = new Map([
  [0, "0"],
  [1000, "1K"],
  [30_885_482, "30.9M"],
  [2_200_000_000, "2.2B"],
]);
for (const [value, expected] of cases) {
  const actual = module.formatMetricTokenCount(value);
  if (actual !== expected) throw new Error(`${value} formatted as ${actual}, expected ${expected}`);
}
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_execution_log_helpers_format_status_and_failure_reason(self):
        completed = self.run_module_script("""
const event = {
  event_type: "code_reviewed",
  payload: {verdict: "fail", reasons: ["测试未通过", "存在回归"]},
};
if (module.executionLogLabel(event, {}) !== "Code Review 失败") throw new Error("review status was not localized");
if (module.executionLogReason(event) !== "测试未通过；存在回归") throw new Error("failure reasons were not joined");
if (module.executionLogLabel({event_type: "transitioned", payload: {to: "blocked"}}, {blocked: "阻塞"}) !== "阻塞") throw new Error("transition status did not use workflow labels");
const formatted = module.formatLogTimestamp("2026-09-04 08:44:39");
if (!/^2026-09-04 \\d{2}:44:39$/.test(formatted)) throw new Error(`unexpected log timestamp: ${formatted}`);
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_execution_log_is_a_separate_page(self):
        app = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        script = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        self.assertIn('id="execution-log-nav"', app)
        self.assertNotIn('id="execution-log-list"', app)
        self.assertIn('id="execution-log-list"', script)
        self.assertIn('if (state.view === "logs") renderExecutionLog();', script)
        self.assertIn("renderExecutionLog()", script)
        self.assertIn('item.event_type !== "token_usage_updated"', script)

    def test_only_period_token_cards_use_metric_formatting(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        period_summary = app.split(
            'return `<div class="token-summary token-period-summary">', 1
        )[1].split('</div><div class="token-chart-grid">', 1)[0]
        for period in ("today", "week", "month"):
            self.assertIn(f"formatMetricTokenCount(periods.{period})", period_summary)
        self.assertEqual(3, period_summary.count("formatMetricTokenCount("))
        self.assertIn("formatTokenCount(item.value)", app)
        self.assertIn("formatTokenCount(metric.token_used)", app)

    def test_json_client_handles_html_errors_and_only_sets_content_type_for_bodies(self):
        completed = self.run_module_script("""
let getOptions;
const successfulFetch = async (_path, options) => {
  getOptions = options;
  return {ok: true, status: 200, text: async () => '{"ok":true}'};
};
const payload = await module.requestJson(successfulFetch, "/api/health");
if (!payload.ok) throw new Error("valid JSON response was rejected");
if (Object.keys(getOptions.headers).length) throw new Error("GET request received an unnecessary content type");

let postOptions;
await module.requestJson(async (_path, options) => {
  postOptions = options;
  return {ok: true, status: 200, text: async () => '{}'};
}, "/api/task-intakes/finalize", {method: "POST", body: "{}"});
if (postOptions.headers["Content-Type"] !== "application/json") throw new Error("JSON body has no content type");

try {
  await module.requestJson(async () => ({ok: false, status: 502, text: async () => '<html>bad gateway</html>'}), "/api/board");
  throw new Error("HTML error response was accepted");
} catch (error) {
  if (error.message !== "HTTP 502") throw error;
}
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_delegated_event_stops_after_the_first_matching_handler(self):
        completed = self.run_module_script("""
const calls = [];
const handled = await module.routeDelegatedEvent({target: {}}, [
  async () => { calls.push("view"); return false; },
  async () => { calls.push("thread"); return true; },
  async () => { calls.push("task"); return true; },
]);
if (!handled) throw new Error("handled event was reported as unhandled");
if (calls.join(",") !== "view,thread") throw new Error(`unexpected routing order: ${calls}`);
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_react_app_is_loaded_through_vite(self):
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        package = (WEB_ROOT / "package.json").read_text(encoding="utf-8")
        entry = (WEB_SOURCE / "main.jsx").read_text(encoding="utf-8")
        vite_config = (WEB_ROOT / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn('<script type="module" src="/src/main.jsx"></script>', html)
        self.assertIn('"vite"', package)
        self.assertIn('"react"', package)
        self.assertIn('createRoot(', entry)
        self.assertIn('changeOrigin: true', vite_config)

    def test_taskboard_layout_fills_the_viewport_and_remaining_height(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        css = (WEB_SOURCE / "styles.css").read_text(encoding="utf-8")

        def declarations(selector: str, source: str = css) -> dict[str, str]:
            match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", source)
            self.assertIsNotNone(match, f"missing CSS rule for {selector}")
            return {
                name.strip(): value.strip()
                for item in match.group(1).split(";")
                if ":" in item
                for name, value in [item.split(":", 1)]
            }

        self.assertIn('<main className="main-content">', html)
        self.assertIn('id="content" className="board-columns"', html)
        self.assertEqual("100%", declarations("html, body, #root")["height"])
        self.assertEqual("100%", declarations(".app-shell")["height"])
        self.assertEqual("100%", declarations(".sidebar")["height"])
        self.assertEqual("100%", declarations(".main-content")["height"])
        content = declarations("#content.board-columns")
        self.assertEqual("1 1 auto", content["flex"])
        self.assertEqual("0", content["min-height"])
        self.assertEqual("hidden", content["overflow"])
        board = declarations("#content.board-columns > .board")
        self.assertEqual("repeat(4, minmax(250px, 1fr))", board["grid-template-columns"])
        self.assertEqual("1 1 auto", board["flex"])
        self.assertEqual("0", board["min-height"])
        self.assertEqual("stretch", board["align-items"])
        self.assertEqual("auto", board["overflow-x"])
        self.assertEqual("hidden", board["overflow-y"])
        self.assertEqual("0", declarations(".column")["min-height"])
        cards = declarations(".cards")
        self.assertEqual("1 1 0", cards["flex"])
        self.assertEqual("0", cards["min-height"])
        self.assertEqual("auto", cards["overflow-y"])

    def test_full_height_layout_keeps_board_controls_and_counts_wired(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        css = (WEB_SOURCE / "styles.css").read_text(encoding="utf-8")

        def declaration_value(source: str, selector: str, name: str) -> str:
            match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", source)
            self.assertIsNotNone(match, f"missing CSS rule for {selector}")
            declarations = dict(
                item.strip().split(":", 1)
                for item in match.group(1).split(";")
                if ":" in item
            )
            return declarations[name].strip()

        for control_id in (
            "board-nav",
            "requirements-nav",
            "new-task-button",
            "new-task-dialog",
            "new-requirement-button",
            "new-requirement-dialog",
            "dispatcher-toggle",
            "completed-tasks",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("const taskColumns = columns.map(column =>", app)
        self.assertNotIn('class="column column-requirements"', app)
        self.assertIn("renderRequirementsBoard", app)
        self.assertIn("requirementCard(requirement, tasks)", app)
        self.assertIn("requirementConversationControl(requirement)", app)
        self.assertIn('aria-label="打开需求拆解会话"', app)
        self.assertIn('data-open-thread="${escapeHtml(threadId)}"', app)
        self.assertIn('data-redecompose-requirement="${escapeHtml(requirement.id)}"', app)
        self.assertIn('data-delete-requirement="${escapeHtml(requirement.id)}"', app)
        self.assertIn("需求已重新加入拆解队列", app)
        self.assertIn("需求已删除，关联任务已保留", app)
        self.assertIn("task.requirement_id === requirement.id", app)
        self.assertIn("left.requirement_task_key", app)
        self.assertIn('class="requirement-task"', app)
        self.assertIn("<span>${visible.length}</span>", app)
        self.assertIn('setContent("#content",', app)
        self.assertLess(html.index('id="board-nav"'), html.index('id="requirements-nav"'))
        self.assertLess(html.index('id="requirements-nav"'), html.index('id="token-panel"'))
        self.assertIn("任务看板", html)
        self.assertIn("需求看板", html)
        self.assertIn("Token 看板", html)
        self.assertNotIn("Codex 原生任务调度", html)
        self.assertIn("<AccountMenu", html)
        self.assertLess(html.index('id="dispatcher-toggle"'), html.index('id="completed-tasks"'))
        self.assertNotIn('id="settings-button"', html)
        self.assertIn('event.target.closest("#board-nav")', app)
        self.assertIn('event.target.closest("#requirements-nav")', app)
        self.assertIn('event.detail === "settings"', app)
        self.assertIn('event.target.closest("#completed-tasks")', app)
        self.assertIn('column.key === "attention"', app)
        self.assertIn('event.target.closest("#dispatcher-toggle")', app)
        self.assertIn('event.target.closest("#new-requirement-button")', app)
        self.assertIn('event.target.closest("#new-task-button")', app)
        self.assertIn('api("/api/codex/projects")', app)
        self.assertIn('api("/api/task-intakes/enqueue"', app)
        self.assertIn('id="new-task-form"', html)
        self.assertIn("加入任务队列", html)
        self.assertIn("Mac Codex 项目（可选）", html)
        self.assertIn("无项目（创建到 Codex 最近）", html)
        self.assertIn("将创建无项目 Codex 会话", app)
        self.assertIn('api("/api/task-intakes/finalize"', app)
        self.assertEqual(2, html.count('name="visual_references"'))
        self.assertIn('api("/api/visual-artifacts"', app)
        self.assertEqual(2, app.count("visual_references: visualReferences"))
        responsive = css.split("@media (max-width: 760px)", 1)[1].split(
            "@media (prefers-color-scheme: dark)", 1
        )[0]
        self.assertEqual("column", declaration_value(responsive, ".app-shell", "flex-direction"))
        self.assertEqual("100%", declaration_value(responsive, ".sidebar", "width"))
        self.assertEqual("auto", declaration_value(responsive, ".sidebar", "height"))
        self.assertEqual(
            "12px",
            declaration_value(responsive, "#content.board-columns > .board", "padding"),
        )

    def test_project_archive_view_and_actions_are_not_rendered(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        self.assertNotIn('id="project-archive-toggle"', html)
        self.assertNotIn('id="add-project"', html)
        self.assertNotIn('id="project-list"', html)

    def test_conversation_controls_open_native_tasks_and_keep_detail_copy(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        self.assertIn('data-open-thread="${escapeHtml(conversation.thread_id)}"', app)
        self.assertIn('codex://threads/${encodeURIComponent(openThread.dataset.openThread)}', app)
        self.assertIn('data-copy-thread=', app)
        self.assertIn("复制原生任务 ID", app)

    def test_completed_tasks_expose_conversation_and_delete_actions(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")

        completed_renderer = app.split("function renderCompletedTasks()", 1)[1].split(
            "function renderTaskChangeConfirmations()", 1
        )[0]
        self.assertIn("taskConversationControl(task)", completed_renderer)
        conversation_control = app.split("function taskConversationControl(task)", 1)[1].split(
            "function taskRecoveryButton", 1
        )[0]
        self.assertIn('task.status === "done"', conversation_control)
        self.assertIn("data-thread-select", conversation_control)
        self.assertIn("选择会话", conversation_control)
        self.assertIn('event.target.closest("[data-thread-select]")', app)
        self.assertIn("deleteIcon()", completed_renderer)
        self.assertIn("data-delete-completed-task", completed_renderer)
        self.assertIn('api(`/api/tasks/${taskId}/delete`', app)
        self.assertIn("Codex 会话会保留", app)

    def test_settings_button_opens_task_token_budget_configuration(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="settings-button"', html)
        self.assertIn('id="task-settings-form"', app)
        self.assertIn('name="task_token_budget"', app)
        self.assertIn('name="max_batch_appended_tasks"', app)
        self.assertIn('api("/api/settings"', app)

    def test_project_picker_uses_explicit_options_and_manual_path_fallback(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")

        self.assertEqual(2, html.count("data-project-select"))
        self.assertEqual(2, html.count("data-manual-project"))
        self.assertNotIn("known-project-paths", html + app)
        self.assertIn("无项目（创建到 Codex 最近）", html + app)
        self.assertIn("手动输入绝对路径", html + app)
        self.assertIn("function selectedProjectPath(values)", app)
        self.assertIn("codexProjects", app)
        self.assertIn(
            'auto_dispatch: values.get("auto_dispatch") === "on"', app
        )

    def test_frontend_loads_workflow_metadata_instead_of_duplicating_statuses(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        core = (WEB_SOURCE / "ui-core.js").read_text(encoding="utf-8")
        self.assertIn('api("/api/workflow")', app)
        self.assertNotIn('draft:"草稿"', core)

    def test_task_conversation_selection_follows_the_current_stage(self):
        completed = self.run_module_script("""
const conversations = [
  {role: "execution", thread_id: "dev-thread"},
  {role: "code_review", thread_id: "review-thread"},
];
if (module.selectTaskConversation({status: "implementing", conversations}).thread_id !== "dev-thread") throw new Error("development did not select its thread");
if (module.selectTaskConversation({status: "code_review", conversations}).thread_id !== "review-thread") throw new Error("review did not select its thread");
if (module.selectTaskConversation({status: "paused", paused_from_status: "code_review", conversations}).thread_id !== "review-thread") throw new Error("paused stage was not restored");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_frontend_uses_dotasks_brand(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        icon = (WEB_ROOT / "public/dotasks-mark.svg").read_text(encoding="utf-8")
        self.assertIn(">DoTasks<", html)
        self.assertIn('alt="DoTasks"', html)
        self.assertIn("<title>DoTasks</title>", index)
        self.assertIn(">DoTasks</title>", icon)
        self.assertNotIn("Codex Taskboard", html + index + icon)

    def test_attention_includes_stages_with_paused_auto_dispatch(self):
        completed = self.run_module_script("""
const statuses = new Set(["failed", "blocked"]);
const pausedStages = new Set(["rework", "code_review"]);
if (!module.taskNeedsAttention({status: "failed", auto_dispatch: 1}, statuses, pausedStages)) throw new Error("failed task was omitted");
if (!module.taskNeedsAttention({status: "code_review", auto_dispatch: 0}, statuses, pausedStages)) throw new Error("paused review was omitted");
if (module.taskNeedsAttention({status: "code_review", auto_dispatch: 1}, statuses, pausedStages)) throw new Error("active review was included");
if (module.taskNeedsAttention({status: "done", auto_dispatch: 0}, statuses, pausedStages)) throw new Error("completed task was included");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_attention_actions_match_task_recovery_state(self):
        completed = self.run_module_script("""
const waiting = module.taskRecoveryAction(
  {status: "waiting_confirmation", retry_run_type: "rework"},
  {restartWaitingConfirmation: true},
);
if (waiting.label !== "重新开始" || waiting.status !== "rework") throw new Error("rework restart action is incorrect");
const failed = module.taskRecoveryAction({status: "failed"});
if (failed.label !== "重新执行" || failed.status !== "ready") throw new Error("failed recovery action is incorrect");
const blocked = module.taskRecoveryAction({status: "blocked", blocked_from_status: "code_review"});
if (blocked.label !== "返回 Code Review" || blocked.status !== "code_review") throw new Error("blocked recovery action is incorrect");
if (module.taskRecoveryAction({status: "paused"}).kind !== "resume") throw new Error("paused task has no resume action");
if (module.taskRecoveryAction({status: "code_review", auto_dispatch: 0}).kind !== "enable_auto") throw new Error("paused review has no continue action");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_attention_column_exposes_cancel_and_recovery_controls(self):
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        self.assertIn('data-cancel-task=', app)
        self.assertIn('restartWaitingConfirmation: needsAttention', app)
        self.assertIn('status: "cancelled"', app)
        self.assertIn('updates.token_budget = nextBudget', app)

    def test_task_change_confirmation_is_an_interactive_dialog(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "taskboard-app.js").read_text(encoding="utf-8")
        self.assertIn('id="task-change-dialog"', html)
        self.assertIn('data-decision="revise"', app)
        self.assertIn('data-decision="create_new"', app)
        self.assertIn('/api/task-changes/${changeId}/resolve', app)


if __name__ == "__main__":
    unittest.main()
