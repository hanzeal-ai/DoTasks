import { useEffect } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog } from "./components/taskboard-dialog";
import { DialogTitle } from "./components/ui/dialog";
import { Checkbox } from "./components/ui/checkbox";
import { AccountMenu } from "./components/account-menu";
import { SidebarMenu, SidebarMenuItem, SidebarMenuButton } from "./components/ui/sidebar";
import { NativeSelect } from "./components/ui/native-select";
import { IntakeEditor } from "./components/intake-editor";

export default function App({ authenticationEnabled = false, username = "本地用户", teamsEnabled = false }) {
  useEffect(() => {
    void import("./taskboard-app.js");
  }, []);

  return (
    <>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="sidebar-brand">
            <img className="brand-mark" src="/dotasks-mark.svg" alt="DoTasks" />
            <span>DoTasks</span>
          </div>
          <nav className="sidebar-nav" aria-label="工作区导航">
            <SidebarMenu className="sidebar-primary-actions">
              <SidebarMenuItem><SidebarMenuButton
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
              </SidebarMenuButton></SidebarMenuItem>
              <SidebarMenuItem><SidebarMenuButton
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
              </SidebarMenuButton></SidebarMenuItem>
              {teamsEnabled && <SidebarMenuItem><a className="sidebar-item" href="/team">
                <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="7" cy="6" r="2.5" /><path d="M2.5 16v-2a4.5 4.5 0 0 1 9 0v2M13 3.5a2.5 2.5 0 0 1 0 5M14 11a4 4 0 0 1 3.5 4v1" /></svg>
                <span>团队协作</span>
              </a></SidebarMenuItem>}
              <SidebarMenuItem><SidebarMenuButton
                variant="ghost"
                id="token-panel"
                className="sidebar-item"
                type="button"
              >
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M4 16V11M10 16V7M16 16V3" />
                </svg>
                <span>Token 看板</span>
              </SidebarMenuButton></SidebarMenuItem>
              <SidebarMenuItem><SidebarMenuButton
                variant="ghost"
                id="execution-log-nav"
                className="sidebar-item"
                type="button"
              >
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M5 3.5h10a1.5 1.5 0 0 1 1.5 1.5v10a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 15V5A1.5 1.5 0 0 1 5 3.5Z" />
                  <path d="M7 7h6M7 10h6M7 13h4" />
                </svg>
                <span>执行日志</span>
              </SidebarMenuButton></SidebarMenuItem>
            </SidebarMenu>
          </nav>
          <div className="sidebar-footer">
            <AccountMenu username={username} authenticationEnabled={authenticationEnabled} />
          </div>
        </aside>

        <main className="main-content">
          <header>
            <div className="header-heading">
              <h1 id="view-title">任务看板</h1>
            </div>
            <div className="header-actions">
              <Button
                variant="outline"
                id="dispatcher-toggle"
                className="ghost"
                type="button"
              >
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
                variant="outline" className="ghost change-confirmation-button"
                type="button"
                hidden
              >
                需求变更(<span id="task-change-count">0</span>)
              </Button>
              <Button id="completed-tasks" variant="outline" className="ghost" type="button">
                已完成任务(<span id="completed-count">0</span>)
              </Button>
            </div>
          </header>
          <p id="dispatcher-status" className="dispatcher-status" role="status" hidden></p>
          <section id="content" className="board-columns"></section>
        </main>
      </div>

      <Dialog id="task-detail-dialog" className="detail-dialog">
        <div className="detail-shell">
          <div className="dialog-head">
            <div>
              <DialogTitle asChild><h2><span id="detail-title">任务记录</span></h2></DialogTitle>
            </div>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="icon-button"
              aria-label="关闭"
              data-close
            >
              ×
            </Button>
          </div>
          <div id="task-detail-content" className="detail-content"></div>
        </div>
      </Dialog>

      <Dialog id="completed-tasks-dialog" className="attention-dialog">
        <div className="detail-shell attention-shell">
          <div className="dialog-head">
            <div>
              <DialogTitle asChild><h2>
                已完成任务(<span id="completed-dialog-count">0</span>)
              </h2></DialogTitle>
            </div>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="icon-button"
              aria-label="关闭"
              data-close
            >
              ×
            </Button>
          </div>
          <div
            id="completed-tasks-content"
            className="attention-tasks-content"
          ></div>
        </div>
      </Dialog>

      <Dialog id="task-change-dialog" className="attention-dialog">
        <div className="detail-shell attention-shell">
          <div className="dialog-head">
            <div>
              <DialogTitle asChild><h2>
                确认需求归属(<span id="task-change-dialog-count">0</span>)
              </h2></DialogTitle>
            </div>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="icon-button"
              aria-label="关闭"
              data-close
            >
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
      </Dialog>

      <Dialog id="new-requirement-dialog">
        <form id="new-requirement-form" className="intake-form">
          <div className="dialog-head">
            <div>
              <DialogTitle asChild><h2>新增需求</h2></DialogTitle>
            </div>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="icon-button"
              aria-label="关闭"
              data-close
            >
              ×
            </Button>
          </div>
          <div className="intake-fields">
          <label>
            Mac Codex 项目（可选）
            <NativeSelect name="project" data-project-select defaultValue="">
              <option value="">无项目（仅保存需求）</option>
              <option value="__manual__">手动输入绝对路径…</option>
            </NativeSelect>
            <Input
              name="manual_project"
              data-manual-project
              placeholder="输入 Mac 上的绝对路径"
              hidden
            />
          </label>
          <IntakeEditor label="需求内容" />
          <details className="intake-options"><summary>其他设置</summary>          <label>
            优先级
            <NativeSelect name="priority" defaultValue="P2">
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </NativeSelect>
          </label>
</details>
          <label className="inline-checkbox">
            <Checkbox name="auto_dispatch" defaultChecked />
            保存后自动调度拆解
          </label>
          </div>
          <div className="form-actions">
            <Button
              variant="outline"
              type="button"
              className="ghost"
              data-close
            >
              取消
            </Button>
            <Button type="submit" className="primary">
              保存并调度
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog id="new-task-dialog">
        <form id="new-task-form" className="intake-form">
          <div className="dialog-head">
            <div>
              <DialogTitle asChild><h2>新增任务</h2></DialogTitle>
            </div>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="icon-button"
              aria-label="关闭"
              data-close
            >
              ×
            </Button>
          </div>
          <div className="intake-fields">
          <label>
            Mac Codex 项目（可选）
            <NativeSelect name="project" data-project-select defaultValue="">
              <option value="">无项目（创建到 Codex 最近）</option>
              <option value="__manual__">手动输入绝对路径…</option>
            </NativeSelect>
            <Input
              name="manual_project"
              data-manual-project
              placeholder="输入 Mac 上的绝对路径"
              hidden
            />
          </label>
          <IntakeEditor label="任务内容" />
          <details className="intake-options"><summary>其他设置</summary>          <label>
            类型
            <NativeSelect name="type" defaultValue="feature">
              <option value="feature">任务</option>
              <option value="bug">Bug</option>
            </NativeSelect>
          </label>
          <label>
            优先级
            <NativeSelect name="priority" defaultValue="P2">
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </NativeSelect>
          </label>
</details>
          <label className="inline-checkbox">
            <Checkbox name="auto_dispatch" defaultChecked />
            保存后自动定位并执行
          </label>
          </div>
          <div className="form-actions">
            <Button
              variant="outline"
              type="button"
              className="ghost"
              data-close
            >
              取消
            </Button>
            <Button type="submit" className="primary">
              创建并执行
            </Button>
          </div>
        </form>
      </Dialog>

      <div id="toast" className="toast" role="status" aria-live="polite"></div>
    </>
  );
}
