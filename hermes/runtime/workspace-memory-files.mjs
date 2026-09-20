// Server-only file policy for Workspace's authenticated memory editor.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

const agentName = /^agents(?:\.[a-z0-9_-]+)?\.md$/i;
const excluded = new Set(['node_modules', 'hermes-agent', 'backups', 'operator-state']);
const maxBytes = 512 * 1024;
export const memoryFileVersion = (content) => createHash('sha256').update(content).digest('hex');

export function createMemoryFiles({ home, workspace, parseConfig }) {
  const roots = { home: path.resolve(home), workspace: path.resolve(workspace) };

  function resolveMemoryFilePath(input) {
    if (typeof input !== 'string' || !input || input.includes('\\') || input.includes('\0')) {
      throw new Error('Path not allowed');
    }
    const parts = input.split('/');
    if (parts.some(part => !part || part.startsWith('.') || excluded.has(part))) throw new Error('Path not allowed');
    const external = parts[0] === 'workspace';
    const leaf = parts.at(-1);
    const instruction = agentName.test(leaf);
    const allowed = external ? instruction : (
      (parts.length === 1 && (['MEMORY.md', 'USER.md', 'SOUL.md'].includes(leaf) || instruction)) ||
      (['memory', 'memories'].includes(parts[0]) && /\.md$/i.test(leaf)) ||
      (parts[0] === 'profiles' && parts.length === 3 && (leaf === 'SOUL.md' || instruction)) ||
      (parts[0] === 'profiles' && parts.length === 4 && parts[2] === 'memories' && ['MEMORY.md', 'USER.md'].includes(leaf)) ||
      (input.startsWith('swarm/worktrees/') && parts.length >= 4 && instruction)
    );
    if (!allowed) throw new Error('Path not allowed');
    const root = external ? roots.workspace : roots.home;
    let fullPath = root;
    if (fs.lstatSync(root).isSymbolicLink()) throw new Error('Symlink root not allowed');
    for (const component of external ? parts.slice(1) : parts) {
      fullPath = path.join(fullPath, component);
      if (fs.lstatSync(fullPath).isSymbolicLink()) throw new Error('Symlink path not allowed');
    }
    const stat = fs.lstatSync(fullPath);
    if (!stat.isFile() || stat.nlink !== 1) throw new Error('File type not allowed');
    if (stat.size > maxBytes) throw new Error('File larger than 512 KiB is not allowed');
    return { fullPath, relativePath: input, stat };
  }

  function readMemoryFile(input) {
    const { fullPath } = resolveMemoryFilePath(input);
    const fd = fs.openSync(fullPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.nlink !== 1 || stat.size > maxBytes) throw new Error('File not allowed');
      return fs.readFileSync(fd, 'utf8');
    } finally { fs.closeSync(fd); }
  }

  function listMemoryFiles() {
    const results = [];
    let visited = 0;
    function walk(directory, prefix, depth = 0) {
      if (!fs.existsSync(directory) || fs.lstatSync(directory).isSymbolicLink()) return;
      if (depth > 20) throw new Error('Instruction tree exceeds editor depth limit');
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        if (++visited > 30_000) throw new Error('Instruction tree exceeds editor scan limit');
        if (entry.name.startsWith('.') || excluded.has(entry.name) || entry.isSymbolicLink()) continue;
        const relative = prefix + entry.name;
        if (entry.isDirectory()) {
          // Profiles expose instructions and native memory, never sessions/logs/caches.
          if (prefix.startsWith('profiles/') && prefix !== 'profiles/' &&
              !(prefix.split('/').length === 3 && entry.name === 'memories')) continue;
          if (prefix === 'swarm/' && entry.name !== 'worktrees') continue;
          if (prefix || ['memory', 'memories', 'profiles', 'swarm'].includes(entry.name)) {
            walk(path.join(directory, entry.name), relative + '/', depth + 1);
          }
        } else {
          try {
            const { stat } = resolveMemoryFilePath(relative);
            results.push({ path: relative, name: entry.name, size: stat.size, modified: stat.mtime.toISOString() });
          } catch { /* Unlisted files, links and oversized files are not editor targets. */ }
        }
      }
    }
    walk(roots.home, '');
    walk(roots.workspace, 'workspace/');
    return results.sort((a, b) => a.path.localeCompare(b.path));
  }

  function writeMemoryFile(input, content, expectedVersion) {
    const { fullPath, stat } = resolveMemoryFilePath(input);
    if (typeof content !== 'string' || Buffer.byteLength(content) > maxBytes) throw new Error('Content not allowed');
    if (typeof expectedVersion !== 'string' || !/^[a-f0-9]{64}$/.test(expectedVersion)) throw new Error('File version is required; reload');
    const previous = readMemoryFile(input);
    if (memoryFileVersion(previous) !== expectedVersion) throw new Error('File changed; reload before saving');
    if (previous === content) return;
    const backupDir = path.join(roots.home, '.memory-editor-backups');
    fs.mkdirSync(backupDir, { mode: 0o700, recursive: true });
    if (fs.lstatSync(backupDir).isSymbolicLink()) throw new Error('Backup symlink not allowed');
    fs.chmodSync(backupDir, 0o700);
    fs.writeFileSync(path.join(backupDir, randomUUID() + '.json'),
      JSON.stringify({ path: input, content: previous }), { flag: 'wx', mode: 0o600 });
    const temporary = path.join(path.dirname(fullPath), '.memory-edit-' + randomUUID());
    try {
      fs.writeFileSync(temporary, content, { flag: 'wx', mode: stat.mode & 0o777 });
      resolveMemoryFilePath(input);
      if (memoryFileVersion(readMemoryFile(input)) !== expectedVersion) throw new Error('File changed; reload before saving');
      fs.renameSync(temporary, fullPath);
    } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  }

  function searchMemoryFiles(query) {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    const results = [];
    for (const file of listMemoryFiles()) {
      const lines = readMemoryFile(file.path).split(/\r?\n/);
      for (let index = 0; index < lines.length; index++) {
        if (lines[index].toLowerCase().includes(needle)) results.push({ path: file.path, line: index + 1, text: lines[index] });
        if (results.length === 200) return results;
      }
    }
    return results;
  }

  function configuredLimit(profile, key) {
    if (!parseConfig || !key) return null;
    // The profile directory was already validated by resolveMemoryFilePath.
    const configPath = path.join(roots.home, ...(profile === null ? [] : ['profiles', profile]), 'config.yaml');
    let fd;
    try {
      fd = fs.openSync(configPath, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.nlink !== 1 || stat.size > maxBytes) return null;
      const value = parseConfig(fs.readFileSync(fd, 'utf8'))?.[key];
      return Number.isSafeInteger(value) && value > 0 ? value : null;
    } catch { return null; } // No guess at upstream defaults; never expose config/errors.
    finally { if (fd !== undefined) fs.closeSync(fd); }
  }

  function describeMemoryFile(input, content = readMemoryFile(input)) {
    const { fullPath } = resolveMemoryFilePath(input);
    const parts = input.split('/');
    const leaf = parts.at(-1);
    const profile = parts[0] === 'profiles' ? parts[1] : 'main';
    const nativeMemory = input === 'memories/' + leaf ||
      (parts[0] === 'profiles' && parts.length === 4 && parts[2] === 'memories');
    let kind = 'reference';
    let applicability = 'Reference only; not automatically loaded. Read explicitly when relevant.';
    if (nativeMemory && ['MEMORY.md', 'USER.md'].includes(leaf)) {
      kind = leaf === 'USER.md' ? 'user' : 'memory';
      applicability = 'Profile-scoped memory snapshot; native delegates skip main memory. Start a new session after edits.';
    } else if (leaf === 'SOUL.md') {
      kind = 'identity';
      applicability = 'Identity for this HERMES_HOME; native delegates do not inherit it. Start a new session after edits.';
    } else if (agentName.test(leaf)) {
      kind = 'instructions';
      const order = ['AGENTS.override.md', 'AGENTS.md', 'agents.md'];
      if (order.includes(leaf)) {
        const winner = order.find(name => fs.existsSync(path.join(path.dirname(fullPath), name)));
        applicability = winner !== leaf ? `Higher-priority candidate ${winner} exists; actual selection depends on readable, non-empty content and cwd.` :
          'Candidate project instructions: inclusion depends on cwd and the git-root directory chain, not editor visibility.';
      }
    }
    const limit = configuredLimit(parts[0] === 'profiles' ? parts[1] : null,
      kind === 'memory' ? 'memory_char_limit' : kind === 'user' ? 'user_char_limit' : null);
    const characters = [...content].length;
    const managedBlocks = [...new Set([...content.matchAll(/<!-- BEGIN ((?:ANSIBLE|HERMES) MANAGED [A-Z0-9 _-]+) -->/g)].map(match => match[1]))];
    return { profile, kind, characters, limit, overLimit: limit !== null && characters > limit,
      managedBlocks, applicability, loadedInSession: null };
  }
  return { listMemoryFiles, readMemoryFile, writeMemoryFile, searchMemoryFiles,
    describeMemoryFile, resolveMemoryFilePath, getMemoryWorkspaceRoot: () => roots.home };
}

const home = process.env.HERMES_HOME || process.env.CLAUDE_HOME || path.join(os.homedir(), '.hermes');
export const createDefaultMemoryFiles = options => createMemoryFiles({
  ...options, home, workspace: process.env.HERMES_INSTRUCTIONS_ROOT || path.join(home, 'workspace'),
});
export const { listMemoryFiles, readMemoryFile, writeMemoryFile, searchMemoryFiles,
  describeMemoryFile, resolveMemoryFilePath, getMemoryWorkspaceRoot } = createDefaultMemoryFiles();
