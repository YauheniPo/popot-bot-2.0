// Server-only adapter for the pinned Workspace companion. No credentials on disk.
import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash } from 'node:crypto';
import { createMcpAdapter, correctWorkspacePresets } from './workspace-mcp-adapter.mjs';

const cookieName = /^__Host-hermes_session_(at|rt|provider)$/;

function sessionCookies(raw) {
  const result = new Map();
  for (const part of (raw || '').split(';')) {
    const separator = part.indexOf('=');
    const name = part.slice(0, separator).trim();
    if (separator > 0 && cookieName.test(name)) {
      result.set(name, part.slice(separator + 1).trim());
    }
  }
  return result;
}

function cookieHeader(cookies) {
  return [...cookies].map(([name, value]) => `${name}=${value}`).join('; ');
}

function sessionKey(cookies) {
  const token = cookies.get('__Host-hermes_session_rt') || cookies.get('__Host-hermes_session_at');
  return token ? createHash('sha256').update(token).digest('hex') : '';
}

function waitWithSignal(promise, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

// Apply one upstream Set-Cookie to the session. Only a Secure, HttpOnly,
// host-scoped cookie is adopted; anything else is ignored unchanged.
function applySetCookie(entry, cookie) {
  const pair = cookie.split(';', 1)[0];
  const separator = pair.indexOf('=');
  const name = pair.slice(0, separator);
  if (!cookieName.test(name) || !/;\s*Secure(?:;|$)/i.test(cookie) ||
      !/;\s*HttpOnly(?:;|$)/i.test(cookie) || !/;\s*Path=\/(?:;|$)/i.test(cookie) ||
      /;\s*Domain=/i.test(cookie)) return;
  const value = pair.slice(separator + 1);
  if (/;\s*Max-Age=0(?:;|$)/i.test(cookie) || !value) entry.cookies.delete(name);
  else entry.cookies.set(name, value);
  entry.changes.set(name, cookie);
}

export function createDashboardBridge({ dashboardUrl, fetchImpl = globalThis.fetch,
  timeoutMs = 15_000, cacheTtlMs = 30_000 }) {
  const base = new URL(dashboardUrl);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' ||
      base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
    throw new Error('Workspace dashboard bridge requires an exact loopback HTTP origin');
  }
  const context = new AsyncLocalStorage();
  // Short-lived refresh coalescing only. Tokens never become a global login.
  // A separate tab talking directly to Dashboard is outside this lock.
  const sessions = new Map();
  const capabilityProbes = new Map();

  async function refreshCapabilities(request, reprobe) {
    const path = new URL(request.url).pathname;
    const scope = context.getStore();
    if (!reprobe || !sessionKey(scope.cookies) ||
        !(path === '/api/gateway-status' || path === '/api/mcp' || path.startsWith('/api/mcp/'))) return;
    // The native route still verifies the Workspace login. Include its cookie
    // in the hash so one browser's successful probe cannot authenticate another.
    // The raw header was captured once in handle(); reusing it keeps the hash
    // key and the forwarded header identical without a second fallback.
    const cookieHeader = scope.rawCookie;
    const key = createHash('sha256').update(cookieHeader).digest('hex');
    const now = Date.now();
    for (const [id, item] of capabilityProbes) if (item.expires <= now) capabilityProbes.delete(id);
    const cached = capabilityProbes.get(key);
    if (cached) return cached.promise;
    if (capabilityProbes.size >= 256) return;
    const item = { expires: now + 120_000, promise: null };
    item.promise = (async () => {
      const url = new URL('/api/gateway-reprobe', request.url);
      const response = await reprobe(new Request(url, { method: 'POST',
        headers: { cookie: cookieHeader } }));
      const body = await response.json().catch(() => null);
      if (!response.ok || !body?.capabilities?.mcp) item.expires = Date.now() + 5_000;
    })().catch(error => { capabilityProbes.delete(key); throw error; });
    capabilityProbes.set(key, item);
    await item.promise;
  }

  function remember(key, entry) {
    if (!key || sessions.has(key) || sessions.size >= 256) return;
    const alias = { entry, expires: Date.now() + cacheTtlMs };
    sessions.set(key, alias);
    // Non-sliding expiry: a replaced RT must not remain redeemable forever.
    // Unref also allows the service/tests to exit with an idle cache.
    setTimeout(() => {
      if (sessions.get(key) === alias) sessions.delete(key);
    }, cacheTtlMs).unref();
  }

  function acquire(cookies) {
    const now = Date.now();
    for (const [key, alias] of sessions) {
      if (alias.expires <= now) sessions.delete(key);
    }
    const key = sessionKey(cookies);
    if (!key) return null;
    let entry = sessions.get(key)?.entry;
    if (!entry) {
      if (sessions.size >= 256) throw new Error('Dashboard session bridge capacity reached; retry later');
      entry = { cookies: new Map(cookies), changes: new Map(), tail: Promise.resolve() };
      remember(key, entry);
    }
    return entry;
  }

  // Adopt every rotation from one response and re-key the session for the
  // cookies it now carries, so later requests join the same queue.
  function adoptRotatedCookies(scope, entry, response) {
    for (const cookie of response.headers.getSetCookie()) applySetCookie(entry, cookie);
    remember(sessionKey(entry.cookies), entry);
    scope.cookies = new Map(entry.cookies);
    for (const [name, cookie] of entry.changes) scope.changes.set(name, cookie);
  }

  // Bound queue/connection waits, not the lifetime of an established SSE
  // response. Honor an upstream caller's explicit deadline when provided.
  function connectionSignal(input, init) {
    const supplied = init.signal || (input instanceof Request ? input.signal : null);
    if (supplied) return { signal: supplied, timer: null };
    const deadline = new AbortController();
    const timer = setTimeout(() => {
      deadline.abort(new DOMException('Dashboard connection timed out', 'TimeoutError'));
    }, timeoutMs);
    return { signal: deadline.signal, timer };
  }

  async function bridgedFetch(input, init = {}) {
    const url = new URL(input instanceof Request ? input.url : input);
    if (url.origin !== base.origin) return fetchImpl(input, init);
    const candidate = context.getStore();
    const scope = candidate?.active ? candidate : null;
    const entry = scope ? acquire(scope.cookies) : null;
    const { signal, timer } = connectionSignal(input, init);
    let release = () => {};
    const previous = entry?.tail || Promise.resolve();
    if (entry) {
      const gate = new Promise(resolve => { release = resolve; });
      entry.tail = previous.catch(() => {}).then(() => gate);
    }
    try {
      await waitWithSignal(previous, signal);
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      if (entry) {
        // Do not combine the old HTML-scraped bearer with the real user session.
        headers.delete('authorization');
        headers.set('cookie', cookieHeader(entry.cookies));
        headers.set('x-forwarded-proto', 'https');
      }
      // Never forward credentials through an upstream redirect (including OAuth).
      const response = await fetchImpl(input, { ...init, headers, signal, redirect: 'manual' });
      if (scope && response.status === 401) scope.authRequired = true;
      if (entry) adoptRotatedCookies(scope, entry, response);
      return response;
    } finally {
      clearTimeout(timer);
      release();
    }
  }

  async function handle(request, handler, reprobe) {
    const rawCookie = request.headers.get('cookie') || '';
    const scope = { rawCookie, cookies: sessionCookies(rawCookie), changes: new Map(), authRequired: false, active: true };
    return context.run(scope, async () => {
      let response;
      try {
        await refreshCapabilities(request, reprobe);
        response = await handler();
        // Repair known broken seed templates only after the upstream auth check.
        if (request.method === 'GET' && new URL(request.url).pathname === '/api/mcp/presets' && response.ok) {
          const body = await response.json();
          const headers = new Headers(response.headers);
          for (const key of ['content-length', 'content-encoding', 'etag']) headers.delete(key);
          headers.set('cache-control', 'no-store');
          response = Response.json(correctWorkspacePresets(body), { status: response.status, headers });
        }
      } finally {
        scope.active = false;
      }
      if (scope.authRequired && response.status >= 400) {
        const hostname = new URL(request.url).hostname;
        const login = /^[a-z0-9.-]+\.ts\.net$/i.test(hostname) ? `https://${hostname}/login` : null;
        const hint = login ? ` ${login}` : '';
        response = Response.json({
          error: 'Hermes Dashboard login required. Open the official Dashboard on HTTPS port 443, sign in, then reload Workspace.' + hint,
          code: 'dashboard_auth_required', login_url: login,
        }, { status: 401 });
      }
      if (!scope.changes.size) return response;
      const headers = new Headers(response.headers);
      for (const cookie of scope.changes.values()) headers.append('set-cookie', cookie);
      headers.set('cache-control', 'no-store');
      return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
    });
  }

  return { fetch: createMcpAdapter({ dashboardUrl, fetchImpl: bridgedFetch }), handle };
}
