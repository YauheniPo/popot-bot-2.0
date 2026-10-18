import assert from 'node:assert/strict';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

const upstream = process.env.HERMES_UPSTREAM_DIR;
const workspace = process.env.WORKSPACE_UPSTREAM_DIR;
const enabled = { skip: !upstream || !workspace };

function fixture() {
  const require = createRequire(path.join(workspace, 'package.json'));
  const ts = require('typescript');
  const source = execFileSync('python3', ['-c', `
import importlib.util,sys,json
from pathlib import Path
spec=importlib.util.spec_from_file_location('patches', 'hermes/runtime/apply-hermes-patches.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
sources={}
for name in ('web/src/pages/CronPage.tsx','web/src/lib/api.ts'):
 source=Path(sys.argv[1], name).read_text()
 for target,marker,old,new in m._PATCHES:
  if target==name:
   assert old in source, marker
   source=source.replace(old,new,1)
 sources[name]=source
print(json.dumps(sources))
`, upstream], { encoding: 'utf8' });
  const sources = JSON.parse(source);
  for (const [fileName, code] of Object.entries(sources)) {
    const result = ts.transpileModule(code, { fileName, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX } });
    assert.deepEqual(result.diagnostics.filter(d => d.category === ts.DiagnosticCategory.Error), []);
  }
  const tree = ts.createSourceFile('CronPage.tsx', sources['web/src/pages/CronPage.tsx'], ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const nodes = [];
  const visit = node => { nodes.push(node); ts.forEachChild(node, visit); };
  visit(tree);
  const run = (text, globals) => {
    const js = ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    return vm.runInNewContext(js, { exports: {}, ...globals });
  };
  return { ts, tree, nodes, run };
}

test('Dashboard button reflects shared activity and blocks unknown or failed status', enabled, () => {
  const f = fixture();
  const button = f.nodes.find(n => f.ts.isJsxElement(n) && n.openingElement.tagName.getText() === 'Button'
    && n.getText().includes('handleTrigger(job)'));
  const code = 'function render(job, triggeringJobKeys, jobsLoadError) { const jobKey="job"; return '
    + button.getText() + '; } render';
  const render = f.run(code, { require: () => ({ jsx: (type, props) => ({ type, props }) }),
    Button: 'button', Spinner: 'spinner', Zap: 'zap', t: { cron: { triggerNow: 'Run now' } } });
  for (const [running, local, error, disabled] of [
    [true, false, null, true], [false, true, null, true], [undefined, false, null, true],
    [false, false, 'offline', true], [false, false, null, false],
  ]) {
    const result = render({ is_running: running }, new Set(local ? ['job'] : []), error);
    assert.equal(result.props.disabled, disabled);
    assert.equal(result.props.children.type, running || local ? 'spinner' : 'zap');
  }
});

test('Dashboard polls sequentially and cancels the pending request when leaving the page', enabled, async () => {
  const f = fixture();
  const effect = f.nodes.find(n => f.ts.isCallExpression(n) && n.expression.getText() === 'useEffect'
    && n.arguments[0].getText().includes('jobsActiveRef.current = true'));
  const timers = [];
  const calls = [];
  let cleanup;
  f.run(effect.getText(), { useEffect: callback => { cleanup = callback(); },
    loadJobs: (_profile, signal) => new Promise(resolve => calls.push({ signal, resolve })),
    selectedProfile: 'default', jobsActiveRef: { current: false },
    selectedProfileRef: { current: null }, jobsRequestGenerationRef: { current: 0 }, AbortController,
    window: { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
      clearTimeout: () => {} } });
  assert.equal(calls.length, 1);
  assert.equal(timers.length, 0);
  calls[0].resolve();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 5000);
  timers[0].callback();
  assert.equal(calls.length, 2);
  cleanup();
  assert.equal(calls[1].signal.aborted, true);
  calls[1].resolve();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.length, 1);
});

test('runtime activity does not change the saved pause/resume state', enabled, () => {
  const f = fixture();
  const state = f.nodes.find(n => f.ts.isFunctionDeclaration(n) && n.name?.getText() === 'getJobState');
  const getState = f.run(state.getText() + '\ngetJobState', { asText: value => typeof value === 'string' ? value : '' });
  assert.equal(getState({ is_running: true, state: 'paused', enabled: false }), 'paused');
  assert.equal(getState({ is_running: true, state: 'scheduled', enabled: true }), 'scheduled');
});
