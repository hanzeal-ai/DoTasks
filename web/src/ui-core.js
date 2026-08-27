export async function requestJson(fetchImplementation, path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body !== undefined && !Object.keys(headers).some(key => key.toLowerCase() === "content-type")) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetchImplementation(path, {...options, headers});
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (_error) {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      throw new Error("服务器返回了无效的 JSON");
    }
  }
  if (!response.ok) {
    const message = payload && typeof payload === "object" ? payload.error : "";
    throw new Error(message || `HTTP ${response.status}`);
  }
  return payload;
}

export async function routeDelegatedEvent(event, handlers) {
  for (const handler of handlers) {
    if (await handler(event)) return true;
  }
  return false;
}

export class LatestRequest {
  constructor() {
    this.sequence = 0;
  }

  begin(scope) {
    return {id: ++this.sequence, scope};
  }

  invalidate() {
    this.sequence += 1;
  }

  isCurrent(ticket, scope) {
    return ticket.id === this.sequence && ticket.scope === scope;
  }
}

export function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
}

export function projectLabel(project) {
  const normalized = String(project || "").replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).filter(Boolean).pop() || normalized;
}

export function relativeTime(timestamp) {
  const value = Number(timestamp || 0) * 1000;
  if (!value) return "";
  const seconds = Math.max(0, Math.round((Date.now() - value) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function timestampValue(timestamp) {
  if (!timestamp) return 0;
  const value = String(timestamp);
  const normalized = value.includes("T") ? value : value.replace(" ", "T");
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized) ? normalized : `${normalized}Z`;
  return Date.parse(zoned) || 0;
}

export function formatTimestamp(timestamp) {
  const value = timestampValue(timestamp);
  return value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "时间未知";
}

export function formatDuration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const remainingSeconds = value % 60;
  const clock = [hours, minutes, remainingSeconds].map(item => String(item).padStart(2, "0")).join(":");
  return days ? `${days}天 ${clock}` : clock;
}

export function durationBetween(start, end = Date.now()) {
  const startedAt = timestampValue(start);
  const endedAt = typeof end === "number" ? end : timestampValue(end);
  return startedAt ? Math.max(0, (endedAt - startedAt) / 1000) : 0;
}

export function formatTokenCount(value) {
  return Math.max(0, Number(value) || 0).toLocaleString("zh-CN");
}

export function formatCompactTokenCount(value) {
  return new Intl.NumberFormat("zh-CN", {notation:"compact", maximumFractionDigits:1}).format(Math.max(0, Number(value) || 0));
}

export function expandTaskboardSlashCommand(value) {
  const message = String(value || "").trim();
  const match = message.match(/^\/taskboard(?:\s+([\s\S]*))?$/i);
  if (!match) return message;
  return `$codex-taskboard${match[1] ? `\n\n${match[1].trim()}` : ""}`;
}

export function taskNeedsAttention(task, attentionStatuses, autoDispatchStatuses) {
  if (!task) return false;
  return attentionStatuses.has(task.status)
    || (autoDispatchStatuses.has(task.status) && Number(task.auto_dispatch) === 0);
}

export function taskRecoveryAction(task, {restartWaitingConfirmation = false} = {}) {
  if (!task) return null;
  if (task.status === "waiting_confirmation" && restartWaitingConfirmation) {
    return {
      kind: "transition",
      label: "重新开始",
      status: task.retry_run_type === "rework" ? "rework" : "ready",
    };
  }
  if (task.status === "failed") {
    return {kind: "transition", label: "重新执行", status: "ready"};
  }
  if (task.status === "blocked") {
    const recoveries = {
      review: {label: "返回验收", status: "review"},
      code_review: {label: "返回 Code Review", status: "code_review"},
      acceptance: {label: "返回功能验收", status: "acceptance"},
      rework: {label: "返回返工", status: "rework"},
    };
    const recovery = recoveries[task.blocked_from_status]
      || (task.retry_run_type === "rework" ? recoveries.rework : {label: "重新进入队列", status: "ready"});
    return {kind: "transition", ...recovery};
  }
  if (task.status === "paused") {
    return {kind: "resume", label: "恢复任务"};
  }
  const autoDispatchLabels = {
    ready: "继续自动领取",
    rework: "继续返工",
    review: "继续验收",
    code_review: "继续 Code Review",
    acceptance: "继续功能验收",
  };
  if (autoDispatchLabels[task.status] && Number(task.auto_dispatch) === 0) {
    return {kind: "enable_auto", label: autoDispatchLabels[task.status]};
  }
  return null;
}

const TASK_CONVERSATION_ROLES = {
  draft: ["source"],
  ready: ["source"],
  claimed: ["execution", "source"],
  investigating: ["execution", "rework", "bugfix"],
  implementing: ["execution", "rework", "bugfix"],
  waiting_confirmation: ["rework", "bugfix", "execution", "source"],
  rework: ["rework", "bugfix", "execution"],
  review: ["review"],
  code_review: ["code_review"],
  acceptance: ["acceptance"],
  acceptance_blocked: ["acceptance"],
  failed: ["rework", "bugfix", "execution"],
};

export function selectTaskConversation(task) {
  const conversations = Array.isArray(task?.conversations) ? [...task.conversations].reverse() : [];
  if (!conversations.length) return null;
  let status = task.status;
  if (status === "paused") status = task.paused_from_status || status;
  if (status === "blocked") status = task.blocked_from_status || status;
  const retryRole = ["execution", "rework", "bugfix", "review", "code_review", "acceptance"].includes(task.retry_run_type)
    ? task.retry_run_type
    : "";
  const roles = [retryRole, ...(TASK_CONVERSATION_ROLES[status] || [])].filter(Boolean);
  for (const role of roles) {
    const match = conversations.find(item => item.role === role);
    if (match) return match;
  }
  return conversations.find(item => item.role !== "source") || conversations[0];
}
