import assert from 'node:assert/strict';
import { test } from 'node:test';
import vm from 'node:vm';
import { workspaceModelPolicy } from './workspace-model-policy.mjs';
import { workspaceSwarmRuntime } from './workspace-swarm-runtime.mjs';

test('Swarm uses managed roster and repository without changing upstream files', () => {
  const plugin = workspaceSwarmRuntime();
  const roster = plugin.transform("export const SWARM_ROSTER_PATH = join(SWARM_CANONICAL_REPO, 'swarm.yaml')", '/src/server/swarm-roster.ts');
  assert.match(roster.code, /process.env.HERMES_SWARM_ROSTER/);
  const environment = plugin.transform('export const SWARM_CANONICAL_REPO = resolve(process.cwd())', '/src/server/swarm-environment.ts');
  assert.match(environment.code, /process.env.HERMES_SWARM_REPO/);
  const dispatch = plugin.transform("return ['chat', '-q', prompt, '-Q', '--yolo', '--ignore-rules', '--source', 'swarm-dispatch']\n  const normalizedPrompt = prompt.replace(/\\r\\n/g, '\\n')\n    'paste-buffer',\n    '-d',\nif (checkpoint && checkpoint.raw !== previousRaw) return checkpoint\nif (runtimeCheckpoint && runtimeCheckpoint.raw !== previousRaw) return runtimeCheckpoint", '/src/routes/api/swarm-dispatch.ts');
  assert.doesNotMatch(dispatch.code, /--yolo|--ignore-rules/);
  assert.match(dispatch.code, /Worker TUI did not become ready/);
  assert.match(dispatch.code, /'paste-buffer',\n    '-p',/);
  assert.match(dispatch.code, /checkpoint.stateLabel !== 'IN_PROGRESS'/);
  assert.throws(() => plugin.transform('changed', '/src/routes/api/swarm-dispatch.ts'), /Unsupported/);
});

test('Swarm resolves every label from current shared model, not upstream defaults', () => {
  const source = 'export function resolveSwarmModelLabel(label) { return null }';
  const result = workspaceModelPolicy().transform(source, '/src/server/swarm-model-resolver.ts');
  let config = { model: { provider: 'test-cloud', default: 'fixture-model' } };
  const context = vm.createContext({
    readFileSync: () => JSON.stringify(config),
    parse: JSON.parse, join: (...parts) => parts.join('/'), homedir: () => '/home/test',
    process: { env: { HERMES_HOME: '/srv/test' } },
  });
  const code = result.code.replace(/^import .*$/gm, '').replaceAll('export ', '');
  vm.runInContext(code, context);
  for (const label of [null, 'GPT-5.5', 'custom/model']) {
    assert.equal(context.resolveSwarmModelLabel(label).default, 'fixture-model');
    assert.equal(context.resolveSwarmModelLabel(label).provider, 'test-cloud');
  }
  config.model.default = 'new-fixture';
  assert.equal(context.resolveSwarmModelLabel('old').default, 'new-fixture');
  config = {};
  assert.throws(() => context.resolveSwarmModelLabel('GPT-5.5'), /managed.*model/i);
});

test('Swarm waits for readiness before delivery and fails within a bounded deadline', async () => {
  const source = `
function buildArgs(prompt) { return ['chat', '-q', prompt, '-Q', '--yolo', '--ignore-rules', '--source', 'swarm-dispatch'] }
async function deliver(prompt) {
  const tmuxBin = 'tmux', sessionName = 'swarm-fixture', workerId = 'fixture', startedAt = Date.now();
  const normalizedPrompt = prompt.replace(/\\r\\n/g, '\\n')
  return { ok: true, normalizedPrompt, args: [
    'paste-buffer',
    '-d',
  ] };
}
function complete(checkpoint, runtimeCheckpoint, previousRaw) {
  if (checkpoint && checkpoint.raw !== previousRaw) return checkpoint
  if (runtimeCheckpoint && runtimeCheckpoint.raw !== previousRaw) return runtimeCheckpoint
  return null;
}`;
  const { code } = workspaceSwarmRuntime().transform(source, '/src/routes/api/swarm-dispatch.ts');
  let now = 0;
  let pane = 'starting';
  let present = true;
  const context = vm.createContext({
    Date: { now: () => now },
    sleep: async (ms) => { now += ms; },
    captureTmuxPane: async () => pane,
    tmuxHasSession: async () => present,
  });
  vm.runInContext(code, context);
  const timedOut = await context.deliver('task');
  assert.equal(timedOut.ok, false);
  assert.equal(now, 40_000);
  assert.match(timedOut.error, /did not become ready/);
  present = false;
  assert.equal((await context.deliver('task')).ok, false);
  assert.equal(now, 40_000, 'a missing session does not consume the whole deadline');
  present = true;
  pane = '─ ready\n❯';
  const delivered = await context.deliver('line1\r\nline2');
  assert.equal(delivered.ok, true);
  assert.equal(delivered.normalizedPrompt, 'line1\nline2');
  assert.equal(delivered.args[1], '-p');
  const progress = { stateLabel: 'IN_PROGRESS', raw: 'new progress' };
  const done = { stateLabel: 'DONE', raw: 'final result' };
  assert.equal(context.complete(progress, progress, 'old'), null);
  assert.equal(context.complete(progress, done, 'old'), done);
  assert.equal(context.complete(done, null, done.raw), null);
});

test('Roster labels are derived for both configured and fallback workers', () => {
  const source = "model: z.string().default('Worker'),\nmodel: 'Worker',";
  const result = workspaceModelPolicy().transform(source, '/src/server/swarm-roster.ts');
  assert.match(result.code, /default\('Worker'\).transform\(managedModelLabel\)/);
  assert.match(result.code, /model: managedModelLabel\(\)/);
  assert.equal(workspaceModelPolicy().transform('x', '/other.ts'), null);
  assert.throws(() => workspaceModelPolicy().transform('changed', '/src/server/swarm-roster.ts'), /Unsupported/);
});
