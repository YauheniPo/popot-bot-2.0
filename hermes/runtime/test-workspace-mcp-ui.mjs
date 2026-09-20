import assert from 'node:assert/strict';
import { test } from 'node:test';
import { workspaceMcpUi } from './workspace-mcp-ui.mjs';

const id = '/checkout/src/screens/mcp/components/mcp-server-card.tsx';
const source = '<dd>{server.discoveredToolsCount}</dd></dl>';

test('unknown or failed discovery is not presented as zero; last Test is labeled', () => {
  const result = workspaceMcpUi().transform(source, id);
  assert.match(result.code, /server.status === 'connected'/);
  assert.match(result.code, /server.lastTestedAt/);
  assert.match(result.code, /Last test/);
  assert.match(result.code, /Not tested/);
  assert.match(result.code, /—/);
  assert.equal(workspaceMcpUi().transform(source, '/other/card.tsx'), null);
});

test('pinned UI contract fails visibly when the source changes', () => {
  assert.throws(() => workspaceMcpUi().transform('new UI contract', id), /Unsupported Workspace MCP card/);
});

const routeSource = `async function mcpFetch(path: string, init: RequestInit): Promise<Response> {
  const capabilities = getCapabilities()
  if (capabilities.dashboard.available) {
    return dashboardFetch(path, init)
  }
  const headers = new Headers(init.headers)
  if (BEARER_TOKEN && !headers.has('Authorization')) {
    headers.set('Authorization', \`Bearer \${BEARER_TOKEN}\`)
  }
  return fetch(\`\${CLAUDE_API}\${path}\`, { ...init, headers })
}`;

test('all managed MCP HTTP operations keep Dashboard as their backend', () => {
  for (const route of ['mcp', 'mcp/configure', 'mcp/test', 'mcp/discover', 'mcp/$name']) {
    const result = workspaceMcpUi().transform(routeSource, `/checkout/src/routes/api/${route}.ts?build`);
    assert.equal(result.code, 'async function mcpFetch(path: string, init: RequestInit): Promise<Response> {\n  return dashboardFetch(path, init)\n}');
  }
  assert.equal(workspaceMcpUi().transform(routeSource, '/checkout/src/routes/api/chat.ts'), null);
});

test('changed or duplicated upstream MCP routing fails the build visibly', () => {
  const path = '/checkout/src/routes/api/mcp.ts';
  for (const source of ['changed upstream', routeSource + routeSource]) {
    assert.throws(() => workspaceMcpUi().transform(source, path), /Unsupported Workspace MCP routing/);
  }
});
