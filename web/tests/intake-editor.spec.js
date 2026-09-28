import { test, expect } from '@playwright/test';
import { createHash } from 'node:crypto';
import { deflateSync } from 'node:zlib';

// A real PNG (not arbitrary bytes) so previews must actually decode.
function png() {
  const crc = buffer => { let c = -1; for (const byte of buffer) { c ^= byte; for (let i = 0; i < 8; i++) c = (c >>> 1) ^ ((c & 1) ? 0xedb88320 : 0); } return (c ^ -1) >>> 0; };
  const chunk = (name, data) => { const body = Buffer.concat([Buffer.from(name), data]); const size = Buffer.alloc(4); size.writeUInt32BE(data.length); const sum = Buffer.alloc(4); sum.writeUInt32BE(crc(body)); return Buffer.concat([size, body, sum]); };
  const header = Buffer.from([0,0,1,64,0,0,0,160,8,2,0,0,0]);
  const pixels = Buffer.concat(Array.from({ length: 160 }, () => Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: 960 }, (_, i) => [57, 130, 246][i % 3]))])));
  return Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
}
const image = png();
const artifactId = `artifact://visuals/${createHash('sha256').update(image).digest('hex')}.png`;
const imageUrl = `/api/visual-artifacts/content?artifact_id=${encodeURIComponent(artifactId)}`;

async function openIntake(page, kind, options = {}) {
  const requests = [];
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let lastPayload;
  let uploadAttempts = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'POST') requests.push({ path, body: route.request().postDataJSON() });
    if (path === '/api/visual-artifacts/content') return route.fulfill({ contentType: 'image/png', body: image });
    if (path === '/api/visual-artifacts') {
      uploadAttempts++;
      if (options.routeUpload) return options.routeUpload(route, uploadAttempts);
      if (options.uploadDelay) await new Promise(resolve => setTimeout(resolve, options.uploadDelay));
      if (options.failFirst && uploadAttempts === 1) return route.fulfill({ status: 500, json: { error: '模拟上传失败' } });
      return route.fulfill({ json: { artifact_id: artifactId, filename: route.request().postDataJSON().filename, content_type: 'image/png' } });
    }
    if (path.startsWith('/api/task-intakes/')) lastPayload = route.request().postDataJSON();
    const record = lastPayload && { ...lastPayload, id: kind === 'task' ? 'TASK-test' : 'REQ-test', status: 'ready', created_at: '2026-09-28' };
    const responses = {
      '/api/auth/status': { authenticated: true, enabled: false },
      '/api/workflow': { columns: [{ key: 'ready', title: '待执行', statuses: ['ready'] }], status_labels: { ready: '待执行' }, token_stages: [] },
      '/api/board': { tasks: record && kind === 'task' ? [record] : [], requirements: record && kind === 'requirement' ? [record] : [], dispatcher: { enabled: false } },
      '/api/codex/projects': { projects: [] },
      '/api/task-intakes/enqueue': { task_id: 'TASK-test' },
      '/api/task-intakes/finalize': { requirement_id: 'REQ-test' },
      '/api/tasks/TASK-test/details': { task: record, conversations: [], runs: [], relations: [], events: [] },
    };
    if (path === '/api/events/stream') return route.fulfill({ contentType: 'text/event-stream', body: '' });
    await route.fulfill({ json: responses[path] || {} });
  });
  await page.goto('/');
  // The workspace controller loads asynchronously; wait for its first board render.
  await expect(page.getByText('还没有待执行的任务', { exact: true })).toBeVisible();
  if (kind === 'requirement') await page.locator('#requirements-nav').click();
  await page.locator(`#new-${kind}-button`).click();
  const form = page.locator(`#new-${kind}-form`);
  const editor = form.getByRole('textbox', { name: kind === 'task' ? '任务内容' : '需求内容', exact: true });
  await expect(editor).toBeVisible();
  return { form, editor, requests, errors };
}
async function values(form) {
  return form.evaluate(node => {
    const data = new FormData(node);
    return { title: data.get('title'), goal: data.get('goal'), error: data.get('intake_error'), images: JSON.parse(data.get('uploaded_visual_references') || '[]'), files: data.getAll('visual_references').map(file => file.name) };
  });
}
async function paste(editor, text, html) {
  await editor.evaluate((node, data) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData('text/plain', data.text);
    if (data.html) clipboardData.setData('text/html', data.html);
    node.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData }));
  }, { text, html });
}
async function addImage(form) {
  await form.getByRole('button', { name: '上传图片', exact: true }).click();
  await form.getByLabel('选择图片', { exact: true }).setInputFiles({ name: '设计图.png', mimeType: 'image/png', buffer: image });
}
async function decoded(locator) {
  await expect(locator).toBeVisible();
  await expect.poll(() => locator.evaluate(node => node.complete && node.naturalWidth)).toBe(320);
}

