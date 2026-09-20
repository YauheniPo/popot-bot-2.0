// Compatibility with the pinned Hermes Dashboard API, not a second MCP runtime.
import { createHash } from 'node:crypto';
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => value && Object.keys(value).length > 0;
const safeName = value => typeof value === 'string' && value.trim() &&
  !['.', '..'].includes(value.trim()) && !/[\\/\x00-\x1f\x7f]/.test(value);

export function correctWorkspacePresets(value) {
  if (!Array.isArray(value?.presets)) return value;
  return { ...value, presets: value.presets.map(preset => {
    const template = preset?.template;
    if (!object(template) || template.command !== 'npx' || template.name !== preset.id ||
        (template.transportType && template.transportType !== 'stdio') ||
        (template.authType && template.authType !== 'none') ||
        (template.toolMode && template.toolMode !== 'all') ||
        Object.keys(template).some(key => !['name', 'transportType', 'command', 'args', 'env', 'authType', 'toolMode'].includes(key)) ||
        Object.entries(template.env || {}).some(([key, value]) => key !== 'GITHUB_PERSONAL_ACCESS_TOKEN' || value !== '')) return preset;
    const args = JSON.stringify(template.args);
    if (preset.id === 'fetch' && args === '["-y","@modelcontextprotocol/server-fetch"]') {
      return { ...preset, homepage: 'https://github.com/modelcontextprotocol/servers/tree/main/src/fetch',
        template: { ...template, command: 'uvx', args: ['mcp-server-fetch'] } };
    }
    if (preset.id === 'github' && args === '["-y","@modelcontextprotocol/server-everything"]') {
      return { ...preset, description: 'Official GitHub MCP, read-only. Supply a limited-scope GitHub PAT as the bearer token.',
        homepage: 'https://github.com/github/github-mcp-server',
        template: { name: template.name, transportType: 'http',
          url: 'https://api.githubcopilot.com/mcp/readonly', authType: 'bearer', toolMode: 'all' } };
    }
    return preset;
  }) };
}

function unsupported(message) {
  return Response.json({ ok: false, error: message }, { status: 422 });
}

function serverView(server) {
  if (!object(server) || !safeName(server.name)) throw new Error('Invalid server response');
  const { name, url, command, args, enabled } = server;
  const selected = Array.isArray(server.tools) ? server.tools : null;
  return { name, url, command, args, enabled,
    transportType: server.transport || (url ? 'http' : 'stdio'),
    authType: server.auth === 'header' ? 'bearer' : (server.auth || 'none'),
    hasBearerToken: server.auth === 'header',
    env: Object.fromEntries(Object.keys(server.env || {}).map(key => [key, '***'])),
    toolMode: selected ? 'include' : 'all', includeTools: selected || [],
    discoveredToolsCount: 0, status: enabled === false ? 'disabled' : 'unknown' };
}

function createInput(body) {
  if (!['http', 'stdio'].includes(body.transportType)) throw new Error('Unknown transport');
  if (body.enabled === false || nonempty(body.headers) || nonempty(body.oauth) ||
      (body.toolMode && body.toolMode !== 'all') || nonempty(body.includeTools) || nonempty(body.excludeTools)) {
    throw new Error('Unsupported create options');
  }
  const result = { name: body.name };
  // Never send fields ignored by the native Pydantic model as if they worked.
  for (const key of ['url', 'command', 'args', 'env']) {
    if (body[key] !== undefined) result[key] = body[key];
  }
  result.auth = body.authType === 'bearer' ? 'header' : (body.authType || 'none');
  if (body.bearerToken !== undefined) result.bearer_token = body.bearerToken;
  return result;
}

