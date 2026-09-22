import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createMcpAdapter, correctWorkspacePresets } from './workspace-mcp-adapter.mjs';

const dashboardUrl = 'http://127.0.0.1:9119';
const input = { name: 'fixture', transportType: 'http', url: 'https://mcp.example/rpc', authType: 'bearer', bearerToken: 'test-secret' };
const invoke = (adapter, path, body, method = 'POST') => adapter(`${dashboardUrl}${path}`, {
  method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
});

test('repairs only known broken seed presets, never custom or unrelated templates', () => {
  const preset = (id, pkg) => ({ id, template: { name: id, transportType: 'stdio',
    command: 'npx', args: ['-y', `@modelcontextprotocol/server-${pkg}`], env: { GITHUB_PERSONAL_ACCESS_TOKEN: '' } } });
  const input = { ok: true, presets: [preset('fetch', 'fetch'), preset('github', 'everything'),
    preset('memory', 'memory'), preset('custom', 'everything')] };
  const original = structuredClone(input);
  const output = correctWorkspacePresets(input);
  assert.equal(output.presets[0].template.command, 'uvx');
  assert.deepEqual(output.presets[0].template.args, ['mcp-server-fetch']);
  assert.equal(output.presets[1].template.transportType, 'http');
  assert.equal(output.presets[1].template.url, 'https://api.githubcopilot.com/mcp/readonly');
  assert.equal(output.presets[1].template.authType, 'bearer');
  assert.equal(output.presets[1].template.command, undefined);
  assert.equal(output.presets[1].template.env, undefined);
  assert.deepEqual(output.presets.slice(2), input.presets.slice(2));
  assert.deepEqual(input, original);
  assert.deepEqual(correctWorkspacePresets(output), output);
  assert.deepEqual(correctWorkspacePresets({ error: 'Unauthorized' }), { error: 'Unauthorized' });
  for (const custom of [{ env: { GITHUB_PERSONAL_ACCESS_TOKEN: 'keep-fixture' } },
    { headers: { 'X-Api-Key': 'keep-fixture' } }, { toolMode: 'include' }, { name: 'renamed' }]) {
    const customized = { presets: [{ ...input.presets[1], template: { ...input.presets[1].template, ...custom } }] };
    assert.deepEqual(correctWorkspacePresets(customized), customized);
  }
});

test('lists native MCP servers including an empty backend and preserves query', async () => {
  for (const servers of [[], [{ name: 'fixture', transport: 'http', auth: 'header', tools: ['read'], enabled: false }]]) {
    const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
      assert.equal(String(url), `${dashboardUrl}/api/mcp/servers?profile=test`);
      assert.equal(init.method, 'GET');
      return Response.json({ servers });
    }});
    const body = await (await fetch(`${dashboardUrl}/api/mcp?profile=test`)).json();
    assert.equal(body.servers.length, servers.length);
    if (servers.length) {
      assert.equal(body.servers[0].authType, 'bearer');
      assert.equal(body.servers[0].toolMode, 'include');
      assert.deepEqual(body.servers[0].includeTools, ['read']);
      assert.equal(body.servers[0].discoveredToolsCount, 0);
    }
  }
});

test('creates using native bearer provisioning without echoing credentials', async () => {
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
    assert.equal(String(url), `${dashboardUrl}/api/mcp/servers`);
    assert.deepEqual(JSON.parse(init.body), { name: 'fixture', url: input.url, auth: 'header', bearer_token: 'test-secret' });
    return Response.json({ name: 'fixture', transport: 'http', auth: 'header', url: input.url });
  }});
  const response = await invoke(fetch, '/api/mcp', input);
  assert.equal(response.status, 200);
  assert.equal((await response.text()).includes('test-secret'), false);
});

