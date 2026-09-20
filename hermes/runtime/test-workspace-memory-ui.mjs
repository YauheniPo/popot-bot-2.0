import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { workspaceMemoryUi } from './workspace-memory-ui.mjs';

test('memory adapter refuses unknown pinned contracts and ignores unrelated files', () => {
  const plugin = workspaceMemoryUi();
  assert.equal(plugin.transform('unrelated', '/other.ts'), null);
  for (const file of ['server/memory-browser.ts', 'routes/api/memory/write.ts',
    'routes/api/memory/read.ts', 'screens/memory/memory-browser-screen.tsx']) {
    assert.throws(() => plugin.transform('changed upstream', '/src/' + file), /Unsupported Workspace memory/);
  }
});

test('memory read keeps auth and exposes metadata without configuration secrets',
  { skip: !process.env.WORKSPACE_UPSTREAM_DIR }, () => {
    const transform = name => workspaceMemoryUi().transform(fs.readFileSync(
      path.join(process.env.WORKSPACE_UPSTREAM_DIR, 'src', name), 'utf8'), '/src/' + name).code;
    const route = transform('routes/api/memory/read.ts');
    assert.match(route, /if \(!isAuthenticated\(request\)\)/);
    assert.match(route, /metadata: describeMemoryFile\(pathParam, content\)/);
    const server = transform('server/memory-browser.ts');
    assert.match(server, /parseConfig: YAML.parse/);
    const ui = transform('screens/memory/memory-browser-screen.tsx');
    assert.match(ui, /metadata\?\.applicability/);
    assert.match(ui, /loaded in this session: unknown/i);
    assert.match(ui, /configured budget/);
    assert.match(ui, /managedBlocks/);
  });
