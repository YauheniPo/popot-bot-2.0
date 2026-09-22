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

const MCP_REQUEST_RULES = [
  (path, method) => ((path === '/api/mcp/servers' || path === '/api/mcp') && ['GET', 'POST'].includes(method)) && 'collection',
  (path, method) => path === '/api/mcp/configure' && method === 'PUT' && 'configure',
  (path, method) => path === '/api/mcp/test' && method === 'POST' && 'test',
  (path, method) => path.startsWith('/api/mcp/servers/') && path.endsWith('/enabled') && 'configure',
  (path, method) => path.startsWith('/api/mcp/servers/') && path.endsWith('/test') && 'test',
  (path, method) => path.startsWith('/api/mcp/servers/') && method === 'DELETE' && 'deletion',
  (path, method) => path.startsWith('/api/mcp/servers/') && ['PUT', 'PATCH'].includes(method) && 'edit',
  (path, method) => /^\/api\/mcp\/[^/]+$/.test(path) && method === 'DELETE' && 'deletion',
  (path, method) => /^\/api\/mcp\/[^/]+$/.test(path) && ['PUT', 'PATCH'].includes(method) && 'edit',
  (path, method) => path.startsWith('/api/mcp/') && method !== 'GET' && 'discover',
  (path, method) => path.startsWith('/api/mcp/') && path.endsWith('/logs') && 'logs',
];

function classifyMcpRequest(path, method) {
  return MCP_REQUEST_RULES.map(rule => rule(path, method)).find(Boolean) || null;
}

function serverPath(path) {
  const canonicalPrefix = '/api/mcp/servers/';
  const legacyPrefix = '/api/mcp/';
  if (path.startsWith(canonicalPrefix)) return path.slice(canonicalPrefix.length);
  if (path.startsWith(legacyPrefix)) return path.slice(legacyPrefix.length);
  return '';
}

function planConfigure(url, body) {
  if (typeof body.enabled !== 'boolean' || Object.keys(body).some(key => !['name', 'enabled'].includes(key))) {
    return { refusal: unsupported('Native Workspace configuration supports the enabled toggle only. Change tool selection in the official Dashboard.') };
  }
  if (url.pathname === '/api/mcp/configure') {
    url.pathname = `/api/mcp/servers/${encodeURIComponent(body.name)}/enabled`;
  }
  return { payload: { enabled: body.enabled } };
}

function planTest(url, body) {
  if (Object.keys(body).some(key => key !== 'name')) {
    return { refusal: unsupported('Save the server first, then test it by name. Unsaved inputs are not tested against an existing server.') };
  }
  if (url.pathname === '/api/mcp/test') {
    url.pathname = `/api/mcp/servers/${encodeURIComponent(body.name)}/test`;
  }
  return { render: value => {
    const validTools = Array.isArray(value.tools) && value.tools.every(tool => object(tool) && typeof tool.name === 'string');
    if (typeof value.ok !== 'boolean' || (value.ok && !validTools)) throw new Error('Invalid native discovery result');
    return { ok: value.ok, status: value.ok ? 'connected' : 'failed',
      discoveredTools: value.ok ? value.tools : [],
      ...(value.ok ? {} : { error: 'Native MCP test failed. Check server connectivity and OAuth in the official Dashboard.' }) };
  } };
}

function buildRequestProfile(input, init) {
  const url = new URL(input instanceof Request ? input.url : input);
  const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
  const path = url.pathname;
  const profile = url.searchParams.get('profile') || '';
  return { url, method, path, profile };
}

async function buildRequestBody(method, init, input) {
  if (method === 'GET' || method === 'DELETE') return {};
  try {
    const raw = init.body ?? (input instanceof Request ? await input.clone().text() : '{}');
    const body = JSON.parse(raw || '{}');
    if (!object(body) || !safeName(body.name)) throw new Error('Invalid payload');
    return body;
  } catch {
    throw new Error('Invalid MCP payload or server name.');
  }
}

function buildFetchHeaders(init, input, payload) {
  const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
  headers.delete('content-length');
  if (payload) headers.set('content-type', 'application/json');
  return headers;
}

function prepareNativeFetch(fetchImpl, init, input, url, method, headers, payload) {
  return fetchImpl(url, {
    ...init,
    method,
    headers,
    signal: init.signal || (input instanceof Request ? input.signal : undefined),
    body: payload ? JSON.stringify(payload) : undefined,
  });
}

function parseResponseBody(response) {
  return response.json().catch(() => null);
}

