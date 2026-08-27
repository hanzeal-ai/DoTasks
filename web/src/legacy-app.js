import {
  durationBetween,
  escapeHtml,
  expandTaskboardSlashCommand,
  formatCompactTokenCount,
  formatDuration,
  formatTimestamp,
  formatTokenCount,
  LatestRequest,
  projectLabel,
  relativeTime,
  requestJson,
  routeDelegatedEvent,
  selectTaskConversation,
  taskNeedsAttention,
  taskRecoveryAction,
} from "./ui-core.js";

const state = {
  board: null,
  settings: null,
  projects: [],
  archivedProjects: false,
  view: "board",
  tokenView: "charts",
  project: "",
  expandedProject: "",
  threads: [],
  selectedThread: "",
  thread: null,
  threadLoading: false,
  threadsLoading: false,
  sending: false,
  collapsedThreadGroups: new Set(),
};
const NAVIGATION_STORAGE_KEY = "codex-taskboard:navigation:v1";
let pendingNavigationRestore = false;
let integrationRequestId = 0;
let boardEventSource = null;
let refreshTimer = null;
let threadPollTimer = null;
let loadingBoard = false;
const threadsRequests = new LatestRequest();
const threadRequests = new LatestRequest();
let columns = [];
let statusLabels = {};
let tokenStages = [];
let attentionStatuses = new Set();
let attentionAutoDispatchStatuses = new Set();
let workflowLoaded = false;
const autoOpenedTaskChanges = new Set();

function taskStageTimer(task) {
  const terminal = ["done", "cancelled"].includes(task.status);
  const start = terminal ? task.created_at : (task.status_started_at || task.updated_at || task.created_at);
  const end = terminal ? (task.status_started_at || task.updated_at) : "";
  return `<time class="stage-timer" data-stage-start="${escapeHtml(start || "")}"${end ? ` data-stage-end="${escapeHtml(end)}"` : ""} title="${terminal ? "任务总用时" : "当前阶段执行时间"}">${terminal ? "总 " : ""}${formatDuration(durationBetween(start, end || Date.now()))}</time>`;
}

function refreshStageTimers() {
  document.querySelectorAll("[data-stage-start]").forEach(element => {
    const prefix = element.dataset.stageEnd ? "总 " : "";
    element.textContent = `${prefix}${formatDuration(durationBetween(element.dataset.stageStart, element.dataset.stageEnd || Date.now()))}`;
  });
}

function persistNavigationState() {
  try {
    sessionStorage.setItem(NAVIGATION_STORAGE_KEY, JSON.stringify({
      view: state.view,
      tokenView: state.tokenView,
      project: state.project,
      expandedProject: state.expandedProject,
      selectedThread: state.selectedThread,
      collapsedThreadGroups: [...state.collapsedThreadGroups],
    }));
  } catch (_error) {
    // Navigation persistence is best-effort; the taskboard remains usable without it.
  }
}

function restoreNavigationState() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(NAVIGATION_STORAGE_KEY) || "null");
    if (!saved) return;
    state.tokenView = saved.tokenView === "details" ? "details" : "charts";
    if (saved.view === "tokens") {
      state.view = "tokens";
      return;
    }
    if (saved.view === "settings") {
      state.view = "settings";
      return;
    }
    if (saved.view !== "project" || typeof saved.project !== "string" || !saved.project) return;
    state.view = "project";
    state.project = saved.project;
    state.expandedProject = typeof saved.expandedProject === "string" && saved.expandedProject
      ? saved.expandedProject
      : saved.project;
    state.selectedThread = typeof saved.selectedThread === "string" ? saved.selectedThread : "";
    state.collapsedThreadGroups = new Set(
      Array.isArray(saved.collapsedThreadGroups)
        ? saved.collapsedThreadGroups.filter(item => typeof item === "string")
        : [],
    );
    pendingNavigationRestore = true;
  } catch (_error) {
    // Ignore malformed or unavailable session storage.
  }
}

async function api(path, options = {}) {
  return requestJson(fetch, path, options);
}

async function loadWorkflow() {
  if (workflowLoaded) return;
  const workflow = await api("/api/workflow");
  if (!Array.isArray(workflow.columns) || !workflow.status_labels || !Array.isArray(workflow.token_stages)) {
    throw new Error("工作流元数据格式无效");
  }
  columns = workflow.columns;
  statusLabels = workflow.status_labels;
  tokenStages = workflow.token_stages;
  attentionStatuses = new Set(workflow.attention_statuses || []);
  attentionAutoDispatchStatuses = new Set(workflow.attention_auto_dispatch_statuses || []);
  workflowLoaded = true;
}

function toast(message) {
  const element = document.querySelector("#toast");
  element.textContent = message;
  element.classList.add("show");
  setTimeout(() => element.classList.remove("show"), 2400);
}

function setIntegrationStatus(id, status, title) {
  const element = document.querySelector(`#${id}`);
  element.className = `integration-status ${status}`;
  element.title = title;
}

async function syncIntegrationStatuses() {
  const requestId = ++integrationRequestId;
  setIntegrationStatus("obsidian-status", "pending", "正在检查 Obsidian");
  setIntegrationStatus("location-status", "pending", "正在检查代码定位");
  try {
    const project = state.view === "project" ? state.project : "";
    const integrations = await api(`/api/integrations${project ? `?project=${encodeURIComponent(project)}` : ""}`);
    if (requestId !== integrationRequestId) return;
    const obsidian = integrations.obsidian;
    setIntegrationStatus("obsidian-status", obsidian.exists ? "connected" : "disconnected", obsidian.exists ? `Obsidian 已连接：${obsidian.vault}` : `Obsidian 未连接：${obsidian.vault}`);
    const location = integrations.location;
    const stale = location.state === "stale";
    const status = stale ? "warning" : location.available ? "connected" : "disconnected";
    const title = !project ? "代码定位：请选择一个具体项目" : stale ? "代码定位证据已过期" : location.available ? `代码定位已完成：${location.summary || project}` : `代码定位未完成：${location.summary || location.reason || project}`;
    setIntegrationStatus("location-status", status, title);
  } catch (error) {
    if (requestId !== integrationRequestId) return;
    setIntegrationStatus("obsidian-status", "disconnected", `Obsidian 状态检查失败：${error.message}`);
    setIntegrationStatus("location-status", "disconnected", `代码定位状态检查失败：${error.message}`);
  }
}

