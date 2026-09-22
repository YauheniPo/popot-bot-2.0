import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { workspaceProfileUi } from './workspace-profile-ui.mjs';

const upstream = process.env.WORKSPACE_UPSTREAM_DIR;
const enabled = { skip: !upstream };
function transformed(file) {
  const source = fs.readFileSync(path.join(upstream, 'src', file), 'utf8');
  return workspaceProfileUi().transform(source, '/src/' + file)?.code ?? source;
}
function route(file, { authenticated = true, fetchImpl } = {}) {
  const ts = createRequire(path.join(upstream, 'package.json'))('typescript');
  const js = ts.transpileModule(transformed('routes/api/profiles/' + file + '.ts'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const context = { exports: {}, URL, AbortSignal, require: () => ({
    createFileRoute: () => value => value,
    json: (value, options) => Response.json(value, options),
    isAuthenticated: () => authenticated,
    ensureGatewayProbed: async () => ({ skills: true, dashboard: { available: true } }),
    dashboardFetch: fetchImpl,
    requireJsonContentType: request => request.headers.get('content-type') === 'application/json'
      ? null : Response.json({ error: 'JSON required' }, { status: 415 }),
  }) };
  vm.runInNewContext(js, context);
  return context.exports.Route.server.handlers;
}
const request = (name = 'builder') => new Request('http://workspace/api/profiles/skills?name=' + encodeURIComponent(name));
const toggle = body => new Request('http://workspace/api/profiles/toggle-skill', {
  method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
});

test('skills adapters reject changed upstream contracts', () => {
  for (const file of ['routes/api/profiles/skills.ts', 'routes/api/profiles/toggle-skill.ts', 'screens/skills/skills-screen.tsx']) {
    assert.throws(() => workspaceProfileUi().transform('changed', '/src/' + file), /Unsupported Workspace profile/);
  }
});

test('each profile reads its own skills through the native query parameter', enabled, async () => {
  const api = route('skills', { fetchImpl: async (url, options) => {
    assert.ok(options.signal);
    assert.match(url, /^\/api\/skills\?profile=/);
    const profile = new URL(url, 'http://dashboard').searchParams.get('profile');
    return Response.json([{ name: profile + '-skill', enabled: true }]);
  } });
  for (const name of ['builder', 'researcher', 'reviewer']) {
    const response = await api.GET({ request: request(name) });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { profile: name, items: [{ name: name + '-skill', enabled: true }] });
  }
});

test('toggle uses native endpoint and explicit profile without changing other profiles', enabled, async () => {
  const states = { builder: true, researcher: true, reviewer: true };
  const api = route('toggle-skill', { fetchImpl: async (url, options) => {
    assert.equal(url, '/api/skills/toggle');
    assert.equal(options.method, 'PUT');
    const body = JSON.parse(options.body);
    assert.equal(body.name, 'fixture');
    states[body.profile] = body.enabled;
    return Response.json({ ok: true });
  } });
  const response = await api.PUT({ request: toggle({ profile: 'builder', name: 'fixture', enabled: false }) });
  assert.equal(response.status, 200);
  assert.deepEqual(states, { builder: false, researcher: true, reviewer: true });
});

test('auth, profile validation and JSON content type remain enforced before proxying', enabled, async () => {
  const fetchImpl = () => assert.fail('must not contact dashboard');
  assert.equal((await route('skills', { authenticated: false, fetchImpl }).GET({ request: request() })).status, 401);
  assert.equal((await route('skills', { fetchImpl }).GET({ request: request('../default') })).status, 400);
  assert.equal((await route('toggle-skill', { authenticated: false, fetchImpl }).PUT({ request: toggle({}) })).status, 401);
  assert.equal((await route('toggle-skill', { fetchImpl }).PUT({ request: new Request('http://workspace', { method: 'PUT' }) })).status, 415);
});

test('backend failures and malformed lists never masquerade as empty skills', enabled, async () => {
  for (const status of [401, 404, 503]) {
    const api = route('skills', { fetchImpl: async () => Response.json({ error: 'fixture' }, { status }) });
    assert.equal((await api.GET({ request: request() })).status, status);
  }
  for (const payload of [{ unexpected: [] }, null, 'wrong']) {
    const api = route('skills', { fetchImpl: async () => Response.json(payload) });
    assert.equal((await api.GET({ request: request() })).status, 502);
  }
  const api = route('skills', { fetchImpl: async () => Response.json([]) });
  assert.deepEqual((await (await api.GET({ request: request() })).json()).items, []);
});

test('skills UI renders a retryable query error instead of the empty grid', enabled, () => {
  const code = transformed('screens/skills/skills-screen.tsx');
  assert.match(code, /skillsQuery\.isError \? \(/);
  assert.match(code, /role="alert"/);
  assert.match(code, /skillsQuery\.refetch\(\)/);
  assert.match(code, /\) : \(\s*<SkillsGrid/);
});