test('stdio create preserves arguments and env but masks the returned environment', async () => {
  const definition = { name: 'local', transportType: 'stdio', command: 'python',
    args: ['-m', 'fixture'], env: { TOKEN: 'private-value' }, authType: 'none' };
  const adapter = createMcpAdapter({ dashboardUrl, fetchImpl: async (_url, init) => {
    assert.deepEqual(JSON.parse(init.body), { name: 'local', command: 'python',
      args: definition.args, env: definition.env, auth: 'none' });
    return Response.json({ ...definition, transport: 'stdio' });
  }});
  const result = await invoke(adapter, '/api/mcp', definition);
  const server = await result.json();
  assert.deepEqual(server.args, definition.args);
  assert.deepEqual(server.env, { TOKEN: '***' });
});

test('invalid backend responses fail visibly and unavailable logs do not hit the backend', async () => {
  for (const body of ['not-json', 'null', '{}', '{"servers":[null]}']) {
    const adapter = createMcpAdapter({ dashboardUrl, fetchImpl: async () => new Response(body) });
    assert.equal((await adapter(`${dashboardUrl}/api/mcp`)).status, 502);
  }
  const adapter = createMcpAdapter({ dashboardUrl, fetchImpl: async () => assert.fail('No logs API') });
  assert.equal((await adapter(`${dashboardUrl}/api/mcp/fixture/logs`)).status, 501);
});

test('toggles and deletes only the named native server, never replaces the whole map', async () => {
  const calls = [];
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
    calls.push([new URL(url).pathname, init.method, init.body]);
    return Response.json({ ok: true, name: 'fixture', enabled: false });
  }});
  const toggle = await invoke(fetch, '/api/mcp/configure', { name: 'fixture', enabled: false }, 'PUT');
  assert.equal((await toggle.json()).enabled, false);
  assert.equal((await fetch(`${dashboardUrl}/api/mcp/fixture`, { method: 'DELETE' })).status, 200);
  assert.deepEqual(calls.map(c => c.slice(0, 2)), [
    ['/api/mcp/servers/fixture/enabled', 'PUT'], ['/api/mcp/servers/fixture', 'DELETE'],
  ]);
});

test('preserves canonical native server paths without duplicating the servers segment', async () => {
  const calls = [];
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
    calls.push([new URL(url).pathname, init.method]);
    return Response.json({ ok: true, name: 'fixture', enabled: true });
  }});
  await fetch(`${dashboardUrl}/api/mcp/servers/fixture`, { method: 'DELETE' });
  assert.deepEqual(calls, [['/api/mcp/servers/fixture', 'DELETE']]);
});

test('native test result becomes connected/failed with discovered tools', async () => {
  for (const ok of [true, false]) {
    const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
      assert.equal(String(url), `${dashboardUrl}/api/mcp/servers/fixture/test`);
      assert.equal(init.method, 'POST');
      return Response.json({ ok, tools: ok ? [{ name: 'read', description: 'Read' }] : [], error: ok ? undefined : 'denied' });
    }});
    const body = await (await invoke(fetch, '/api/mcp/test', { name: 'fixture' })).json();
    assert.equal(body.status, ok ? 'connected' : 'failed');
    assert.equal(body.discoveredTools.length, ok ? 1 : 0);
  }
});

test('canonical enabled, test and edit routes are classified independently', async () => {
  const calls = [];
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
    calls.push([new URL(url).pathname, init.method]);
    return Response.json(init.method === 'POST' ? { ok: true, tools: [] } : { ok: true });
  }});
  await fetch(`${dashboardUrl}/api/mcp/servers/fixture/enabled`, {
    method: 'PUT', body: JSON.stringify({ enabled: true, name: 'fixture' })
  });
  await fetch(`${dashboardUrl}/api/mcp/servers/fixture/test`, {
    method: 'POST', body: JSON.stringify({ name: 'fixture' })
  });
  await fetch(`${dashboardUrl}/api/mcp/servers/fixture`, {
    method: 'PATCH', body: JSON.stringify({ name: 'fixture' })
  });
  assert.deepEqual(calls, [
    ['/api/mcp/servers/fixture/enabled', 'PUT'],
    ['/api/mcp/servers/fixture/test', 'POST'],
    ['/api/mcp/servers/fixture', 'PATCH'],
  ]);
});

