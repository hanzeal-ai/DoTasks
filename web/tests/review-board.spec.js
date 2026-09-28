import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';

const workflow = JSON.parse(execFileSync('./.venv/bin/python', ['-c', 'import json; from core.workflow import workflow_metadata; print(json.dumps(workflow_metadata()))'], { cwd: '..' }).toString());

test('review stays in development and exhausted review appears in attention', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/events/stream') return route.fulfill({ contentType: 'text/event-stream', body: '' });
    const responses = {
      '/api/auth/status': { authenticated: true, enabled: false },
      '/api/workflow': workflow,
      '/api/board': { tasks: [
        { id: 'TASK-review', title: '子 agent 审查中', status: 'code_review', auto_dispatch: true, token_usage_status: 'partial' },
        { id: 'TASK-pending', title: '第二次审查未通过', status: 'waiting_confirmation', auto_dispatch: false, review_rework_count: 2 },
      ], requirements: [], dispatcher: { enabled: false } },
      '/api/codex/projects': { projects: [] },
    };
    await route.fulfill({ json: responses[path] || {} });
  });
  await page.goto('/');
  await expect(page.locator('.board > .column')).toHaveCount(3);
  await expect(page.locator('.column-implementing')).toContainText('子 agent 审查中');
  await expect(page.locator('.column-attention')).toContainText('第二次审查未通过');
  await expect(page.locator('.column-code-review')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Code Review', exact: true })).toHaveCount(0);
  await page.locator('.column-implementing .card-more summary').click();
  await expect(page.locator('.column-implementing .card-more')).toContainText('Token 用量部分上报');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '待处理', exact: true }).click();
  await expect(page.locator('.column-attention')).toBeVisible();
  await page.locator('.column-attention .card-more summary').click();
  await expect(page.locator('.column-attention .card-more')).toContainText('Token 用量未上报');
  expect(errors).toEqual([]);
});
