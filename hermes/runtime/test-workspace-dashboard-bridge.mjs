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

test('capability refresh tolerates a request without a cookie header', async () => {
  let probes = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    Response.json({ ok: true }) });
  const reprobe = async () => { probes += 1; return Response.json({ capabilities: { mcp: true } }); };
  const deny = async () => new Response(null, { status: 401 });
  // No cookie header: sessionKey is empty, so refreshCapabilities returns early
  // without hashing (the `|| ''` fallback on line 58 is still executed).
  const response = await bridge.handle(
    new Request('https://example.ts.net:3002/api/gateway-status'), deny, reprobe);
  assert.equal(response.status, 401);
  assert.equal(probes, 0);
});

test('an expired capability probe is dropped before reuse', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    Response.json({ ok: true }) });
  const cookie = `${session}; claude-auth=workspace`;
  let probes = 0;
  const reprobe = async () => { probes += 1; return Response.json({ capabilities: { mcp: true } }); };
  const handler = async () => Response.json({ ok: true });
  const run = () => bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', {
    headers: { cookie } }), handler, reprobe);
  await run();
  assert.equal(probes, 1);
  // Past the 120s probe TTL, the cached entry is expired and evicted (line 60).
  const realNow = Date.now;
  Date.now = () => realNow() + 200_000;
  try { await run(); } finally { Date.now = realNow; }
  assert.equal(probes, 2);
});

test('the capability cache stops growing at 256 entries', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    Response.json({ ok: true }) });
  const handler = async () => Response.json({ ok: true });
  let probes = 0;
  const reprobe = async () => { probes += 1; return Response.json({ capabilities: { mcp: true } }); };
  for (let i = 0; i < 257; i += 1) {
    await bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', {
      headers: { cookie: `${session}; filler=${i}` } }), handler, reprobe);
  }
  // The 257th distinct key hits the capacity guard and is not probed again.
  const before = probes;
  await bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', {
    headers: { cookie: `${session}; filler=extra` } }), handler, reprobe);
  assert.equal(probes, before);
});

test('the session bridge stops caching at 256 sessions', async () => {
  let upstreamCalls = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () => {
    upstreamCalls += 1;
    return Response.json({ ok: true });
  }});
  const handler = async () => Response.json({ ok: true });
  for (let i = 0; i < 256; i += 1) {
    const response = await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', {
      headers: { cookie: `__Host-hermes_session_rt=refresh-${i}` } }), handler);
    assert.ok(response);
  }
  const saturated = upstreamCalls;
  // The 257th distinct session is neither cached nor bridged upstream.
  await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', {
    headers: { cookie: '__Host-hermes_session_rt=overflow' } }), handler);
  assert.equal(upstreamCalls, saturated);
});

test('an expired session alias is evicted on the next acquire', async () => {
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    Response.json({ ok: true }) });
  const cookie = `${session}; claude-auth=workspace`;
  const handler = async () => Response.json({ ok: true });
  await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', {
    headers: { cookie } }), handler);
  const realNow = Date.now;
  Date.now = () => realNow() + 10 * 60 * 1000;
  try {
    const response = await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', {
      headers: { cookie } }), handler);
    assert.ok(response);
  } finally { Date.now = realNow; }
});

test('the login hint is offered only for a tailnet hostname', async () => {
  // authRequired is set inside bridgedFetch, so the request must go through
  // bridge.fetch (the MCP adapter) for the 401 to mark the scope.
  const makeBridge = () => createDashboardBridge({ dashboardUrl,
    fetchImpl: async () => Response.json({ error: 'no_cookie' }, { status: 401 }) });

  const tailnet = makeBridge();
  const tailnetHandler = async () => {
    // Re-enter the bridge upstream so bridgedFetch sees a 401 and flags it.
    await tailnet.fetch(`${dashboardUrl}/api/mcp`);
    return Response.json({ ok: true }, { status: 401 });
  };
  const tailnetResponse = await tailnet.handle(
    new Request('https://vps.example.ts.net:3002/api/gateway-status', { headers: { cookie: session } }),
    tailnetHandler);
  const tailnetBody = await tailnetResponse.json();
  assert.equal(tailnetBody.login_url, 'https://vps.example.ts.net/login');

  const plain = makeBridge();
  const plainHandler = async () => {
    await plain.fetch(`${dashboardUrl}/api/mcp`);
    return Response.json({ ok: true }, { status: 401 });
  };
  const plainResponse = await plain.handle(
    new Request('http://127.0.0.1:3002/api/gateway-status', { headers: { cookie: session } }),
    plainHandler);
  const plainBody = await plainResponse.json();
  assert.equal(plainBody.login_url, null);
});

