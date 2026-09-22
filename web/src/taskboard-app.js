import { openDialog, closeDialog, isDialogOpen } from "./components/taskboard-dialog";
import { setContent } from "./ui-markup.js";
import {
  durationBetween,
  escapeHtml,
  executionLogLabel,
  executionLogReason,
  formatCompactTokenCount,
  formatDuration,
  formatLogTimestamp,
  formatMetricTokenCount,
  formatTimestamp,
  formatTokenCount,
  projectLabel,
  requestJson,
  routeDelegatedEvent,
  selectTaskConversation,
  taskNeedsAttention,
  taskRecoveryAction,
} from "./ui-core.js";

const state = {
  board: null,
  settings: null,
  view: "board",
  tokenView: "charts",
};
const NAVIGATION_STORAGE_KEY = "dotasks:navigation:v1";
let boardEventSource = null;
let refreshTimer = null;
let loadingBoard = false;
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
    if (saved.view === "logs") {
      state.view = "logs";
      return;
    }
    if (saved.view === "tokens") {
      state.view = "tokens";
      return;
    }
    if (saved.view === "requirements") {
      state.view = "requirements";
      return;
    }
    if (saved.view === "settings") {
      state.view = "settings";
      return;
    }
  } catch (_error) {
    // Ignore malformed or unavailable session storage.
  }
}

async function api(path, options = {}) {
  return requestJson(async (...args) => {
    const response = await fetch(...args);
    if (response.status === 401) {
      boardEventSource?.close();
      window.location.replace("/login");
    }
    return response;
  }, path, options);
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

function syncSidebar() {
  document.querySelector("#execution-log-nav").classList.toggle("selected", state.view === "logs");
  document.querySelector("#board-nav").classList.toggle("selected", state.view === "board");
  document.querySelector("#requirements-nav").classList.toggle("selected", state.view === "requirements");
  document.querySelector("#token-panel").classList.toggle("selected", state.view === "tokens");
}

function executionLogs() {
  return (state.board?.execution_logs || [])
    .filter(item => item.event_type !== "token_usage_updated")
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

function renderExecutionLog() {
  const logs = executionLogs();
  setContent("#content", '<section id="execution-log-list" class="execution-log-list" aria-label="执行日志"></section>');
  setContent("#execution-log-list", logs.length
    ? logs.map(item => {
      const reason = executionLogReason(item);
      const problem = /failed|blocked|expired|interrupted|exhausted|stopped/.test(item.event_type)
        || item.payload?.verdict === "fail"
        || ["blocked", "failed"].includes(item.payload?.to);
      const reasonKind = item.event_type.includes("blocked") || item.payload?.to === "blocked"
        ? "阻塞原因"
        : problem ? "失败原因" : "原因";
      const threadId = item.thread_id || "—";
      const taskExists = (state.board?.tasks || []).some(task => task.id === item.task_id);
      const taskLabel = taskExists
        ? `<button type="button" data-details="${escapeHtml(item.task_id)}" title="查看任务详情">${escapeHtml(item.task_id)}</button>`
        : `<b>${escapeHtml(item.task_id)}</b>`;
      return `<article class="execution-log-item${problem ? " problem" : ""}"><div class="execution-log-line"><time>[${escapeHtml(formatLogTimestamp(item.created_at))}]</time>${taskLabel}<strong>${escapeHtml(executionLogLabel(item, statusLabels))}</strong>${item.thread_id ? `<button type="button" class="execution-log-thread" data-open-thread="${escapeHtml(item.thread_id)}" title="打开会话 ${escapeHtml(item.thread_id)}">${escapeHtml(threadId)}</button>` : `<code title="此时尚未创建会话">${escapeHtml(threadId)}</code>`}</div>${reason ? `<p><span>${reasonKind}：</span>${escapeHtml(reason)}</p>` : ""}</article>`;
    }).join("")
    : '<p class="execution-log-empty">暂无任务执行日志</p>');
}

function syncHeader() {
  const tokenView = state.view === "tokens";
  const requirementsView = state.view === "requirements";
  const settingsView = state.view === "settings";
  const completedCount = (state.board?.tasks || []).filter(task => task.status === "done").length;
  const taskChangeCount = (state.board?.pending_task_changes || []).length;
  document.querySelector("#view-title").textContent = state.view === "logs" ? "执行日志" : tokenView ? "Token 使用看板" : requirementsView ? "需求看板" : settingsView ? "设置" : "任务面板";
  document.querySelector("#view-title").title = "";
  document.querySelector("#completed-tasks").hidden = false;
  document.querySelector("#completed-count").textContent = String(completedCount);
  document.querySelector("#task-change-count").textContent = String(taskChangeCount);
  document.querySelector("#task-change-confirmations").hidden = taskChangeCount === 0;
  document.querySelector("#dispatcher-toggle").hidden = false;
  document.querySelector("#new-task-button").hidden = state.view !== "board";
  document.querySelector("#new-requirement-button").hidden = !requirementsView;
}

async function load() {
  if (loadingBoard) return;
  loadingBoard = true;
  try {
    await loadWorkflow();
    state.board = await api("/api/board");
    if (state.view === "settings" && !state.settings) {
      state.settings = await api("/api/settings");
    }
    const dispatcher = state.board.dispatcher || {};
    const dispatcherToggle = document.querySelector("#dispatcher-toggle");
    dispatcherToggle.textContent = dispatcher.enabled ? "暂停调度" : "恢复调度";
    dispatcherToggle.dataset.action = dispatcher.enabled ? "pause" : "resume";
    syncSidebar();
    render();
    const pendingChange = state.board?.pending_task_changes?.[0];
    if (pendingChange && !isDialogOpen("task-change-dialog") && !autoOpenedTaskChanges.has(pendingChange.id)) {
      autoOpenedTaskChanges.add(pendingChange.id);
      openDialog("task-change-dialog");
      renderTaskChangeConfirmations();
    }
  } catch (error) {
    toast(error.message);
  } finally {
    loadingBoard = false;
  }
}

function scheduleBoardRefresh() {
  clearTimeout(refreshTimer);
  // Coalesce frequent token updates into a single board refresh.
  refreshTimer = setTimeout(() => void load(), 250);
}

function connectBoardEvents() {
  boardEventSource?.close();
  boardEventSource = new EventSource("/api/events/stream");
  boardEventSource.addEventListener("board_changed", scheduleBoardRefresh);
  boardEventSource.addEventListener("error", async () => {
    try {
      const auth = await api("/api/auth/status");
      if (!auth.authenticated) {
        boardEventSource?.close();
        window.location.replace("/login");
      }
    } catch (_error) {
    }
  });
}

const conversationRoleLabels = {
  source: "需求会话", execution: "开发会话", rework: "返工会话", bugfix: "Bug 修复会话",
  review: "Review 会话", code_review: "Code Review 会话", acceptance: "验收会话",
};

function conversationIcon() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4.75h16v11.5H9.2L5.1 19.7v-3.45H4V4.75Zm2 2v7.5h1.1v1.15l1.4-1.15H18v-7.5H6Z"/></svg>';
}

function deleteIcon() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8l.75 2H20v2H4V6h3.25L8 4Zm-1 6h2v8h6v-8h2v10H7V10Zm3 0h2v6h-2v-6Zm3 0h2v6h-2v-6Z"/></svg>';
}

