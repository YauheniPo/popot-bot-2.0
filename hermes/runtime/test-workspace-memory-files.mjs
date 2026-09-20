import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import { spawnSync } from 'node:child_process';
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

test('rejects non-string, empty, backslash and NUL path inputs before any lookup', (t) => {
  const { api } = fixture(t);
  for (const bad of [null, undefined, 42, {}, [], '', 'memories\\USER.md', 'memories\u0000USER.md']) {
    assert.throws(() => api.readMemoryFile(bad), /not allowed/i, String(bad));
    assert.throws(() => api.writeMemoryFile(bad, 'x', memoryFileVersion('x')), /not allowed/i, String(bad));
  }
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

test('metadata identifies shared and environment managed blocks', t => {
  const { api, put } = fixture(t);
  put('workspace/AGENTS.md', '<!-- BEGIN HERMES MANAGED COMMON -->\nRules\n<!-- END HERMES MANAGED COMMON -->\n' +
    '<!-- BEGIN HERMES MANAGED ENVIRONMENT -->\nContainer\n<!-- END HERMES MANAGED ENVIRONMENT -->\n' +
    '<!-- BEGIN ANSIBLE MANAGED DELEGATION POLICY -->\nTeam\n<!-- END ANSIBLE MANAGED DELEGATION POLICY -->');
  assert.deepEqual(api.describeMemoryFile('workspace/AGENTS.md').managedBlocks,
    ['HERMES MANAGED COMMON', 'HERMES MANAGED ENVIRONMENT', 'ANSIBLE MANAGED DELEGATION POLICY']);
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

test('the module default home falls back through CLAUDE_HOME to the user home', async () => {
  // Line 172 picks home from HERMES_HOME, else CLAUDE_HOME, else ~/.hermes.
  // Each fallback is a separate branch, so run the module fresh for each env.
  const source = './workspace-memory-files.mjs';
  const load = async (env) => {
    const saved = { ...process.env };
    for (const key of ['HERMES_HOME', 'CLAUDE_HOME', 'HERMES_INSTRUCTIONS_ROOT']) delete process.env[key];
    Object.assign(process.env, env);
    try {
      return await import(`${source}?fallback=${Math.random()}`);
    } finally {
      for (const key of Object.keys(process.env)) delete process.env[key];
      Object.assign(process.env, saved);
    }
  };
  const withHermes = await load({ HERMES_HOME: '/tmp/fixture-hermes-home' });
  assert.equal(withHermes.getMemoryWorkspaceRoot(), '/tmp/fixture-hermes-home');
  const withClaude = await load({ CLAUDE_HOME: '/tmp/fixture-claude-home' });
  assert.equal(withClaude.getMemoryWorkspaceRoot(), '/tmp/fixture-claude-home');
  const withNeither = await load({});
  assert.ok(withNeither.getMemoryWorkspaceRoot().length > 0);
});

test('a symbolic-link root and an oversized file are both refused', (t) => {
  const { home, workspace, put } = fixture(t);
  // The non-external root is the configured home; if it is a symlink, reading
  // any home-relative instruction must be refused outright.
  const realHome = path.join(path.dirname(home), 'real-home');
  fs.mkdirSync(realHome, { recursive: true });
  fs.writeFileSync(path.join(realHome, 'AGENTS.md'), 'x');
  const linkedHome = path.join(path.dirname(home), 'linked-home');
  fs.symlinkSync(realHome, linkedHome);
  const linkedApi = createMemoryFiles({ home: linkedHome, workspace });
  assert.throws(() => linkedApi.readMemoryFile('AGENTS.md'), /Symlink root not allowed/);

  // Oversized file: resolveMemoryFilePath rejects it before the fd read.
  put('workspace/AGENTS.big.md', 'x'.repeat(512 * 1024 + 1));
  assert.throws(() => createMemoryFiles({ home, workspace }).readMemoryFile('workspace/AGENTS.big.md'),
    /larger than 512 KiB/);
});

test('unreadable and non-regular entries are skipped, not fatal', (t) => {
  const { api, home, workspace, put } = fixture(t);
  put('home/memories/USER.md', 'ok');
  const fifo = path.join(home, 'memories', 'AGENTS.pipe.md');
  // A FIFO is not a regular file; resolving it must not silently succeed.
  spawnSync('mkfifo', [fifo]);
  try {
    assert.throws(() => api.readMemoryFile('memories/AGENTS.pipe.md'), /not allowed|ENOENT/i);
    // Listing tolerates it and still returns the regular file.
    assert.ok(api.listMemoryFiles().some(f => f.path === 'memories/USER.md'));
  } finally { fs.rmSync(fifo, { force: true }); }
});

test('a deep or very wide instruction tree trips the editor limits', (t) => {
  const { api, home, put } = fixture(t);
  // The walk only descends recognized top-level names, so nest under memories/.
  let dir = 'memories';
  for (let i = 0; i < 22; i += 1) {
    dir = `${dir}/d${i}`;
    put(`home/${dir}/USER.md`, 'level');
  }
  assert.throws(() => api.listMemoryFiles(), /depth limit/);

  const { api: wideApi, home: wideHome } = fixture(t);
  const wide = path.join(wideHome, 'memories');
  fs.mkdirSync(wide, { recursive: true });
  for (let i = 0; i < 30_001; i += 1) fs.writeFileSync(path.join(wide, `f${i}.txt`), 'x');
  assert.throws(() => wideApi.listMemoryFiles(), /scan limit/);
});

test('swarm keeps only the worktrees directory', (t) => {
  const { api, put } = fixture(t);
  put('home/swarm/worktrees/w/AGENTS.md', 'worker');
  put('home/swarm/other/AGENTS.md', 'skip me');
  put('home/swarm/swarm.yaml', 'workers: []');
  const files = api.listMemoryFiles().map(f => f.path);
  assert.ok(files.includes('swarm/worktrees/w/AGENTS.md'));
  assert.ok(!files.some(f => f.startsWith('swarm/other/')));
});

test('saving identical content is a no-op and an oversized write is refused', (t) => {
  const { api, put } = fixture(t);
  const file = put('home/memories/USER.md', 'same');
  const version = memoryFileVersion('same');
  // Equal content returns before writing a backup.
  assert.equal(api.writeMemoryFile('memories/USER.md', 'same', version), undefined);
  assert.equal(fs.readFileSync(file, 'utf8'), 'same');
  assert.throws(() => api.writeMemoryFile('memories/USER.md', 'y'.repeat(512 * 1024 + 1), version),
    /Content not allowed/);
  // A non-string body is refused by the same branch.
  assert.throws(() => api.writeMemoryFile('memories/USER.md', 42, version), /Content not allowed/);
});

test('search returns nothing for a blank query', (t) => {
  const { api } = fixture(t);
  assert.deepEqual(api.searchMemoryFiles('   '), []);
});

test('config limits ignore a non-regular, hardlinked or oversized config file', (t) => {
  const { home, workspace, put } = fixture(t);
  put('home/memories/MEMORY.md', 'fact');
  const cfg = put('home/config.yaml', JSON.stringify({ memory_char_limit: 12 }));
  const api = createMemoryFiles({ home, workspace, parseConfig: JSON.parse });
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, 12);
  // Hardlinked config is refused (nlink !== 1).
  fs.linkSync(cfg, path.join(home, 'config.yaml.link'));
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, null);
  // Missing config exercises the catch branch.
  fs.rmSync(cfg);
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, null);
});

test('hardlinked and oversized instruction targets are refused before read', (t) => {
  const { home, workspace, put } = fixture(t);
  const api = createMemoryFiles({ home, workspace });
  // nlink !== 1 on the *target* (line 50) - a hardlink to a regular file.
  const real = put('home/memories/USER.md', 'x');
  fs.linkSync(real, path.join(home, 'memories', 'USER.link.md'));
  assert.throws(() => api.readMemoryFile('memories/USER.link.md'), /File type not allowed|not allowed/);

  // Oversized target reaches the same guard from resolveMemoryFilePath.
  put('home/AGENTS.big.md', 'x'.repeat(512 * 1024 + 1));
  assert.throws(() => api.readMemoryFile('AGENTS.big.md'), /larger than 512 KiB/);
});

test('a missing or symlinked directory is skipped while walking', (t) => {
  const { api, home } = fixture(t);
  // A symlinked directory entry under memories is skipped, not followed.
  const outside = path.join(path.dirname(home), 'outside-memories');
  fs.mkdirSync(outside, { recursive: true });
  fs.writeFileSync(path.join(outside, 'USER.md'), 'external');
  fs.mkdirSync(path.join(home, 'memories'), { recursive: true });
  fs.symlinkSync(outside, path.join(home, 'memories', 'linked'));
  const files = api.listMemoryFiles().map(f => f.path);
  assert.ok(!files.some(f => f.includes('linked')));
  // Removing a directory between existsSync and the walk must not throw.
  fs.rmSync(path.join(home, 'memories'), { recursive: true, force: true });
  assert.ok(Array.isArray(api.listMemoryFiles()));
});

test('a symlinked memory backup directory blocks the save', (t) => {
  const { api, home, put } = fixture(t);
  const file = put('home/memories/USER.md', 'before');
  const outside = path.join(path.dirname(home), 'outside-backups');
  fs.mkdirSync(outside, { recursive: true });
  fs.symlinkSync(outside, path.join(home, '.memory-editor-backups'));
  assert.throws(() => api.writeMemoryFile('memories/USER.md', 'after', memoryFileVersion('before')),
    /Backup symlink not allowed/);
  assert.equal(fs.readFileSync(file, 'utf8'), 'before');
  assert.equal(fs.readdirSync(outside).length, 0);
});

test('a concurrent change during save is detected and the temp file removed', (t) => {
  const { api, home, workspace, put } = fixture(t);
  const file = put('home/memories/USER.md', 'first');
  const version = memoryFileVersion('first');
  // Rewrite the file after the version check reads it: the re-check before
  // rename must reject the save and the finally block must clean up.
  const realWrite = fs.writeFileSync;
  let calls = 0;
  fs.writeFileSync = (...args) => {
    calls += 1;
    if (calls === 2) {
      const result = realWrite(...args);
      api2.writeMemoryFile('memories/USER.md', 'racing', memoryFileVersion('first'));
      return result;
    }
    return realWrite(...args);
  };
  const api2 = createMemoryFiles({ home, workspace });
  try {
    assert.throws(() => api.writeMemoryFile('memories/USER.md', 'second', version),
      /File changed; reload/);
  } finally { fs.writeFileSync = realWrite; }
  assert.equal(fs.readdirSync(path.join(home, 'memories')).some(n => n.startsWith('.memory-edit-')), false);
});

test('a search stops once 200 matches are collected', (t) => {
  const { api, home } = fixture(t);
  const dir = path.join(home, 'memories');
  fs.mkdirSync(dir, { recursive: true });
  for (let i = 0; i < 260; i += 1) {
    fs.writeFileSync(path.join(dir, `AGENTS.f${i}.md`), `needle ${i}`);
  }
  assert.equal(api.searchMemoryFiles('needle').length, 200);
});

test('an unreadable config file yields no limit guess', (t) => {
  const { home, workspace, put } = fixture(t);
  put('home/memories/MEMORY.md', 'fact');
  const blocked = path.join(home, 'config.yaml');
  fs.mkdirSync(blocked);  // a directory is not a regular file
  const api = createMemoryFiles({ home, workspace, parseConfig: JSON.parse });
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, null);
});

