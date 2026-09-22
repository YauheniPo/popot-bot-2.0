"""Opt-in smoke test of a built pinned Workspace; only fake loopback backends.

WORKSPACE_UPSTREAM_DIR must point to its source with dist/ already built.
No packages are downloaded, real credentials loaded or VPS requests made.
"""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('workspace_launch_smoke', ROOT / 'workspace-launch.py')
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)
UPSTREAM = os.environ.get('WORKSPACE_UPSTREAM_DIR')


class FakeBackend(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log cookie headers.

    def native_mcp(self, path):
        servers = self.server.mcp_servers
        if path == '/api/mcp/servers':
            if self.command == 'GET':
                return {'servers': list(servers.values())}, 200
            if self.command == 'POST':
                body = self.parsed_body
                if set(body) - {'name', 'url', 'command', 'args', 'env', 'auth', 'bearer_token'}:
                    return {'detail': 'Unknown native field'}, 422
                if body['name'] in servers:
                    return {'detail': 'Duplicate'}, 409
                servers[body['name']] = {'name': body['name'], 'url': body.get('url'),
                    'transport': 'http', 'auth': body.get('auth', 'none'), 'enabled': True}
                return servers[body['name']], 200
        parts = path.removeprefix('/api/mcp/servers/').split('/')
        name = parts[0]
        if name not in servers:
            return {'detail': 'Missing'}, 404
        if len(parts) == 1 and self.command == 'DELETE':
            del servers[name]
            return {'ok': True}, 200
        if parts[-1] == 'enabled' and self.command == 'PUT':
            servers[name]['enabled'] = self.parsed_body['enabled']
            return {'ok': True, 'name': name, 'enabled': self.parsed_body['enabled']}, 200
        if parts[-1] == 'test' and self.command == 'POST':
            return {'ok': True, 'tools': [{'name': 'fixture-tool', 'description': 'test only'}]}, 200
        return {'detail': 'Missing route'}, 404

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if not self.server.gateway:
            self.server.unrelated_cookie_seen |= 'grafana_session=' in self.headers.get('cookie', '')
        if self.server.gateway:
            if path.startswith('/api/mcp'):
                self.server.mcp_requests.append((self.command, path))
            payload = {'health': 'ok'} if path == '/health' else {'data': []}
            code = 200 if path in ('/health', '/v1/models') else 404
            if path == '/v1/chat/completions':
                code = 405
        elif path == '/api/status':
            payload, code = {'version': 'test'}, self.server.status_code
        elif '__Host-hermes_session_rt=smoke-refresh' not in self.headers.get('cookie', ''):
            payload, code = {'error': 'unauthenticated', 'reason': 'no_cookie'}, 401
        else:
            # A refresh-only browser must receive both rotated cookies over HTTP.
            self.server.authenticated_paths.append(path)
            payload = {
                '/api/sessions': {'sessions': [{'id': 'smoke-session', 'title': 'Smoke'}], 'total': 1},
                '/api/skills': {'skills': []},
                '/api/cron/jobs': {'jobs': []},
                '/api/config': {},  # No configured servers must still unlock native MCP.
                '/api/conductor/missions': {'missions': []},
                '/api/plugins/kanban/board': {'columns': []},
            }.get(path, {})
            code = 404 if path == '/api/mcp' else 200
            if path.startswith('/api/profiles/') and path.endswith('/skills'):
                payload, code = {'error': 'No nested profile skills route'}, 404
            if path == '/api/skills':
                profile = parse_qs(urlsplit(self.path).query).get('profile', [None])[0]
                if profile:
                    payload = [{'name': profile + '-skill', 'enabled': True}]
            if path == '/api/skills/toggle':
                payload = {'ok': True, **self.parsed_body}
            if path.startswith('/api/mcp/servers'):
                payload, code = self.native_mcp(path)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        if not self.server.gateway and code == 200 and path != '/api/status':
            for pair in ('__Host-hermes_session_at=smoke-access',
                         '__Host-hermes_session_rt=smoke-refresh'):
                self.send_header('Set-Cookie', f'{pair}; Path=/; Secure; HttpOnly; SameSite=Lax')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('content-length', '0')))
        self.server.posts.append((self.path, body))
        self.parsed_body = json.loads(body or '{}')
        self.do_GET()

    do_PUT = do_POST
    do_DELETE = do_POST