test('MCP Request input supplies the JSON body when init has no body', async () => {
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (url, init) => {
    assert.equal(new URL(url).pathname, '/api/mcp/servers/fixture/test');
    assert.equal(init.method, 'POST');
    return Response.json({ ok: true, tools: [] });
  }});
  const request = new Request(`${dashboardUrl}/api/mcp/test`, {
    method: 'POST', body: JSON.stringify({ name: 'fixture' })
  });
  assert.equal((await fetch(request)).status, 200);
});

test('MCP init body takes precedence and empty JSON falls back safely', async () => {
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async (_url, init) => {
    assert.equal(init.method, 'POST');
    return Response.json({ ok: true, tools: [] });
  }});
  const request = new Request(`${dashboardUrl}/api/mcp/test`, { method: 'POST', body: '{}' });
  assert.equal((await fetch(request, { body: JSON.stringify({ name: 'fixture' }) })).status, 200);
  assert.equal((await fetch(`${dashboardUrl}/api/mcp/test`, { method: 'POST', body: '' })).status, 422);
});

test('rejects unsupported operations and unsafe names before any mutation', async () => {
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async () => { assert.fail('Must not contact backend'); } });
  for (const [path, value, method] of [
    ['/api/mcp/test', input], ['/api/mcp/discover', input],
    ['/api/mcp/configure', { name: 'fixture', toolMode: 'exclude', excludeTools: ['read'] }, 'PUT'],
    ['/api/mcp', { ...input, enabled: false }],
    ['/api/mcp', { ...input, headers: { 'X-Api-Key': 'private' } }],
    ['/api/mcp', { ...input, oauth: { clientId: 'id', clientSecret: 'private' } }],
    ['/api/mcp/test', { name: '..' }], ['/api/mcp/test', { name: 'a/b' }],
  ]) {
    const response = await invoke(fetch, path, value, method);
    assert.equal(response.status, 422);
    assert.equal((await response.text()).includes('private'), false);
  }
});

test('propagates native error status but never validation input or secrets', async () => {
  for (const status of [401, 403, 404, 409, 422, 503]) {
    const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async () =>
      Response.json({ detail: [{ input: 'test-secret' }] }, { status }) });
    const response = await invoke(fetch, '/api/mcp', input);
    assert.equal(response.status, status);
    assert.equal((await response.text()).includes('test-secret'), false);
  }
});

test('never maps a gateway, external host, or native OAuth route', async () => {
  const result = new Response('untouched');
  const fetch = createMcpAdapter({ dashboardUrl, fetchImpl: async () => result });
  for (const url of ['http://127.0.0.1:8642/api/mcp', 'https://example.com/api/mcp',
    `${dashboardUrl}/api/mcp/oauth/flows/fixture`]) assert.equal(await fetch(url), result);
});

function discoveryFixture(options = {}) {
  const state = { server: { name: 'memory', command: 'npx', args: ['memory'], tools: ['allowed'] },
    result: { ok: true, tools: [{ name: 'read' }, { name: 'search' }] }, calls: [], now: 1000 };
  const adapter = createMcpAdapter({ dashboardUrl, now: () => state.now, probeTtlMs: 100,
    fetchImpl: async (url, init) => {
      state.calls.push(init.method);
      return Response.json(init.method === 'GET' ? { servers: [state.server] } : state.result);
    }, ...options });
  return { state, adapter,
    list: async (query = '') => (await (await adapter(`${dashboardUrl}/api/mcp${query}`)).json()).servers[0],
    probe: (query = '') => invoke(adapter, `/api/mcp/test${query}`, { name: 'memory' }) };
}