function syncSidebar() {
  document.querySelector("#board-nav").classList.toggle("selected", state.view === "board");
  document.querySelector("#token-panel").classList.toggle("selected", state.view === "tokens");
  document.querySelector("#board-count").textContent = String(state.board?.tasks?.length || 0);
  const container = document.querySelector("#project-list");
  document.querySelector("#project-list-title").textContent = state.archivedProjects ? "已归档项目" : "项目";
  const archiveToggle = document.querySelector("#project-archive-toggle");
  archiveToggle.textContent = state.archivedProjects ? "返回项目" : "查看归档";
  archiveToggle.setAttribute("aria-pressed", String(state.archivedProjects));
  document.querySelector("#add-project").hidden = state.archivedProjects;
  container.innerHTML = state.projects.length ? state.projects.map(project => {
    const selected = state.view === "project" && state.project === project.path;
    const expanded = state.expandedProject === project.path;
    const initial = (project.name || "P").slice(0, 1);
    const threads = expanded ? `<div class="project-threads">${renderThreadList()}</div>` : "";
    const action = state.archivedProjects
      ? `<button class="project-action" type="button" data-restore-project="${escapeHtml(project.path)}" title="恢复项目" aria-label="恢复 ${escapeHtml(project.name)}">恢复</button>`
      : `<button class="project-action" type="button" data-archive-project="${escapeHtml(project.path)}" title="归档项目" aria-label="归档 ${escapeHtml(project.name)}">归档</button>`;
    const item = state.archivedProjects
      ? `<div class="project-item archived" title="${escapeHtml(project.path)}"><span class="project-glyph">${escapeHtml(initial)}</span><span class="project-item-text">${escapeHtml(project.name)}</span></div>`
      : `<button class="project-item${selected ? " selected" : ""}" type="button" data-project-path="${escapeHtml(project.path)}" title="${escapeHtml(project.path)}" aria-expanded="${expanded}"><span class="project-disclosure">›</span><span class="project-glyph">${escapeHtml(initial)}</span><span class="project-item-text">${escapeHtml(project.name)}</span>${project.thread_count ? `<span class="project-thread-count">${project.thread_count}</span>` : ""}</button>`;
    return `<div class="project-node"><div class="project-row">${item}${action}</div>${threads}</div>`;
  }).join("") : `<div class="sidebar-empty">${state.archivedProjects ? "暂无已归档项目" : "暂无项目，点击＋加载文件夹"}</div>`;
}

function syncHeader() {
  const projectView = state.view === "project";
  const tokenView = state.view === "tokens";
  const settingsView = state.view === "settings";
  const attentionCount = (state.board?.tasks || []).filter(task => taskNeedsAttention(
    task, attentionStatuses, attentionAutoDispatchStatuses,
  )).length;
  const taskChangeCount = (state.board?.pending_task_changes || []).length;
  document.querySelector("#view-eyebrow").textContent = projectView ? "PROJECT" : tokenView ? "TOKEN USAGE" : settingsView ? "SETTINGS" : "TASKBOARD";
  document.querySelector("#view-title").textContent = projectView ? projectLabel(state.project) : tokenView ? "Token 使用看板" : settingsView ? "设置" : "任务面板";
  document.querySelector("#view-title").title = projectView ? state.project : "";
  document.querySelector("#new-chat").hidden = !projectView;
  document.querySelector("#attention-tasks").hidden = projectView;
  document.querySelector("#attention-count").textContent = String(attentionCount);
  document.querySelector("#task-change-count").textContent = String(taskChangeCount);
  document.querySelector("#task-change-confirmations").hidden = projectView || taskChangeCount === 0;
  document.querySelector("#dispatcher-toggle").hidden = projectView;
  const settingsButton = document.querySelector("#settings-button");
  settingsButton.hidden = projectView;
  settingsButton.classList.toggle("selected", settingsView);
}

async function loadProjects() {
  const dot = document.querySelector("#codex-sync-dot");
  const label = document.querySelector("#codex-sync-label");
  dot.className = "sync-dot pending";
  label.textContent = "正在同步 Codex 项目";
  try {
    const result = await api(`/api/codex/projects${state.archivedProjects ? "?archived=true" : ""}`);
    state.projects = result.projects || [];
    dot.className = "sync-dot connected";
    label.textContent = "已同步 Codex 项目";
    if (pendingNavigationRestore) {
      pendingNavigationRestore = false;
      const projectExists = state.projects.some(item => item.path === state.project);
      if (projectExists) {
        await loadThreads(state.selectedThread);
      } else {
        state.view = "board";
        state.project = "";
        state.expandedProject = "";
        state.selectedThread = "";
        state.thread = null;
        persistNavigationState();
        render();
      }
    }
    syncSidebar();
  } catch (error) {
    dot.className = "sync-dot failed";
    label.textContent = "Codex 项目同步失败";
    syncSidebar();
    toast(error.message);
  }
}

async function load({refreshAuxiliary = true} = {}) {
  if (loadingBoard) return;
  loadingBoard = true;
  try {
    await loadWorkflow();
    state.board = await api("/api/board");
    if (state.view === "settings" && !state.settings) {
      state.settings = await api("/api/settings");
    }
    const dispatcher = state.board.dispatcher || {};
    document.querySelector("#health").textContent = dispatcher.enabled && dispatcher.running === false ? "本地服务已连接 · 调度未运行" : "本地服务已连接";
    document.querySelector("#health").title = dispatcher.last_error || "";
    document.querySelector("#health").classList.add("ok");
    const dispatcherToggle = document.querySelector("#dispatcher-toggle");
    dispatcherToggle.textContent = dispatcher.enabled ? "暂停调度" : "恢复调度";
    dispatcherToggle.dataset.action = dispatcher.enabled ? "pause" : "resume";
    syncSidebar();
    render();
    const pendingChange = state.board?.pending_task_changes?.[0];
    const changeDialog = document.querySelector("#task-change-dialog");
    if (pendingChange && !changeDialog.open && !autoOpenedTaskChanges.has(pendingChange.id)) {
      autoOpenedTaskChanges.add(pendingChange.id);
      renderTaskChangeConfirmations();
      changeDialog.showModal();
    }
    if (refreshAuxiliary) {
      void syncIntegrationStatuses();
      void loadProjects();
    }
  } catch (error) {
    document.querySelector("#health").textContent = "连接失败";
    toast(error.message);
  } finally {
    loadingBoard = false;
  }
}

function scheduleBoardRefresh() {
  clearTimeout(refreshTimer);
  // Token events can be frequent. Refresh the board data, but keep the much
  // slower Codex project scan and integration probes on navigation/actions.
  refreshTimer = setTimeout(() => void load({refreshAuxiliary: false}), 250);
}

function connectBoardEvents() {
  boardEventSource?.close();
  boardEventSource = new EventSource("/api/events/stream");
  boardEventSource.addEventListener("board_changed", scheduleBoardRefresh);
}

const conversationRoleLabels = {
  source: "需求会话", execution: "开发会话", rework: "返工会话", bugfix: "Bug 修复会话",
  review: "Review 会话", code_review: "Code Review 会话", acceptance: "验收会话",
};

function conversationIcon() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4.75h16v11.5H9.2L5.1 19.7v-3.45H4V4.75Zm2 2v7.5h1.1v1.15l1.4-1.15H18v-7.5H6Z"/></svg>';
}

function taskConversationControl(task) {
  const conversations = Array.isArray(task.conversations) ? [...task.conversations].reverse() : [];
  if (task.status === "done") {
    const options = conversations.map(item => {
      const role = conversationRoleLabels[item.role] || item.role || "会话";
      const title = item.title && item.title !== role ? ` · ${item.title}` : "";
      return `<option value="${escapeHtml(item.thread_id)}">${escapeHtml(role + title)}</option>`;
    }).join("");
    return `<label class="conversation-select${options ? "" : " disabled"}" title="选择要查看的会话">${conversationIcon()}<select data-thread-select aria-label="选择要查看的会话"${options ? "" : " disabled"}><option value="">${options ? "选择会话" : "暂无会话"}</option>${options}</select></label>`;
  }
  const conversation = selectTaskConversation(task);
  if (!conversation) return `<button class="small conversation-button" type="button" aria-label="暂无可查看会话" title="暂无可查看会话" disabled>${conversationIcon()}</button>`;
  const label = conversationRoleLabels[conversation.role] || "会话";
  return `<button class="small conversation-button" type="button" data-open-thread="${escapeHtml(conversation.thread_id)}" aria-label="查看${escapeHtml(label)}" title="查看${escapeHtml(label)}">${conversationIcon()}</button>`;
}

