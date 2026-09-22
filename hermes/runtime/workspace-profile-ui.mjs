// Compatibility layer for the pinned, colocated Workspace. Auth/CSRF routes
// remain upstream-owned; SOUL writes reuse the versioned memory editor.
import { fileURLToPath } from 'node:url';

export function workspaceProfileUi() {
  const memory = fileURLToPath(new URL('./workspace-memory-files.mjs', import.meta.url));
  const prompts = fileURLToPath(new URL('./workspace-profile-prompts.mjs', import.meta.url));
  return { name: 'hermes-profile-instructions', enforce: 'pre', transform(source, id) {
    const file = id.split('?')[0];
    let code = source;
    const replace = (anchor, value, count = 1) => {
      if (code.split(anchor).length !== count + 1) throw new Error('Unsupported Workspace profile contract: ' + file);
      code = code.split(anchor).join(value);
    };
    if (file.endsWith('/src/routes/api/profiles/skills.ts')) {
      // Hermes scopes skills via a query parameter, not nested profile routes.
      replace('`/api/profiles/${encodeURIComponent(profile)}/skills`',
        '`/api/skills?profile=${encodeURIComponent(profile)}`');
      replace('          const items = Array.isArray(parsed) ? parsed : []', `          if (!Array.isArray(parsed)) {
            return json({ error: 'Dashboard returned an invalid profile skills list' }, { status: 502 })
          }
          const items = parsed`);
    } else if (file.endsWith('/src/routes/api/profiles/toggle-skill.ts')) {
      replace('`/api/profiles/${encodeURIComponent(profile)}/skills/toggle`', "'/api/skills/toggle'");
      replace('JSON.stringify({ name, enabled })', 'JSON.stringify({ name, enabled, profile })');
    } else if (file.endsWith('/src/screens/skills/skills-screen.tsx')) {
      replace('            <TabsPanel value="installed" className="pt-2">\n              <SkillsGrid', `            <TabsPanel value="installed" className="pt-2">
              {skillsQuery.isError ? (
                <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                  <p>Could not load skills for {effectiveProfile || 'the active profile'}.</p>
                  <p>{skillsQuery.error instanceof Error ? skillsQuery.error.message : 'Backend request failed.'}</p>
                  <Button variant="outline" disabled={skillsQuery.isFetching} onClick={() => void skillsQuery.refetch()}>
                    Retry
                  </Button>
                </div>
              ) : (
              <SkillsGrid`);
      replace('              />\n            </TabsPanel>\n\n            <TabsPanel value="marketplace"',
        '              />\n              )}\n            </TabsPanel>\n\n            <TabsPanel value="marketplace"');
    } else if (file.endsWith('/src/server/profiles-browser.ts')) {
      code = `import { readMemoryFile as readProfileSoul } from ${JSON.stringify(memory)};\n` + code;
      replace('  systemPrompt?: string', '  systemPrompt?: string\n  soulPath?: string');
      replace('  systemPrompt: string', '  systemPrompt: string\n  soulPath?: string');
      replace('function extractSystemPrompt(', `function profileSoulPath(config: Record<string, unknown>, profilePath: string): string | undefined {
  if (typeof config.system_prompt === 'string' && config.system_prompt.trim()) return undefined
  const relative = path.relative(getClaudeRoot(), profilePath)
  const candidate = relative ? relative.split(path.sep).join('/') + '/SOUL.md' : 'SOUL.md'
  try { readProfileSoul(candidate); return candidate }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined; throw error }
}

function extractSystemPrompt(`);
      replace("return safeReadText(soulPath).trim()", "return readProfileSoul(profileSoulPath(config, profilePath)!)");
      replace('  // Try dashboard first for split-host deployments', `  // This deployment shares Hermes home: prefer complete local profile metadata.
  if (fs.existsSync(path.join(getClaudeRoot(), 'config.yaml'))) {
    return { profiles: listProfiles(), activeProfile: getActiveProfileName() }
  }
  // Try dashboard first for split-host deployments`);
      replace('systemPrompt: extractSystemPrompt(config, profilePath)',
        'soulPath: profileSoulPath(config, profilePath),\n        systemPrompt: extractSystemPrompt(config, profilePath)', 2);
      replace('systemPrompt: extractSystemPrompt(config, root)',
        'soulPath: profileSoulPath(config, root),\n    systemPrompt: extractSystemPrompt(config, root)');
    } else if (file.endsWith('/src/screens/agents/hooks/use-operations.ts')) {
      code = `import { saveProfileSoul } from ${JSON.stringify(prompts)};\n` + code;
      replace('  systemPrompt?: string', '  systemPrompt?: string\n  soulPath?: string', 2);
      replace('systemPrompt: readString(row.systemPrompt) || undefined,',
        "systemPrompt: typeof row.systemPrompt === 'string' ? row.systemPrompt : undefined,\n      soulPath: readString(row.soulPath) || undefined,");
      replace("systemPrompt: profile.systemPrompt || '',", "systemPrompt: profile.systemPrompt || '',\n    soulPath: profile.soulPath,");
      replace('const fallbackSystemPrompt = readString(fallback?.systemPrompt)',
        "const fallbackSystemPrompt = typeof fallback?.systemPrompt === 'string' ? fallback.systemPrompt : ''");
      replace('description: readString(parsed.description) || fallbackDescription,',
        'description: fallback !== undefined ? fallbackDescription : readString(parsed.description),');
      replace('systemPrompt: readString(parsed.systemPrompt) || fallbackSystemPrompt,',
        'systemPrompt: fallback !== undefined ? fallbackSystemPrompt : readString(parsed.systemPrompt),');
      replace('      agentId: string\n      name: string', '      agentId: string\n      originalPrompt: string\n      name: string');
      replace("      // Persist model + system prompt to the profile's config.yaml so they\n      // survive across machines / clients.", `      // SOUL-backed profiles share the Memory editor, not a second YAML prompt.
      const currentAgent = normalizeAgentList(configQuery.data?.parsed?.agents?.list).find(a => a.id === input.agentId)
      if (!currentAgent) throw new Error('Profile disappeared; reload')
      if (currentAgent.soulPath) {
        await saveProfileSoul(currentAgent.soulPath, input.systemPrompt, input.originalPrompt)
      }`);
      replace('if (input.model.trim()) patch.model = input.model.trim()',
        'if (input.model.trim() && input.model.trim() !== currentAgent.model) patch.model = input.model.trim()');
      replace('if (input.systemPrompt.trim()) patch.system_prompt = input.systemPrompt.trim()',
        'if (!currentAgent.soulPath && input.systemPrompt.trim()) patch.system_prompt = input.systemPrompt.trim()');
    } else if (file.endsWith('/src/screens/agents/components/operations-agent-detail.tsx')) {
      replace('    agentId: string\n    name: string', '    agentId: string\n    originalPrompt: string\n    name: string');
      replace("const [systemPrompt, setSystemPrompt] = useState('')",
        "const [systemPrompt, setSystemPrompt] = useState('')\n  const [originalPrompt, setOriginalPrompt] = useState('')\n  const editingProfile = useRef<string | null>(null)");
      replace('    if (!agent || !open) return\n    setName(agent.name)',
        "    if (!agent || !open) { editingProfile.current = null; return }\n    if (editingProfile.current === agent.id) return\n    editingProfile.current = agent.id\n    setName(agent.name)");
      replace('setSystemPrompt(agent.meta.systemPrompt)', 'setSystemPrompt(agent.meta.systemPrompt)\n    setOriginalPrompt(agent.meta.systemPrompt)');
      replace('                  agentId: agent.id,', '                  agentId: agent.id,\n                  originalPrompt,');
      // Mutation onError already displays the failure; retain the draft on error.
      replace('                  systemPrompt,\n                })',
        '                  systemPrompt,\n                }).then(() => setOriginalPrompt(systemPrompt), () => undefined)');
      replace('            System Prompt\n', "            {agent.soulPath ? 'SOUL.md — live profile instructions' : 'System Prompt — legacy config.yaml'}\n");
      replace('          <textarea\n            value={systemPrompt}', `          <p className="text-xs text-[var(--theme-muted)]">
            {agent.soulPath ? 'Source: ' + agent.soulPath + '. Save writes this file and keeps a backup. Deploy restores the ANSIBLE MANAGED block; put custom rules under Personal additions. Start a new session to apply.' : 'Legacy YAML prompt; this profile has not been migrated to a SOUL-backed editor.'}
          </p>
          <textarea
            value={systemPrompt}`);
    } else return null;
    return { code, map: null };
  } };
}