function taskConversationControl(task) {
  const conversations = Array.isArray(task.conversations) ? [...task.conversations].reverse() : [];
  if (task.status === "done") {
    const options = conversations.map(item => {
      const role = conversationRoleLabels[item.role] || item.role || "会话";
      const title = item.title && item.title !== role ? ` · ${item.title}` : "";
      return `<option value="${escapeHtml(item.thread_id)}">${escapeHtml(role + title)}</option>`;
    }).join("");
    return `<label class="conversation-select${options ? "" : " disabled"}" title="选择要打开的会话">${conversationIcon()}<select data-thread-select aria-label="选择要打开的会话"${options ? "" : " disabled"}><option value="">${options ? "选择会话" : "暂无会话"}</option>${options}</select></label>`;
  }
  const conversation = selectTaskConversation(task);
  if (!conversation) return `<button class="small conversation-button" type="button" aria-label="暂无可查看会话" title="暂无可查看会话" disabled>${conversationIcon()}</button>`;
  const label = conversationRoleLabels[conversation.role] || "会话";
  return `<button class="small conversation-button" type="button" data-open-thread="${escapeHtml(conversation.thread_id)}" aria-label="打开${escapeHtml(label)}" title="在 Codex 中打开${escapeHtml(label)}">${conversationIcon()}</button>`;
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

function taskLogButton(task) {
  return task.status === "failed" || task.last_failure_reason || task.last_review_reasons?.length || task.dispatch_blockers?.length
    ? `<button class="small" type="button" data-task-logs="${escapeHtml(task.id)}">查看日志</button>` : "";
}

function taskCard(task) {
  const needsAttention = taskNeedsAttention(task, attentionStatuses, attentionAutoDispatchStatuses);
  const actionButton = taskRecoveryButton(task, {restartWaitingConfirmation: needsAttention});
  const cancelButton = needsAttention || task.status === "draft" ? `<button class="small danger" type="button" data-cancel-task="${escapeHtml(task.id)}">取消任务</button>` : "";
  const contextButton = task.status === "draft" ? "" : `<button class="small" data-context="${task.id}">查看上下文</button>`;
  const budgetToken = Number(task.effective_token_used ?? task.token_used) || 0;
  const token = task.token_budget ? Math.round((budgetToken / task.token_budget) * 100) : 0;
  const conflictTag = task.target_conflicts?.length ? `<span class="tag conflict">冲突 ${task.target_conflicts.length}</span>` : "";
  const dispatchBlockerTag = task.dispatch_blockers?.length ? `<span class="tag conflict">等待调度</span>` : "";
  const reviewFailedTag = task.review_failed_at && !["done", "cancelled"].includes(task.status) ? '<span class="tag conflict">验收不达标</span>' : "";
  const dispatchPausedLabels = {ready: "自动领取已暂停", rework: "自动返工已暂停", code_review: "自动 Code Review 已暂停"};
  const dispatchPausedTag = dispatchPausedLabels[task.status] && !task.auto_dispatch ? `<span class="tag conflict">${dispatchPausedLabels[task.status]}</span>` : "";
  const logButton = taskLogButton(task);
  const bugTag = task.type === "bug" ? `<span class="tag conflict">BUG</span>` : "";
  return `<article class="card"><div class="card-top"><span>${task.id}</span><span class="card-stage"><span>${escapeHtml(statusLabels[task.status] || task.status)}</span>${taskStageTimer(task)}</span></div><h3 title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</h3><p>${escapeHtml(task.goal || "")}</p><div class="card-meta"><span class="tag priority-${task.priority}">${task.priority}</span>${bugTag}${(task.modules || []).slice(0,2).map(module => `<span class="tag">${escapeHtml(module)}</span>`).join("")}${reviewFailedTag}${dispatchPausedTag}${dispatchBlockerTag}${conflictTag}<span class="tag">Token ${token}%</span></div><div class="card-actions">${actionButton}${cancelButton}${logButton}${taskConversationControl(task)}${contextButton}</div></article>`;
}

const requirementStatusLabels = {
  ready: "待拆解",
  decomposing: "拆解中",
  decomposed: "已拆解",
  failed: "拆解失败",
  cancelled: "已取消",
};

function requirementTaskItem(task) {
  const typeTag = task.type === "bug" ? '<span class="tag conflict">BUG</span>' : "";
  return `<article class="requirement-task"><div class="requirement-task-main"><div class="requirement-task-heading"><span>${escapeHtml(task.id)}</span><strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong></div><p>${escapeHtml(task.goal || "")}</p><div class="card-meta">${typeTag}<span class="tag priority-${escapeHtml(task.priority)}">${escapeHtml(task.priority)}</span>${(task.modules || []).slice(0, 2).map(module => `<span class="tag">${escapeHtml(module)}</span>`).join("")}</div></div><div class="requirement-task-side">${taskLogButton(task)}<span class="requirement-task-status">${escapeHtml(statusLabels[task.status] || task.status)}</span><button class="small" type="button" data-context="${escapeHtml(task.id)}">查看上下文</button></div></article>`;
}

function requirementConversationControl(requirement) {
  const threadId = String(requirement.codex_thread_id || "").trim();
  if (!threadId) return `<button class="small conversation-button" type="button" aria-label="暂无可查看拆解会话" title="暂无可查看拆解会话" disabled>${conversationIcon()}</button>`;
  return `<button class="small conversation-button" type="button" data-open-thread="${escapeHtml(threadId)}" aria-label="打开需求拆解会话" title="在 Codex 中打开需求拆解会话">${conversationIcon()}</button>`;
}

function requirementCard(requirement, tasks) {
  const status = requirementStatusLabels[requirement.status] || requirement.status;
  const summary = requirement.goal || requirement.description || requirement.original_content || "";
  const childTasks = tasks
    .filter(task => task.requirement_id === requirement.id)
    .sort((left, right) => String(left.requirement_task_key || left.created_at || left.id).localeCompare(String(right.requirement_task_key || right.created_at || right.id)));
  const failure = requirement.last_decomposition_error
    ? `<p class="status-reason">拆解异常：${escapeHtml(requirement.last_decomposition_error)}</p>`
    : "";
  const autoDispatchTag = !requirement.auto_dispatch && requirement.status === "ready"
    ? '<span class="tag conflict">自动拆解已暂停</span>'
    : "";
  const replaceableStatuses = new Set(["draft", "ready", "failed", "cancelled"]);
  const canRedecompose = Boolean(requirement.project)
    && requirement.status !== "decomposing"
    && childTasks.every(task => replaceableStatuses.has(task.status));
  const redecomposeReason = !requirement.project
    ? "请先为需求选择项目"
    : requirement.status === "decomposing"
      ? "需求正在拆解中"
      : canRedecompose
        ? "重新拆解需求"
        : "已有拆分任务进入执行，不能重新拆解";
  const requirementActions = `<button class="small" type="button" data-redecompose-requirement="${escapeHtml(requirement.id)}" title="${redecomposeReason}"${canRedecompose ? "" : " disabled"}>重新拆解</button><button class="small danger" type="button" data-delete-requirement="${escapeHtml(requirement.id)}">删除</button>`;
  return `<article class="requirement-card"><div class="requirement-summary"><div class="card-top"><span>${escapeHtml(requirement.id)}</span><span>${escapeHtml(status)}</span></div><h2 title="${escapeHtml(requirement.title)}">${escapeHtml(requirement.title)}</h2><p>${escapeHtml(summary)}</p>${failure}<div class="card-meta"><span class="tag priority-${escapeHtml(requirement.priority)}">${escapeHtml(requirement.priority)}</span>${requirement.project ? `<span class="tag">${escapeHtml(projectLabel(requirement.project))}</span>` : ""}${(requirement.modules || []).slice(0, 2).map(module => `<span class="tag">${escapeHtml(module)}</span>`).join("")}${autoDispatchTag}<span class="tag">拆解 ${Number(requirement.decomposition_attempts) || 0} 次</span>${requirementConversationControl(requirement)}${requirementActions}</div></div><section class="requirement-tasks"><div class="requirement-tasks-head"><h3>拆分任务</h3><span>${childTasks.length}</span></div><div class="requirement-task-list">${childTasks.length ? childTasks.map(requirementTaskItem).join("") : '<div class="empty requirement-task-empty">尚未拆分任务</div>'}</div></section></article>`;
}

function traceSection(title, items, renderItem) {
  return items.length ? `<section class="trace-section"><h3>${title}</h3><div class="trace-list">${items.map(renderItem).join("")}</div></section>` : "";
}

async function showTaskLogs(taskId) {
  const details = await api(`/api/tasks/${encodeURIComponent(taskId)}/details`);
  openDialog("task-detail-dialog");
  document.querySelector("#detail-title").textContent = `${details.task.id} · 执行日志`;
  const currentReason = details.task.last_failure_reason || (details.task.last_review_reasons || []).join("；");
  const reason = currentReason ? `<section class="trace-section"><h3>当前异常</h3><p class="status-reason">${escapeHtml(currentReason)}</p></section>` : "";
  const blockers = (details.task.dispatch_blockers || []).length
    ? traceSection("调度等待原因", details.task.dispatch_blockers, item => `<p>${escapeHtml(item.message)}</p>`) : "";
  const events = [...(details.events || [])].reverse();
  setContent("#task-detail-content", (reason + blockers + traceSection("执行日志", events, item => `<article class="trace-item"><div><strong>${escapeHtml(executionLogLabel(item, statusLabels))}</strong><time>${escapeHtml(formatLogTimestamp(item.created_at))}</time></div><p>${escapeHtml(executionLogReason(item))}</p></article>`)) || '<div class="trace-empty">暂无执行日志</div>');
}

async function showTaskDetails(taskId) {
  const details = await api(`/api/tasks/${taskId}/details`);
  openDialog("task-detail-dialog");
  document.querySelector("#detail-title").textContent = `${details.task.id} · ${details.task.title}`;
  const conversations = traceSection("Codex 原生任务", details.conversations, item => `<article class="trace-item"><div><strong>${escapeHtml(item.title || item.role)}</strong><span>${escapeHtml(item.role)} · ${escapeHtml(item.status)}</span></div><code>${escapeHtml(item.thread_id)}</code>${item.run_ids?.length ? `<p>关联运行：${item.run_ids.map(escapeHtml).join("、")}</p>` : ""}${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}<div class="trace-actions"><button class="small" data-copy-thread="${escapeHtml(item.thread_id)}">复制原生任务 ID</button></div></article>`);
  const runs = traceSection("执行轮次", details.runs, item => {
    const startedAt = item.started_at || item.created_at;
    const endedAt = item.stage_completed_at || (["awaiting_thread", "running"].includes(item.status) ? Date.now() : item.updated_at);
    const stage = tokenStages.find(entry => entry.key === item.run_type)?.label || item.run_type;
    return `<article class="trace-item"><div><strong>${escapeHtml(item.id)} · ${escapeHtml(stage)}</strong><span>${escapeHtml(item.status)}</span></div><div class="run-metrics"><span>Token ${formatTokenCount(item.token_used)}</span><span>用时 ${formatDuration(durationBetween(startedAt, endedAt))}</span></div>${item.conversation_thread_id ? `<code>${escapeHtml(item.conversation_thread_id)}</code>` : ""}${item.delivery_summary ? `<p>${escapeHtml(item.delivery_summary)}</p>` : ""}</article>`;
  });
  const relations = traceSection("任务关系", details.relations, item => `<article class="trace-item"><div><strong>${escapeHtml(item.relation_type)}</strong><span>${escapeHtml(item.direction)}</span></div><p>${escapeHtml(item.source_task_id)} → ${escapeHtml(item.target_task_id)}</p></article>`);
  const targets = traceSection("定位目标", details.task.location_context?.targets || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.file)}</strong><span>${escapeHtml((item.symbols || []).join(", ") || "文件级")}</span></div><p>${escapeHtml(item.reason || "")}</p></article>`);
  const conflicts = traceSection("目标冲突", details.task.target_conflicts || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.task_id)} · ${escapeHtml(item.title)}</strong><span>${escapeHtml(item.status)}</span></div><p>${item.targets.map(target => `${escapeHtml(target.file)}${target.symbol ? `#${escapeHtml(target.symbol)}` : ""}`).join("；")}</p></article>`);
  const dispatchBlockers = traceSection("调度等待原因", details.task.dispatch_blockers || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.message)}</strong><span>${escapeHtml(item.code)}</span></div></article>`);
  const reviews = traceSection("Code Review 记录", details.reviews || [], item => `<article class="trace-item"><div><strong>第 ${item.round} 轮 · ${escapeHtml(item.verdict)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml((item.reasons || []).join("；") || "Code Review 通过")}</p></article>`);
  const acceptanceChecks = traceSection("历史验收检查", details.acceptance_checks || [], item => `<article class="trace-item"><div><strong>${escapeHtml(item.criterion)}</strong><span>${escapeHtml(item.status)} · ${item.duration_ms}ms</span></div><code>${escapeHtml(item.command)}</code>${item.output ? `<p>${escapeHtml(item.output)}</p>` : ""}</article>`);
  const revisions = traceSection("需求修订", details.revisions || [], item => `<article class="trace-item"><div><strong>v${item.version} · ${escapeHtml(item.after_snapshot?.title || details.task.title)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml(item.reason || "需求已调整")}</p></article>`);
  const events = traceSection("异常与状态记录", (details.events || []).filter(item => ["execution_failed", "review_interrupted", "review_preparation_failed", "context_build_failed", "lease_expired"].includes(item.event_type) || (item.event_type === "transitioned" && item.payload?.reason)), item => `<article class="trace-item"><div><strong>${escapeHtml(item.event_type)}</strong><span>${escapeHtml(item.created_at)}</span></div><p>${escapeHtml(item.payload?.reason || item.payload?.error || "")}</p></article>`);
  setContent("#task-detail-content", (dispatchBlockers + conflicts + targets + revisions + reviews + acceptanceChecks + events + conversations + runs + relations) || '<div class="trace-empty">暂无任务记录</div>');
}

function renderBoard() {
  const tasks = state.board?.tasks || [];
  const taskColumns = columns.map(column => {
    const visible = column.key === "attention"
      ? tasks.filter(task => taskNeedsAttention(task, attentionStatuses, attentionAutoDispatchStatuses))
      : tasks.filter(task => column.statuses.includes(task.status));
    return `<section class="column column-${column.key}"><div class="column-head">${column.title}<span>${visible.length}</span></div><div class="cards">${visible.length ? visible.map(taskCard).join("") : '<div class="empty">暂无任务</div>'}</div></section>`;
  }).join("");
  setContent("#content", `<div class="board">${taskColumns}</div>`);
  refreshStageTimers();
}

function renderRequirementsBoard() {
  const requirements = state.board?.requirements || [];
  const tasks = state.board?.tasks || [];
  setContent("#content", `<div class="requirements-board">${requirements.length ? requirements.map(requirement => requirementCard(requirement, tasks)).join("") : '<div class="requirements-empty"><strong>暂无需求</strong></div>'}</div>`);
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
    meta: project === projectLabel(project) ? "" : project,
    title: project,
    value,
  }));
  return `<div class="token-summary token-period-summary"><article><span>当天 Token</span><strong>${formatMetricTokenCount(periods.today)}</strong></article><article><span>本周 Token</span><strong>${formatMetricTokenCount(periods.week)}</strong></article><article><span>本月 Token</span><strong>${formatMetricTokenCount(periods.month)}</strong></article></div><div class="token-chart-grid"><article class="token-chart-card token-trend-card"><div class="token-chart-head"><div><h2>本月每日 Token</h2></div><small>按 Token 增量记录时间统计</small></div>${renderTokenTrend(analytics.daily || [])}</article><article class="token-chart-card"><div class="token-chart-head"><div><h2>${taskUsage.length > 10 ? "Token 消耗前 10 任务" : "各任务 Token 使用量"}</h2></div><small>${taskUsage.length > 10 ? `共 ${taskUsage.length} 个已记录任务` : "按总使用量降序"}</small></div>${renderTokenBarList(topTasks, "暂无任务 Token 记录")}</article><article class="token-chart-card"><div class="token-chart-head"><div><h2>各项目 Token 使用量</h2></div><small>按任务所属项目汇总</small></div>${renderTokenBarList(projects, "暂无项目 Token 记录")}</article></div>`;
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
  setContent("#content", `<section class="token-page"><div class="token-page-content">${viewSwitch}${content}</div></section>`);
}

function renderCompletedTasks() {
  const tasks = (state.board?.tasks || []).filter(task => task.status === "done");
  document.querySelector("#completed-dialog-count").textContent = String(tasks.length);
  setContent("#completed-tasks-content", tasks.length ? `<div class="attention-list">${tasks.map(task => {
    const taskId = escapeHtml(task.id);
    return `<article class="attention-task completed-task"><div class="attention-task-head"><div class="attention-task-title"><span>${taskId}</span><strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong></div><span class="attention-task-status">${escapeHtml(statusLabels[task.status] || task.status)}</span></div><p>${escapeHtml(task.delivery_summary || task.goal || "任务已完成")}</p><div class="attention-task-meta"><span>${escapeHtml(task.priority || "")}</span><span>完成于 ${escapeHtml(formatTimestamp(task.updated_at || task.created_at))}</span><div class="attention-task-actions">${taskConversationControl(task)}<button class="small" type="button" data-details="${taskId}">查看详情</button><button class="small conversation-button danger" type="button" data-delete-completed-task="${taskId}" aria-label="删除完成任务" title="删除完成任务">${deleteIcon()}</button></div></div></article>`;
  }).join("")}</div>` : '<div class="trace-empty">当前没有完成任务</div>');
}

function renderTaskChangeConfirmations() {
  const changes = state.board?.pending_task_changes || [];
  document.querySelector("#task-change-dialog-count").textContent = String(changes.length);
  setContent("#task-change-content", changes.length ? `<div class="attention-list">${changes.map(change => {
    const proposed = change.proposed_task || {};
    const reasons = change.evidence?.reasons || change.evidence?.candidate?.reasons || [];
    return `<article class="attention-task task-change-card"><div class="attention-task-head"><div class="attention-task-title"><span>${escapeHtml(change.id)}</span><strong>${escapeHtml(change.candidate_task_id)} · ${escapeHtml(change.candidate_title || "当前任务")}</strong></div><span class="attention-task-status">${escapeHtml(statusLabels[change.candidate_status] || change.candidate_status || "执行中")}</span></div><div class="change-request-copy"><span>新需求</span><p>${escapeHtml(change.request_text)}</p></div><div class="change-proposal-copy"><span>任务将调整为</span><strong>${escapeHtml(proposed.title || change.candidate_title || "")}</strong><p>${escapeHtml(proposed.goal || "")}</p></div>${reasons.length ? `<p class="change-match-reason">匹配依据：${escapeHtml(reasons.join("；"))}</p>` : ""}<div class="change-choice-actions"><button class="primary" type="button" data-resolve-task-change="${escapeHtml(change.id)}" data-decision="revise">修订 ${escapeHtml(change.candidate_task_id)}</button><button class="ghost" type="button" data-resolve-task-change="${escapeHtml(change.id)}" data-decision="create_new">创建新任务</button></div>${change.error ? `<p class="status-reason">上次处理失败：${escapeHtml(change.error)}</p>` : ""}</article>`;
  }).join("")}</div>` : '<div class="trace-empty">当前没有待确认的需求变更</div>');
}

function renderSettings() {
  const budget = Number(state.settings?.task_token_budget) || 60000;
  const maxAppendedTasks = Number.isInteger(Number(state.settings?.max_batch_appended_tasks)) ? Number(state.settings.max_batch_appended_tasks) : 3;
  const parallelEnabled = Boolean(state.settings?.parallel_development_enabled);
  const maxParallelDevelopment = Number.isInteger(Number(state.settings?.max_parallel_development)) ? Number(state.settings.max_parallel_development) : 2;
  setContent("#content", `<section class="settings-page"><form id="task-settings-form" class="settings-panel"><div class="settings-panel-head"><div><h2>任务设置</h2></div></div><label class="settings-field" for="task-token-budget"><span>任务 Token 预算</span><input id="task-token-budget" name="task_token_budget" type="number" min="1" step="1" value="${budget}" required /><small>仅影响新任务；达到预算后暂停并等待确认。</small></label><label class="settings-field" for="max-batch-appended-tasks"><span>批次最多追加任务数</span><input id="max-batch-appended-tasks" name="max_batch_appended_tasks" type="number" min="0" max="20" step="1" value="${maxAppendedTasks}" required /><small>不含初始任务；并行开发时不自动追加。</small></label><label class="settings-field settings-toggle" for="parallel-development-enabled"><span>开启并行开发</span><input id="parallel-development-enabled" name="parallel_development_enabled" type="checkbox" ${parallelEnabled ? "checked" : ""} /><small>每个任务使用独立 Worktree。</small></label><label class="settings-field" for="max-parallel-development"><span>最大并行开发数</span><input id="max-parallel-development" name="max_parallel_development" type="number" min="1" max="8" step="1" value="${maxParallelDevelopment}" required /><small>最多 8 个。</small></label><div class="settings-actions"><button class="primary" type="submit">保存设置</button></div></form></section>`);
}

function render() {
  syncSidebar();
  syncHeader();
  if (state.view === "logs") renderExecutionLog();
  else if (state.view === "tokens") renderTokenPanel();
  else if (state.view === "requirements") renderRequirementsBoard();
  else if (state.view === "settings") renderSettings();
  else renderBoard();
  if (isDialogOpen("completed-tasks-dialog")) renderCompletedTasks();
  if (isDialogOpen("task-change-dialog")) renderTaskChangeConfirmations();
}

let accountActionPending = false;
document.addEventListener("account-action", async event => {
  if (accountActionPending) return;
  accountActionPending = true;
  try {
    if (event.detail === "settings") {
      if (!state.settings) state.settings = await api("/api/settings");
      state.view = "settings";
      persistNavigationState();
      render();
    } else if (event.detail === "logout") {
      await api("/api/auth/logout", {method: "POST", body: "{}"});
      boardEventSource?.close();
      window.location.replace("/login");
    }
  } catch (error) {
    toast(error.message);
  } finally {
    accountActionPending = false;
  }
});

async function handleViewNavigation(event) {
  if (event.target.closest("#execution-log-nav")) {
    state.view = "logs";
    persistNavigationState();
    render();
    return true;
  }
  if (event.target.closest("#board-nav")) {
    state.view = "board";
    persistNavigationState();
    render();

    return true;
  }
  if (event.target.closest("#requirements-nav")) {
    state.view = "requirements";
    persistNavigationState();
    render();

    return true;
  }
  if (event.target.closest("#token-panel")) {
    state.view = "tokens";
    persistNavigationState();
    render();

    return true;
  }
  const tokenView = event.target.closest("[data-token-view]");
  if (tokenView) {
    state.tokenView = tokenView.dataset.tokenView === "details" ? "details" : "charts";
    persistNavigationState();
    renderTokenPanel();
    return true;
  }
  if (event.target.closest("#completed-tasks")) {
    openDialog("completed-tasks-dialog");
    renderCompletedTasks();
    return true;
  }
  if (event.target.closest("#task-change-confirmations")) {
    openDialog("task-change-dialog");
    renderTaskChangeConfirmations();
    return true;
  }
  return false;
}

async function handleTaskAction(event) {
  const taskLogs = event.target.closest("[data-task-logs]");
  if (taskLogs) {
    try { await showTaskLogs(taskLogs.dataset.taskLogs); } catch (error) { toast(error.message); }
    return true;
  }
  const dispatcherToggle = event.target.closest("#dispatcher-toggle");
  if (dispatcherToggle) {
    const action = dispatcherToggle.dataset.action;
    if (!new Set(["pause", "resume"]).has(action)) {
      toast("调度状态尚未加载完成");
      return true;
    }
    try {
      const result = await api(`/api/dispatcher/${action}`, {method:"POST", body:JSON.stringify({reason:"用户从任务看板暂停"})});
      await load();
      toast(action === "pause"
        ? "调度已暂停；现有任务继续运行"
        : result.agent_configured
          ? "调度已恢复；已发送 Local Agent 信号"
          : "调度已恢复，但 Local Agent 尚未配置云端连接");
    } catch (error) { toast(error.message); }
    return true;
  }
  if (
    event.target.closest("#new-task-button")
    || event.target.closest("#new-requirement-button")
  ) {
    const historicProjects = [
      ...(state.board?.requirements || []).map(item => item.project),
      ...(state.board?.tasks || []).map(item => item.project),
    ].filter(Boolean).map(path => ({path, name: projectLabel(path)}));
    let codexProjects = [];
    try {
      const result = await api("/api/codex/projects");
      codexProjects = Array.isArray(result.projects) ? result.projects : [];
    } catch (_error) {
      // Keep the dialog usable with historical values while the Local Agent is offline.
    }
    const projects = new Map();
    [...codexProjects, ...historicProjects].forEach(project => {
      const path = String(project.path || "").trim();
      if (path && !projects.has(path)) projects.set(path, String(project.name || projectLabel(path)));
    });
    const dialogId = event.target.closest("#new-task-button") ? "new-task-dialog" : "new-requirement-dialog";
    openDialog(dialogId);
    const dialog = document.getElementById(dialogId);
    const select = dialog.querySelector("[data-project-select]");
    const emptyLabel = event.target.closest("#new-task-button")
      ? "无项目（创建到 Codex 最近）"
      : "无项目（仅保存需求）";
    select.innerHTML = [
      `<option value="">${emptyLabel}</option>`,
      ...[...projects].map(([path, name]) =>
        `<option value="${escapeHtml(path)}">${escapeHtml(name)} · ${escapeHtml(path)}</option>`
      ),
      '<option value="__manual__">手动输入绝对路径…</option>',
    ].join("");
    select.value = "";
    const manual = dialog.querySelector("[data-manual-project]");
    manual.hidden = true;
    manual.required = false;
    manual.value = "";
    return true;
  }
  if (event.target.closest("[data-close]")) {
    closeDialog(event.target.closest('[data-slot="dialog-content"]').id);
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
  const deleteCompletedTask = event.target.closest("[data-delete-completed-task]");
  if (deleteCompletedTask) {
    const taskId = deleteCompletedTask.dataset.deleteCompletedTask;
    const task = state.board?.tasks?.find(item => item.id === taskId);
    const taskLabel = task ? `${task.id}《${task.title}》` : taskId;
    if (!window.confirm(`确定删除完成任务 ${taskLabel} 吗？DoTasks 任务记录将永久删除，Codex 会话会保留。`)) return true;
    try {
      await api(`/api/tasks/${taskId}/delete`, {method: "POST", body: "{}"});
      await load();
      renderCompletedTasks();
      toast("完成任务已删除；Codex 会话已保留");
    } catch (error) { toast(error.message); }
    return true;
  }
  const redecomposeRequirement = event.target.closest("[data-redecompose-requirement]");
  if (redecomposeRequirement) {
    const requirementId = redecomposeRequirement.dataset.redecomposeRequirement;
    const requirement = state.board?.requirements?.find(item => item.id === requirementId);
    const childCount = (state.board?.tasks || []).filter(
      task => task.requirement_id === requirementId
    ).length;
    const warning = childCount
      ? `当前 ${childCount} 个尚未执行的拆分任务会被取消并保留记录。`
      : "";
    if (!window.confirm(`确定重新拆解 ${requirementId}《${requirement?.title || ""}》吗？${warning}`)) return true;
    try {
      await api(`/api/requirements/${requirementId}/redecompose`, {
        method: "POST", body: "{}",
      });
      await load();
      toast("需求已重新加入拆解队列");
    } catch (error) { toast(error.message); }
    return true;
  }
  const deleteRequirement = event.target.closest("[data-delete-requirement]");
  if (deleteRequirement) {
    const requirementId = deleteRequirement.dataset.deleteRequirement;
    const requirement = state.board?.requirements?.find(item => item.id === requirementId);
    if (!window.confirm(`确定删除 ${requirementId}《${requirement?.title || ""}》吗？已拆分任务会保留在任务看板。`)) return true;
    try {
      await api(`/api/requirements/${requirementId}/delete`, {
        method: "POST", body: "{}",
      });
      await load();
      toast("需求已删除，关联任务已保留");
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
      if (!(state.board?.pending_task_changes || []).length) closeDialog(dialog.id);
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
      closeDialog("completed-tasks-dialog");
      await showTaskDetails(details.dataset.details);
    } catch (error) { toast(error.message); }
    return true;
  }
  const openThread = event.target.closest("[data-open-thread]");
  if (openThread) {
    window.location.href = `codex://threads/${encodeURIComponent(openThread.dataset.openThread)}`;
    return true;
  }
  const copyThread = event.target.closest("[data-copy-thread]");
  if (copyThread) {
    await navigator.clipboard.writeText(copyThread.dataset.copyThread);
    toast("会话 ID 已复制");
    return true;
  }
  return false;
}