function taskRecoveryButton(task, options = {}) {
  const action = taskRecoveryAction(task, options);
  if (!action) return "";
  const taskId = escapeHtml(task.id);
  const label = escapeHtml(action.label);
  if (action.kind === "resume") {
    return `<button class="small" type="button" data-resume-task="${taskId}">${label}</button>`;
  }
  if (action.kind === "enable_auto") {
    return `<button class="small" type="button" data-enable-auto="${taskId}">${label}</button>`;
  }
  return `<button class="small" type="button" data-transition="${taskId}" data-status="${escapeHtml(action.status)}">${label}</button>`;
}

function taskCard(task) {
  let actionButton = taskRecoveryButton(task);
  if (task.status === "waiting_confirmation") actionButton = task.codex_thread_id ? `<button class="small" data-open-thread="${escapeHtml(task.codex_thread_id)}">去确认</button>` : `<button class="small" data-details="${task.id}">查看确认信息</button>`;
  const budgetToken = Number(task.effective_token_used ?? task.token_used) || 0;
  const token = task.token_budget ? Math.round((budgetToken / task.token_budget) * 100) : 0;
  const conflictTag = task.target_conflicts?.length ? `<span class="tag conflict">冲突 ${task.target_conflicts.length}</span>` : "";
  const projectBlockerTag = task.project_blockers?.length ? `<span class="tag conflict">项目阻塞 ${task.project_blockers.length}</span>` : "";
  const reviewFailedTag = task.review_failed_at && !["done", "cancelled"].includes(task.status) ? '<span class="tag conflict">验收不达标</span>' : "";
  const dispatchPausedLabels = {ready: "自动领取已暂停", rework: "自动返工已暂停", review: "自动验收已暂停", code_review: "自动 Code Review 已暂停", acceptance: "自动功能验收已暂停"};
  const dispatchPausedTag = dispatchPausedLabels[task.status] && !task.auto_dispatch ? `<span class="tag conflict">${dispatchPausedLabels[task.status]}</span>` : "";
  const attentionReason = task.status === "rework" && (task.last_review_reasons?.length || task.last_failure_reason) ? `<p class="status-reason">返工原因：${escapeHtml((task.last_review_reasons || []).join("；") || task.last_failure_reason)}</p>` : task.status === "acceptance_blocked" && task.last_failure_reason ? `<p class="status-reason">验收失败原因：${escapeHtml(task.last_failure_reason)}</p>` : ["waiting_confirmation", "blocked", "failed"].includes(task.status) && task.last_failure_reason ? `<p class="status-reason">${task.status === "failed" ? "失败" : task.status === "blocked" ? "阻塞" : "待确认"}原因：${escapeHtml(task.last_failure_reason)}</p>` : "";
  const bugTag = task.type === "bug" ? `<span class="tag conflict">BUG${task.parent_acceptance_task_id ? ` · 来源 ${escapeHtml(task.parent_acceptance_task_id)}` : ""}</span>` : "";
  return `<article class="card"><div class="card-top"><span>${task.id}</span><span class="card-stage"><span>${escapeHtml(statusLabels[task.status] || task.status)}</span>${taskStageTimer(task)}</span></div><h3 title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</h3><p>${escapeHtml(task.goal || "尚未补充任务目标")}</p>${attentionReason}<div class="card-meta"><span class="tag priority-${task.priority}">${task.priority}</span>${bugTag}${(task.modules || []).slice(0,2).map(module => `<span class="tag">${escapeHtml(module)}</span>`).join("")}${reviewFailedTag}${dispatchPausedTag}${conflictTag}${projectBlockerTag}<span class="tag">Token ${token}%</span></div><div class="card-actions">${actionButton}${taskConversationControl(task)}<button class="small" data-context="${task.id}">查看上下文</button></div></article>`;
}

function traceSection(title, items, renderItem) {
  return `<section class="trace-section"><h3>${title}</h3>${items.length ? `<div class="trace-list">${items.map(renderItem).join("")}</div>` : '<div class="trace-empty">暂无记录</div>'}</section>`;
}

async function showTaskDetails(taskId) {
  const details = await api(`/api/tasks/${taskId}/details`);
  document.querySelector("#detail-title").textContent = `${details.task.id} · ${details.task.title}`;
  const conversations = traceSection("关联会话", details.conversations, item => `<article class="trace-item"><div><strong>${escapeHtml(item.title || item.role)}</strong><span>${escapeHtml(item.role)} · ${escapeHtml(item.status)}</span></div><code>${escapeHtml(item.thread_id)}</code>${item.run_ids?.length ? `<p>关联运行：${item.run_ids.map(escapeHtml).join("、")}</p>` : ""}${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}<div class="trace-actions"><button class="small" data-open-thread="${escapeHtml(item.thread_id)}">在会话区打开</button><button class="small" data-copy-thread="${escapeHtml(item.thread_id)}">复制 ID</button></div></article>`);
  const runs = traceSection("执行轮次", details.runs, item => {
    const startedAt = item.started_at || item.created_at;
    const endedAt = item.stage_completed_at || (["awaiting_thread", "running"].includes(item.status) ? Date.now() : item.updated_at);
    const stage = tokenStages.find(entry => entry.key === item.run_type)?.label || item.run_type;
    return `<article class="trace-item"><div><strong>${escapeHtml(item.id)} · ${escapeHtml(stage)}</strong><span>${escapeHtml(item.status)}</span></div><div class="run-metrics"><span>Token ${formatTokenCount(item.token_used)}</span><span>用时 ${formatDuration(durationBetween(startedAt, endedAt))}</span></div>${item.conversation_thread_id ? `<code>${escapeHtml(item.conversation_thread_id)}</code>` : ""}${item.delivery_summary ? `<p>${escapeHtml(item.delivery_summary)}</p>` : ""}</article>`;
  });
  const relations = traceSection("任务关系", details.relations, item => `<article class="trace-item"><div><strong>${escapeHtml(item.relation_type)}</strong><span>${escapeHtml(item.direction)}</span></div><p>${escapeHtml(item.source_task_id)} → ${escapeHtml(item.target_task_id)}</p></article>`);
  const targets = traceSection("定位目标", details.task.location_context?.targets || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.file)}</strong><span>${escapeHtml((item.symbols || []).join(", ") || "文件级")}</span></div><p>${escapeHtml(item.reason || "")}</p></article>`);
  const conflicts = traceSection("目标冲突", details.task.target_conflicts || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.task_id)} · ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.status)}</span></div><p>${item.targets.map(target => `${escapeHtml(target.file)}${target.symbol ? `#${escapeHtml(target.symbol)}` : ""}`).join("；")}</p></article>`);
  const projectBlockers = traceSection("项目级阻塞", details.task.project_blockers || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.task_id)} · ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.blocker_type)}</span></div><p>${escapeHtml(item.last_failure_reason || statusLabels[item.status] || item.status)}</p></article>`);
  const reviews = traceSection("验收记录", details.reviews || [], item => `<article class="trace-item"><div><strong>第 ${item.round} 轮 · ${escapeHtml(item.verdict)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml((item.reasons || []).join("；") || "验收通过")}</p></article>`);
  const acceptanceResults = traceSection("功能验收结果", details.acceptance_results || [], item => `<article class="trace-item"><div><strong>第 ${item.round} 轮 · ${escapeHtml(item.verdict)}</strong><span>${escapeHtml(item.created_at)}</span></div>${item.reasons?.length ? `<p>原因：${escapeHtml(item.reasons.join("；"))}</p>` : ""}${item.failed_criteria?.length ? `<p>失败标准：${escapeHtml(item.failed_criteria.join("；"))}</p>` : ""}${item.created_bug_task_id ? `<p>已创建 Bug：<button class="small" data-details="${escapeHtml(item.created_bug_task_id)}">${escapeHtml(item.created_bug_task_id)}</button></p>` : ""}</article>`);
  const acceptanceChecks = traceSection("自动化验收", details.acceptance_checks || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.criterion)}</strong><span>${escapeHtml(item.status)} · ${item.duration_ms}ms</span></div><code>${escapeHtml(item.command)}</code>${item.output ? `<p>${escapeHtml(item.output)}</p>` : ""}</article>`);
  const revisions = traceSection("需求修订", details.revisions || [], item => `<article class="trace-item"><div><strong>v${item.version} · ${escapeHtml(item.after_snapshot?.title || details.task.title)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml(item.reason || "需求已调整")}</p></article>`);
  const events = traceSection("异常与状态记录", (details.events || []).filter(item => ["execution_failed", "review_interrupted", "review_preparation_failed", "context_build_failed", "lease_expired"].includes(item.event_type) || (item.event_type === "transitioned" && item.payload?.reason)), item => `<article class="trace-item"><div><strong>${escapeHtml(item.event_type)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml(item.payload?.reason || item.payload?.error || "")}</p></article>`);
  document.querySelector("#task-detail-content").innerHTML = projectBlockers + conflicts + targets + revisions + reviews + acceptanceResults + acceptanceChecks + events + conversations + runs + relations;
  document.querySelector("#task-detail-dialog").showModal();
}

