import { useEffect } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export default function App({authenticationEnabled = false}) {
  useEffect(() => {
    void import("./taskboard-app.js");
  }, []);

  return (
    <>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="sidebar-brand">
            <img
              className="brand-mark"
              src="/dotasks-mark.svg"
              alt="DoTasks"
            />
            <span>DoTasks</span>
          </div>
          <nav className="sidebar-nav" aria-label="工作区导航">
            <div className="sidebar-primary-actions">
              <Button
                variant="ghost"
                id="board-nav"
                className="sidebar-item selected"
                type="button"
              >
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <rect x="3.5" y="3.5" width="5" height="5" rx="1" />
                  <rect x="11.5" y="3.5" width="5" height="5" rx="1" />
                  <rect x="3.5" y="11.5" width="5" height="5" rx="1" />
                  <rect x="11.5" y="11.5" width="5" height="5" rx="1" />
                </svg>
                <span>任务看板</span>
                <span id="board-count" className="sidebar-count">
                  0
                </span>
              </Button>
              <Button
                variant="ghost"
                id="requirements-nav"
                className="sidebar-item"
                type="button"
              >
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M5 3.5h10a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 15V5A1.5 1.5 0 0 1 5 3.5Z" />
                  <path d="M6.5 7h7M6.5 10h7M6.5 13h4" />
                </svg>
                <span>需求看板</span>
                <span id="requirements-count" className="sidebar-count">
                  0
                </span>
              </Button>
              <Button variant="ghost" id="token-panel" className="sidebar-item" type="button">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M4 16V11M10 16V7M16 16V3" />
                </svg>
                <span>Token 看板</span>
              </Button>
              <Button variant="ghost" id="execution-log-nav" className="sidebar-item" type="button">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M5 3.5h10a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 15V5A1.5 1.5 0 0 1 5 3.5Z" />
                  <path d="M7 7h6M7 10h6M7 13h4" />
                </svg>
                <span>执行日志</span>
                <span id="execution-log-count" className="sidebar-count">0</span>
              </Button>
            </div>
          </nav>
          <div className="sidebar-footer">
            <span className="sync-dot connected"></span>
            <span>Codex 原生任务调度</span>
            {authenticationEnabled && <Button variant="ghost" id="logout-button" type="button">退出登录</Button>}
          </div>
        </aside>

        <main className="main-content">
          <header>
            <div className="header-heading">
              <p id="view-eyebrow" className="eyebrow">
                DOTASKS
              </p>
              <h1 id="view-title">任务面板</h1>
            </div>
            <div className="header-actions">
              <div className="integration-statuses" aria-label="集成连接状态">
                <span
                  id="obsidian-status"
                  className="integration-status pending"
                  title="正在检查 Obsidian"
                >
                  <span className="integration-dot"></span>Obsidian
                </span>
                <span
                  id="location-status"
                  className="integration-status pending"
                  title="正在检查代码定位"
                >
                  <span className="integration-dot"></span>代码定位
                </span>
              </div>
              <span id="health" className="health">
                正在连接…
              </span>
              <Button variant="outline" id="dispatcher-toggle" className="ghost" type="button">
                调度状态
              </Button>
              <Button
                id="new-task-button"
                className="primary"
                type="button"
                hidden
              >
                新增任务
              </Button>
              <Button
                id="new-requirement-button"
                className="primary"
                type="button"
                hidden
              >
                新增需求
              </Button>
              <Button
                id="task-change-confirmations"
                className="primary change-confirmation-button"
                type="button"
                hidden
              >
                需求变更（<span id="task-change-count">0</span>）
              </Button>
              <Button id="completed-tasks" className="primary" type="button">
                完成任务（<span id="completed-count">0</span>）
              </Button>
              <Button
                variant="outline"
                id="settings-button"
                className="ghost"
                type="button"
                aria-label="打开设置"
              >
                设置
              </Button>
            </div>
          </header>
          <section id="content" className="board-columns"></section>
        </main>
      </div>

      <dialog id="task-detail-dialog" className="detail-dialog">
        <div className="detail-shell">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">TASK TRACE</p>
              <h2 id="detail-title">任务记录</h2>
            </div>
            <Button variant="ghost" size="icon" type="button" className="icon-button" data-close>
              ×
            </Button>
          </div>
          <div id="task-detail-content" className="detail-content"></div>
        </div>
      </dialog>

      <dialog id="completed-tasks-dialog" className="attention-dialog">
        <div className="detail-shell attention-shell">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">COMPLETED</p>
              <h2>
                完成任务（<span id="completed-dialog-count">0</span>）
              </h2>
            </div>
            <Button variant="ghost" size="icon" type="button" className="icon-button" data-close>
              ×
            </Button>
          </div>
          <div
            id="completed-tasks-content"
            className="attention-tasks-content"
          ></div>
        </div>
      </dialog>

      <dialog id="task-change-dialog" className="attention-dialog">
        <div className="detail-shell attention-shell">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">REQUIREMENT CHANGE</p>
              <h2>
                确认需求归属（<span id="task-change-dialog-count">0</span>）
              </h2>
            </div>
            <Button variant="ghost" size="icon" type="button" className="icon-button" data-close>
              ×
            </Button>
          </div>
          <p className="change-confirmation-help">
            请选择将新需求合并到正在执行的任务，或拆成一个独立任务。选择后立即生效。
          </p>
          <div
            id="task-change-content"
            className="attention-tasks-content"
          ></div>
        </div>
      </dialog>

      <dialog id="new-requirement-dialog">
        <form id="new-requirement-form">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">NEW REQUIREMENT</p>
              <h2>新增需求</h2>
            </div>
            <Button variant="ghost" size="icon" type="button" className="icon-button" data-close>
              ×
            </Button>
          </div>
          <label>
            标题
            <Input name="title" maxLength="120" required autoFocus />
          </label>
          <label>
            Mac Codex 项目（可选）
            <select
              name="project"
              data-project-select
              defaultValue=""
            >
              <option value="">无项目（仅保存需求）</option>
              <option value="__manual__">手动输入绝对路径…</option>
            </select>
            <Input
              name="manual_project"
              data-manual-project
              placeholder="输入 Mac 上的绝对路径"
              hidden
            />
          </label>
          <label>
            需求目标
            <Textarea name="goal" rows="5" required />
          </label>
          <label>
            需求截图（最多 8 张，每张不超过 10 MiB）
            <Input
              name="visual_references"
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
            />
          </label>
          <label>
            优先级
            <select name="priority" defaultValue="P2">
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
          </label>
          <label className="inline-checkbox">
            <Input name="auto_dispatch" type="checkbox" defaultChecked />
            保存后自动调度拆解
          </label>
          <div className="form-actions">
            <Button variant="outline" type="button" className="ghost" data-close>取消</Button>
            <Button type="submit" className="primary">保存并调度</Button>
          </div>
        </form>
      </dialog>

      <dialog id="new-task-dialog">
        <form id="new-task-form">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">NEW TASK</p>
              <h2>新增任务</h2>
            </div>
            <Button variant="ghost" size="icon" type="button" className="icon-button" data-close>
              ×
            </Button>
          </div>
          <label>
            标题
            <Input name="title" maxLength="120" required autoFocus />
          </label>
          <label>
            类型
            <select name="type" defaultValue="feature">
              <option value="feature">任务</option>
              <option value="bug">Bug</option>
            </select>
          </label>
          <label>
            Mac Codex 项目（可选）
            <select
              name="project"
              data-project-select
              defaultValue=""
            >
              <option value="">无项目（创建到 Codex 最近）</option>
              <option value="__manual__">手动输入绝对路径…</option>
            </select>
            <Input
              name="manual_project"
              data-manual-project
              placeholder="输入 Mac 上的绝对路径"
              hidden
            />
          </label>
          <label>
            任务目标
            <Textarea name="goal" rows="5" required />
          </label>
          <label>
            任务截图（最多 8 张，每张不超过 10 MiB）
            <Input
              name="visual_references"
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
            />
          </label>
          <label>
            优先级
            <select name="priority" defaultValue="P2">
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
          </label>
          <label className="inline-checkbox">
            <Input name="auto_dispatch" type="checkbox" defaultChecked />
            保存后自动定位并执行
          </label>
          <div className="form-actions">
            <Button variant="outline" type="button" className="ghost" data-close>取消</Button>
            <Button type="submit" className="primary">加入任务队列</Button>
          </div>
        </form>
      </dialog>

      <div id="toast" className="toast"></div>
    </>
  );
}
