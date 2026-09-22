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

test('memory write adapter rewrites the pinned contract and rejects a broken one', () => {
  const plugin = workspaceMemoryUi();
  const file = '/src/routes/api/memory/write.ts';
  const source = [
    "import fs from 'node:fs'",
    "import path from 'node:path'",
    "import { isAuthenticated } from '../../../server/auth-middleware'",
    'import { getMemoryWorkspaceRoot } from ' + "'../../../server/memory-browser'",
    '',
    'function validateMemoryWritePath(inputPath: unknown): { relativePath: string } {',
    "  return { relativePath: String(inputPath) }",
    '}',
    '',
    'export const Route = {',
    "  content?: unknown",
    '}',
    '',
    "const { relativePath, fullPath } = validateMemoryWritePath(body.path)",
    "const content = typeof body.content === 'string' ? body.content : ''",
    "fs.mkdirSync(path.dirname(fullPath), { recursive: true })",
    "          fs.writeFileSync(fullPath, content, 'utf-8')",
    "return json({ error: message }, { status })",
  ].join('\n');
  const code = plugin.transform(source, file).code;
  // The pinned validator and the direct fs write are replaced by the managed helper.
  assert.match(code, /const relativePath = body\.path/);
  assert.match(code, /writeMemoryFile\(relativePath, content, body\.version\)/);
  assert.doesNotMatch(code, /validateMemoryWritePath\(/);
  assert.doesNotMatch(code, /writeFileSync\(fullPath/);
  // A missing anchor must fail closed rather than emit a half-rewritten route.
  assert.throws(() => plugin.transform('export const Route = null\n', file),
    /Unsupported Workspace memory contract/);
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

test('the memory write adapter rejects a broken anchor order', () => {
  const plugin = workspaceMemoryUi();
  const file = '/src/routes/api/memory/write.ts';
  // Satisfy the two earlier replace() anchors so execution reaches line 30,
  // where `end` appears BEFORE `start` and the slice would corrupt the module.
  const reversed = [
    "import fs from 'node:fs'",
    "import path from 'node:path'",
    'import { getMemoryWorkspaceRoot } from ' + "'../../../server/memory-browser'",
    'export const Route = null',
    'function validateMemoryWritePath(input) { return { relativePath: input } }',
  ].join('\n');
  assert.throws(() => plugin.transform(reversed, file), /Unsupported Workspace memory write contract/);
  // A missing start anchor hits the same guard.
  const noStart = [
    "import fs from 'node:fs'",
    "import path from 'node:path'",
    'import { getMemoryWorkspaceRoot } from ' + "'../../../server/memory-browser'",
    'export const Route = null',
  ].join('\n');
  assert.throws(() => plugin.transform(noStart, file), /Unsupported Workspace memory write contract/);
});