function renderBoard() {
  const tasks = state.board?.tasks || [];
  const taskColumns = columns.map(column => {
    const visible = tasks.filter(task => column.statuses.includes(task.status));
    return `<section class="column column-${column.key}"><div class="column-head">${column.title}<span>${visible.length}</span></div><div class="cards">${visible.length ? visible.map(taskCard).join("") : '<div class="empty">暂无任务</div>'}</div></section>`;
  }).join("");
  document.querySelector("#content").innerHTML = `<div class="board">${taskColumns}</div>`;
  refreshStageTimers();
}

function renderTokenTrend(daily) {
  const values = daily.map(item => Math.max(0, Number(item.token_used) || 0));
  const maximum = Math.max(1, ...values);
  const width = 760;
  const height = 230;
  const left = 56;
  const right = 14;
  const top = 16;
  const bottom = 34;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const slot = plotWidth / Math.max(1, daily.length);
  const barWidth = Math.max(3, Math.min(18, slot * .62));
  const grid = [0, .25, .5, .75, 1].map(ratio => {
    const y = top + plotHeight * (1 - ratio);
    return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" /><text x="${left - 8}" y="${y + 4}" text-anchor="end">${escapeHtml(formatCompactTokenCount(maximum * ratio))}</text>`;
  }).join("");
  const labelStep = Math.max(1, Math.ceil(daily.length / 6));
  const bars = daily.map((item, index) => {
    const value = values[index];
    const barHeight = value ? Math.max(2, value / maximum * plotHeight) : 0;
    const x = left + index * slot + (slot - barWidth) / 2;
    const y = top + plotHeight - barHeight;
    const showLabel = index % labelStep === 0 || index === daily.length - 1;
    return `<g><title>${escapeHtml(item.label)}：${formatTokenCount(value)} Token</title><rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="2" />${showLabel ? `<text x="${(x + barWidth / 2).toFixed(2)}" y="${height - 10}" text-anchor="middle">${escapeHtml(item.label.replace("月", "/").replace("日", ""))}</text>` : ""}</g>`;
  }).join("");
  return daily.length ? `<svg class="token-trend" viewBox="0 0 ${width} ${height}" role="img" aria-label="本月每日 Token 使用趋势"><g class="token-grid">${grid}</g><g class="token-bars">${bars}</g></svg>` : '<div class="trace-empty">本月暂无 Token 记录</div>';
}

function renderTokenBarList(items, emptyMessage) {
  if (!items.length) return `<div class="trace-empty">${escapeHtml(emptyMessage)}</div>`;
  const maximum = Math.max(1, ...items.map(item => item.value));
  return `<div class="token-bar-list">${items.map((item, index) => `<div class="token-bar-row"><span class="token-bar-rank">${index + 1}</span><div class="token-bar-label" title="${escapeHtml(item.title || item.label)}"><strong>${escapeHtml(item.label)}</strong>${item.meta ? `<small>${escapeHtml(item.meta)}</small>` : ""}</div><div class="token-bar-track"><span style="width:${Math.max(1.5, item.value / maximum * 100).toFixed(2)}%"></span></div><strong class="token-bar-value">${formatTokenCount(item.value)}</strong></div>`).join("")}</div>`;
}

function renderTokenCharts(tasks) {
  const analytics = state.board?.token_analytics || {periods:{}, daily:[]};
  const periods = analytics.periods || {};
  const taskUsage = tasks.filter(task => Number(task.token_used) > 0).sort((a, b) => Number(b.token_used) - Number(a.token_used));
  const topTasks = taskUsage.slice(0, 10).map(task => ({
    label: task.title,
    meta: task.id,
    title: `${task.id} · ${task.title}`,
    value: Number(task.token_used) || 0,
  }));
  const projectTotals = new Map();
  tasks.forEach(task => {
    const project = String(task.project || "未归属项目");
    projectTotals.set(project, (projectTotals.get(project) || 0) + (Number(task.token_used) || 0));
  });
  const projects = [...projectTotals.entries()].filter(([, value]) => value > 0).sort((a, b) => b[1] - a[1]).map(([project, value]) => ({
    label: projectLabel(project),
    meta: project,
    title: project,
    value,
  }));
  return `<div class="token-summary token-period-summary"><article><span>当天 Token</span><strong>${formatTokenCount(periods.today)}</strong></article><article><span>本周 Token</span><strong>${formatTokenCount(periods.week)}</strong></article><article><span>本月 Token</span><strong>${formatTokenCount(periods.month)}</strong></article></div><div class="token-chart-grid"><article class="token-chart-card token-trend-card"><div class="token-chart-head"><div><span>使用趋势</span><h2>本月每日 Token</h2></div><small>按 Token 增量记录时间统计</small></div>${renderTokenTrend(analytics.daily || [])}</article><article class="token-chart-card"><div class="token-chart-head"><div><span>任务排行</span><h2>${taskUsage.length > 10 ? "Token 消耗前 10 任务" : "各任务 Token 使用量"}</h2></div><small>${taskUsage.length > 10 ? `共 ${taskUsage.length} 个已记录任务` : "按总使用量降序"}</small></div>${renderTokenBarList(topTasks, "暂无任务 Token 记录")}</article><article class="token-chart-card"><div class="token-chart-head"><div><span>项目分布</span><h2>各项目 Token 使用量</h2></div><small>按任务所属项目汇总</small></div>${renderTokenBarList(projects, "暂无项目 Token 记录")}</article></div>`;
}