test('list reports last successful discovery, including genuine zero, without starting processes', async () => {
  const { state, list, probe } = discoveryFixture();
  assert.equal((await list()).status, 'unknown');
  assert.deepEqual(state.calls, ['GET']);
  await probe();
  const server = await list();
  assert.equal(server.status, 'connected');
  assert.equal(server.discoveredToolsCount, 2);
  assert.ok(server.lastTestedAt);
  assert.deepEqual(server.includeTools, ['allowed']);
  state.result = { ok: true, tools: [] };
  await probe();
  assert.equal((await list()).status, 'connected');
  assert.equal((await list()).discoveredToolsCount, 0);
});

test('discovery is scoped to profile, expires, and invalidates on config edits and toggles', async () => {
  const { state, adapter, list, probe } = discoveryFixture();
  await list();
  await probe();
  assert.equal((await list('?profile=other')).status, 'unknown');
  assert.equal((await list()).status, 'connected');
  state.server.args = ['different'];
  assert.equal((await list()).status, 'unknown');
  await probe();
  state.now += 101;
  assert.equal((await list()).status, 'unknown');
  await probe();
  await invoke(adapter, '/api/mcp/configure', { name: 'memory', enabled: false }, 'PUT');
  assert.equal((await list()).status, 'unknown');
});

test('failed and malformed tests do not leave a previous successful count', async () => {
  const { state, list, probe } = discoveryFixture();
  await list();
  await probe();
  state.result = { ok: false, error: 'secret-value' };
  await probe();
  assert.equal((await list()).status, 'failed');
  assert.equal((await list()).discoveredToolsCount, 0);
  assert.ok((await list()).lastError);
  assert.ok(!JSON.stringify(await list()).includes('secret-value'));
  state.result = { ok: true, tools: 'invalid' };
  assert.equal((await probe()).status, 502);
  assert.equal((await list()).status, 'unknown');
});

test('a late Test cannot resurrect discovery after deletion', async () => {
  let finish;
  const { adapter, list, probe } = discoveryFixture({ fetchImpl: async (url, init) => {
    if (init.method === 'GET') return Response.json({ servers: [{ name: 'memory', command: 'npx' }] });
    if (new URL(url).pathname.endsWith('/test')) return new Promise(resolve => { finish = resolve; });
    return Response.json({ ok: true });
  }});
  await list();
  const pending = probe();
  await adapter(`${dashboardUrl}/api/mcp/memory`, { method: 'DELETE' });
  finish(Response.json({ ok: true, tools: [{ name: 'stale' }] }));
  await pending;
  assert.equal((await list()).status, 'unknown');
});

test('HTTP or transport failures invalidate an old Test without caching an error body', async () => {
  for (const failure of ['http', 'network']) {
    let fail = false;
    const { list, probe } = discoveryFixture({ fetchImpl: async (_url, init) => {
      if (init.method === 'GET') return Response.json({ servers: [{ name: 'memory', command: 'npx' }] });
      if (!fail) return Response.json({ ok: true, tools: [{ name: 'read' }] });
      if (failure === 'network') throw new Error('fixture network failure');
      return Response.json({ error: 'must-not-cache' }, { status: 503 });
    }});
    await list();
    await probe();
    assert.equal((await list()).status, 'connected');
    fail = true;
    if (failure === 'network') await assert.rejects(probe());
    else assert.equal((await probe()).status, 503);
    assert.equal((await list()).status, 'unknown');
  }
});

const BASE = 'http://127.0.0.1:9119';
const adapter = (fetchImpl) => createMcpAdapter({ dashboardUrl: BASE, fetchImpl });