test('a config parser that throws yields no limit guess', (t) => {
  const { home, workspace, put } = fixture(t);
  put('home/memories/MEMORY.md', 'fact');
  put('home/config.yaml', 'not: valid: yaml: at all');
  // The read succeeds but the parser throws, so the catch branch returns null.
  const api = createMemoryFiles({ home, workspace, parseConfig: () => { throw new Error('bad yaml'); } });
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, null);
});

test('readMemoryFile refuses a non-regular target for each separate reason', (t) => {
  const { api, home } = fixture(t);
  // (a) a directory is not a regular file - the !isFile side of line 50.
  fs.mkdirSync(path.join(home, 'AGENTS.dir.md'), { recursive: true });
  assert.throws(() => api.readMemoryFile('AGENTS.dir.md'), /not allowed|EISDIR|ENOENT/i);
  // (b) an oversized target - the size side of line 50.
  const big = path.join(home, 'AGENTS.huge.md');
  fs.writeFileSync(big, 'x'.repeat(512 * 1024 + 1));
  assert.throws(() => api.readMemoryFile('AGENTS.huge.md'), /larger than 512 KiB/);
});

test('the walk returns early for a missing or symlinked directory', (t) => {
  const { api, home } = fixture(t);
  fs.mkdirSync(path.join(home, 'memories'), { recursive: true });
  fs.writeFileSync(path.join(home, 'memories', 'USER.md'), 'ok');
  // A symlinked subdirectory directly under a walked root is skipped by walk().
  const outside = path.join(path.dirname(home), 'outside-walk');
  fs.mkdirSync(outside, { recursive: true });
  fs.writeFileSync(path.join(outside, 'USER.md'), 'external');
  fs.symlinkSync(outside, path.join(home, 'memories', 'linked'));
  const files = api.listMemoryFiles().map(f => f.path);
  assert.ok(files.includes('memories/USER.md'));
  assert.ok(!files.some(f => f.includes('linked')));
});

test('a config whose parser throws yields no limit guess', (t) => {
  const { home, workspace, put } = fixture(t);
  put('home/memories/MEMORY.md', 'fact');
  put('home/config.yaml', '{not json');
  // The fd opens and fstat passes, then parseConfig throws -> catch returns null.
  const api = createMemoryFiles({ home, workspace, parseConfig: () => { throw new Error('bad'); } });
  assert.equal(api.describeMemoryFile('memories/MEMORY.md').limit, null);
});
