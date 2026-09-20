import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createMemoryFiles, memoryFileVersion } from './workspace-memory-files.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-memory-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const home = path.join(root, 'home'), workspace = path.join(root, 'workspace');
  const put = (name, text = 'fixture') => {
    const file = path.join(root, name);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, text);
    return file;
  };
  put('home/SOUL.md', 'identity');
  put('workspace/AGENTS.md', 'default instructions');
  return { root, home, workspace, put, api: createMemoryFiles({ home, workspace }) };
}

test('lists and searches memory plus default, additional and profile instructions', (t) => {
  const { api, put } = fixture(t);
  const allowed = ['home/memories/USER.md', 'home/AGENTS.md', 'home/profiles/builder/SOUL.md',
    'home/swarm/worktrees/builder/nested/AGENTS.md', 'workspace/repo/AGENTS.override.md',
    'workspace/AGENTS.extra.md'];
  for (const name of allowed) put(name, 'instruction fixture');
  put('home/hermes-agent/AGENTS.md');
  put('workspace/repo/.git/AGENTS.md');
  put('workspace/repo/node_modules/pkg/AGENTS.md');
  put('workspace/repo/README.md');
  const files = api.listMemoryFiles().map(file => file.path);
  assert.deepEqual(new Set(files), new Set(['SOUL.md', 'workspace/AGENTS.md',
    ...allowed.map(name => name.replace(/^home\//, ''))]));
  assert.equal(api.readMemoryFile('workspace/AGENTS.md'), 'default instructions');
  assert.equal(api.searchMemoryFiles('instruction').length, 7);
});

test('edits real instruction files, preserves a backup and rejects stale saves', (t) => {
  const { api, home, workspace } = fixture(t);
  const version = memoryFileVersion('default instructions');
  api.writeMemoryFile('workspace/AGENTS.md', 'updated', version);
  assert.equal(fs.readFileSync(path.join(workspace, 'AGENTS.md'), 'utf8'), 'updated');
  const backups = fs.readdirSync(path.join(home, '.memory-editor-backups'));
  assert.equal(backups.length, 1);
  const backupPath = path.join(home, '.memory-editor-backups', backups[0]);
  const backup = JSON.parse(fs.readFileSync(backupPath, 'utf8'));
  assert.equal(backup.content, 'default instructions');
  assert.equal(backup.path, 'workspace/AGENTS.md');
  assert.equal(fs.statSync(backupPath).mode & 0o777, 0o600);
  assert.throws(() => api.writeMemoryFile('workspace/AGENTS.md', 'stale', version), /changed.*reload/i);
  assert.throws(() => api.writeMemoryFile('SOUL.md', 'no version'), /version.*required/i);
  assert.equal(fs.readFileSync(path.join(workspace, 'AGENTS.md'), 'utf8'), 'updated');
  api.writeMemoryFile('SOUL.md', 'new identity', memoryFileVersion('identity'));
  assert.equal(api.readMemoryFile('SOUL.md'), 'new identity');
});

test('rejects traversal, secrets, symlink directories/files and hardlinked targets', (t) => {
  const { api, root, home, workspace, put } = fixture(t);
  const secret = put('outside/private.md', 'not allowed');
  fs.symlinkSync(secret, path.join(home, 'AGENTS.md'));
  fs.symlinkSync(path.join(root, 'outside'), path.join(workspace, 'linked'));
  fs.linkSync(secret, path.join(workspace, 'AGENTS.extra.md'));
  for (const name of ['../outside/private.md', '/etc/passwd', '.env', 'config.yaml',
    'hermes-agent/AGENTS.md', 'workspace/repo/.git/AGENTS.md', 'AGENTS.md',
    'workspace/linked/AGENTS.md', 'workspace/AGENTS.extra.md']) {
    assert.throws(() => api.readMemoryFile(name), /not allowed|ENOENT/i, name);
    assert.throws(() => api.writeMemoryFile(name, 'bad', memoryFileVersion('not allowed')), /not allowed|ENOENT/i, name);
  }
  assert.equal(api.listMemoryFiles().some(file => file.path.includes('linked') || file.path === 'AGENTS.md'), false);
  assert.equal(fs.readFileSync(secret, 'utf8'), 'not allowed');
});

test('profile memory stays isolated and exposes purpose, configured budget and managed blocks', t => {
  const { home, workspace, put } = fixture(t);
  const api = createMemoryFiles({ home, workspace, parseConfig: JSON.parse });
  put('home/config.yaml', JSON.stringify({ memory_char_limit: 20, user_char_limit: 15 }));
  put('home/profiles/builder/config.yaml', JSON.stringify({ memory_char_limit: 8 }));
  put('home/profiles/builder/memories/MEMORY.md', 'Русский😀текст');
  put('home/profiles/builder/memories/USER.md', 'private role user');
  put('home/profiles/builder/sessions/notes.md', 'not memory');
  put('home/memories/MEMORY.md', 'main memory');
  put('home/memories/USER.md', 'user preferences');
  put('home/profiles/main/memories/MEMORY.md', 'named main profile');
  put('home/profiles/main/config.yaml', JSON.stringify({memory_char_limit: 77}));
  put('home/SOUL.md', '<!-- BEGIN ANSIBLE MANAGED RESPONSE LANGUAGE -->\npolicy\n<!-- END ANSIBLE MANAGED RESPONSE LANGUAGE -->');
  const files = api.listMemoryFiles().map(file => file.path);
  assert.ok(files.includes('profiles/builder/memories/MEMORY.md'));
  assert.ok(files.includes('profiles/builder/memories/USER.md'));
  assert.ok(!files.some(file => file.includes('sessions/')));
  const meta = api.describeMemoryFile('profiles/builder/memories/MEMORY.md');
  assert.equal(meta.profile, 'builder');
  assert.equal(meta.kind, 'memory');
  assert.equal(meta.limit, 8);
  assert.equal(meta.characters, [...'Русский😀текст'].length);
  assert.equal(meta.overLimit, true);
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, 20);
  assert.equal(api.describeMemoryFile('memories/USER.md').limit, 15);
  assert.equal(api.describeMemoryFile('profiles/main/memories/MEMORY.md').limit, 77);
  assert.equal(api.describeMemoryFile('profiles/builder/memories/USER.md').limit, null);
  assert.deepEqual(api.describeMemoryFile('SOUL.md').managedBlocks, ['ANSIBLE MANAGED RESPONSE LANGUAGE']);
  api.writeMemoryFile('profiles/builder/memories/MEMORY.md', 'new role fact', memoryFileVersion('Русский😀текст'));
  assert.equal(api.readMemoryFile('memories/MEMORY.md'), 'main memory');
});

test('instruction metadata distinguishes override, reference-only and unknown runtime context', t => {
  const { api, put } = fixture(t);
  put('workspace/AGENTS.override.md', 'override');
  put('workspace/AGENTS.extra.md', 'reference');
  assert.match(api.describeMemoryFile('workspace/AGENTS.md').applicability, /AGENTS.override.md/);
  assert.match(api.describeMemoryFile('workspace/AGENTS.extra.md').applicability, /not automatically loaded/);
  assert.match(api.describeMemoryFile('workspace/AGENTS.override.md').applicability, /cwd/);
  assert.equal(api.describeMemoryFile('workspace/AGENTS.md').loadedInSession, null);
});

test('metadata never follows config symlinks or exposes config content', t => {
  const { api, home, workspace, put } = fixture(t);
  const target = put('outside/config.yaml', '{"memory_char_limit":99,"secret":"fixture-only"}');
  fs.symlinkSync(target, path.join(home, 'config.yaml'));
  put('home/memories/MEMORY.md', 'fact');
  const withParser = createMemoryFiles({ home, workspace, parseConfig: JSON.parse });
  const meta = withParser.describeMemoryFile('memories/MEMORY.md');
  assert.equal(meta.limit, null);
  assert.ok(!JSON.stringify(meta).includes('fixture-only'));
  assert.throws(() => api.describeMemoryFile('config.yaml'), /not allowed/);
});
