import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, resolveConfig } from 'vite';
import { createServer as netServer } from 'node:net';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { devApiProxy } from '../dev-api-proxy.js';

test('real local backend through Vite accepts same-origin upload but rejects foreign origins', async () => {
  const home = await mkdtemp(join(tmpdir(), 'dotasks-proxy-test-'));
  const reservation = netServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const backend = spawn('python3', ['-m', 'taskboard.server', '--host', '127.0.0.1', '--port', String(port), '--home', home], {
    cwd: fileURLToPath(new URL('../../', import.meta.url)),
    env: { ...process.env, DOTASKS_MODE: 'local', DOTASKS_HTTP_USER: '', DOTASKS_HTTP_PASSWORD: '' }, stdio: 'ignore',
  });
  const target = `http://127.0.0.1:${port}`;
  const vite = await createServer({ configFile: false, server: { host: '127.0.0.1', port: 0, proxy: { '/api': devApiProxy(target) } } });
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(target + '/api/health')).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, 'isolated backend starts');
    await vite.listen();
    const origin = `http://127.0.0.1:${vite.httpServer.address().port}`;
    const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZsAAAAASUVORK5CYII=';
    const body = JSON.stringify({ filename: 'proxy.png', content_base64: png, purpose: '正文图片' });
    for (const untrusted of ['https://evil.example', 'null', 'http://127.0.0.1:1']) {
      const response = await fetch(origin + '/api/visual-artifacts', { method: 'POST', headers: { Origin: untrusted, 'Content-Type': 'application/json' }, body });
      assert.equal(response.status, 403);
    }
    const response = await fetch(origin + '/api/visual-artifacts', { method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/json' }, body });
    assert.equal(response.status, 201, await response.clone().text());
    const artifact = await response.json();
    const preview = await fetch(origin + '/api/visual-artifacts/content?artifact_id=' + encodeURIComponent(artifact.artifact_id));
    assert.equal(preview.status, 200);
    assert.equal(preview.headers.get('content-type'), 'image/png');
    assert.deepEqual(Buffer.from(await preview.arrayBuffer()), Buffer.from(png, 'base64'));
  } finally {
    await vite.close();
    const exited = new Promise(resolve => backend.once('exit', resolve));
    backend.kill('SIGTERM'); await exited;
    await rm(home, { recursive: true, force: true });
  }
});


test('development and browser-test servers use isolated optimizer caches', async () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  const development = await resolveConfig({ root, mode: 'development' }, 'serve');
  const browserTest = await resolveConfig({ root, mode: 'intake-test' }, 'serve');
  assert.notEqual(development.cacheDir, browserTest.cacheDir);
  assert.ok(development.cacheDir.endsWith('/.vite/development'));
  assert.ok(browserTest.cacheDir.endsWith('/.vite/intake-test'));
});
