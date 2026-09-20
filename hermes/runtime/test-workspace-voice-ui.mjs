import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { workspaceVoiceUi } from './workspace-voice-ui.mjs';

test('voice adapter fails closed when the pinned contract changes', () => {
  const plugin = workspaceVoiceUi();
  assert.equal(plugin.transform('other', '/src/other.ts'), null);
  for (const file of ['lib/stt-config.ts', 'components/settings-dialog/settings-dialog.tsx']) {
    assert.throws(() => plugin.transform('changed', '/src/' + file), /Unsupported Workspace voice/);
  }
});

test('voice controls expose shared language independently of provider and retain Nous',
  { skip: !process.env.WORKSPACE_UPSTREAM_DIR }, () => {
    const transform = name => workspaceVoiceUi().transform(
      fs.readFileSync(path.join(process.env.WORKSPACE_UPSTREAM_DIR, 'src', name), 'utf8'), '/src/' + name).code;
    const providers = transform('lib/stt-config.ts');
    assert.match(providers, /value: 'nous'/);
    const ui = transform('components/settings-dialog/settings-dialog.tsx');
    assert.match(ui, /\)\}\s*<Row label="Recognition language"/);
    assert.match(ui, /defaultValue={String\(stt.language \|\| ''\)}/);
    assert.match(ui, /saveStt\('language', e.target.value\)/);
    assert.match(ui, /if \(!response.ok\) throw new Error/);
    assert.match(ui, /value.trim\(\).toLowerCase\(\) === 'auto'/);
    assert.match(ui, /Speech vocabulary/);
    assert.match(ui, /saveStt\('prompt',/);
    assert.match(ui, /Telegram/);
  });

test('saving voice settings sends autodetect and vocabulary, and reports rejected writes',
  { skip: !process.env.WORKSPACE_UPSTREAM_DIR }, async () => {
    const root = process.env.WORKSPACE_UPSTREAM_DIR;
    const require = createRequire(path.join(root, 'package.json'));
    const ts = require('typescript');
    const source = workspaceVoiceUi().transform(fs.readFileSync(path.join(root,
      'src/components/settings-dialog/settings-dialog.tsx'), 'utf8'),
      '/src/components/settings-dialog/settings-dialog.tsx').code;
    const start = source.indexOf('  const saveStt = async');
    const end = source.indexOf('  const ttsProvider', start);
    const executable = ts.transpileModule(source.slice(start, end), {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText;
    let status, sent;
    let current = { provider: 'nous', language: 'en' };
    let ok = true;
    const save = new Function('fetch', 'setMsg', 'setStt', 'setTimeout',
      executable + '\nreturn saveStt;')(
      async (_url, options) => { sent = JSON.parse(options.body); return { ok }; },
      value => { status = value; }, update => { current = update(current); }, () => {});
    await save('language', ' AUTO ');
    assert.equal(sent.config.stt.language, '');
    assert.equal(current.language, '');
    assert.equal(current.provider, 'nous');
    assert.equal(status, 'Saved');
    await save('prompt', 'Евгений, Playwright');
    assert.equal(sent.config.stt.prompt, 'Евгений, Playwright');
    ok = false;
    await save('language', 'ru');
    assert.equal(status, 'Failed');
    assert.equal(current.language, '');
  });
