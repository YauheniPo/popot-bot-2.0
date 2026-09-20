import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { workspaceSwarmRuntime } from './workspace-swarm-runtime.mjs';

const upstream = process.env.WORKSPACE_UPSTREAM_DIR;
const file = '/src/screens/swarm2/swarm2-activity-feed.tsx';
const enabled = { skip: !upstream };
const now = Date.parse('2026-09-19T18:00:00Z');
function load() {
  const source = fs.readFileSync(path.join(upstream, file), 'utf8');
  const code = workspaceSwarmRuntime().transform(source, file)?.code ?? source;
  const ts = createRequire(path.join(upstream, 'package.json'))('typescript');
  const logic = code.slice(0, code.indexOf('export function Swarm2ActivityFeed')).replace(/^import .*$/gm, '');
  const js = ts.transpileModule(logic, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const context = vm.createContext({ exports: {}, Date: class extends Date { static now() { return now; } } });
  vm.runInContext(js, context);
  return context;
}
const member = { id: 'builder', displayName: 'Builder', lastSessionAt: null };
function rows(c, entry, m = member) { return c.buildRows([m], new Map([[m.id, entry]])); }

test('activity adapter fails closed on an unsupported upstream', () => {
  assert.throws(() => workspaceSwarmRuntime().transform('changed', file), /Unsupported Workspace Swarm activity/);
});
test('seconds and milliseconds display the same age, unknown is not just now', enabled, () => {
  const c = load();
  assert.equal(c.relativeTime((now - 3600000) / 1000), '1h ago');
  assert.equal(c.relativeTime(now - 3600000), '1h ago');
  for (const value of [null, 0, NaN, -1, now + 3600000]) assert.equal(c.relativeTime(value), 'time unknown');
});
test('routine memory trim is hidden but warnings are retained', enabled, () => {
  const c = load();
  const tail = '2026-09-19T17:59:00Z INFO hermes_cli.mem_trim: memory trim: reason=idle reaper';
  assert.equal(rows(c, { recentLogTail: tail }).length, 0);
  const result = rows(c, { recentLogTail: tail.replace('INFO', 'WARNING') });
  assert.equal(result.length, 1);
  assert.match(result[0].text, /WARNING/);
});
test('log time belongs to the selected log line, never a previous task timestamp', enabled, () => {
  const c = load();
  const result = rows(c, { lastOutputAt: now - 43200000,
    recentLogTail: '2026-09-19T17:58:00Z WARNING tool: connection failed\n2026-09-19T17:59:00Z INFO hermes_cli.mem_trim: memory trim' });
  assert.equal(result[0].ts, now - 120000);
  assert.equal(c.relativeTime(result[0].ts), '2m ago');
  assert.equal(rows(c, { recentLogTail: 'WARNING tool: no timestamp', lastOutputAt: now })[0].ts, null);
});
test('latest known event wins; session timestamps are normalized before sorting', enabled, () => {
  const c = load();
  const members = [member, { id: 'workspace', displayName: 'Workspace', lastSessionTitle: 'Owner task', lastSessionAt: (now - 60000) / 1000 }];
  const result = c.buildRows(members, new Map([['builder', { lastOutputAt: now - 120000, lastResult: 'Task done', recentLogTail: 'old un-timestamped info' }]]));
  assert.equal(result[0].workerId, 'workspace');
  assert.equal(result[1].text, 'Task done');
  assert.equal(result[0].ts, now - 60000);
});