function renderTokenDetails(tasks) {
  const total = tasks.reduce((sum, task) => sum + (Number(task.token_used) || 0), 0);
  const active = tasks.filter(task => !["done", "cancelled"].includes(task.status)).length;
  const stageMetrics = tasks.flatMap(task => Object.values(task.token_by_stage || {}));
  const totalInput = stageMetrics.reduce((sum, metric) => sum + (Number(metric.input_tokens) || 0), 0);
  const totalCachedInput = stageMetrics.reduce((sum, metric) => sum + (Number(metric.cached_input_tokens) || 0), 0);
  const totalOutput = stageMetrics.reduce((sum, metric) => sum + (Number(metric.output_tokens) || 0), 0);
  const headers = tokenStages.map(stage => `<th>${escapeHtml(stage.label)}</th>`).join("");
  const rows = tasks.map(task => {
    const cells = tokenStages.map(stage => {
      const metric = task.token_by_stage?.[stage.key];
      if (!metric?.token_used) return '<td class="token-empty">—</td>';
      const detail = `输入 ${formatTokenCount(metric.input_tokens)} · 缓存 ${formatTokenCount(metric.cached_input_tokens)} · 输出 ${formatTokenCount(metric.output_tokens)} · 有效 ${formatTokenCount(metric.effective_token_used)}`;
      return `<td title="${escapeHtml(detail)}"><strong>${formatTokenCount(metric.token_used)}</strong><small>${metric.attempts} 轮</small><small>${escapeHtml(detail)}</small></td>`;
    }).join("");
    const effective = Number(task.effective_token_used ?? task.token_used) || 0;
    return `<tr><th><button class="token-task-link" data-details="${escapeHtml(task.id)}"><span>${escapeHtml(task.id)}</span><strong>${escapeHtml(task.title)}</strong></button></th>${cells}<td class="token-total"><strong>${formatTokenCount(task.token_used)}</strong><small>有效 ${formatTokenCount(effective)} · 预算 ${Math.round(effective / Math.max(1, Number(task.token_budget) || 1) * 100)}%</small></td></tr>`;
  }).join("");
  return `<div class="token-summary"><article><span>全部任务 Token</span><strong>${formatTokenCount(total)}</strong></article><article><span>输入 Token</span><strong>${formatTokenCount(totalInput)}</strong></article><article><span>缓存输入</span><strong>${formatTokenCount(totalCachedInput)}</strong></article><article><span>输出 Token</span><strong>${formatTokenCount(totalOutput)}</strong></article><article><span>已记录任务</span><strong>${tasks.filter(task => Number(task.token_used) > 0).length}</strong></article><article><span>进行中任务</span><strong>${active}</strong></article></div>${tasks.length ? `<div class="token-table-wrap"><table class="token-table"><thead><tr><th>任务</th>${headers}<th>总计</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="trace-empty">暂无任务 Token 记录</div>'}`;
}

function renderTokenPanel() {
  const tasks = state.board?.tasks || [];
  const viewSwitch = `<div class="token-view-switch" role="tablist" aria-label="Token 面板展示方式"><button type="button" role="tab" data-token-view="charts" aria-selected="${state.tokenView === "charts"}" class="${state.tokenView === "charts" ? "selected" : ""}">图表展示</button><button type="button" role="tab" data-token-view="details" aria-selected="${state.tokenView === "details"}" class="${state.tokenView === "details" ? "selected" : ""}">明细展示</button></div>`;
  const content = state.tokenView === "details" ? renderTokenDetails(tasks) : renderTokenCharts(tasks);
  document.querySelector("#content").innerHTML = `<section class="token-page"><div class="token-page-content">${viewSwitch}${content}</div></section>`;
}

function attentionTaskMessage(task) {
  if (task.last_failure_reason) return task.last_failure_reason;
  if (attentionAutoDispatchStatuses.has(task.status) && Number(task.auto_dispatch) === 0) {
    return "自动调度已暂停，需要人工确认后继续当前阶段。";
  }
  return ({
    waiting_confirmation: "任务正在等待用户确认。",
    failed: "任务执行失败，等待检查或重新执行。",
    blocked: "任务当前被阻塞，等待解除阻塞条件。",
    paused: "任务已暂停，等待恢复。",
    cancelled: "任务已取消。",
  })[task.status] || "暂无待处理说明。";
}

function renderAttentionTasks() {
  const tasks = (state.board?.tasks || []).filter(task => taskNeedsAttention(
    task, attentionStatuses, attentionAutoDispatchStatuses,
  ));
  document.querySelector("#attention-dialog-count").textContent = String(tasks.length);
  document.querySelector("#attention-tasks-content").innerHTML = tasks.length ? `<div class="attention-list">${tasks.map(task => {
    const taskId = escapeHtml(task.id);
    const confirmationButton = task.status === "waiting_confirmation" && task.codex_thread_id
      ? `<button class="small" type="button" data-open-thread="${escapeHtml(task.codex_thread_id)}">去确认</button>`
      : "";
    return `<article class="attention-task"><div class="attention-task-head"><div class="attention-task-title"><span>${taskId}</span><strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong></div><span class="attention-task-status">${escapeHtml(statusLabels[task.status] || task.status)}</span></div><p><strong>待处理信息：</strong>${escapeHtml(attentionTaskMessage(task))}</p><div class="attention-task-meta"><span>${escapeHtml(task.priority || "")}</span><span>更新于 ${escapeHtml(formatTimestamp(task.updated_at || task.created_at))}</span><div class="attention-task-actions">${confirmationButton}${taskRecoveryButton(task, {restartWaitingConfirmation: true})}<button class="small danger" type="button" data-cancel-task="${taskId}">取消任务</button><button class="small" type="button" data-details="${taskId}">查看详情</button></div></div></article>`;
  }).join("")}</div>` : '<div class="trace-empty">当前没有待处理任务</div>';
}

