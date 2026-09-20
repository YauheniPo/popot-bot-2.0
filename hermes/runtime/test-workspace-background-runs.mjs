import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const upstream = process.env.WORKSPACE_UPSTREAM_DIR;
const enabled = { skip: !upstream };
const adapter = await import('./workspace-background-runs.mjs');
const storeFile = '/src/server/run-store.ts';
const activeFile = '/src/routes/api/runs/active.ts';
const dismissFile = '/src/routes/api/runs/$sessionKey.$runId.abandon.ts';
const uiFile = '/src/components/agent-view/background-runs-section.tsx';
const now = 1800000000000;

function load(file, modules = {}, globals = {}) {
  const source = fs.readFileSync(path.join(upstream, file), 'utf8');
  const code = adapter.workspaceBackgroundRuns().transform(source, file)?.code ?? source;
  const require = createRequire(path.join(upstream, 'package.json'));
  const ts = require('typescript');
  const js = ts.transpileModule(code, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const context = { exports: {}, process, URL, Date: class extends Date { static now() { return now; } },
    require: name => name in modules ? modules[name] : require(name), ...globals };
  vm.runInNewContext(js, context);
  return context.exports;
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'background-runs-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const live = new Set();
  const tracker = { hasActiveSendRun: id => live.has(id) };
  const store = load(storeFile, { './claude-paths': { getHermesRoot: () => root }, './send-run-tracker': tracker });
  const run = { sessionKey: 'main', friendlyId: 'main', runId: 'old-run', status: 'active',
    createdAt: now - 7200000, updatedAt: now - 3600000, lastEventAt: now - 3600000,
    assistantText: 'Last saved output', thinkingText: '', toolCalls: [], lifecycleEvents: [] };
  const write = record => {
    const dir = path.join(root, 'webui-mvp/runs', encodeURIComponent(record.sessionKey));
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, record.runId + '.json'), JSON.stringify(record));
  };
  write(run);
  const route = file => load(file, {
    '@tanstack/react-router': { createFileRoute: () => value => value },
    '@tanstack/react-start': { json: (body, init) => Response.json(body, init) },
    '../../../server/auth-middleware': { isAuthenticated: request => request.headers.get('X-Test-Auth') === 'yes' },
    '../../../server/run-store': store, '../../../server/send-run-tracker': tracker,
  }).Route.server.handlers;
  const dismiss = (record = run, authenticated = true) => route(dismissFile).POST({
    request: new Request('https://workspace.test/api/runs/main/old-run/abandon', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Test-Auth': authenticated ? 'yes' : 'no' },
      body: JSON.stringify({ updatedAt: record.updatedAt }) }),
    params: { sessionKey: record.sessionKey, runId: record.runId },
  });
  return { root, store, run, write, live, route, dismiss };
}

