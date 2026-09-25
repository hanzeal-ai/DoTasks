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

export function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
}

export function projectLabel(project) {
  const normalized = String(project || "").replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).filter(Boolean).pop() || normalized;
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

export function formatLogTimestamp(timestamp) {
  const value = timestampValue(timestamp);
  if (!value) return "时间未知";
  const date = new Date(value);
  const pad = part => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

const EXECUTION_LOG_LABELS = {
  created: "创建",
  intake_queued: "加入队列",
  claimed: "领取",
  conversation_bound: "绑定会话",
  native_thread_bound: "启动会话",
  transitioned: "状态变更",
  paused: "暂停",
  resumed: "恢复",
  delivery_submitted: "提交交付",
  batch_delivery_submitted: "提交批次交付",
  delivery_integrated: "交付已集成",
  delivery_integration_failed: "交付集成失败",
  code_reviewed: "Code Review",
  review_interrupted: "评审中断",
  review_preparation_failed: "评审准备失败",
  execution_stopped: "执行中断",
  execution_requeued: "重新排队",
  execution_failed: "执行失败",
  context_build_failed: "上下文准备失败",
  lease_expired: "会话超时",
  token_budget_exceeded: "Token 预算耗尽",
  stage_budget_preflight_blocked: "阶段预算阻塞",
  self_heal_scheduled: "安排自动修复",
  self_heal_exhausted: "自动修复失败",
  target_conflict_detected: "检测到目标冲突",
  execution_batch_joined: "加入执行批次",
  auto_batched: "自动加入批次",
  relation_added: "关联任务",
  deleted: "删除",
};

export function executionLogLabel(event, statusLabels = {}) {
  const payload = event?.payload || {};
  if (event?.event_type === "transitioned") {
    return statusLabels[payload.to] || payload.to || EXECUTION_LOG_LABELS.transitioned;
  }
  if (event?.event_type === "code_reviewed") {
    return payload.verdict === "pass" ? "Code Review 通过" : payload.verdict === "fail" ? "Code Review 失败" : EXECUTION_LOG_LABELS.code_reviewed;
  }
  return EXECUTION_LOG_LABELS[event?.event_type]
    || String(event?.event_type || "状态更新").replaceAll("_", " ");
}

export function executionLogReason(event) {
  const payload = event?.payload || {};
  if (payload.reason) return String(payload.reason);
  if (payload.error) return String(payload.error);
  if (Array.isArray(payload.reasons) && payload.reasons.length) {
    return payload.reasons.map(String).join("；");
  }
  if (Array.isArray(payload.failed_criteria) && payload.failed_criteria.length) {
    return payload.failed_criteria.map(item => typeof item === "string" ? item : (item?.criterion || item?.description || JSON.stringify(item))).join("；");
  }
  return "";
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

export function formatMetricTokenCount(value) {
  const count = Math.max(0, Number(value) || 0);
  const unit = count >= 1_000_000_000
    ? [1_000_000_000, "B"]
    : count >= 1_000_000
      ? [1_000_000, "M"]
      : count >= 1_000
        ? [1_000, "K"]
        : null;
  if (!unit) return String(count);
  return `${Number((count / unit[0]).toFixed(1))}${unit[1]}`;
}

export function formatCompactTokenCount(value) {
  return new Intl.NumberFormat("zh-CN", {notation:"compact", maximumFractionDigits:1}).format(Math.max(0, Number(value) || 0));
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
      code_review: {label: "返回 Code Review", status: "code_review"},
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
    code_review: "继续 Code Review",
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
  code_review: ["code_review"],
  failed: ["rework", "bugfix", "execution"],
};

export function selectTaskConversation(task) {
  const conversations = Array.isArray(task?.conversations) ? [...task.conversations].reverse() : [];
  if (!conversations.length) return null;
  let status = task.status;
  if (status === "paused") status = task.paused_from_status || status;
  if (status === "blocked") status = task.blocked_from_status || status;
  const retryRole = ["execution", "rework", "bugfix", "code_review"].includes(task.retry_run_type)
    ? task.retry_run_type
    : "";
  const roles = [retryRole, ...(TASK_CONVERSATION_ROLES[status] || [])].filter(Boolean);
  for (const role of roles) {
    const match = conversations.find(item => item.role === role);
    if (match) return match;
  }
  return conversations.find(item => item.role !== "source") || conversations[0];
}
