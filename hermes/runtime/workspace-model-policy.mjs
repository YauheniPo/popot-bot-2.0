// Build-time adapters for the pinned Workspace. No tracked upstream edits.
const resolver = `import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { parse } from 'yaml'

export function resolveSwarmModelLabel(_label) {
  const home = process.env.HERMES_HOME || join(homedir(), '.hermes')
  const model = parse(readFileSync(join(home, 'config.yaml'), 'utf8'))?.model
  if (!model || typeof model.provider !== 'string' || !model.provider.trim() ||
      typeof model.default !== 'string' || !model.default.trim()) {
    throw new Error('Managed Swarm model requires shared Hermes provider and default')
  }
  return { provider: model.provider, default: model.default }
}
`;

function replaceOnce(source, anchor, replacement) {
  if (source.split(anchor).length !== 2) {
    throw new Error('Unsupported Workspace Swarm model contract; re-verify model policy');
  }
  return source.replace(anchor, replacement);
}

export function workspaceModelPolicy() {
  return {
    name: 'hermes-shared-swarm-model', enforce: 'pre',
    transform(source, id) {
      const path = id.split('?')[0];
      if (path.endsWith('/src/server/swarm-model-resolver.ts')) {
        replaceOnce(source, 'export function resolveSwarmModelLabel(', '');
        return { code: resolver, map: null };
      }
      if (!path.endsWith('/src/server/swarm-roster.ts')) return null;
      let code = replaceOnce(source, "model: z.string().default('Worker'),",
        "model: z.string().default('Worker').transform(managedModelLabel),");
      code = replaceOnce(code, "model: 'Worker',", 'model: managedModelLabel(),');
      return { map: null, code: `import { resolveSwarmModelLabel } from './swarm-model-resolver'
function managedModelLabel() {
  const model = resolveSwarmModelLabel(null)
  return model.provider + '/' + model.default
}
` + code };
    },
  };
}
