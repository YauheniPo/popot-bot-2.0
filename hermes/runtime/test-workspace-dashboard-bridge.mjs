import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createDashboardBridge } from './workspace-dashboard-bridge.mjs';

test('preset correction runs only on an authorized response and preserves errors', async () => {
  const bridge = createDashboardBridge({ dashboardUrl: 'http://127.0.0.1:9119' });
  const request = new Request('https://workspace.example/api/mcp/presets');
  const rejected = Response.json({ error: 'Unauthorized' }, { status: 401 });
  assert.equal(await bridge.handle(request, async () => rejected), rejected);
  const response = await bridge.handle(request, async () => Response.json({ presets: [{
    id: 'fetch', template: { name: 'fetch', command: 'npx', args: ['-y', '@modelcontextprotocol/server-fetch'] },
  }] }));
  assert.equal((await response.json()).presets[0].template.command, 'uvx');
});

const dashboardUrl = 'http://127.0.0.1:9119';
const request = (cookie = '') => new Request('https://example.ts.net:3002/api/sessions', {
  headers: { cookie },
});
const session = '__Host-hermes_session_at=access; __Host-hermes_session_rt=refresh';
const rotated = '__Host-hermes_session_rt=new-refresh; Path=/; Secure; HttpOnly; SameSite=Lax';

test('authenticated MCP reprobe uses the protected route and coalesces concurrent requests', async () => {
  let probes = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (_url, init) => {
    assert.equal(new Headers(init.headers).get('cookie'), session);
    return Response.json({ servers: [] });
  }});
  const cookie = `${session}; claude-auth=workspace`;
  const reprobe = async probe => {
    probes++;
    assert.equal(new URL(probe.url).pathname, '/api/gateway-reprobe');
    assert.equal(probe.method, 'POST');
    assert.equal(probe.headers.get('cookie'), cookie);
    assert.equal(probe.headers.get('authorization'), null);
    await bridge.fetch(`${dashboardUrl}/api/mcp`);
    return Response.json({ capabilities: { mcp: true } });
  };
  const run = () => bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', {
    headers: { cookie, authorization: 'must-not-forward' },
  }), async () => Response.json({ ok: true }), reprobe);
  const results = await Promise.all([run(), run(), run()]);
  assert.ok(results.every(response => response.ok));
  await run();
  assert.equal(probes, 1);
});

test('reprobe does not grant Workspace authorization or run for anonymous/unrelated requests', async () => {
  let probes = 0;
  const bridge = createDashboardBridge({ dashboardUrl });
  const reprobe = async () => {
    probes++;
    return Response.json({ error: 'Workspace login required' }, { status: 401 });
  };
  const deny = async () => new Response(null, { status: 401 });
  await bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status'), deny, reprobe);
  await bridge.handle(request(session), deny, reprobe);
  assert.equal(probes, 0);
  const result = await bridge.handle(new Request('https://example.ts.net:3002/api/mcp', {
    headers: { cookie: session },
  }), deny, reprobe);
  assert.equal(probes, 1);
  assert.equal(result.status, 401);
});

test('failed reprobe is retryable and does not use another Workspace session cache', async () => {
  let probes = 0;
  const bridge = createDashboardBridge({ dashboardUrl });
  const reprobe = async () => {
    if (++probes === 1) throw new Error('Probe unavailable');
    return Response.json({ capabilities: { mcp: true } });
  };
  const run = workspace => bridge.handle(new Request('https://example.ts.net:3002/api/mcp', {
    headers: { cookie: `${session}; claude-auth=${workspace}` },
  }), async () => new Response('{}'), reprobe);
  await assert.rejects(run('one'), /Probe unavailable/);
  await run('one');
  await run('one');
  await run('two');
  assert.equal(probes, 3);
});