export function createMcpAdapter({ dashboardUrl, fetchImpl, now = Date.now, probeTtlMs = 300_000 }) {
  const base = new URL(dashboardUrl);
  // Last explicit Test only, never an implicit process launch or an auth cache.
  // Every list still reaches the protected native API. No credentials on disk.
  const observations = new Map();
  const keyFor = (profile, name) => JSON.stringify([profile, name]);
  function listView(servers, profile) {
    const present = new Set(servers.map(server => keyFor(profile, server?.name)));
    for (const [key, entry] of observations) {
      if (entry.profile === profile && !present.has(key)) observations.delete(key);
    }
    return servers.map(server => {
      const view = serverView(server);
      const key = keyFor(profile, server.name);
      const fingerprint = createHash('sha256').update(JSON.stringify(server)).digest('hex');
      let entry = observations.get(key);
      if (!entry || entry.fingerprint !== fingerprint) {
        entry = { profile, fingerprint };
        observations.delete(key);
        if (observations.size >= 256) observations.delete(observations.keys().next().value);
        observations.set(key, entry);
      }
      if (server.enabled !== false && entry.result && now() - entry.testedAt < probeTtlMs) {
        Object.assign(view, { status: entry.result.status,
          discoveredToolsCount: entry.result.toolCount,
          lastTestedAt: new Date(entry.testedAt).toISOString(), lastError: entry.result.error });
      }
      return view;
    });
  }
  return async function mcpFetch(input, init = {}) {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.origin !== base.origin || !/^\/api\/mcp(?:\/|$)/.test(url.pathname)) {
      return fetchImpl(input, init);
    }
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const path = url.pathname;
    const profile = url.searchParams.get('profile') || '';
    const collection = path === '/api/mcp' && ['GET', 'POST'].includes(method);
    const configure = path === '/api/mcp/configure' && method === 'PUT';
    const test = path === '/api/mcp/test' && method === 'POST';
    const deletion = /^\/api\/mcp\/[^/]+$/.test(path) && method === 'DELETE';
    if (path === '/api/mcp/discover' && method === 'POST') {
      return unsupported('Native Hermes tests saved servers only. Save the server, then use Test; unsaved discovery is unavailable.');
    }
    if (/^\/api\/mcp\/[^/]+\/logs$/.test(path) && method === 'GET') {
      return Response.json({ ok: false, error: 'This Hermes version has no per-server MCP log stream. Use the official Dashboard.' }, { status: 501 });
    }
    if (!collection && !configure && !test && !deletion) return fetchImpl(input, init);

    let body = {};
    try {
      if (method !== 'GET' && method !== 'DELETE') {
        body = JSON.parse(init.body ?? (input instanceof Request ? await input.clone().text() : '{}'));
        if (!object(body) || !safeName(body.name)) throw new Error('Invalid payload');
      }
      if (deletion && !safeName(decodeURIComponent(path.slice('/api/mcp/'.length)))) throw new Error('Invalid name');
    } catch {
      return unsupported('Invalid MCP payload or server name.');
    }

    let payload;
    let render = value => value;
    if (collection) {
      url.pathname = '/api/mcp/servers';
      if (method === 'POST') {
        try { payload = createInput(body); } catch {
          return unsupported('Native create supports URL/command, stdio env and bearer/automatic OAuth. Custom headers, OAuth client settings, disabled creation and tool filters must be configured in the official Dashboard.');
        }
        render = serverView;
      } else {
        render = value => {
          if (!Array.isArray(value.servers)) throw new Error('Invalid MCP list');
          return { servers: listView(value.servers, profile) };
        };
      }
    } else if (configure) {
      if (typeof body.enabled !== 'boolean' || Object.keys(body).some(key => !['name', 'enabled'].includes(key))) {
        return unsupported('Native Workspace configuration supports the enabled toggle only. Change tool selection in the official Dashboard.');
      }
      url.pathname = `/api/mcp/servers/${encodeURIComponent(body.name)}/enabled`;
      payload = { enabled: body.enabled };
    } else if (test) {
      if (Object.keys(body).some(key => key !== 'name')) {
        return unsupported('Save the server first, then test it by name. Unsaved inputs are not tested against an existing server.');
      }
      url.pathname = `/api/mcp/servers/${encodeURIComponent(body.name)}/test`;
      render = value => {
        if (typeof value.ok !== 'boolean' || (value.ok && (!Array.isArray(value.tools) ||
            value.tools.some(tool => !object(tool) || typeof tool.name !== 'string')))) {
          throw new Error('Invalid native discovery result');
        }
        return { ok: value.ok, status: value.ok ? 'connected' : 'failed',
          discoveredTools: value.ok ? value.tools : [],
          ...(value.ok ? {} : { error: 'Native MCP test failed. Check server connectivity and OAuth in the official Dashboard.' }) };
      };
    } else {
      url.pathname = `/api/mcp/servers/${path.slice('/api/mcp/'.length)}`;
    }

    const key = keyFor(profile, deletion ? decodeURIComponent(path.slice('/api/mcp/'.length)) : body.name);
    let testedEntry;
    if (method !== 'GET') {
      const previous = observations.get(key);
      observations.delete(key);
      if (test && previous) {
        testedEntry = { profile, fingerprint: previous.fingerprint };
        observations.set(key, testedEntry);
      }
    }
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.delete('content-length');
    if (payload) headers.set('content-type', 'application/json');
    const response = await fetchImpl(url, { ...init, method, headers,
      signal: init.signal || (input instanceof Request ? input.signal : undefined),
      body: payload ? JSON.stringify(payload) : undefined });
    // Do not reflect native validation input, URLs or provider errors containing secrets.
    const value = await response.json().catch(() => null);
    const outputHeaders = new Headers(response.headers);
    for (const key of ['content-length', 'content-encoding', 'etag']) outputHeaders.delete(key);
    outputHeaders.set('cache-control', 'no-store');
    if (!response.ok) return Response.json({ ok: false,
      error: `Hermes MCP request failed (HTTP ${response.status}). Check authorization, server name and native field requirements.` },
    { status: response.status, headers: outputHeaders });
    try {
      if (!object(value)) throw new Error('Invalid MCP response');
      const rendered = render(value);
      // A delete/edit or a newer Test must win over a late result.
      if (testedEntry && observations.get(key) === testedEntry) {
        testedEntry.result = { status: rendered.status,
          toolCount: rendered.discoveredTools.length, error: rendered.error };
        testedEntry.testedAt = now();
      }
      return Response.json(rendered, { headers: outputHeaders });
    } catch {
      return Response.json({ ok: false, error: 'Unexpected native Hermes MCP response.' }, { status: 502 });
    }
  };
}