document.addEventListener("click", async event => {
  try {
    await routeDelegatedEvent(event, [
      handleViewNavigation,
      handleTaskAction,
    ]);
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("change", event => {
  const threadSelect = event.target.closest("[data-thread-select]");
  if (threadSelect?.value) {
    const threadId = threadSelect.value;
    threadSelect.value = "";
    window.location.href = `codex://threads/${encodeURIComponent(threadId)}`;
    return;
  }
  const select = event.target.closest("[data-project-select]");
  if (!select) return;
  const manual = select.closest("form").querySelector("[data-manual-project]");
  const enabled = select.value === "__manual__";
  manual.hidden = !enabled;
  manual.required = enabled;
  if (!enabled) manual.value = "";
  else manual.focus();
});

function selectedProjectPath(values) {
  const selected = String(values.get("project") || "").trim();
  return selected === "__manual__"
    ? String(values.get("manual_project") || "").trim()
    : selected;
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const encoded = String(reader.result || "").split(",", 2)[1] || "";
      encoded ? resolve(encoded) : reject(new Error(`无法读取图片：${file.name}`));
    });
    reader.addEventListener("error", () => reject(new Error(`无法读取图片：${file.name}`)));
    reader.readAsDataURL(file);
  });
}

async function uploadVisualReferences(values) {
  const files = values.getAll("visual_references").filter(file => file instanceof File && file.size);
  if (files.length > 8) throw new Error("一次最多上传 8 张图片");
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) throw new Error(`${file.name} 超过 10 MiB`);
  }
  return Promise.all(files.map(async file => api("/api/visual-artifacts", {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      content_base64: await fileAsBase64(file),
      purpose: "任务视觉参考",
    }),
  })));
}