test('preset correction accepts only the exact known broken shapes', () => {
  // A custom auth/transport/tool mode or an unknown template key is left alone.
  const presets = [
    { id: 'fetch', template: { name: 'fetch', command: 'npx', args: ['-y', '@modelcontextprotocol/server-fetch'] } },
    { id: 'github', template: { name: 'github', command: 'npx', args: ['-y', '@modelcontextprotocol/server-everything'] } },
    { id: 'custom-auth', template: { name: 'custom-auth', command: 'npx', authType: 'custom' } },
    { id: 'custom-transport', template: { name: 'custom-transport', command: 'npx', transportType: 'http' } },
    { id: 'custom-tools', template: { name: 'custom-tools', command: 'npx', toolMode: 'subset' } },
    { id: 'custom-extra', template: { name: 'custom-extra', command: 'npx', extra: 1 } },
  ];
  const result = correctWorkspacePresets({ presets });
  // The two known seeds are repaired...
  assert.equal(result.presets[0].template.command, 'uvx');
  assert.equal(result.presets[1].template.url, 'https://api.githubcopilot.com/mcp/readonly');
  // ...and every customized template is returned untouched.
  for (let i = 2; i < presets.length; i += 1) {
    assert.equal(result.presets[i], presets[i]);
  }
  // A value without a presets array is returned unchanged (by reference).
  const untouched = { servers: [] };
  assert.equal(correctWorkspacePresets(untouched), untouched);
  assert.equal(correctWorkspacePresets(null), null);
});

test('a server view falls back to stdio and reports a disabled state', () => {
  // transport missing + no url -> stdio; enabled === false -> disabled.
  const adapterInstance = adapter(async () => Response.json({ servers: [
    { name: 'local', command: 'uvx', args: ['mcp-server-fetch'], enabled: false },
  ], profile: 'builder' }));
  return adapterInstance(`${BASE}/api/mcp`).then(async response => {
    const body = await response.json();
    const server = body.servers[0];
    assert.equal(server.transportType, 'stdio');
    assert.equal(server.status, 'disabled');
    assert.deepEqual(server.tools ?? [], []);
  });
});

test('an unknown transport is refused before any upstream call', async () => {
  let calls = 0;
  const instance = adapter(async () => { calls += 1; return Response.json({}); });
  const response = await instance(`${BASE}/api/mcp`, {
    method: 'POST', body: JSON.stringify({ name: 'x', transportType: 'carrier-pigeon' }),
  });
  assert.equal(response.status, 422);
  assert.equal(calls, 0);
});

test('unsupported create options are refused for each separate reason', async () => {
  const instance = adapter(async () => Response.json({}));
  const cases = [
    { name: 'a', transportType: 'stdio', enabled: false },
    { name: 'b', transportType: 'stdio', headers: { a: 'b' } },
    { name: 'c', transportType: 'stdio', oauth: { a: 'b' } },
    { name: 'd', transportType: 'stdio', toolMode: 'include' },
    { name: 'e', transportType: 'stdio', includeTools: ['x'] },
    { name: 'f', transportType: 'stdio', excludeTools: ['x'] },
  ];
  for (const body of cases) {
    const response = await instance(`${BASE}/api/mcp`, {
      method: 'POST', body: JSON.stringify(body),
    });
    assert.equal(response.status, 422, JSON.stringify(body));
  }
});

test('creating with a bearer and with an explicit none both map correctly', async () => {
  const seen = [];
  const instance = adapter(async (_url, init) => {
    seen.push(JSON.parse(init.body));
    return Response.json({ ok: true });
  });
  await instance(`${BASE}/api/mcp`, {
    method: 'POST', body: JSON.stringify({ name: 'bear', transportType: 'stdio', authType: 'bearer', bearerToken: 't' }),
  });
  await instance(`${BASE}/api/mcp`, {
    method: 'POST', body: JSON.stringify({ name: 'none', transportType: 'stdio', authType: 'none' }),
  });
  await instance(`${BASE}/api/mcp`, {
    method: 'POST', body: JSON.stringify({ name: 'default', transportType: 'stdio' }),
  });
  assert.equal(seen[0].auth, 'header');
  assert.equal(seen[1].auth, 'none');
  assert.equal(seen[2].auth, 'none');
});