for (const kind of ['task', 'requirement']) {
  test(`${kind}: complete toolbar, Chinese composition, formatting, history and reset`, async ({ page }) => {
    const { form, editor, errors } = await openIntake(page, kind);
    await expect(form.getByRole('toolbar')).toBeVisible();
    await expect(form.getByRole('button', { name: '段落格式' })).toBeVisible();
    await expect(form.getByLabel('列表', { exact: true })).toBeVisible();
    await expect(form.getByLabel('插入链接', { exact: true })).toBeVisible();
    await editor.click();
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Input.imeSetComposition', { text: '中文标题', selectionStart: 4, selectionEnd: 4 });
    await cdp.send('Input.insertText', { text: '中文标题' });
    await editor.press('Enter');
    await page.keyboard.insertText('正文');
    await editor.press('Shift+ArrowLeft', { delay: 30 });
    await editor.press('Shift+ArrowLeft', { delay: 30 });
    await form.getByLabel('加粗', { exact: true }).click();
    await expect(editor.locator('strong')).toHaveText('正文');
    await form.getByLabel('斜体', { exact: true }).click();
    await expect(editor.locator('em')).toHaveText('正文');
    await form.getByLabel('撤销', { exact: true }).click();
    await expect(editor.locator('em')).toHaveCount(0);
    await form.getByLabel('重做', { exact: true }).click();
    await expect(editor.locator('em')).toHaveText('正文');
    expect((await values(form)).title).toBe('中文标题');
    expect((await values(form)).goal).toContain('正文');
    await form.evaluate(node => node.reset());
    await expect(editor).toHaveText('');
    await expect(form.getByLabel('撤销', { exact: true })).toBeDisabled();
    await editor.click();
    await page.keyboard.insertText('新内容');
    await expect(editor.locator('strong, em')).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  test(`${kind}: real inline image, final payload, saved preview and deletion`, async ({ page }) => {
    const { form, editor, requests, errors } = await openIntake(page, kind);
    await editor.click();
    await page.keyboard.insertText('图片需求');
    await editor.press('Enter');
    await addImage(form);
    await decoded(editor.locator('img'));
    if (kind === 'task') await page.screenshot({ path: '/tmp/dotasks-inline-image.png', fullPage: true });
    const data = await values(form);
    expect(data.error).toBeNull();
    expect(data.title).toBe('图片需求');
    expect(data.goal).toContain(imageUrl);
    expect(data.goal).not.toMatch(/blob:|data:image|<img/);
    expect(data.images).toEqual([{ artifact_id: artifactId, filename: '设计图.png' }]);
    expect(requests.find(item => item.path === '/api/visual-artifacts').body).toEqual({ filename: '设计图.png', content_base64: image.toString('base64'), purpose: '正文图片' });
    // Standard Tiptap node selection and keyboard deletion.
    await editor.locator('img').click();
    await page.keyboard.press('Backspace');
    await expect(editor.locator('img')).toHaveCount(0);
    expect((await values(form)).images).toEqual([]);
    await form.getByLabel('撤销', { exact: true }).click();
    await decoded(editor.locator('img'));
    await form.locator('button[type=submit]').click();
    await expect.poll(() => requests.find(item => item.path.startsWith('/api/task-intakes/'))).toBeTruthy();
    const submitted = requests.find(item => item.path.startsWith('/api/task-intakes/')).body;
    expect(submitted.visual_references).toEqual(data.images);
    expect(submitted.goal).toContain(imageUrl);
    expect(requests.filter(item => item.path === '/api/visual-artifacts')).toHaveLength(1);
    if (kind === 'task') await page.locator('[data-details="TASK-test"]').click();
    await decoded(page.locator('.intake-saved-images img'));
    expect(errors).toEqual([]);
  });

  test(`${kind}: upload error retry, pending submit guard and reset cancellation`, async ({ page }) => {
    const { form, editor, requests, errors } = await openIntake(page, kind, { failFirst: true, uploadDelay: 400 });
    await addImage(form);
    await expect(form.locator('.tiptap-image-upload-progress-text')).toBeVisible();
    expect((await values(form)).error).toContain('请完成');
    await form.locator('button[type=submit]').click();
    expect(requests.some(item => item.path.startsWith('/api/task-intakes/'))).toBe(false);
    await expect(form.getByText('模拟上传失败', { exact: true }).first()).toBeVisible();
    await form.getByRole('button', { name: '重试', exact: true }).click();
    await decoded(editor.locator('img'));
    expect(requests.filter(item => item.path === '/api/visual-artifacts')).toHaveLength(2);
    await form.evaluate(node => node.reset());
    await addImage(form);
    await form.evaluate(node => node.reset());
    await expect(form.locator('.tiptap-image-upload')).toHaveCount(0);
    await page.waitForTimeout(500); // Deliberately outlive the mocked response to check stale completions.
    await expect(editor.locator('img')).toHaveCount(0);
    expect((await values(form)).images).toEqual([]);
    expect(errors).toEqual([]);
  });

  test(`${kind}: image paste/drop and attachment limits`, async ({ page }) => {
    const { form, editor, errors } = await openIntake(page, kind);
    for (const type of ['paste', 'drop']) {
      await editor.click();
      await editor.evaluate((node, { type, bytes }) => {
        const transfer = new DataTransfer();
        transfer.items.add(new File([new Uint8Array(bytes)], `${type}.png`, { type: 'image/png' }));
        node.dispatchEvent(type === 'paste'
          ? new ClipboardEvent(type, { bubbles: true, cancelable: true, clipboardData: transfer })
          : new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: transfer }));
      }, { type, bytes: [...image] });
      await expect(editor.locator('img')).toHaveCount(type === 'paste' ? 1 : 2);
    }
    expect((await values(form)).images).toHaveLength(1); // Content-addressed duplicate is one attachment.
    const input = form.getByLabel('上传文件', { exact: true });
    await input.setInputFiles(Array.from({ length: 6 }, (_, i) => ({ name: `${i}.txt`, mimeType: 'text/plain', buffer: Buffer.alloc(i === 0 ? 10 * 1024 * 1024 : 1) })));
    expect((await values(form)).files).toHaveLength(6);
    await addImage(form);
    await expect(form.getByRole('alert').first()).toContainText('最多添加 8');
    await form.evaluate(node => node.reset());
    for (const size of [0, 10 * 1024 * 1024 + 1]) {
      await input.setInputFiles({ name: 'invalid', mimeType: 'text/plain', buffer: Buffer.alloc(size) });
      await expect(form.getByRole('alert')).toContainText('10 MiB');
    }
    await form.getByLabel('上传录音', { exact: true }).setInputFiles({ name: 'recording.wav', mimeType: 'audio/wav', buffer: Buffer.from('audio fixture') });
    await expect(form.locator('audio')).toHaveCount(1);
    await form.getByRole('button', { name: '移除 recording.wav' }).click();
    expect((await values(form)).files).toEqual([]);
    expect(errors).toEqual([]);
  });

  test(`${kind}: parallel image selection and narrow-screen controls`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const { form, editor, errors } = await openIntake(page, kind);
    await form.getByRole('button', { name: '上传图片', exact: true }).click();
    await form.getByLabel('选择图片').setInputFiles([1, 2, 3].map(i => ({ name: `图${i}.png`, mimeType: 'image/png', buffer: image })));
    await expect(editor.locator('img')).toHaveCount(3);
    expect((await values(form)).error).toBeNull();
    await expect(form.getByRole('button', { name: '上传图片', exact: true })).toBeVisible();
    const box = await form.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    if (kind === 'task') await page.screenshot({ path: '/tmp/dotasks-editor-mobile.png', fullPage: true });
    expect(errors).toEqual([]);
  });

  test(`${kind}: native copy round trip and file-only submission`, async ({ page }) => {
    const { form, editor, requests, errors } = await openIntake(page, kind);
    await editor.click();
    await paste(editor, '复制正文', '<p><strong>复制正文</strong></p>');
    await editor.press('ControlOrMeta+a');
    const copied = await editor.evaluate(async node => {
      const clipboardData = new DataTransfer();
      node.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData }));
      return { text: clipboardData.getData('text/plain'), html: clipboardData.getData('text/html') };
    });
    expect(copied.text).toBe('复制正文');
    await form.evaluate(node => node.reset());
    await editor.click();
    await paste(editor, copied.text, copied.html);
    await expect(editor.locator('strong')).toHaveText('复制正文');
    await form.evaluate(node => node.reset());
    await form.getByLabel('上传文件', { exact: true }).setInputFiles({ name: '需求.txt', mimeType: 'text/plain', buffer: Buffer.from('file contents') });
    await form.locator('button[type=submit]').click();
    await expect.poll(() => requests.find(item => item.path.startsWith('/api/task-intakes/'))).toBeTruthy();
    const submitted = requests.find(item => item.path.startsWith('/api/task-intakes/')).body;
    expect(submitted.title).toBe('需求.txt');
    expect(submitted.goal).toBe('请根据附件完成：需求.txt');
    expect(submitted.visual_references).toHaveLength(1);
    expect(requests.find(item => item.path === '/api/visual-artifacts').body).toEqual({ filename: '需求.txt', content_base64: Buffer.from('file contents').toString('base64'), purpose: '需求与任务附件', kind: 'attachment' });
    expect(errors).toEqual([]);
  });

  test(`${kind}: safe rich/plain paste, lists and title limit`, async ({ page }) => {
    const { form, editor, errors } = await openIntake(page, kind);
    await editor.click();
    await paste(editor, '标题\n正文', '<p>标题</p><p><strong>加粗</strong> <em>斜体</em></p><ul><li>列表</li></ul><script>window.bad=true</script>');
    await expect(editor.locator('strong')).toHaveText('加粗');
    await expect(editor.locator('li')).toHaveText('列表');
    expect((await values(form)).goal).toContain('**加粗**');
    await expect(editor.locator('script')).toHaveCount(0);
    expect(await page.evaluate(() => window.bad)).toBeUndefined();
    await form.evaluate(node => node.reset());
    await editor.click();
    await paste(editor, '<script>alert(1)</script> & **literal**');
    expect((await values(form)).title).toBe('<script>alert(1)</script> & **literal**');
    expect((await values(form)).goal).toMatch(/&lt;script&gt;|\\<script>/);
    expect((await values(form)).error).toBeNull();
    await form.evaluate(node => node.reset());
    await editor.click();
    await page.keyboard.insertText('中'.repeat(130));
    expect((await values(form)).title).toHaveLength(120);
    if (kind === 'task') await page.screenshot({ path: '/tmp/dotasks-complete-editor.png', fullPage: true });
    expect(errors).toEqual([]);
  });
}