function stripOutputHeaders(headers) {
  const outputHeaders = new Headers(headers);
  for (const key of ['content-length', 'content-encoding', 'etag']) outputHeaders.delete(key);
  outputHeaders.set('cache-control', 'no-store');
  return outputHeaders;
}

function buildErrorResponse(status, message) {
  return Response.json({ ok: false, error: message }, { status, headers: { 'cache-control': 'no-store' } });
}

function handleSuccessfulResponse(rendered, outputHeaders) {
  return Response.json(rendered, { headers: outputHeaders });
}

function handleRenderFailure() {
  return Response.json({ ok: false, error: 'Unexpected native Hermes MCP response.' }, { status: 502, headers: { 'cache-control': 'no-store' } });
}

function manageObservations(method, kind, key, observations, test, profile) {
  if (method === 'GET') return;
  const previous = observations.get(key);
  observations.delete(key);
  if (test && previous) {
    observations.set(key, { profile, fingerprint: previous.fingerprint });
  }
}

async function processResponse(fetchImpl, response, testedEntry, key, observations, now, render) {
  const value = await parseResponseBody(response);
  const outputHeaders = stripOutputHeaders(response.headers);
  if (!response.ok) return buildErrorResponse(response.status,
    `Hermes MCP request failed (HTTP ${response.status}). Check authorization, server name and native field requirements.`);
  try {
    if (!object(value)) throw new Error('Invalid MCP response');
    const rendered = render(value);
    if (testedEntry && observations.get(key) === testedEntry) {
      testedEntry.result = { status: rendered.status,
        toolCount: rendered.discoveredTools.length, error: rendered.error };
      testedEntry.testedAt = now();
    }
    return handleSuccessfulResponse(rendered, outputHeaders);
  } catch {
    return handleRenderFailure();
  }
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

  function planCollection(url, body, method, profile) {
    url.pathname = '/api/mcp/servers';
    if (method === 'POST') {
      let payload;
      try { payload = createInput(body); } catch {
        return { refusal: unsupported('Native create supports URL/command, stdio env and bearer/automatic OAuth. Custom headers, OAuth client settings, disabled creation and tool filters must be configured in the official Dashboard.') };
      }
      return { payload, render: serverView };
    }
    return { render: value => {
      if (!Array.isArray(value.servers)) throw new Error('Invalid MCP list');
      return { servers: listView(value.servers, profile) };
    } };
  }

  function planMcpRequest(kind, url, body, profile, method) {
    if (kind === 'collection') return planCollection(url, body, method, profile);
    if (kind === 'configure') return planConfigure(url, body);
    if (kind === 'test') return planTest(url, body);
    url.pathname = `/api/mcp/servers/${serverPath(url.pathname)}`;
    return {};
  }

  return async function mcpFetch(input, init = {}) {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.origin !== base.origin || !/^\/api\/mcp(?:\/|$)/.test(url.pathname)) {
      return fetchImpl(input, init);
    }
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const path = url.pathname;
    const profile = url.searchParams.get('profile') || '';
    const kind = classifyMcpRequest(path, method);
    if (kind === 'discover') {
      return unsupported('Native Hermes tests saved servers only. Save the server, then use Test; unsaved discovery is unavailable.');
    }
    if (kind === 'logs') {
      return Response.json({ ok: false, error: 'This Hermes version has no per-server MCP log stream. Use the official Dashboard.' }, { status: 501 });
    }
    if (!kind) return fetchImpl(input, init);

    let body;
    try {
      body = await buildRequestBody(method, init, input);
      if (kind === 'deletion' && !safeName(decodeURIComponent(serverPath(path)))) throw new Error('Invalid name');
    } catch {
      return unsupported('Invalid MCP payload or server name.');
    }

    const planResult = planMcpRequest(kind, url, body, profile, method);
    if (planResult.refusal) return planResult.refusal;
    const payload = planResult.payload;
    const render = planResult.render || (value => value);
    const deletion = kind === 'deletion';
    const test = kind === 'test';

    const key = keyFor(profile, deletion ? decodeURIComponent(serverPath(path)) : body.name);
    manageObservations(method, kind, key, observations, test, profile);
    const testedEntry = observations.get(key);

    const headers = buildFetchHeaders(init, input, payload);
    const response = await prepareNativeFetch(fetchImpl, init, input, url, method, headers, payload);

    return processResponse(fetchImpl, response, testedEntry, key, observations, now, render);
  };
}
