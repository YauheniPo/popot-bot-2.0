"""Narrow, opt-in repair of the pinned Workspace's two broken MCP presets."""
from copy import deepcopy
from pathlib import PurePosixPath


def stdio_runtime(config, hermes_home):
    """Default package caches inside the writable home, not the service HOME.

    Hermes filters subprocess environment: these belong in each server's env,
    not only systemd Environment=. Preserve operator overrides and HTTP servers.
    """
    home = PurePosixPath(hermes_home)
    if not home.is_absolute() or home == PurePosixPath('/') or '..' in home.parts:
        raise ValueError('Hermes home must be an absolute non-root path without traversal')
    result = deepcopy(config)
    servers = result.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcp_servers must be a mapping')
    for server in servers.values():
        if not isinstance(server, dict) or server.get('url'):
            continue
        command = server.get('command')
        if not isinstance(command, str):
            continue
        binary = PurePosixPath(command).name
        if binary not in ('npx', 'npm', 'uvx', 'uv'):
            continue
        env = server.setdefault('env', {})
        if not isinstance(env, dict):
            raise ValueError('MCP stdio env must be a mapping')
        if binary in ('npx', 'npm'):
            if not any(str(key).lower() == 'npm_config_cache' for key in env):
                env['npm_config_cache'] = f'{home}/.cache/mcp/npm'
        else:
            env.setdefault('UV_CACHE_DIR', f'{home}/.cache/mcp/uv')
            env.setdefault('UV_PYTHON_INSTALL_DIR', f'{home}/.cache/mcp/python')
            env.setdefault('UV_TOOL_DIR', f'{home}/.cache/mcp/uv-tools')
            env.setdefault('UV_TOOL_BIN_DIR', f'{home}/.cache/mcp/uv-bin')
            env.setdefault('PATH', f'{home}/bin:${{PATH}}')
    return result


def repair(config, vault):
    result = deepcopy(config)
    servers = result.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcp_servers must be a mapping')
    for name, package in (('fetch', 'fetch'), ('github', 'everything')):
        server = servers.get(name)
        if not isinstance(server, dict):
            continue
        if (server.get('command') != 'npx'
                or server.get('args') != ['-y', f'@modelcontextprotocol/server-{package}']
                or server.get('url') or server.get('headers')
                or server.get('auth') not in (None, 'none')
                or server.get('transport') not in (None, 'stdio')):
            continue
        environment = server.get('env', {})
        # The runtime defaults may already have been added by an earlier deploy.
        allowed_env = {'npm_config_cache', 'NPM_CONFIG_CACHE'}
        if name == 'github':
            allowed_env.add('GITHUB_PERSONAL_ACCESS_TOKEN')
        if not isinstance(environment, dict) or set(environment) - allowed_env:
            continue
        if name == 'fetch':
            server.update(command='uvx', args=['mcp-server-fetch'])
        else:
            if not isinstance(vault, dict):
                raise ValueError('Vault secrets must be a mapping before repairing GitHub MCP')
            token = vault.get('MCP_GITHUB_API_KEY')
            if not isinstance(token, str) or not token.strip():
                raise ValueError('Put a limited-scope PAT in Vault hermes_secret_env.MCP_GITHUB_API_KEY before repairing GitHub MCP')
            for key in ('command', 'args', 'env', 'transport', 'auth'):
                server.pop(key, None)
            server.update(url='https://api.githubcopilot.com/mcp/readonly',
                          headers={'Authorization': 'Bearer ${MCP_GITHUB_API_KEY}'})
    return result


class FilterModule:
    def filters(self):
        return {'hermes_repair_workspace_mcp': repair,
                'hermes_mcp_stdio_runtime': stdio_runtime}
