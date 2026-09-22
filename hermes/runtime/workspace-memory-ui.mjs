// Compatibility adapter for the pinned memory UI. Keep upstream auth/CSRF gates.
import { fileURLToPath } from 'node:url';

export function workspaceMemoryUi() {
  const modulePath = fileURLToPath(new URL('./workspace-memory-files.mjs', import.meta.url));
  return { name: 'hermes-memory-instructions', enforce: 'pre', transform(source, id) {
    const file = id.split('?')[0];
    let code = source;
    const replace = (anchor, value) => {
      if (code.split(anchor).length !== 2) throw new Error('Unsupported Workspace memory contract: ' + file);
      code = code.replace(anchor, value);
    };
    if (file.endsWith('/src/server/memory-browser.ts')) {
      for (const name of ['listMemoryFiles', 'readMemoryFile', 'searchMemoryFiles', 'resolveMemoryFilePath']) {
        if (!source.includes('export function ' + name + '(')) throw new Error('Unsupported Workspace memory browser');
      }
      code = `import YAML from 'yaml';
import { createDefaultMemoryFiles, memoryFileVersion } from ${JSON.stringify(modulePath)};
export { memoryFileVersion };
export const { listMemoryFiles, readMemoryFile, writeMemoryFile, searchMemoryFiles,
  describeMemoryFile, resolveMemoryFilePath, getMemoryWorkspaceRoot } = createDefaultMemoryFiles({ parseConfig: YAML.parse });`;
    } else if (file.endsWith('/src/routes/api/memory/read.ts')) {
      replace('import { readMemoryFile }', 'import { readMemoryFile, memoryFileVersion, describeMemoryFile }');
      replace('return json({ path: pathParam, content })', 'return json({ path: pathParam, content, version: memoryFileVersion(content), metadata: describeMemoryFile(pathParam, content) })');
    } else if (file.endsWith('/src/routes/api/memory/write.ts')) {
      replace("import fs from 'node:fs'\nimport path from 'node:path'\n", '');
      replace('import { getMemoryWorkspaceRoot }', 'import { writeMemoryFile }');
      const start = code.indexOf('function validateMemoryWritePath(');
      const end = code.indexOf('export const Route =');
      if (start < 0 || end < start) throw new Error('Unsupported Workspace memory write contract');
      code = code.slice(0, start) + code.slice(end);
      replace('content?: unknown', 'content?: unknown\n            version?: unknown');
      replace('const { relativePath, fullPath } = validateMemoryWritePath(body.path)', 'const relativePath = body.path');
      replace("const content = typeof body.content === 'string' ? body.content : ''", 'const content = body.content');
      replace("fs.mkdirSync(path.dirname(fullPath), { recursive: true })\n          fs.writeFileSync(fullPath, content, 'utf-8')",
        'writeMemoryFile(relativePath, content, body.version)');
      replace('return json({ error: message }, { status })',
        "return json({ error: /ENOENT/.test(message) ? 'File not found' : message }, { status: /changed; reload/.test(message) ? 409 : /not allowed|required/.test(message) ? 400 : /ENOENT/.test(message) ? 404 : status })");
    } else if (file.endsWith('/src/screens/memory/memory-browser-screen.tsx')) {
      replace('type ReadResponse = { path?: string; content?: string }', `type ReadResponse = { path?: string; content?: string; version?: string;
  metadata?: { profile: string; kind: string; characters: number; limit: number | null;
    overLimit: boolean; managedBlocks: string[]; applicability: string; loadedInSession: null } }`);
      replace("const [draftContent, setDraftContent] = useState('')", "const [draftContent, setDraftContent] = useState('')\n  const [editVersion, setEditVersion] = useState<string | undefined>()");
      replace('function handleStartEditing() {', 'function handleStartEditing() {\n    setEditVersion(contentQuery.data?.version)');
      replace('content: draftContent }', 'content: draftContent, version: editVersion }');
      replace("file.path.startsWith('memory/') || file.path.startsWith('memories/'),", "file.path !== 'MEMORY.md',");
      replace('                  memory/ or memories/', '                  Memory &amp; instructions');
      replace('No files in memory/ or memories/', 'No memory or instruction files');
      // Display policy next to the editor, including managed-block caveats.
      replace('<div className="truncate font-mono text-sm text-primary-900 dark:text-neutral-100">',
        `<p className="text-xs text-primary-500">Paths are relative to Hermes home; workspace/ is the configured working directory. Saving affects real instructions. Deploy restores managed blocks. Start a new session to apply. A backup is kept on save.</p>
              <div className="text-xs text-primary-500" aria-live="polite">
                {contentQuery.data?.metadata && <>
                  <p>Profile: {contentQuery.data.metadata.profile} · Purpose: {contentQuery.data.metadata.kind} · Loaded in this session: unknown</p>
                  <p>{contentQuery.data?.metadata?.applicability}</p>
                  <p>File characters: {isEditing ? [...draftContent].length : contentQuery.data.metadata.characters}
                    {['memory', 'user'].includes(contentQuery.data.metadata.kind) && <> / configured budget: {contentQuery.data.metadata.limit ?? 'unknown (not explicitly configured)'}. Raw file count, not prompt tokens.</>}
                  </p>
                  {contentQuery.data.metadata.limit !== null && (isEditing ? [...draftContent].length : contentQuery.data.metadata.characters) > contentQuery.data.metadata.limit && <p role="status">Above configured budget. Consolidate verified facts; saving does not increase Hermes memory limits.</p>}
                  {contentQuery.data.metadata.managedBlocks.length > 0 && <p>Restored on deploy: {contentQuery.data.metadata.managedBlocks.join(', ')}. Put personal notes outside these blocks.</p>}
                </>}
              </div>
              <div className="truncate font-mono text-sm text-primary-900 dark:text-neutral-100">`);
    } else return null;
    return { code, map: null };
  } };
}
