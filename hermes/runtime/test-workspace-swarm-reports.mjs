import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';
import { workspaceSwarmRuntime } from './workspace-swarm-runtime.mjs';

const file = '/src/screens/swarm2/swarm2-reports-view.tsx';
const upstream = process.env.WORKSPACE_UPSTREAM_DIR;
function load() {
  const source = fs.readFileSync(path.join(upstream, file), 'utf8');
  const code = workspaceSwarmRuntime().transform(source, file)?.code ?? source;
  const require = createRequire(path.join(upstream, 'package.json'));
  const ts = require('typescript');
  const logic = code.slice(0, code.indexOf('export function Swarm2ReportsView'))
    .replace(/^import .*$/gm, '');
  const js = ts.transpileModule(logic, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const context = vm.createContext({ exports: {} });
  vm.runInContext(js, context);
  return { context, code };
}
function mission(id, state, time, extra = {}) {
  return { id, title: 'Owner task', updatedAt: time, assignments: [{
    id: `${id}-assignment`, workerId: 'builder', state, completedAt: time,
    checkpoint: { stateLabel: state === 'blocked' ? 'BLOCKED' : 'DONE',
      filesChanged: 'none', blocker: state === 'blocked' ? 'old failure' : 'none', result: id },
  }], ...extra };
}
const enabled = { skip: !upstream };

test('report transform rejects an unsupported upstream contract', () => {
  assert.throws(() => workspaceSwarmRuntime().transform('changed source', file), /Unsupported Workspace Swarm report/);
});

test('none is not a file and mission history never inherits current runtime files', enabled, () => {
  const { context: c } = load();
  const rows = c.buildSwarm2ReportRows({ missions: [mission('old', 'done', 1)], runtimes: [{
    workerId: 'builder', lastResult: 'different task', artifacts: [{ id: 'x', path: 'other.py' }],
  }] });
  assert.equal(rows.find(row => row.missionId === 'old').artifacts.length, 0);
  assert.equal(c.splitChangedFiles('none, None, N/A, [], —, src/a.py, src/a.py').length, 1);
});

test('latest report wins over historical errors without erasing history', enabled, () => {
  const { context: c } = load();
  const rows = c.buildSwarm2ReportRows({ missions: [mission('old', 'blocked', 1), mission('new', 'done', 2)], runtimes: [] });
  const [card] = c.buildWorkerReportCards(rows);
  assert.equal(card.latest.missionId, 'new');
  assert.equal(card.state, 'ready');
  assert.equal(card.blockedCount, 1);
  assert.equal(card.rows.length, 2);
  const next = c.buildSwarm2ReportRows({ missions: [mission('newer-error', 'blocked', 3)], runtimes: [] });
  assert.equal(c.buildWorkerReportCards([...rows, ...next])[0].state, 'blocked');
});

test('files are counted by unique path, not repeated mentions or previews', enabled, () => {
  const { context: c } = load();
  const rows = c.buildSwarm2ReportRows({ missions: [mission('one', 'done', 1), mission('two', 'done', 2)], runtimes: [] });
  for (const row of rows) {
    row.artifacts = [{ id: row.id, path: 'src/a.py' }, { id: 'empty', path: 'none' }];
    row.previews = [{ id: 'preview', url: 'http://localhost', label: 'Preview' }];
  }
  assert.equal(c.buildWorkerReportCards(rows)[0].artifactCount, 1);
});

test('installation checks, task reports and runtime snapshots are separate views', enabled, () => {
  const { context: c, code } = load();
  const rows = c.buildSwarm2ReportRows({ missions: [mission('work', 'done', 1),
    mission('smoke', 'blocked', 2, { title: 'Managed Swarm installation smoke check' })],
  runtimes: [{ workerId: 'builder', currentTask: 'Installation smoke check only. Do not use tools.', lastResult: 'SWARM_SMOKE_OK' }] });
  assert.equal(c.selectSwarmReportRows(rows, 'tasks').length, 1);
  assert.equal(c.selectSwarmReportRows(rows, 'installation').length, 1);
  assert.equal(c.selectSwarmReportRows(rows, 'runtime').length, 1);
  assert.match(code, /Latest report/);
  assert.match(code, /not live worker readiness/);
  assert.match(code, /const counts = filteredRows.reduce/);
});

test('a dispatch route without the checkpoint anchors is refused', () => {
  // Line 194: swarm-dispatch must carry the checkpoint anchors, otherwise the
  // dispatcher would ship without the in-progress guard.
  // Every earlier anchor is satisfied so execution reaches the checkpoint loop.
  const source = [
    "return ['chat', '-q', prompt, '-Q', '--yolo', '--ignore-rules', '--source', 'swarm-dispatch']",
    "  const normalizedPrompt = prompt.replace(/\\r\\n/g, '\\n')",
    "    'paste-buffer',",
    "    '-d',",
  ].join('\n');
  assert.throws(() => workspaceSwarmRuntime().transform(source, '/src/routes/api/swarm-dispatch.ts'),
    /Unsupported Swarm checkpoint contract/);
});