document.addEventListener("submit", async event => {
  if (event.target.id === "new-task-form") {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    const project = selectedProjectPath(values);
    button.disabled = true;
    try {
      const visualReferences = await uploadVisualReferences(values);
      const result = await api("/api/task-intakes/enqueue", {
        method: "POST",
        body: JSON.stringify({
          title: String(values.get("title") || "").trim(),
          type: String(values.get("type") || "feature"),
          project,
          goal: String(values.get("goal") || "").trim(),
          priority: String(values.get("priority") || "P2"),
          modules: [],
          scope: [],
          out_of_scope: [],
          visual_references: visualReferences,
          auto_dispatch: values.get("auto_dispatch") === "on",
        }),
      });
      form.reset();
      closeDialog("new-task-dialog");
      await load();
      toast(`${result.task_id} 已加入任务队列${project ? "，正在等待定位" : "，将创建无项目 Codex 会话"}`);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (event.target.id === "new-requirement-form") {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button[type="submit"]');
    const values = new FormData(form);
    const project = selectedProjectPath(values);
    button.disabled = true;
    try {
      const visualReferences = await uploadVisualReferences(values);
      const result = await api("/api/task-intakes/finalize", {
        method: "POST",
        body: JSON.stringify({
          intake_kind: "requirement",
          title: String(values.get("title") || "").trim(),
          project,
          goal: String(values.get("goal") || "").trim(),
          description: String(values.get("goal") || "").trim(),
          priority: String(values.get("priority") || "P2"),
          source_type: "web",
          modules: [],
          scope: [],
          out_of_scope: [],
          acceptance_criteria: [],
          visual_references: visualReferences,
          decomposition_tasks: [],
          auto_dispatch: Boolean(project) && values.get("auto_dispatch") === "on",
        }),
      });
      form.reset();
      closeDialog("new-requirement-dialog");
      await load();
      toast(`${result.requirement_id} 已保存到云端${project ? "" : "；未选择项目，未自动调度"}`);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (event.target.id === "task-settings-form") {
    event.preventDefault();
    const input = document.querySelector("#task-token-budget");
    const appendedInput = document.querySelector("#max-batch-appended-tasks");
    const parallelEnabledInput = document.querySelector("#parallel-development-enabled");
    const maxParallelInput = document.querySelector("#max-parallel-development");
    const taskTokenBudget = Number(input.value);
    const maxBatchAppendedTasks = Number(appendedInput.value);
    const parallelDevelopmentEnabled = parallelEnabledInput.getAttribute("aria-checked") === "true";
    const maxParallelDevelopment = Number(maxParallelInput.value);
    if (!Number.isInteger(taskTokenBudget) || taskTokenBudget <= 0) {
      input.setCustomValidity("请输入大于 0 的整数");
      input.reportValidity();
      return;
    }
    input.setCustomValidity("");
    if (!Number.isInteger(maxBatchAppendedTasks) || maxBatchAppendedTasks < 0 || maxBatchAppendedTasks > 20) {
      appendedInput.setCustomValidity("请输入 0 到 20 的整数");
      appendedInput.reportValidity();
      return;
    }
    appendedInput.setCustomValidity("");
    if (!Number.isInteger(maxParallelDevelopment) || maxParallelDevelopment < 1 || maxParallelDevelopment > 8) {
      maxParallelInput.setCustomValidity("请输入 1 到 8 的整数");
      maxParallelInput.reportValidity();
      return;
    }
    maxParallelInput.setCustomValidity("");
    const button = event.target.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      state.settings = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          task_token_budget: taskTokenBudget,
          max_batch_appended_tasks: maxBatchAppendedTasks,
          parallel_development_enabled: parallelDevelopmentEnabled,
          max_parallel_development: maxParallelDevelopment,
        }),
      });
      renderSettings();
      toast("设置已保存，新的调度将使用当前并行配置");
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
    return;
  }
});

restoreNavigationState();
void load().then(connectBoardEvents);
const stageTimerInterval = setInterval(refreshStageTimers, 1000);
window.addEventListener("beforeunload", () => {
  boardEventSource?.close();
  clearInterval(stageTimerInterval);
});
