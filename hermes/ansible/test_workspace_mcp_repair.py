"""Opt-in repair must not replace customized MCP servers or persist credentials."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('workspace_mcp', Path(__file__).parent / 'filter_plugins/workspace_mcp.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class WorkspaceMcpRepairTests(unittest.TestCase):
    def test_stdio_cache_defaults_survive_filtered_environment_and_are_idempotent(self):
        config = {'mcp_servers': {
            'memory': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-memory']},
            'fetch': {'command': 'uvx', 'args': ['mcp-server-fetch'], 'enabled': False},
            'remote': {'url': 'https://example.test/mcp'},
        }}
        result = module.stdio_runtime(config, '/srv/hermes')
        self.assertEqual(result['mcp_servers']['memory']['env']['npm_config_cache'], '/srv/hermes/.cache/mcp/npm')
        self.assertEqual(result['mcp_servers']['fetch']['env']['UV_CACHE_DIR'], '/srv/hermes/.cache/mcp/uv')
        self.assertEqual(result['mcp_servers']['fetch']['env']['UV_TOOL_DIR'], '/srv/hermes/.cache/mcp/uv-tools')
        self.assertEqual(result['mcp_servers']['fetch']['env']['UV_TOOL_BIN_DIR'], '/srv/hermes/.cache/mcp/uv-bin')
        self.assertEqual(result['mcp_servers']['fetch']['env']['PATH'], '/srv/hermes/bin:${PATH}')
        self.assertFalse(result['mcp_servers']['fetch']['enabled'])
        self.assertEqual(result['mcp_servers']['remote'], config['mcp_servers']['remote'])
        self.assertNotIn('env', config['mcp_servers']['memory'])
        self.assertEqual(module.stdio_runtime(result, '/srv/hermes'), result)
        self.assertEqual(module.stdio_runtime({}, '/srv/hermes'), {})

    def test_stdio_runtime_preserves_explicit_paths_secrets_and_unknown_launchers(self):
        config = {'mcp_servers': {
            'npm': {'command': '/usr/bin/npx', 'env': {'NPM_CONFIG_CACHE': '/custom/cache', 'TOKEN': '${KEY}'}},
            'uv': {'command': 'uvx', 'env': {'UV_CACHE_DIR': '/custom/uv', 'PATH': '/custom/bin'}},
            'custom': {'command': 'custom-runner'},
        }}
        result = module.stdio_runtime(config, '/srv/hermes')
        self.assertEqual(result['mcp_servers']['npm'], config['mcp_servers']['npm'])
        self.assertEqual(result['mcp_servers']['uv']['env']['UV_CACHE_DIR'], '/custom/uv')
        self.assertEqual(result['mcp_servers']['uv']['env']['PATH'], '/custom/bin')
        self.assertEqual(result['mcp_servers']['custom'], config['mcp_servers']['custom'])
        for home in ('/', 'relative', '/srv/../etc'):
            with self.assertRaises(ValueError):
                module.stdio_runtime(config, home)

    def test_exact_broken_presets_are_repaired_idempotently_without_secret_values(self):
        config = {'model': {'default': 'custom'}, 'mcp_servers': {
            'fetch': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-fetch'], 'enabled': False},
            'github': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-everything'],
                       'tools': ['read']},
            'memory': {'command': 'custom-memory'},
        }}
        result = module.repair(config, {'MCP_GITHUB_API_KEY': 'new-fixture-secret'})
        self.assertEqual(result['mcp_servers']['fetch']['command'], 'uvx')
        self.assertFalse(result['mcp_servers']['fetch']['enabled'])
        self.assertEqual(result['mcp_servers']['github']['headers'], {'Authorization': 'Bearer ${MCP_GITHUB_API_KEY}'})
        self.assertEqual(result['mcp_servers']['github']['tools'], ['read'])
        self.assertNotIn('fixture-secret', str(result))
        self.assertEqual(result['model'], config['model'])
        self.assertEqual(result['mcp_servers']['memory'], config['mcp_servers']['memory'])
        self.assertEqual(config['mcp_servers']['fetch']['command'], 'npx')
        self.assertEqual(module.repair(result, {}), result)

    def test_custom_launchers_flags_urls_and_credentials_are_not_overwritten(self):
        for extras in ({'command': 'custom-npx'}, {'args': ['--custom']},
                       {'url': 'https://custom.example/mcp'}, {'env': {'OTHER_TOKEN': 'keep'}},
                       {'headers': {'Authorization': 'keep'}}):
            config = {'mcp_servers': {'github': {'command': 'npx',
                'args': ['-y', '@modelcontextprotocol/server-everything'], **extras}}}
            self.assertEqual(module.repair(config, {}), config)

    def test_legacy_github_personal_token_does_not_authorize_preset_repair(self):
        config = {'mcp_servers': {'github': {
            'command': 'npx',
            'args': ['-y', '@modelcontextprotocol/server-everything'],
            'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'legacy-fixture-token'},
        }}}
        self.assertEqual(module.repair(config, {'MCP_GITHUB_API_KEY': 'new-token'}), config)

    def test_cache_defaults_do_not_block_later_opt_in_preset_repair(self):
        config = {'mcp_servers': {
            'fetch': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-fetch']},
            'github': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-everything']},
        }}
        cached = module.stdio_runtime(config, '/srv/hermes')
        repaired = module.repair(cached, {'MCP_GITHUB_API_KEY': 'fixture-secret'})
        result = module.stdio_runtime(repaired, '/srv/hermes')
        self.assertEqual(result['mcp_servers']['fetch']['command'], 'uvx')
        self.assertEqual(result['mcp_servers']['fetch']['env']['UV_TOOL_DIR'], '/srv/hermes/.cache/mcp/uv-tools')
        self.assertEqual(result['mcp_servers']['github']['url'], 'https://api.githubcopilot.com/mcp/readonly')
        self.assertNotIn('env', result['mcp_servers']['github'])
        self.assertNotIn('fixture-secret', str(result))

    def test_missing_vault_token_fails_before_any_mutation(self):
        config = {'mcp_servers': {'github': {'command': 'npx',
            'args': ['-y', '@modelcontextprotocol/server-everything']}}}
        with self.assertRaisesRegex(ValueError, 'MCP_GITHUB_API_KEY'):
            module.repair(config, {})
        self.assertEqual(config['mcp_servers']['github']['command'], 'npx')
        self.assertEqual(module.repair({}, {}), {})

    def test_unreadable_vault_is_reported_separately_from_an_unset_token(self):
        # A sealed or unreadable Vault must not look like a deliberate omission,
        # so the error names the mapping rather than the missing key.
        config = {'mcp_servers': {'github': {'command': 'npx',
            'args': ['-y', '@modelcontextprotocol/server-everything']}}}
        with self.assertRaisesRegex(ValueError, 'Vault secrets must be a mapping'):
            module.repair(config, None)
        self.assertEqual(config['mcp_servers']['github']['command'], 'npx')

    def test_non_mapping_sections_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'mcp_servers must be a mapping'):
            module.stdio_runtime({'mcp_servers': ['not', 'a', 'mapping']}, '/srv/hermes')
        with self.assertRaisesRegex(ValueError, 'mcp_servers must be a mapping'):
            module.repair({'mcp_servers': 'npx'}, {})
        with self.assertRaisesRegex(ValueError, 'env must be a mapping'):
            module.stdio_runtime({'mcp_servers': {'memory': {'command': 'npx', 'env': 'not-a-map'}}},
                                 '/srv/hermes')

    def test_servers_without_a_launcher_command_are_left_alone(self):
        config = {'mcp_servers': {
            'no-command': {'args': ['-y', '@modelcontextprotocol/server-fetch']},
            'numeric': {'command': 5},
            'remote': {'url': 'https://example.test/mcp', 'env': {}},
        }}
        self.assertEqual(module.stdio_runtime(config, '/srv/hermes'), config)

    def test_filter_module_exposes_both_filters(self):
        self.assertEqual(set(module.FilterModule().filters()),
                         {'hermes_repair_workspace_mcp', 'hermes_mcp_stdio_runtime'})