test('forwards only Hermes session cookies to the exact loopback dashboard', async () => {
  const calls = [];
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (url, init) => {
    calls.push({ url: String(url), headers: new Headers(init?.headers), redirect: init?.redirect });
    return new Response('{}');
  }});
  await bridge.handle(request(`${session}; claude-auth=workspace; grafana_session=private`), async () => {
    await bridge.fetch(`${dashboardUrl}/api/sessions`, { headers: { Authorization: 'Bearer legacy' } });
    await bridge.fetch('https://example.com/api/sessions');
    await bridge.fetch('http://127.0.0.1:8642/v1/models');
    return new Response('{}');
  });
  assert.equal(calls[0].headers.get('cookie'), session);
  assert.equal(calls[0].headers.get('authorization'), null);
  assert.equal(calls[0].headers.get('x-forwarded-proto'), 'https');
  assert.equal(calls[0].redirect, 'manual');
  assert.equal(calls[1].headers.get('cookie'), null);
  assert.equal(calls[2].headers.get('cookie'), null);
});

test('isolates concurrent browser sessions and background probes', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (_url, init) => {
    await new Promise(resolve => setTimeout(resolve, 2));
    return Response.json({ cookie: new Headers(init?.headers).get('cookie') });
  }});
  const run = cookie => bridge.handle(request(cookie), async () => bridge.fetch(`${dashboardUrl}/api/sessions`));
  const responses = await Promise.all([run(session), run('__Host-hermes_session_at=another'),
    bridge.fetch(`${dashboardUrl}/api/status`)]);
  assert.deepEqual(await Promise.all(responses.map(r => r.json())), [
    { cookie: session }, { cookie: '__Host-hermes_session_at=another' }, { cookie: null },
  ]);
});

test('serializes refresh for one session and returns separate rotated Set-Cookie headers', async () => {
  const sent = [];
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (_url, init) => {
    sent.push(new Headers(init.headers).get('cookie'));
    await new Promise(resolve => setTimeout(resolve, 5));
    const headers = new Headers();
    headers.append('set-cookie', rotated);
    headers.append('set-cookie', '__Host-hermes_session_at=new-access; Path=/; Secure; HttpOnly');
    headers.append('set-cookie', 'grafana_session=must-not-forward; Path=/');
    return new Response('{}', { headers });
  }});
  const run = () => bridge.handle(request(session), async () => {
    await bridge.fetch(`${dashboardUrl}/api/sessions`);
    return new Response('{}', { headers: { 'set-cookie': 'claude-auth=keep; HttpOnly' } });
  });
  const responses = await Promise.all([run(), run()]);
  assert.equal(sent[0], session);
  assert.match(sent[1], /hermes_session_rt=new-refresh/);
  for (const response of responses) {
    const cookies = response.headers.getSetCookie();
    assert.equal(cookies.length, 3);
    assert.ok(cookies.includes(rotated));
    assert.ok(cookies.includes('claude-auth=keep; HttpOnly'));
    assert.equal(cookies.some(c => c.includes('grafana')), false);
  }
});

test('does not follow redirects with credentials', async () => {
  let calls = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (_url, init) => {
    calls++;
    assert.equal(init.redirect, 'manual');
    return new Response(null, { status: 302, headers: { location: 'https://attacker.example/' } });
  }});
  await bridge.handle(request(session), async () => bridge.fetch(`${dashboardUrl}/api/sessions`));
  assert.equal(calls, 1);
});

test('maps backend no_cookie to actionable auth error without exposing body or secrets', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    Response.json({ error: 'unauthenticated', reason: 'no_cookie', secret: 'hidden' }, { status: 401 }) });
  const response = await bridge.handle(request(), async () => {
    await bridge.fetch(`${dashboardUrl}/api/sessions`);
    return Response.json({ error: 'nested upstream error' }, { status: 500 });
  });
  assert.equal(response.status, 401);
  const body = await response.json();
  assert.equal(body.code, 'dashboard_auth_required');
  assert.equal(body.login_url, 'https://example.ts.net/login');
  assert.equal(JSON.stringify(body).includes('hidden'), false);
});

test('bounds a hung upstream request', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, timeoutMs: 20,
    fetchImpl: async (_url, init) => new Promise((_, reject) => {
      init.signal.addEventListener('abort', () => reject(init.signal.reason), { once: true });
    }) });
  await assert.rejects(bridge.handle(request(session), () => bridge.fetch(`${dashboardUrl}/api/sessions`)));
});

test('rejects remote dashboard origins instead of forwarding credentials', () => {
  for (const url of ['https://example.com', 'http://127.0.0.1:9119/path', 'http://user:pass@127.0.0.1:9119']) {
    assert.throws(() => createDashboardBridge({ dashboardUrl: url }));
  }
});

