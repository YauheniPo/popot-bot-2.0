import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { workspaceCronUi } from './workspace-cron-ui.mjs';

const root = process.env.WORKSPACE_UPSTREAM_DIR;
const enabled = { skip: !root };
const files = ['/src/routes/api/claude-jobs.ts', '/src/lib/jobs-api.ts', '/src/screens/jobs/jobs-screen.tsx'];

function load(file, modules, globals = {}, suffix = '') {
  const source = fs.readFileSync(path.join(root, file), 'utf8');
  const code = workspaceCronUi().transform(source, file).code + suffix;
  const require = createRequire(path.join(root, 'package.json'));
  const ts = require('typescript');
  const js = ts.transpileModule(code, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  } }).outputText;
  const context = { exports: {}, URL, Request, Response, AbortSignal,
    require: name => name in modules ? modules[name] : require(name), ...globals };
  vm.runInNewContext(js, context);
  return context.exports;
}

test('cron adapter rejects incompatible upstream source', () => {
  for (const file of files) assert.throws(() => workspaceCronUi().transform('changed', file), /Unsupported Workspace cron/);
});

test('unknown files are left to the pinned Workspace', () => {
  assert.equal(workspaceCronUi().transform('changed', '/src/other.ts'), null);
});

test('job activity comes from the authenticated backend and keeps profiles separate', enabled, async () => {
  const jobs = [{ id: 'default:same', jobId: 'same', profile: 'default' },
    { id: 'other:same', jobId: 'same', profile: 'other' }];
  let status = 200;
  let reads = 0;
  const api = load(files[0], {
    '@tanstack/react-router': { createFileRoute: () => value => value },
    '../../server/auth-middleware': { isAuthenticated: req => req.headers.has('X-Test-Auth') },
    '../../server/gateway-capabilities': { dashboardFetch: async target => {
      assert.equal(target, '/api/cron/jobs?profile=all'); reads++;
      return status === 200 ? Response.json([
        { id: 'same', profile: 'default', is_running: true },
        { id: 'same', profile: 'other', is_running: false },
      ]) : Response.json({ error: 'Login required' }, { status });
    } },
    '../../server/hermes-cron-profiles': { listProfileCronJobs: () => jobs },
    '@/lib/feature-gates': {},
  }).Route.server.handlers;
  const request = auth => new Request('https://workspace.test/api/claude-jobs?profiles=all',
    { headers: auth ? { 'X-Test-Auth': 'yes' } : {} });
  assert.equal((await api.GET({ request: request(false) })).status, 401);
  assert.equal(reads, 0);
  const response = await api.GET({ request: request(true) });
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual((await response.json()).jobs.map(j => j.is_running), [true, false]);
  status = 401;
  assert.equal((await api.GET({ request: request(true) })).status, 401);
});

function flatten(node) {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap(flatten);
  return [node, ...flatten(node.props?.children)];
}

test('Run now is disabled for CLI activity, pending actions and unavailable status', enabled, () => {
  const jsx = (type, props) => ({ type, props });
  const icons = new Proxy({}, { get: (_o, key) => String(key) });
  const ui = load(files[2], {
    react: { useState: initial => [initial, () => {}], useMemo: f => f(), useCallback: f => f },
    'react/jsx-runtime': { jsx, jsxs: jsx },
    '@tanstack/react-query': { useQuery: () => ({ data: [] }) }, 'motion/react': { motion: { div: 'div' } },
    '@hugeicons/react': { HugeiconsIcon: 'icon' }, '@hugeicons/core-free-icons': icons,
    './create-job-dialog': {}, './edit-job-dialog': {}, '@/components/ui/toast': {},
    '@/lib/utils': { cn: (...a) => a.join(' ') },
    '@/lib/jobs-api': { getLastRunErrorText: () => null },
  }, {}, '\nexport { JobCard };');
  for (const [activity, pending, unavailable, disabled] of [
    [true, false, false, true], [false, true, false, true],
    [false, false, true, true], [undefined, false, false, true], [false, false, false, false],
  ]) {
    const tree = ui.JobCard({ job: { id: 'job', name: 'job', enabled: true, state: 'scheduled',
      schedule: {}, is_running: activity }, isTriggering: pending, activityUnavailable: unavailable });
    const button = flatten(tree).find(n => n.props?.['aria-label'] === 'Run now');
    assert.equal(button.props.disabled, disabled);
    const icon = flatten(button).find(n => n.type === 'icon');
    assert.equal(icon.props.icon, activity || pending ? 'RefreshIcon' : 'PlayIcon');
  }
});

test('jobs fetch supports cancellation and a bounded request deadline', enabled, async () => {
  let signal;
  const api = load(files[1], {}, { fetch: async (_url, init) => {
    signal = init.signal;
    assert.equal(init.cache, 'no-store');
    return Response.json({ jobs: [] });
  } });
  const controller = new AbortController();
  await api.fetchJobs({ signal: controller.signal });
  controller.abort();
  assert.equal(signal.aborted, true);
});
