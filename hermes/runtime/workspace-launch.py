#!/usr/bin/env python3
"""Launch only Workspace, using the existing Hermes home and Vault credentials."""

import argparse
import json
import os
from pathlib import Path
import tempfile


def managed_entry(source, bridge):
    """Adapt the pinned entry without modifying tracked upstream files."""
    import_anchor = "import server from './dist/server/server.js'"
    headers_anchor = 'const headers = Object.fromEntries(response.headers.entries())'
    if source.count(import_anchor) != 1 or source.count(headers_anchor) != 1:
        raise ValueError('Unsupported Workspace server entry; re-verify the dashboard bridge')
    replacement = (
        f'import {{ createDashboardBridge }} from {json.dumps(bridge.resolve().as_uri())}\n'
        'const bridge = createDashboardBridge({ dashboardUrl: process.env.HERMES_DASHBOARD_URL })\n'
        'globalThis.fetch = bridge.fetch\n'
        "const upstream = (await import('./dist/server/server.js')).default\n"
        'const server = { fetch: request => bridge.handle(request, () => upstream.fetch(request), probe => upstream.fetch(probe)) }'
    )
    return source.replace(import_anchor, replacement).replace(
        headers_anchor, headers_anchor + '\n'
        "    if (response.headers.getSetCookie().length) headers['set-cookie'] = response.headers.getSetCookie()",
    )


def write_managed_entry(source_dir):
    root = source_dir.resolve()
    content = managed_entry((root / 'server-entry.js').read_text(),
                            Path(__file__).with_name('workspace-dashboard-bridge.mjs'))
    target = root / '.hermes-managed-server.mjs'
    # The leaf name is fixed, but confirm the resolved path still sits directly
    # inside the resolved source directory before it is handed to Node.
    if target.resolve().parent != root:
        raise ValueError('Managed entry must stay inside the Workspace source directory')
    # Atomic replacement does not follow an existing leaf symlink. No secrets
    # are written here; credentials remain in the launch environment/cookies.
    with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def trusted_executable(value):
    """Accept only an absolute path to an existing executable file.

    The service unit supplies this path, not an interactive caller, so resolve
    it once and refuse anything that is not an executable file.
    """
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError('--node must be an absolute path')
    resolved = candidate.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f'--node is not an executable file: {resolved}')
    return resolved


def workspace_environment(base, secrets, port, api_port, dashboard_port):
    for key in ('API_SERVER_KEY', 'HERMES_WORKSPACE_PASSWORD'):
        value = secrets.get(key)
        if not isinstance(value, str) or len(value) < 4:
            raise ValueError(f'{key} must contain at least 4 characters in Hermes .env')
    env = dict(base)
    env.update({
        'NODE_ENV': 'production',
        'HOST': '127.0.0.1',
        'PORT': str(port),
        'HERMES_API_URL': f'http://127.0.0.1:{api_port}',
        'HERMES_DASHBOARD_URL': f'http://127.0.0.1:{dashboard_port}',
        'HERMES_API_TOKEN': secrets['API_SERVER_KEY'],
        'HERMES_PASSWORD': secrets['HERMES_WORKSPACE_PASSWORD'],
        'COOKIE_SECURE': '1',
        'TRUST_PROXY': '1',
        'HERMES_ALLOW_INSECURE_REMOTE': '0',
        'CLAUDE_ALLOW_INSECURE_REMOTE': '0',
    })
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--api-port', type=int, required=True)
    parser.add_argument('--dashboard-port', type=int, required=True)
    args = parser.parse_args()
    # Use Hermes's installed dotenv parser, never source an environment file
    # as shell code. Read afresh on every service start after Vault rotation.
    from dotenv import dotenv_values
    home = Path(os.environ['HERMES_HOME'])
    try:
        env = workspace_environment(os.environ, dotenv_values(home / '.env', interpolate=False),
                                    args.port, args.api_port, args.dashboard_port)
        node = trusted_executable(args.node)
        source = args.source.resolve()
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    try:
        entry = write_managed_entry(source)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Workspace entry setup failed: {error}\n')
    os.chdir(source)
    os.execve(node, [str(node), str(entry)], env)


if __name__ == '__main__':
    main()