test('background run adapter refuses a changed upstream contract', () => {
  for (const file of [storeFile, activeFile, dismissFile, uiFile]) {
    assert.throws(() => adapter.workspaceBackgroundRuns().transform('changed', file), /Unsupported Workspace background/);
  }
});
test('list distinguishes a registered stream from a stale record without claiming provider liveness', enabled, async t => {
  const f = fixture(t);
  f.live.add('registered');
  f.write({ ...f.run, runId: 'registered' });
  const response = await f.route(activeFile).GET({ request: new Request('https://workspace.test/api/runs/active', { headers: { 'X-Test-Auth': 'yes' } }) });
  const { runs } = await response.json();
  assert.equal(runs.find(r => r.runId === 'registered').localStreamActive, true);
  assert.equal(runs.find(r => r.runId === 'registered').canDismiss, false);
  assert.equal(runs.find(r => r.runId === 'old-run').localStreamActive, false);
  assert.equal(runs.find(r => r.runId === 'old-run').canDismiss, true);
});
test('dismiss hides only the old record, preserves status/history and permits resumed output', enabled, async t => {
  const f = fixture(t);
  assert.equal((await f.dismiss()).status, 200);
  const saved = await f.store.getPersistedRun('main', 'old-run');
  assert.equal(saved.status, 'active');
  assert.equal(saved.assistantText, f.run.assistantText);
  assert.equal(saved.updatedAt, f.run.updatedAt);
  assert.equal(saved.dismissedAt, now);
  assert.equal((await f.store.listAllActiveRuns()).length, 0);
  f.live.add(f.run.runId);
  assert.equal((await f.store.listAllActiveRuns()).length, 1);
  f.live.clear();
  await f.store.appendRunText('main', 'old-run', ' resumed');
  assert.equal((await f.store.listAllActiveRuns()).length, 1);
});
test('live, fresh and concurrently updated runs cannot be dismissed; auth remains required', enabled, async t => {
  const f = fixture(t);
  assert.equal((await f.dismiss(f.run, false)).status, 401);
  f.live.add(f.run.runId);
  assert.equal((await f.dismiss()).status, 409);
  f.live.clear();
  f.write({ ...f.run, updatedAt: now });
  assert.equal((await f.dismiss({ ...f.run, updatedAt: now })).status, 409);
  f.write(f.run);
  const update = f.store.appendRunText('main', 'old-run', ' new');
  assert.equal((await f.dismiss()).status, 409);
  await update;
  assert.equal((await f.store.listAllActiveRuns()).length, 1);
  assert.equal((await f.dismiss({ ...f.run, runId: '../escape' })).status, 400);
});
test('dismiss rejects missing version and cross-origin requests', enabled, async t => {
  const f = fixture(t);
  for (const [headers, body, status] of [
    [{}, '{}', 415],
    [{ 'Content-Type': 'application/json' }, '{}', 400],
    [{ 'Content-Type': 'application/json' }, 'invalid', 400],
    [{ 'Content-Type': 'application/json', Origin: 'https://other.test' }, JSON.stringify({ updatedAt: f.run.updatedAt }), 403],
  ]) {
    const response = await f.route(dismissFile).POST({
      request: new Request('https://workspace.test/api/runs/main/old-run/abandon', { method: 'POST',
        headers: { ...headers, 'X-Test-Auth': 'yes' }, body }),
      params: { sessionKey: f.run.sessionKey, runId: f.run.runId },
    });
    assert.equal(response.status, status);
  }
  assert.equal((await f.store.listAllActiveRuns()).length, 1);
});
test('dismiss accepts the original HTTPS origin behind the loopback HTTP server', enabled, async t => {
  const f = fixture(t);
  const response = await f.route(dismissFile).POST({
    request: new Request('http://workspace.test:3002/api/runs/main/old-run/abandon', { method: 'POST',
      headers: { 'X-Test-Auth': 'yes', 'Content-Type': 'application/json', Origin: 'https://workspace.test:3002' },
      body: JSON.stringify({ updatedAt: f.run.updatedAt }) }),
    params: { sessionKey: f.run.sessionKey, runId: f.run.runId },
  });
  assert.equal(response.status, 200);
});

function uiHarness(fetch) {
  const states = [], effects = [], timers = [];
  let cursor = 0;
  const jsx = (type, props) => ({ type, props });
  const ui = load(uiFile, {
    react: { useCallback: f => f, useState: initial => {
      const index = cursor++; if (!(index in states)) states[index] = initial;
      return [states[index], next => { states[index] = typeof next === 'function' ? next(states[index]) : next; }];
    }, useEffect: f => effects.push(f) },
    'react/jsx-runtime': { jsx, jsxs: jsx },
    '@tanstack/react-router': { useNavigate: () => () => {} },
    '@hugeicons/react': { HugeiconsIcon: 'icon' }, '@hugeicons/core-free-icons': {},
    '@/components/ui/collapsible': { Collapsible: 'section', CollapsiblePanel: 'panel', CollapsibleTrigger: 'trigger' },
    '@/lib/utils': { cn: (...a) => a.join(' ') },
  }, { fetch, AbortController, AbortSignal, window: {
    confirm: () => true, setTimeout: f => { timers.push(f); return timers.length; }, clearTimeout: () => {},
  } });
  return { states, effects, timers, render() { cursor = 0; return ui.BackgroundRunsSection(); } };
}
function flatten(node) {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap(flatten);
  return [node, ...flatten(node.props?.children)];
}
test('failed dismissal keeps the row visible and displays an error; no kill wording', enabled, async () => {
  const h = uiHarness(async () => Response.json({ ok: false, error: 'Run changed; refresh first' }, { status: 409 }));
  h.render();
  h.states[0] = [{ runId: 'old', sessionKey: 'main', status: 'active', updatedAt: now - 3600000,
    stalenessMs: 3600000, localStreamActive: false, canDismiss: true }];
  const tree = h.render();
  const button = flatten(tree).find(n => n.type === 'button' && n.props.children === 'Dismiss record');
  assert.ok(button);
  await button.props.onClick();
  assert.equal(h.states[0].length, 1);
  assert.match(JSON.stringify(h.render()), /Run changed/);
  assert.doesNotMatch(JSON.stringify(tree), /Mark dead|Killing|running/);
});
test('polling shows failures and schedules only after completion, aborts on unmount', enabled, async () => {
  let release, signal;
  // Separate instance so the test controls an in-flight network request.
  const c = uiHarness((_url, options) => { signal = options.signal; return new Promise(resolve => { release = resolve; }); });
  c.render();
  const cleanup = c.effects[0]();
  assert.equal(c.timers.length, 0);
  release(Response.json({ ok: false }, { status: 503 }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.timers.length, 1);
  assert.match(JSON.stringify(c.render()), /503/);
  cleanup();
  assert.equal(signal.aborted, true);
});
