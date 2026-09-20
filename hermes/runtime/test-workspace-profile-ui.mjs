import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { workspaceProfileUi } from './workspace-profile-ui.mjs';

test('profile adapter fails closed on changed upstream contracts', () => {
  const plugin = workspaceProfileUi();
  assert.equal(plugin.transform('unrelated', '/other.ts'), null);
  for (const name of ['server/profiles-browser.ts', 'screens/agents/hooks/use-operations.ts',
    'screens/agents/components/operations-agent-detail.tsx']) {
    assert.throws(() => plugin.transform('changed contract', '/src/' + name), /Unsupported Workspace profile/);
  }
});

test('pinned source transforms retain auth routes and share the real SOUL editor',
  { skip: !process.env.WORKSPACE_UPSTREAM_DIR }, () => {
    const transform = name => workspaceProfileUi().transform(
      fs.readFileSync(path.join(process.env.WORKSPACE_UPSTREAM_DIR, 'src', name), 'utf8'), '/src/' + name).code;
    const server = transform('server/profiles-browser.ts');
    assert.match(server, /soulPath: profileSoulPath/);
    assert.match(server, /profiles: listProfiles\(\), activeProfile: getActiveProfileName\(\)/);
    assert.match(server, /readProfileSoul/);
    const hook = transform('screens/agents/hooks/use-operations.ts');
    assert.match(hook, /await saveProfileSoul/);
    assert.match(hook, /if \(!currentAgent.soulPath && input.systemPrompt.trim\(\)\)/);
    assert.doesNotMatch(hook, /systemPrompt: readString\(parsed.systemPrompt\) \|\| fallbackSystemPrompt/);
    const detail = transform('screens/agents/components/operations-agent-detail.tsx');
    assert.match(detail, /originalPrompt/);
    assert.match(detail, /editingProfile.current === agent.id/);
    assert.match(detail, /then\(\(\) => setOriginalPrompt\(systemPrompt\)/);
    assert.match(detail, /Personal additions/);
  });