test('does not keep an old refresh token usable by repeatedly accessing its cache entry', async () => {
  const sent = [];
  const bridge = createDashboardBridge({ dashboardUrl, cacheTtlMs: 40,
    fetchImpl: async (_url, init) => {
      sent.push(new Headers(init.headers).get('cookie'));
      return new Response('{}', { headers: sent.length === 1 ? { 'set-cookie': rotated } : {} });
    } });
  const run = () => bridge.handle(request(session), () => bridge.fetch(`${dashboardUrl}/api/sessions`));
  await run();
  await run();
  assert.match(sent[1], /rt=new-refresh/);
  await new Promise(resolve => setTimeout(resolve, 65));
  await run();
  assert.equal(sent[2], session);
});

test('preserves CRUD methods, query, bodies, errors and caller cancellation', async () => {
  const controller = new AbortController();
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (input, init) => {
    assert.equal(String(input), `${dashboardUrl}/api/config?scope=global`);
    assert.equal(init.method, 'PATCH');
    assert.equal(init.body, '{"model":"test-model"}');
    assert.equal(init.headers.get('content-type'), 'application/json');
    assert.equal(init.headers.get('cookie'), session);
    controller.abort();
    assert.equal(init.signal.aborted, true);
    return Response.json({ detail: 'validation error' }, { status: 422 });
  }});
  const result = await bridge.handle(request(session), () => bridge.fetch(
    `${dashboardUrl}/api/config?scope=global`, { method: 'PATCH',
      body: '{"model":"test-model"}', headers: { 'content-type': 'application/json' },
      signal: controller.signal }));
  assert.equal(result.status, 422);
  assert.equal((await result.json()).detail, 'validation error');
});

test('does not abort an established dashboard stream at the connection deadline', async () => {
  let signal;
  const bridge = createDashboardBridge({ dashboardUrl, timeoutMs: 15,
    fetchImpl: async (_url, init) => {
      signal = init.signal;
      return new Response('data: alive\n\n', { headers: { 'content-type': 'text/event-stream' } });
    } });
  const result = await bridge.handle(request(session), () => bridge.fetch(`${dashboardUrl}/api/events`));
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(signal.aborted, false);
  assert.equal(await result.text(), 'data: alive\n\n');
});

test('background work cannot inherit a completed browser request session', async () => {
  let trigger;
  let background;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (_url, init) =>
    Response.json({ cookie: new Headers(init.headers).get('cookie') }) });
  await bridge.handle(request(session), async () => {
    background = new Promise(resolve => { trigger = resolve; }).then(() => bridge.fetch(`${dashboardUrl}/api/sessions`));
    return new Response('{}');
  });
  trigger();
  assert.equal((await (await background).json()).cookie, null);
});

test('gateway chat streaming and API token pass through completely unchanged', async () => {
  const output = new Response('data: chat\n\n', { headers: { 'content-type': 'text/event-stream' } });
  const options = { method: 'POST', headers: { authorization: 'Bearer gateway-only' }, body: '{"stream":true}' };
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (url, init) => {
    assert.equal(url, 'http://127.0.0.1:8642/v1/chat/completions');
    assert.equal(init, options);
    return output;
  }});
  assert.equal(await bridge.handle(request(session), () => bridge.fetch(
    'http://127.0.0.1:8642/v1/chat/completions', options)), output);
});

test('cancelled queued request releases its slot without bypassing an earlier request', async () => {
  let releaseFirst;
  let startedFirst;
  const started = new Promise(resolve => { startedFirst = resolve; });
  let calls = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () => {
    calls++;
    if (calls === 1) {
      startedFirst();
      await new Promise(resolve => { releaseFirst = resolve; });
    }
    return new Response('{}');
  }});
  const run = init => bridge.handle(request(session), () => bridge.fetch(`${dashboardUrl}/api/sessions`, init));
  const first = run();
  await started;
  const controller = new AbortController();
  const second = run({ signal: controller.signal });
  controller.abort();
  await assert.rejects(second);
  const third = run();
  assert.equal(calls, 1);
  releaseFirst();
  await Promise.all([first, third]);
  assert.equal(calls, 2);
});