function renderTaskChangeConfirmations() {
  const changes = state.board?.pending_task_changes || [];
  document.querySelector("#task-change-dialog-count").textContent = String(changes.length);
  document.querySelector("#task-change-content").innerHTML = changes.length ? `<div class="attention-list">${changes.map(change => {
    const proposed = change.proposed_task || {};
    const reasons = change.evidence?.reasons || change.evidence?.candidate?.reasons || [];
    return `<article class="attention-task task-change-card"><div class="attention-task-head"><div class="attention-task-title"><span>${escapeHtml(change.id)}</span><strong>${escapeHtml(change.candidate_task_id)} · ${escapeHtml(change.candidate_title || "当前任务")}</strong></div><span class="attention-task-status">${escapeHtml(statusLabels[change.candidate_status] || change.candidate_status || "执行中")}</span></div><div class="change-request-copy"><span>新需求</span><p>${escapeHtml(change.request_text)}</p></div><div class="change-proposal-copy"><span>任务将调整为</span><strong>${escapeHtml(proposed.title || change.candidate_title || "")}</strong><p>${escapeHtml(proposed.goal || "")}</p></div>${reasons.length ? `<p class="change-match-reason">匹配依据：${escapeHtml(reasons.join("；"))}</p>` : ""}<div class="change-choice-actions"><button class="primary" type="button" data-resolve-task-change="${escapeHtml(change.id)}" data-decision="revise">修订 ${escapeHtml(change.candidate_task_id)}</button><button class="ghost" type="button" data-resolve-task-change="${escapeHtml(change.id)}" data-decision="create_new">创建新任务</button></div>${change.error ? `<p class="status-reason">上次处理失败：${escapeHtml(change.error)}</p>` : ""}</article>`;
  }).join("")}</div>` : '<div class="trace-empty">当前没有待确认的需求变更</div>';
}

function threadMessages(thread) {
  const messages = [];
  for (const turn of thread?.turns || []) {
    for (const item of turn.items || []) {
      if (item.type === "userMessage") {
        const text = (item.content || []).filter(part => part.type === "text").map(part => part.text || "").join("\n").trim();
        if (text) messages.push({role: "user", text, phase: ""});
      } else if (item.type === "agentMessage" && item.text) {
        messages.push({role: "agent", text: item.text, phase: item.phase || ""});
      }
    }
  }
  return messages;
}

function renderThreadList() {
  if (state.threadsLoading) return '<div class="sidebar-loading">正在加载会话…</div>';
  if (!state.threads.length) return '<div class="sidebar-empty">这个项目还没有会话</div>';
  const groups = new Map();
  for (const thread of state.threads) {
    const name = thread.groupName || `${projectLabel(state.project)}-其他会话`;
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(thread);
  }
  return Array.from(groups.entries()).map(([name, threads]) => {
    const collapsed = state.collapsedThreadGroups.has(name);
    const active = threads.some(thread => thread.status?.type === "active");
    const items = collapsed ? "" : `<div class="thread-group-threads">${threads.map(thread => {
      const selected = thread.id === state.selectedThread;
      const status = thread.status?.type || "notLoaded";
      const displayName = thread.displayName || thread.name || thread.preview || "未命名会话";
      return `<button class="project-thread-item${selected ? " selected" : ""}" type="button" data-thread-id="${escapeHtml(thread.id)}" title="${escapeHtml(displayName)}"><span class="thread-status ${status}"></span><span class="project-thread-title">${escapeHtml(displayName)}</span><span class="project-thread-time">${escapeHtml(relativeTime(thread.updatedAt))}</span></button>`;
    }).join("")}</div>`;
    return `<section class="thread-group"><button class="thread-group-item" type="button" data-thread-group="${escapeHtml(name)}" aria-expanded="${!collapsed}" title="${escapeHtml(name)}"><span class="thread-group-disclosure">›</span><span class="thread-folder${active ? " active" : ""}">▰</span><span class="thread-group-title">${escapeHtml(name)}</span><span class="thread-group-count">${threads.length}</span></button>${items}</section>`;
  }).join("");
}

function renderChat() {
  if (state.threadLoading) return '<div class="chat-loading">正在读取会话…</div>';
  if (!state.selectedThread || !state.thread) return '<div class="conversation-empty"><div><strong>选择或创建一个会话</strong><p>这里会显示当前项目的 Codex 会话，包括 Taskboard 自动启动的执行记录。</p></div></div>';
  const messages = threadMessages(state.thread);
  const active = state.thread.status?.type === "active";
  const unavailable = active || state.sending;
  const body = messages.length ? messages.map(message => `<article class="message ${message.role}${message.phase === "commentary" ? " commentary" : ""}"><span class="message-role">${message.role === "user" ? "你" : message.phase === "commentary" ? "Codex · 进度" : "Codex"}</span><div class="message-text">${escapeHtml(message.text)}</div></article>`).join("") : '<div class="conversation-empty"><div><strong>新会话已就绪</strong><p>发送第一条消息，Codex 将以当前项目文件夹作为工作目录。</p></div></div>';
  const placeholder = active ? "Codex 正在运行…" : "给 Codex 发送消息，输入 /taskboard 调用任务看板";
  return `<div class="chat-head"><span class="chat-title">${escapeHtml(state.thread.displayName || state.thread.name || state.thread.preview || "未命名会话")}</span><div class="chat-actions"><span class="chat-path" title="${escapeHtml(state.project)}">${escapeHtml(state.project)}</span></div></div><div id="messages" class="messages">${body}</div><div class="composer-wrap"><form id="composer" class="composer"><textarea id="message-input" placeholder="${placeholder}" ${unavailable ? "disabled" : ""}></textarea><button class="send-button" type="submit" title="发送" ${unavailable ? "disabled" : ""}>↑</button></form></div>`;
}

function renderProject() {
  document.querySelector("#content").innerHTML = `<section class="chat-pane project-chat-pane">${renderChat()}</section>`;
  requestAnimationFrame(() => {
    const messages = document.querySelector("#messages");
    if (messages) messages.scrollTop = messages.scrollHeight;
  });
}

function renderSettings() {
  const budget = Number(state.settings?.task_token_budget) || 60000;
  document.querySelector("#content").innerHTML = `<section class="settings-page"><form id="task-settings-form" class="settings-panel"><div class="settings-panel-head"><div><p class="eyebrow">TASK DEFAULTS</p><h2>任务设置</h2></div></div><label class="settings-field" for="task-token-budget"><span>任务 Token 预算</span><input id="task-token-budget" name="task_token_budget" type="number" min="1" step="1" value="${budget}" required /><small>用于新建任务的有效 Token 上限；达到预算后任务将暂停并等待确认。已创建任务不受影响。</small></label><div class="settings-actions"><button class="primary" type="submit">保存设置</button></div></form></section>`;
}

function render() {
  syncSidebar();
  syncHeader();
  if (state.view === "project") renderProject();
  else if (state.view === "tokens") renderTokenPanel();
  else if (state.view === "settings") renderSettings();
  else renderBoard();
  if (document.querySelector("#attention-tasks-dialog")?.open) renderAttentionTasks();
  if (document.querySelector("#task-change-dialog")?.open) renderTaskChangeConfirmations();
}

function scheduleThreadPoll() {
  clearTimeout(threadPollTimer);
  if (state.view !== "project" || !state.selectedThread || state.thread?.status?.type !== "active") return;
  threadPollTimer = setTimeout(() => void loadThread(state.selectedThread, true), 1500);
}

function invalidateThreadRequests() {
  threadsRequests.invalidate();
  threadRequests.invalidate();
  state.threadsLoading = false;
  state.threadLoading = false;
}