for (const kind of ['task', 'requirement']) {
  test(`${kind}: external image syntax never fetches and escaped filenames retain references`, async ({ page }) => {
    const { form, editor, requests, errors } = await openIntake(page, kind);
    const external = [];
    page.on('request', request => { if (request.url().includes('external.invalid')) external.push(request.url()); });
    await editor.click();
    await page.keyboard.type('![x](https://external.invalid/track.png) ');
    await paste(editor, 'external', '<p><img src="https://external.invalid/paste.png"></p>');
    await expect(editor.locator('img')).toHaveCount(0);
    expect(external).toEqual([]);
    await form.evaluate(node => node.reset());
    await form.getByRole('button', { name: '上传图片', exact: true }).click();
    const filename = 'design"]draft.png';
    await form.getByLabel('选择图片').setInputFiles({ name: filename, mimeType: 'image/png', buffer: image });
    await decoded(editor.locator('img'));
    expect((await values(form)).images).toEqual([{ artifact_id: artifactId, filename }]);
    await form.locator('button[type=submit]').click();
    await expect.poll(() => requests.find(item => item.path.startsWith('/api/task-intakes/'))).toBeTruthy();
    expect(requests.find(item => item.path.startsWith('/api/task-intakes/')).body.visual_references).toEqual([{ artifact_id: artifactId, filename }]);
    expect(errors).toEqual([]);
  });

  test(`${kind}: partial batch cancellation and retry preserve files`, async ({ page }) => {
    let phase = 'clear';
    let retry = false;
    const { form, editor, requests, errors } = await openIntake(page, kind, { routeUpload: async route => {
      const name = route.request().postDataJSON().filename;
      if (name === 'slow.png') await new Promise(resolve => setTimeout(resolve, 1200));
      if (phase === 'retry' && name === 'first.png' && !retry) return route.fulfill({ status: 500, json: { error: 'first failed' } });
      return route.fulfill({ json: { artifact_id: artifactId } });
    } });
    const choose = async () => {
      await form.getByRole('button', { name: '上传图片', exact: true }).click();
      await form.getByLabel('选择图片').setInputFiles(['first.png', 'slow.png'].map(name => ({ name, mimeType: 'image/png', buffer: image })));
    };
    await choose();
    await expect(form.locator('.tiptap-image-upload-progress-text')).toHaveCount(1);
    await form.getByRole('button', { name: 'Clear All' }).click();
    await page.waitForTimeout(1300);
    await expect(editor.locator('img')).toHaveCount(0);
    await form.evaluate(node => node.reset());
    phase = 'retry';
    await choose();
    await expect(form.getByText('first failed').first()).toBeVisible();
    await form.getByRole('button', { name: '重试', exact: true }).click(); // Still busy: must not remove failed item.
    await expect(form.locator('.tiptap-image-upload-previews')).toContainText('first.png');
    await expect(editor.locator('img')).toHaveCount(1);
    await expect(editor.locator('img')).toHaveAttribute('alt', 'slow.png');
    retry = true;
    await form.getByRole('button', { name: '重试', exact: true }).click();
    await expect(editor.locator('img')).toHaveCount(2);
    await expect(form.locator('.tiptap-image-upload')).toHaveCount(0);
    expect((await values(form)).error).toBeNull();
    expect(requests.filter(item => item.path === '/api/visual-artifacts')).toHaveLength(5);
    expect(errors).toEqual([]);
  });
}

for (const kind of ['task', 'requirement']) {
  test(`${kind}: official heading list and link popovers work inside the form`, async ({ page }) => {
    const { form, editor, requests, errors } = await openIntake(page, kind);
    await editor.click();
    await page.keyboard.insertText('编辑标题');
    await form.getByRole('button', { name: '段落格式', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Heading 2' }).click();
    await expect(editor.locator('h2')).toHaveText('编辑标题');
    await editor.press('End'); await editor.press('Enter');
    await page.keyboard.insertText('列表内容');
    await form.getByRole('button', { name: '列表', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Bullet List' }).click();
    await expect(editor.locator('ul li')).toHaveText('列表内容');
    await editor.press('ControlOrMeta+a');
    await form.getByRole('button', { name: '插入链接', exact: true }).click();
    const url = page.getByPlaceholder('Paste a link...');
    await url.fill('https://example.com/');
    await url.press('Enter');
    await expect(editor.locator('a').first()).toHaveAttribute('href', 'https://example.com/');
    expect((await values(form)).goal).toContain('https://example.com/');
    expect(requests.some(item => item.path.startsWith('/api/task-intakes/'))).toBe(false);
    expect(errors).toEqual([]);
  });
}