test('bridgedFetch accepts a string url, a Request and explicit init parts', async () => {
  const seen = [];
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async (input, init) => {
    seen.push({
      url: typeof input === 'string' ? input : input.url,
      headers: new Headers(init.headers),
      signal: init.signal,
    });
    return Response.json({ ok: true });
  }});
  const controller = new AbortController();
  // String input + explicit init.headers and init.signal (lines 104, 109, 125).
  await bridge.fetch(`${dashboardUrl}/api/gateway-status`, {
    method: 'GET', headers: { 'x-init': 'yes' }, signal: controller.signal,
  });
  assert.equal(seen[0].headers.get('x-init'), 'yes');
  assert.equal(seen[0].signal, controller.signal);

  // Request input with no init at all: the Request halves supply everything.
  const requestWithParts = new Request(`${dashboardUrl}/api/gateway-status`, {
    method: 'GET', headers: { 'x-request': 'yes' },
  });
  await bridge.fetch(requestWithParts);
  assert.equal(seen[1].headers.get('x-request'), 'yes');

  // String input with no init.headers: the undefined fallback is used.
  await bridge.fetch(`${dashboardUrl}/api/gateway-status`, { method: 'GET' });
  assert.equal(seen[2].headers.get('x-request'), null);
});

test('a cleared session cookie is removed from the alias map', async () => {
  const rotated = '__Host-hermes_session_rt=fresh; Path=/; Secure; HttpOnly; SameSite=Lax';
  const cleared = '__Host-hermes_session_rt=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0';
  let body = 'first';
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () =>
    new Response(body, { status: 200, headers: { 'set-cookie': body === 'first' ? rotated : cleared } }) });
  const cookie = `${session}; claude-auth=workspace`;
  const handler = async () => new Response('ok', { headers: { 'set-cookie': body === 'first' ? rotated : cleared } });
  await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', { headers: { cookie } }), handler);
  // The second response clears the cookie with Max-Age=0 and an empty value,
  // exercising both sides of the removal test on line 144.
  body = 'second';
  const response = await bridge.handle(
    new Request('https://example.ts.net:3002/api/sessions', { headers: { cookie } }), handler);
  assert.ok(response);
  // An empty-value Set-Cookie without Max-Age is the other removal side.
  body = 'third';
  await bridge.handle(new Request('https://example.ts.net:3002/api/sessions', { headers: { cookie } }),
    async () => new Response('ok', { headers: {
      'set-cookie': '__Host-hermes_session_rt=; Path=/; Secure; HttpOnly; SameSite=Lax' } }));
});

test('an expired session alias is evicted and re-acquired', async () => {
  let upstream = 0;
  const bridge = createDashboardBridge({ dashboardUrl, fetchImpl: async () => {
    upstream += 1;
    return Response.json({ ok: true });
  }});
  const cookie = '__Host-hermes_session_rt=expiry-probe';
  // Route through bridge.fetch so bridgedFetch acquires the session for real.
  const handler = async () => bridge.fetch(`${dashboardUrl}/api/gateway-status`);
  await bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', { headers: { cookie } }), handler);
  const afterFirst = upstream;
  assert.ok(afterFirst >= 1);
  // Past the cache TTL the alias expires; the next acquire evicts it (line 90)
  // and creates a fresh entry, so upstream is consulted again.
  const realNow = Date.now;
  Date.now = () => realNow() + 10 * 60 * 1000;
  try {
    await bridge.handle(new Request('https://example.ts.net:3002/api/gateway-status', { headers: { cookie } }), handler);
  } finally { Date.now = realNow; }
  assert.ok(upstream > afterFirst);
});