async function loadThread(threadId, quiet = false) {
  if (!threadId || state.view !== "project") return;
  const project = state.project;
  const scope = `${project}\0${threadId}`;
  const ticket = threadRequests.begin(scope);
  const isCurrent = () => (
    threadRequests.isCurrent(ticket, `${state.project}\0${state.selectedThread}`)
    && state.view === "project"
  );
  if (!quiet) state.threadLoading = true;
  renderProject();
  try {
    const result = await api(`/api/codex/threads/${encodeURIComponent(threadId)}`);
    if (!isCurrent()) return;
    state.thread = result.thread;
    const summary = state.threads.find(item => item.id === threadId);
    if (summary) summary.status = result.thread.status;
  } catch (error) {
    if (isCurrent()) toast(error.message);
  } finally {
    if (isCurrent()) {
      state.threadLoading = false;
      renderProject();
      scheduleThreadPoll();
    }
  }
}

async function loadThreads(preferredThread = "") {
  if (!state.project) return;
  const project = state.project;
  const ticket = threadsRequests.begin(project);
  const isCurrent = () => (
    threadsRequests.isCurrent(ticket, state.project)
    && state.view === "project"
  );
  state.threadsLoading = true;
  render();
  try {
    const result = await api(`/api/codex/threads?project=${encodeURIComponent(project)}`);
    if (!isCurrent()) return;
    state.threads = result.threads || [];
    const requested = preferredThread || state.selectedThread;
    state.selectedThread = state.threads.some(thread => thread.id === requested) ? requested : (state.threads[0]?.id || "");
    persistNavigationState();
  } catch (error) {
    if (isCurrent()) toast(error.message);
  } finally {
    if (isCurrent()) {
      state.threadsLoading = false;
      render();
    }
  }
  if (isCurrent() && state.selectedThread) await loadThread(state.selectedThread);
}

async function openProject(project, preferredThread = "") {
  clearTimeout(threadPollTimer);
  // In-flight responses belong to the previous project and must never mutate the new view.
  invalidateThreadRequests();
  state.view = "project";
  state.project = project;
  state.expandedProject = project;
  state.threads = [];
  state.selectedThread = preferredThread;
  state.thread = null;
  persistNavigationState();
  render();
  void syncIntegrationStatuses();
  await loadThreads(preferredThread);
}

async function openThreadInWorkspace(threadId) {
  try {
    const result = await api(`/api/codex/threads/${encodeURIComponent(threadId)}`);
    const project = result.thread.cwd;
    if (!project) throw new Error("该会话没有项目工作目录");
    document.querySelector("#task-detail-dialog").close();
    await loadProjects();
    await openProject(project, threadId);
  } catch (error) {
    toast(error.message);
  }
}

async function createChat() {
  if (!state.project) return;
  try {
    const result = await api("/api/codex/threads", {method:"POST", body:JSON.stringify({project:state.project, title:"新会话"})});
    await loadThreads(result.thread.id);
    document.querySelector("#message-input")?.focus();
  } catch (error) {
    toast(error.message);
  }
}