@unittest.skipUnless(UPSTREAM and shutil.which('node'), 'Set WORKSPACE_UPSTREAM_DIR to a built pinned Workspace')
class WorkspaceUpstreamSmokeTests(unittest.TestCase):
    def check_background_run_records(self, root, call, cookie):
        run = {'sessionKey': 'main', 'friendlyId': 'main', 'runId': 'old-run',
               'status': 'active', 'updatedAt': int(time.time() * 1000) - 3600000,
               'assistantText': 'saved output', 'thinkingText': '',
               'toolCalls': [], 'lifecycleEvents': []}
        run['createdAt'] = run['lastEventAt'] = run['updatedAt']
        run_dir = root / 'hermes/webui-mvp/runs/main'
        run_dir.mkdir(parents=True)
        run_file = run_dir / 'old-run.json'
        run_file.write_text(json.dumps(run))
        endpoint = '/api/runs/main/old-run/abandon'
        body = json.dumps({'updatedAt': run['updatedAt']})
        self.assertEqual(call('/api/runs/active')[0], 401)
        self.assertEqual(call(endpoint, method='POST', body=body)[0], 401)
        status, _, data = call('/api/runs/active', cookie)
        self.assertEqual(status, 200)
        self.assertFalse(data['runs'][0]['localStreamActive'])
        self.assertTrue(data['runs'][0]['canDismiss'])
        self.assertEqual(call(endpoint, cookie, 'POST', '{}')[0], 400)
        self.assertEqual(call(endpoint, cookie, 'POST', body)[0], 200)
        self.assertEqual(call('/api/runs/active', cookie)[2]['runs'], [])
        saved = json.loads(run_file.read_text())
        self.assertEqual(saved['status'], 'active')
        self.assertEqual(saved['assistantText'], 'saved output')
        self.assertEqual(saved['updatedAt'], run['updatedAt'])
        self.assertIn('dismissedAt', saved)
        run['updatedAt'] = int(time.time() * 1000)
        run_file.write_text(json.dumps(run))
        self.assertEqual(call(endpoint, cookie, 'POST', body)[0], 409)
        self.assertEqual(len(call('/api/runs/active', cookie)[2]['runs']), 1)

    def test_native_routes_and_auth_with_managed_entry(self):
        self.check_native_routes(status_code=200)

    def test_mcp_stays_on_dashboard_when_general_status_probe_fails(self):
        self.check_native_routes(status_code=503)

    def check_native_routes(self, status_code):
        with tempfile.TemporaryDirectory(prefix='workspace-smoke-') as directory:
            root = Path(directory)
            upstream = Path(UPSTREAM).resolve()
            self.assertTrue((upstream / 'dist/server/server.js').is_file())
            (root / 'dist').symlink_to(upstream / 'dist', target_is_directory=True)
            (root / 'package.json').write_text('{"type":"module"}')
            (root / 'server-entry.js').write_text((upstream / 'server-entry.js').read_text())
            (root / 'hermes').mkdir()
            (root / 'hermes/SOUL.md').write_text('fixture identity')
            (root / 'instructions').mkdir()
            (root / 'instructions/AGENTS.md').write_text('fixture instructions')
            (root / 'instructions/AGENTS.extra.md').write_text('extra instructions')
            (root / 'hermes/config.yaml').write_text(json.dumps({
                'model': {'provider': 'fixture-cloud', 'default': 'shared-test-model'}}))
            profile = root / 'hermes/profiles/builder'
            profile.mkdir(parents=True)
            (profile / 'config.yaml').write_text(json.dumps({
                'model': {'provider': 'fixture-cloud', 'default': 'shared-test-model'},
                'description': 'Fixture builder description', 'memory_char_limit': 37}))
            (profile / 'SOUL.md').write_text('Builder instructions with trailing newline\n')
            (profile / 'memories').mkdir()
            (profile / 'memories/MEMORY.md').write_text('fixture profile fact')
            (root / 'swarm.yaml').write_text(json.dumps({'version': 1, 'workers': [
                {'id': 'builder', 'model': 'GPT-5.5'},
                {'id': 'reviewer', 'model': 'other/old-model'},
            ]}))
            entry = launcher.write_managed_entry(root)
            servers = []
            for gateway in (False, True):
                server = ThreadingHTTPServer(('127.0.0.1', 0), FakeBackend)
                server.gateway = gateway
                server.status_code = 200
                server.mcp_requests = []
                server.authenticated_paths = []
                server.posts = []
                server.unrelated_cookie_seen = False
                server.mcp_servers = {}
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                servers.append(server)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            env = launcher.workspace_environment({
                'PATH': os.environ['PATH'], 'HOME': str(root),
                'HERMES_HOME': str(root / 'hermes'),
                'HERMES_INSTRUCTIONS_ROOT': str(root / 'instructions'),
            }, {'API_SERVER_KEY': 'smoke-gateway-token', 'HERMES_WORKSPACE_PASSWORD': 'smoke-password'},
                port, servers[1].server_port, servers[0].server_port)
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(['node', str(entry)], cwd=root, env=env,
                                           stdout=output, stderr=output)
                try:
                    def call(path, cookie='', method='GET', body=None):
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
                        try:
                            connection.request(method, path, body=body, headers={
                                'Cookie': cookie, 'Content-Type': 'application/json',
                                'Host': 'smoke.ts.net:3002',
                            })
                            response = connection.getresponse()
                            return response.status, response.getheaders(), json.loads(response.read())
                        finally:
                            connection.close()

                    for _ in range(100):
                        if process.poll() is not None:
                            self.fail('Managed Workspace process exited before becoming ready')
                        try:
                            call('/api/auth-check')
                            break
                        except (ConnectionError, OSError):
                            time.sleep(0.1)
                    else:
                        self.fail('Managed Workspace did not start within 10 seconds')
                    self.assertEqual(call('/api/sessions')[0], 401)
                    status, headers, _ = call('/api/auth', method='POST',
                                              body=json.dumps({'password': 'smoke-password'}))
                    self.assertEqual(status, 200)
                    cookie = next(value.split(';', 1)[0] for key, value in headers
                                  if key.lower() == 'set-cookie')
                    self.check_background_run_records(root, call, cookie)
                    self.assertEqual(call('/api/profiles/list')[0], 401)
                    self.assertEqual(call('/api/profiles/skills?name=builder')[0], 401)
                    status, _, profiles = call('/api/profiles/list', cookie)
                    self.assertEqual(status, 200)
                    builder = next(p for p in profiles['profiles'] if p['name'] == 'builder')
                    self.assertEqual(builder['description'], 'Fixture builder description')
                    self.assertEqual(builder['systemPrompt'], 'Builder instructions with trailing newline\n')
                    self.assertEqual(builder['soulPath'], 'profiles/builder/SOUL.md')
                    self.assertEqual(call('/api/memory/list')[0], 401)
                    self.assertEqual(call('/api/memory/read?path=SOUL.md')[0], 401)
                    self.assertEqual(call('/api/memory/write', method='POST', body='{}')[0], 401)
                    status, _, listed = call('/api/memory/list', cookie)
                    self.assertEqual(status, 200)
                    self.assertEqual({file['path'] for file in listed['files']},
                                     {'SOUL.md', 'workspace/AGENTS.md', 'workspace/AGENTS.extra.md', 'profiles/builder/SOUL.md', 'profiles/builder/memories/MEMORY.md'})
                    status, _, profile_memory = call('/api/memory/read?path=profiles/builder/memories/MEMORY.md', cookie)
                    self.assertEqual(status, 200)
                    self.assertEqual(profile_memory['metadata']['profile'], 'builder')
                    self.assertEqual(profile_memory['metadata']['limit'], 37)
                    self.assertIsNone(profile_memory['metadata']['loadedInSession'])
                    for name in ('SOUL.md', 'workspace/AGENTS.md', 'workspace/AGENTS.extra.md', 'profiles/builder/SOUL.md'):
                        status, _, memory = call('/api/memory/read?path=' + name, cookie)
                        self.assertEqual(status, 200)
                        edit = json.dumps({'path': name, 'content': 'updated fixture', 'version': memory['version']})
                        self.assertEqual(call('/api/memory/write', cookie, 'POST', edit)[0], 200)
                        self.assertEqual(call('/api/memory/read?path=' + name, cookie)[2]['content'], 'updated fixture')
                        self.assertEqual(call('/api/memory/write', cookie, 'POST', edit)[0], 409)
                    profiles = call('/api/profiles/list', cookie)[2]['profiles']
                    self.assertEqual(next(p for p in profiles if p['name'] == 'builder')['systemPrompt'], 'updated fixture')
                    self.assertNotIn('system_prompt', json.loads((profile / 'config.yaml').read_text()))
                    for name in ('../secrets.md', '.env', 'hermes-agent/AGENTS.md'):
                        self.assertEqual(call('/api/memory/read?path=' + name, cookie)[0], 400)
                        bad = json.dumps({'path': name, 'content': 'bad', 'version': '0' * 64})
                        self.assertEqual(call('/api/memory/write', cookie, 'POST', bad)[0], 400)
                    status, _, roster = call('/api/swarm-roster', cookie)
                    self.assertEqual(status, 200)
                    workers = roster['roster']['workers']
                    self.assertEqual(len(workers), 2)
                    self.assertTrue(all(w['model'] == 'fixture-cloud/shared-test-model' for w in workers))
                    status, _, body = call('/api/sessions', cookie)
                    self.assertEqual(status, 401)
                    self.assertEqual(body['code'], 'dashboard_auth_required')
                    self.assertEqual(call('/api/profiles/skills?name=builder', cookie)[0], 401)
                    cookie += '; __Host-hermes_session_rt=smoke-refresh; grafana_session=unrelated'
                    for route, backend_path in (
                        ('/api/sessions', '/api/sessions'),
                        ('/api/skills?tab=installed', '/api/skills'),
                        ('/api/claude-jobs?profiles=active', '/api/cron/jobs'),
                    ):
                        with self.subTest(route=route):
                            status, headers, body = call(route, cookie)
                            self.assertEqual(status, 200)
                            self.assertNotIn('error', body)
                            self.assertIn(backend_path, servers[0].authenticated_paths)
                            cookies = [value for key, value in headers if key.lower() == 'set-cookie']
                            self.assertEqual(len(cookies), 2)
                            self.assertTrue(all('Secure' in value and 'HttpOnly' in value for value in cookies))
                    for name in ('builder', 'researcher', 'reviewer'):
                        status, _, body = call('/api/profiles/skills?name=' + name, cookie)
                        self.assertEqual(status, 200)
                        self.assertEqual(body, {'profile': name, 'items': [{'name': name + '-skill', 'enabled': True}]})
                        toggle = {'profile': name, 'name': name + '-skill', 'enabled': False}
                        status, _, body = call('/api/profiles/toggle-skill', cookie, 'PUT', json.dumps(toggle))
                        self.assertEqual(status, 200)
                        self.assertEqual(body, {'ok': True, **toggle})
                        target, data = servers[0].posts[-1]
                        self.assertEqual(target, '/api/skills/toggle')
                        self.assertEqual(json.loads(data), toggle)
                    job = json.dumps({'name': 'smoke', 'prompt': 'fixture only', 'schedule': '0 0 * * *'})
                    self.assertEqual(call('/api/claude-jobs', cookie, 'POST', job)[0], 200)
                    self.assertIn(('/api/cron/jobs', job.encode()), servers[0].posts)
                    self.assertFalse(servers[0].unrelated_cookie_seen)
                    # Startup probed without cookies. Logging in must recover MCP
                    # without a manual reprobe, restart or config fallback key.
                    servers[0].status_code = status_code
                    status, _, body = call('/api/gateway-status', cookie)
                    self.assertEqual(status, 200)
                    self.assertTrue(body['capabilities']['mcp'])
                    self.assertFalse(body['capabilities']['mcpFallback'])
                    self.assertEqual(body['capabilities']['dashboard']['available'], status_code == 200)
                    servers[1].mcp_requests.clear()
                    self.assertEqual(call('/api/mcp', cookie)[0], 200)
                    self.assertEqual(call('/api/mcp', cookie)[2]['servers'], [])
                    definition = json.dumps({'name': 'fixture', 'transportType': 'http',
                        'url': 'https://mcp.example/rpc', 'authType': 'bearer', 'bearerToken': 'test-secret'})
                    self.assertEqual(call('/api/mcp', '', 'POST', definition)[0], 401)
                    self.assertEqual(call('/api/mcp', cookie, 'POST', definition)[0], 200)
                    self.assertEqual(call('/api/mcp', cookie, 'POST', definition)[0], 409)
                    status, _, body = call('/api/mcp', cookie)
                    self.assertEqual(status, 200)
                    self.assertEqual(body['servers'][0]['name'], 'fixture')
                    self.assertNotIn('test-secret', json.dumps(body))
                    status, _, body = call('/api/mcp/test', cookie, 'POST', '{"name":"fixture"}')
                    self.assertEqual(status, 200)
                    self.assertEqual(body['status'], 'connected')
                    self.assertEqual(body['discoveredTools'][0]['name'], 'fixture-tool')
                    status, _, body = call('/api/mcp', cookie)
                    self.assertEqual(status, 200)
                    self.assertEqual(body['servers'][0]['status'], 'connected')
                    self.assertEqual(body['servers'][0]['discoveredToolsCount'], 1)
                    self.assertTrue(body['servers'][0]['lastTestedAt'])
                    status, _, body = call('/api/mcp/configure', cookie, 'PUT', '{"name":"fixture","enabled":false}')
                    self.assertEqual(status, 200)
                    self.assertFalse(servers[0].mcp_servers['fixture']['enabled'])
                    self.assertEqual(call('/api/mcp/fixture', cookie, 'DELETE')[0], 200)
                    self.assertEqual(call('/api/mcp', cookie)[2]['servers'], [])
                    self.assertEqual(servers[1].mcp_requests, [])
                    # A healthy capability cache is not authorization. No native
                    # session must still fail closed, never fall back to Gateway.
                    workspace_cookie = cookie.split(';', 1)[0]
                    status, _, body = call('/api/mcp', workspace_cookie)
                    self.assertEqual(status, 401)
                    self.assertEqual(body['code'], 'dashboard_auth_required')
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
