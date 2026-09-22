// Build-time compatibility for the pinned upstream. Tracked sources stay intact.
const mcpFetchBody = `  const capabilities = getCapabilities()
  if (capabilities.dashboard.available) {
    return dashboardFetch(path, init)
  }
  const headers = new Headers(init.headers)
  if (BEARER_TOKEN && !headers.has('Authorization')) {
    headers.set('Authorization', \`Bearer \${BEARER_TOKEN}\`)
  }
  return fetch(\`\${CLAUDE_API}\${path}\`, { ...init, headers })`;

export function workspaceMcpUi() {
  return {
    name: 'hermes-mcp-card', enforce: 'pre',
    transform(source, id) {
      const path = id.split('?')[0];
      if (/\/src\/routes\/api\/mcp(?:\/(?:configure|test|discover|\$name))?\.ts$/.test(path)) {
        // The managed deployment has one native MCP backend: Dashboard. A
        // failed general /api/status probe must not move MCP to Gateway, whose
        // OpenAI-compatible API has no MCP routes. Keep dashboardFetch's auth,
        // timeouts and errors; never retry mutations on another backend.
        if (source.split(mcpFetchBody).length !== 2) {
          throw new Error('Unsupported Workspace MCP routing; re-verify the managed API adapter');
        }
        return { map: null, code: source.replace(mcpFetchBody, '  return dashboardFetch(path, init)') };
      }
      if (!path.endsWith('/src/screens/mcp/components/mcp-server-card.tsx')) return null;
      const count = '{server.discoveredToolsCount}';
      const details = '</dl>';
      if (source.split(count).length !== 2 || source.split(details).length !== 2) {
        throw new Error('Unsupported Workspace MCP card; re-verify the managed UI adapter');
      }
      return { map: null, code: source.replace(count,
        "{server.enabled && server.status === 'connected' && server.lastTestedAt ? server.discoveredToolsCount : '—'}")
        .replace(details, `${details}
      <p className="text-xs text-primary-500">
        {server.lastTestedAt
          ? 'Last test: ' + new Date(server.lastTestedAt).toLocaleString() + ' (not live monitoring)'
          : 'Not tested, expired or changed — click Test to discover tools.'}
      </p>`) };
    },
  };
}
