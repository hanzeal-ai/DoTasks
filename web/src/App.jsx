import { useEffect } from "react";

export default function App() {
  useEffect(() => {
    void import("./legacy-app.js");
  }, []);

  return (
    <>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="sidebar-brand">
            <img
              className="brand-mark"
              src="/codex-taskboard-mark.svg"
              alt=""
            />
            <span>Taskboard</span>
          </div>
          <nav className="sidebar-nav" aria-label="工作区导航">
            <button
              id="board-nav"
              className="sidebar-item selected"
              type="button"
            >
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M3.5 4.5h5v11h-5zm8 0h5v6h-5zm0 9h5v2h-5z" />
              </svg>
              <span>任务面板</span>
              <span id="board-count" className="sidebar-count">
                0
              </span>
            </button>
            <button id="token-panel" className="sidebar-item" type="button">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M3.5 14.5h3v2h-3zm5-5h3v7h-3zm5-6h3v13h-3z" />
              </svg>
              <span>Token 面板</span>
            </button>
            <section className="projects-section">
              <div className="sidebar-section-head">
                <span id="project-list-title">项目</span>
                <div className="sidebar-section-actions">
                  <button
                    id="project-archive-toggle"
                    className="sidebar-text-button"
                    type="button"
                  >
                    查看归档
                  </button>
                  <button
                    id="add-project"
                    className="sidebar-icon-button"
                    type="button"
                    title="加载文件夹"
                    aria-label="加载文件夹"
                  >
                    ＋
                  </button>
                </div>
              </div>
              <div id="project-list" className="project-list">
                <div className="sidebar-loading">正在同步 Codex 项目…</div>
              </div>
            </section>
          </nav>
          <div className="sidebar-footer">
            <span id="codex-sync-dot" className="sync-dot pending"></span>
            <span id="codex-sync-label">正在连接 Codex</span>
          </div>
        </aside>

        <main className="main-content">
          <header>
            <div className="header-heading">
              <p id="view-eyebrow" className="eyebrow">
                TASKBOARD
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
              <button id="dispatcher-toggle" className="ghost" type="button">
                调度状态
              </button>
              <button id="new-chat" className="primary" type="button" hidden>
                ＋ 新建会话
              </button>
              <button
                id="task-change-confirmations"
                className="primary change-confirmation-button"
                type="button"
                hidden
              >
                需求变更（<span id="task-change-count">0</span>）
              </button>
              <button id="attention-tasks" className="primary" type="button">
                待处理任务（<span id="attention-count">0</span>）
              </button>
              <button
                id="settings-button"
                className="ghost"
                type="button"
                aria-label="打开设置"
              >
                设置
              </button>
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
            <button type="button" className="icon-button" data-close>
              ×
            </button>
          </div>
          <div id="task-detail-content" className="detail-content"></div>
        </div>
      </dialog>

      <dialog id="attention-tasks-dialog" className="attention-dialog">
        <div className="detail-shell attention-shell">
          <div className="dialog-head">
            <div>
              <p className="eyebrow">ATTENTION</p>
              <h2>
                待处理任务（<span id="attention-dialog-count">0</span>）
              </h2>
            </div>
            <button type="button" className="icon-button" data-close>
              ×
            </button>
          </div>
          <div
            id="attention-tasks-content"
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
            <button type="button" className="icon-button" data-close>
              ×
            </button>
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

      <div id="toast" className="toast"></div>
    </>
  );
}
