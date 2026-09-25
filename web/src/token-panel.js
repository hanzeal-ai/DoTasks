import { setContent } from "./ui-markup.js";
import { escapeHtml, formatCompactTokenCount, formatMetricTokenCount, formatTokenCount, projectLabel } from "./ui-core.js";

function renderTokenTrend(daily) {
  if (!daily.some(item => Number(item.token_used) > 0)) return '<div class="trace-empty">本月暂无用量，任务运行后将在这里展示趋势。</div>';
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

function renderTokenCharts(tasks, analytics) {
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

function renderTokenDetails(tasks, tokenStages) {
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

export function renderTokenPanel(board, view, tokenStages) {
  const tasks = board?.tasks || [];
  const viewSwitch = `<div class="token-view-switch" role="tablist" aria-label="Token 面板展示方式"><button type="button" role="tab" data-token-view="charts" aria-selected="${view === "charts"}" class="${view === "charts" ? "selected" : ""}">图表展示</button><button type="button" role="tab" data-token-view="details" aria-selected="${view === "details"}" class="${view === "details" ? "selected" : ""}">明细展示</button></div>`;
  const content = view === "details" ? renderTokenDetails(tasks, tokenStages) : renderTokenCharts(tasks, board?.token_analytics || {periods:{}, daily:[]});
  setContent("#content", `<section class="token-page"><div class="token-page-content">${viewSwitch}${content}</div></section>`);
}