async function handleViewNavigation(event) {
  if (event.target.closest("#project-archive-toggle")) {
    state.archivedProjects = !state.archivedProjects;
    await loadProjects();
    return true;
  }
  const archiveProject = event.target.closest("[data-archive-project]");
  if (archiveProject) {
    const path = archiveProject.dataset.archiveProject;
    if (!window.confirm(`确定归档项目 ${projectLabel(path)} 吗？项目数据不会被删除。`)) return true;
    await api("/api/codex/projects/archive", {method:"POST", body:JSON.stringify({path})});
    if (state.project === path) {
      clearTimeout(threadPollTimer);
      invalidateThreadRequests();
      state.view = "board";
      state.project = "";
      state.expandedProject = "";
      state.threads = [];
      state.selectedThread = "";
      state.thread = null;
      persistNavigationState();
      render();
      void syncIntegrationStatuses();
    }
    await loadProjects();
    toast("项目已归档");
    return true;
  }
  const restoreProject = event.target.closest("[data-restore-project]");
  if (restoreProject) {
    await api("/api/codex/projects/restore", {method:"POST", body:JSON.stringify({path:restoreProject.dataset.restoreProject})});
    state.archivedProjects = false;
    await loadProjects();
    toast("项目已恢复");
    return true;
  }
  if (event.target.closest("#board-nav")) {
    clearTimeout(threadPollTimer);
    invalidateThreadRequests();
    state.view = "board";
    state.thread = null;
    persistNavigationState();
    render();
    void syncIntegrationStatuses();
    return true;
  }
  const projectItem = event.target.closest("[data-project-path]");
  if (projectItem) {
    if (state.expandedProject === projectItem.dataset.projectPath) {
      state.expandedProject = "";
      persistNavigationState();
      syncSidebar();
      return true;
    }
    await openProject(projectItem.dataset.projectPath);
    return true;
  }
  if (event.target.closest("#add-project")) {
    const button = document.querySelector("#add-project");
    button.disabled = true;
    try {
      const result = await api("/api/codex/projects/pick", {method:"POST", body:"{}"});
      if (!result.cancelled && result.project) {
        await loadProjects();
        if (result.project.archived) toast("该项目仍在归档中，可在归档视图恢复");
        else await openProject(result.project.path);
      }
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
    return true;
  }
  if (event.target.closest("#new-chat, [data-new-chat]")) {
    await createChat();
    return true;
  }
  if (event.target.closest("#token-panel")) {
    clearTimeout(threadPollTimer);
    invalidateThreadRequests();
    state.view = "tokens";
    state.thread = null;
    persistNavigationState();
    render();
    void syncIntegrationStatuses();
    return true;
  }
  if (event.target.closest("#settings-button")) {
    clearTimeout(threadPollTimer);
    invalidateThreadRequests();
    state.view = "settings";
    state.thread = null;
    persistNavigationState();
    if (!state.settings) state.settings = await api("/api/settings");
    render();
    void syncIntegrationStatuses();
    return true;
  }
  const tokenView = event.target.closest("[data-token-view]");
  if (tokenView) {
    state.tokenView = tokenView.dataset.tokenView === "details" ? "details" : "charts";
    persistNavigationState();
    renderTokenPanel();
    return true;
  }
  if (event.target.closest("#attention-tasks")) {
    renderAttentionTasks();
    document.querySelector("#attention-tasks-dialog").showModal();
    return true;
  }
  if (event.target.closest("#task-change-confirmations")) {
    renderTaskChangeConfirmations();
    document.querySelector("#task-change-dialog").showModal();
    return true;
  }
  return false;
}

async function handleThreadNavigation(event) {
  const threadItem = event.target.closest("[data-thread-id]");
  if (threadItem) {
    state.selectedThread = threadItem.dataset.threadId;
    state.thread = null;
    persistNavigationState();
    syncSidebar();
    await loadThread(state.selectedThread);
    return true;
  }
  const threadGroup = event.target.closest("[data-thread-group]");
  if (threadGroup) {
    const name = threadGroup.dataset.threadGroup;
    if (state.collapsedThreadGroups.has(name)) state.collapsedThreadGroups.delete(name);
    else state.collapsedThreadGroups.add(name);
    persistNavigationState();
    syncSidebar();
    return true;
  }
  return false;
}

async function handleTaskAction(event) {
  const dispatcherToggle = event.target.closest("#dispatcher-toggle");
  if (dispatcherToggle) {
    const action = dispatcherToggle.dataset.action;
    try {
      await api(`/api/dispatcher/${action}`, {method:"POST", body:JSON.stringify({reason:"用户从任务看板暂停"})});
      await load();
      toast(action === "pause" ? "所有任务与调度已暂停" : "调度已恢复；暂停任务需单独恢复");
    } catch (error) { toast(error.message); }
    return true;
  }
  if (event.target.matches("[data-close]")) {
    event.target.closest("dialog").close();
    return true;
  }
  const transition = event.target.closest("[data-transition]");
  if (transition) {
    const taskId = transition.dataset.transition;
    const task = state.board?.tasks?.find(item => item.id === taskId);
    const updates = {auto_dispatch: true};
    const tokenBudget = Number(task?.token_budget) || 0;
    const effectiveTokenUsed = Number(task?.effective_token_used ?? task?.token_used) || 0;
    if (task?.status === "waiting_confirmation" && tokenBudget > 0 && effectiveTokenUsed >= tokenBudget) {
      const nextBudget = Math.max(tokenBudget * 2, Math.ceil((effectiveTokenUsed + 1000) / 1000) * 1000);
      const confirmed = window.confirm(`当前有效 Token 已用 ${formatTokenCount(effectiveTokenUsed)} / ${formatTokenCount(tokenBudget)}。重新开始会将预算上限提高到 ${formatTokenCount(nextBudget)}，是否继续？`);
      if (!confirmed) return true;
      updates.token_budget = nextBudget;
    }
    try { await api(`/api/tasks/${taskId}/transition`, {method:"POST", body:JSON.stringify({status:transition.dataset.status, updates})}); await load(); toast("任务已重新开始"); } catch (error) { toast(error.message); }
    return true;
  }
  const cancelTask = event.target.closest("[data-cancel-task]");
  if (cancelTask) {
    const taskId = cancelTask.dataset.cancelTask;
    const task = state.board?.tasks?.find(item => item.id === taskId);
    const taskLabel = task ? `${task.id}《${task.title}》` : taskId;
    if (!window.confirm(`确定取消任务 ${taskLabel} 吗？取消后不可恢复。`)) return true;
    try {
      await api(`/api/tasks/${taskId}/transition`, {
        method: "POST",
        body: JSON.stringify({status: "cancelled", reason: "用户从待处理任务中取消"}),
      });
      await load();
      toast("任务已取消");
    } catch (error) { toast(error.message); }
    return true;
  }
  const resumeTask = event.target.closest("[data-resume-task]");
  if (resumeTask) {
    try { await api(`/api/tasks/${resumeTask.dataset.resumeTask}/resume`, {method:"POST", body:"{}"}); await load(); toast("任务已恢复"); } catch (error) { toast(error.message); }
    return true;
  }
  const resolveTaskChange = event.target.closest("[data-resolve-task-change]");
  if (resolveTaskChange) {
    const changeId = resolveTaskChange.dataset.resolveTaskChange;
    const decision = resolveTaskChange.dataset.decision;
    const dialog = document.querySelector("#task-change-dialog");
    dialog.querySelectorAll("button").forEach(button => { button.disabled = true; });
    try {
      const result = await api(`/api/task-changes/${changeId}/resolve`, {
        method: "POST", body: JSON.stringify({decision}),
      });
      await load();
      if (!(state.board?.pending_task_changes || []).length) dialog.close();
      else renderTaskChangeConfirmations();
      toast(decision === "revise" ? `${result.task?.id || result.result_task_id} 已转入返工` : `${result.task?.id || result.result_task_id} 已创建`);
    } catch (error) {
      toast(error.message);
      renderTaskChangeConfirmations();
    } finally {
      dialog.querySelectorAll("button").forEach(button => { button.disabled = false; });
    }
    return true;
  }
  const enableAuto = event.target.closest("[data-enable-auto]");
  if (enableAuto) {
    const task = state.board.tasks.find(item => item.id === enableAuto.dataset.enableAuto);
    try { await api(`/api/tasks/${enableAuto.dataset.enableAuto}/transition`, {method:"POST", body:JSON.stringify({status:task.status, updates:{auto_dispatch:true}})}); await load(); toast(({review:"已恢复自动验收", code_review:"已继续 Code Review", acceptance:"已继续功能验收", rework:"已继续自动返工"})[task.status] || "已恢复自动调度"); } catch (error) { toast(error.message); }
    return true;
  }
  const context = event.target.closest("[data-context]");
  if (context) {
    try { const result = await api(`/api/tasks/${context.dataset.context}/context`); await navigator.clipboard.writeText(JSON.stringify(result, null, 2)); toast("最小上下文已复制"); } catch (error) { toast(error.message); }
    return true;
  }
  const details = event.target.closest("[data-details]");
  if (details) {
    try {
      event.target.closest("#attention-tasks-dialog")?.close();
      await showTaskDetails(details.dataset.details);
    } catch (error) { toast(error.message); }
    return true;
  }
  const copyThread = event.target.closest("[data-copy-thread]");
  if (copyThread) {
    await navigator.clipboard.writeText(copyThread.dataset.copyThread);
    toast("会话 ID 已复制");
    return true;
  }
  const openThread = event.target.closest("[data-open-thread]");
  if (openThread) {
    await openThreadInWorkspace(openThread.dataset.openThread);
    return true;
  }
  return false;
}

document.addEventListener("click", async event => {
  try {
    await routeDelegatedEvent(event, [
      handleViewNavigation,
      handleThreadNavigation,
      handleTaskAction,
    ]);
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("change", async event => {
  const select = event.target.closest("[data-thread-select]");
  if (!select?.value) return;
  const threadId = select.value;
  select.value = "";
  await openThreadInWorkspace(threadId);
});

document.addEventListener("submit", async event => {
  if (event.target.id === "task-settings-form") {
    event.preventDefault();
    const input = document.querySelector("#task-token-budget");
    const taskTokenBudget = Number(input.value);
    if (!Number.isInteger(taskTokenBudget) || taskTokenBudget <= 0) {
      input.setCustomValidity("请输入大于 0 的整数");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      state.settings = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({task_token_budget: taskTokenBudget}),
      });
      renderSettings();
      toast("设置已保存，新建任务将使用新的 Token 预算");
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
    return;
  }
  if (event.target.id !== "composer") return;
  event.preventDefault();
  const input = document.querySelector("#message-input");
  const message = expandTaskboardSlashCommand(input.value);
  if (!message || !state.selectedThread || state.sending) return;
  state.sending = true;
  renderProject();
  try {
    await api(`/api/codex/threads/${encodeURIComponent(state.selectedThread)}/turns`, {method:"POST", body:JSON.stringify({message})});
    await loadThreads(state.selectedThread);
  } catch (error) {
    toast(error.message);
  } finally {
    state.sending = false;
    renderProject();
    scheduleThreadPoll();
  }
});

document.addEventListener("keydown", event => {
  if (event.target.id === "message-input" && event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    event.target.closest("form").requestSubmit();
  }
});

restoreNavigationState();
void load().then(connectBoardEvents);
const stageTimerInterval = setInterval(refreshStageTimers, 1000);
window.addEventListener("beforeunload", () => {
  boardEventSource?.close();
  clearTimeout(threadPollTimer);
  clearInterval(stageTimerInterval);
});
