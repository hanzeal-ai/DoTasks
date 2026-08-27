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
            [os.environ.get("CODEX_TASKBOARD_NODE_BIN", "node"), "--input-type=module", "-e", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_header_does_not_show_integration_statuses(self):
        css = (WEB_SOURCE / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".integration-statuses { display: none; }", css)

    def test_request_guard_rejects_stale_scopes(self):
        completed = self.run_module_script("""
const guard = new module.LatestRequest();
const first = guard.begin("project-a");
const second = guard.begin("project-b");
if (guard.isCurrent(first, "project-a")) throw new Error("stale request remained current");
if (!guard.isCurrent(second, "project-b")) throw new Error("latest request was rejected");
guard.invalidate();
if (guard.isCurrent(second, "project-b")) throw new Error("invalidated request remained current");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

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

    def test_only_period_token_cards_use_metric_formatting(self):
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
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
        self.assertEqual("repeat(5, minmax(250px, 1fr))", board["grid-template-columns"])
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
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
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
            "dispatcher-toggle",
            "attention-tasks",
            "settings-button",
        ):
            self.assertIn(f'id="{control_id}"', html)
        self.assertIn("const taskColumns = columns.map(column =>", app)
        self.assertIn("<span>${visible.length}</span>", app)
        self.assertIn('document.querySelector("#content").innerHTML', app)
        self.assertLess(html.index('id="board-nav"'), html.index('id="token-panel"'))
        self.assertLess(html.index('id="dispatcher-toggle"'), html.index('id="attention-tasks"'))
        self.assertLess(html.index('id="attention-tasks"'), html.index('id="settings-button"'))
        self.assertIn('event.target.closest("#board-nav")', app)
        self.assertIn('event.target.closest("#settings-button")', app)
        self.assertIn('event.target.closest("#attention-tasks")', app)
        self.assertIn('event.target.closest("#dispatcher-toggle")', app)
        responsive = css.split("@media (max-width: 760px)", 1)[1].split(
            "@media (prefers-color-scheme: dark)", 1
        )[0]
        self.assertEqual("182px", declaration_value(responsive, ".sidebar", "flex-basis"))
        self.assertEqual(
            "12px",
            declaration_value(responsive, "#content.board-columns > .board", "padding"),
        )

    def test_project_archive_view_and_actions_are_wired(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
        self.assertIn('id="project-archive-toggle"', html)
        self.assertIn('data-archive-project=', app)
        self.assertIn('data-restore-project=', app)
        self.assertIn('window.confirm(`确定归档项目', app)
        self.assertIn('/api/codex/projects/archive', app)
        self.assertIn('/api/codex/projects/restore', app)
        self.assertIn('state.view = "board"', app)
        self.assertIn('?archived=true', app)

    def test_settings_button_opens_task_token_budget_configuration(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
        self.assertLess(html.index('id="attention-tasks"'), html.index('id="settings-button"'))
        self.assertIn('id="task-settings-form"', app)
        self.assertIn('name="task_token_budget"', app)
        self.assertIn('api("/api/settings"', app)

    def test_frontend_loads_workflow_metadata_instead_of_duplicating_statuses(self):
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
        core = (WEB_SOURCE / "ui-core.js").read_text(encoding="utf-8")
        self.assertIn('api("/api/workflow")', app)
        self.assertNotIn('draft:"草稿"', core)

    def test_task_conversation_selection_follows_the_current_stage(self):
        completed = self.run_module_script("""
const conversations = [
  {role: "execution", thread_id: "dev-thread"},
  {role: "code_review", thread_id: "review-thread"},
  {role: "acceptance", thread_id: "acceptance-thread"},
];
if (module.selectTaskConversation({status: "implementing", conversations}).thread_id !== "dev-thread") throw new Error("development did not select its thread");
if (module.selectTaskConversation({status: "code_review", conversations}).thread_id !== "review-thread") throw new Error("review did not select its thread");
if (module.selectTaskConversation({status: "acceptance", conversations}).thread_id !== "acceptance-thread") throw new Error("acceptance did not select its thread");
if (module.selectTaskConversation({status: "paused", paused_from_status: "code_review", conversations}).thread_id !== "review-thread") throw new Error("paused stage was not restored");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_taskboard_slash_command_expands_to_explicit_skill_invocation(self):
        completed = self.run_module_script("""
if (module.expandTaskboardSlashCommand("/taskboard") !== "$codex-taskboard") throw new Error("bare command was not expanded");
if (module.expandTaskboardSlashCommand("/taskboard 打开任务看板") !== "$codex-taskboard\\n\\n打开任务看板") throw new Error("command arguments were not preserved");
if (module.expandTaskboardSlashCommand("/TASKBOARD\\n调度下一项任务") !== "$codex-taskboard\\n\\n调度下一项任务") throw new Error("multiline command was not expanded");
if (module.expandTaskboardSlashCommand("/taskboards") !== "/taskboards") throw new Error("unrelated command was rewritten");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_attention_includes_stages_with_paused_auto_dispatch(self):
        completed = self.run_module_script("""
const statuses = new Set(["failed", "blocked"]);
const pausedStages = new Set(["rework", "code_review", "acceptance"]);
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
if (module.taskRecoveryAction({status: "acceptance", auto_dispatch: 0}).kind !== "enable_auto") throw new Error("paused acceptance has no continue action");
""")
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_attention_dialog_exposes_cancel_and_recovery_controls(self):
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
        self.assertIn('data-cancel-task=', app)
        self.assertIn('restartWaitingConfirmation: true', app)
        self.assertIn('status: "cancelled"', app)
        self.assertIn('updates.token_budget = nextBudget', app)

    def test_task_change_confirmation_is_an_interactive_dialog(self):
        html = (WEB_SOURCE / "App.jsx").read_text(encoding="utf-8")
        app = (WEB_SOURCE / "legacy-app.js").read_text(encoding="utf-8")
        self.assertIn('id="task-change-dialog"', html)
        self.assertIn('data-decision="revise"', app)
        self.assertIn('data-decision="create_new"', app)
        self.assertIn('/api/task-changes/${changeId}/resolve', app)


if __name__ == "__main__":
    unittest.main()