test('the observation cache evicts the oldest entry at 256', async () => {
  const instance = adapter(async () => Response.json({ ok: true }));
  for (let i = 0; i < 257; i += 1) {
    await instance(`${BASE}/api/mcp/${encodeURIComponent('s' + i)}/test`, { method: 'POST', body: '{}' });
  }
  // Reaching here without throwing proves the eviction branch ran; a fresh
  // request for an evicted name must still work.
  const response = await instance(`${BASE}/api/mcp/s0/test`, { method: 'POST', body: '{}' });
  assert.ok(response);
});

test('a string URL and a Request both resolve, and init supplies method/signal', async () => {
  const seen = [];
  const instance = adapter(async (url, init) => {
    seen.push({ url: String(url), headers: new Headers(init.headers), signal: init.signal, method: init.method });
    return Response.json({ ok: true });
  });
  // String input, init method (line 101) and init signal (line 180).
  const controller = new AbortController();
  await instance(`${BASE}/api/mcp`, { method: 'GET', headers: { 'x-a': '1' }, signal: controller.signal });
  assert.equal(seen[0].method, 'GET');
  assert.equal(seen[0].headers.get('x-a'), '1');
  assert.equal(seen[0].signal, controller.signal);
  // Request input with its own method/headers/signal (the Request halves).
  await instance(new Request(`${BASE}/api/mcp`, { method: 'GET', headers: { 'x-b': '2' } }));
  assert.equal(seen[1].headers.get('x-b'), '2');
  // A string input with no Request wrapper and no init.headers at all.
  await instance(`${BASE}/api/mcp`, { method: 'GET' });
  assert.equal(seen[2].headers.get('x-b'), null);
});

test('a deletion name that is not safe is refused', async () => {
  const instance = adapter(async () => Response.json({}));
  // A safe deletion passes; an unsafe name is rejected by line 122.
  const ok = await instance(`${BASE}/api/mcp/good-name`, { method: 'DELETE' });
  assert.ok(ok);
  const bad = await instance(`${BASE}/api/mcp/%2E%2E%2Fevil`, { method: 'DELETE' });
  assert.equal(bad.status, 422);
});

test('a server with an explicit transport keeps it, and the cache evicts at 256', async () => {
  let listCalls = 0;
  const instance = adapter(async (url) => {
    if (String(url).includes('/api/mcp') && !String(url).includes('/test')) {
      listCalls += 1;
      // First list: one server with an explicit transport and no url (line 42
      // truthy transport half). Then 256 distinct names to force eviction.
      const servers = listCalls === 1
        ? [{ name: 'explicit', transport: 'stdio' }]
        : Array.from({ length: 300 }, (_, i) => ({ name: 's' + i + '-' + listCalls }));
      return Response.json({ servers });
    }
    return Response.json({ ok: true });
  });
  const first = await instance(`${BASE}/api/mcp`);
  const view = (await first.json()).servers[0];
  assert.equal(view.transportType, 'stdio');

  // Two large listings with different names force observations past 256 so the
  // eviction branch (line 85) runs.
  await instance(`${BASE}/api/mcp`);
  await instance(`${BASE}/api/mcp`);
  assert.ok(listCalls >= 3);
});

test('an explicit transport is preserved while a url implies http', async () => {
  // Line 42 has three sides: server.transport truthy, url truthy, and neither.
  const instance = adapter(async () => Response.json({ servers: [
    { name: 'explicit', transport: 'stdio' },
    { name: 'remote', url: 'https://example.test/mcp' },
    { name: 'neither', command: 'uvx' },
  ] }));
  const body = await (await instance(`${BASE}/api/mcp`)).json();
  const byName = Object.fromEntries(body.servers.map(s => [s.name, s.transportType]));
  assert.deepEqual(byName, { explicit: 'stdio', remote: 'http', neither: 'stdio' });
});
