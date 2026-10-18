"""Execute real Ansible tasks locally, replacing only VPS-facing commands."""
from copy import deepcopy
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ANSIBLE = Path(__file__).resolve().parent


def run_play(root, tasks, variables, handlers=None):
    path = root / 'play.yml'
    path.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                                    'vars': {'ansible_python_interpreter': sys.executable, **variables},
                                    'tasks': tasks, 'handlers': handlers or []}]))
    return subprocess.run(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(path)],
                          text=True, capture_output=True, timeout=40,
                          env={**os.environ, 'ANSIBLE_CONFIG': str(ANSIBLE / 'ansible.cfg'),
                               'ANSIBLE_INJECT_FACT_VARS': 'false',
                               'ANSIBLE_DISPLAY_TRACEBACK': 'deprecated',
                               'ANSIBLE_FILTER_PLUGINS': str(ANSIBLE / 'filter_plugins')})


@unittest.skipUnless(shutil.which('ansible-playbook'), 'Ansible is required')
class ReconciliationPlaybookTests(unittest.TestCase):
    def test_mcp_filter_does_not_copy_ansible_plugin_loaders(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_play(Path(directory), [
                {'ansible.builtin.set_fact': {'configured': "{{ existing | hermes_mcp_stdio_runtime('/srv/hermes') }}"}},
                {'ansible.builtin.assert': {'that': ["configured.mcp_servers.fetch.env.UV_CACHE_DIR == '/srv/hermes/.cache/mcp/uv'"]}},
            ], {'existing': {'mcp_servers': {'fetch': {'command': 'uvx'}}}})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('PluginLoader with aliases', result.stdout + result.stderr)

    def test_config_repeat_preserves_native_yaml_format_and_repairs_permissions(self):
        source = yaml.safe_load((ANSIBLE / 'tasks/runtime.yml').read_text())
        install = next(t for t in source if t.get('name') == 'Apply the managed Hermes model and voice configuration')
        install = deepcopy(install)
        install['no_log'] = False  # This fixture contains no credentials.
        install['register'] = 'configuration_write'
        # Use the actual local user's group, which need not match their username.
        install['ansible.builtin.copy']['group'] = grp.getgrgid(os.getgid()).gr_name
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.yaml'
            config.write_text('web: {search_backend: searxng}\npersonal: {keep: true}\n')
            config.chmod(0o600)
            reads = [
                {'ansible.builtin.stat': {'path': str(config)}, 'register': 'hermes_config_file'},
                {'ansible.builtin.slurp': {'src': str(config)}, 'register': 'hermes_config_contents'},
                {'ansible.builtin.set_fact': {'hermes_existing_config': '{{ hermes_config_contents.content | b64decode | from_yaml }}'}},
            ]
            # Match the production group comparison using a fixture-only group name.
            install['when'][-1] = install['when'][-1].replace(
                'hermes_config_file.stat.gr_name != hermes_user',
                'hermes_config_file.stat.gr_name != fixture_group')
            tasks = reads + [install, {'ansible.builtin.assert': {'that': ['configuration_write.changed']}}]
            tasks += reads + [
                {'ansible.builtin.copy': {'dest': str(config), 'mode': '0600',
                    'content': "# native formatting\n{{ hermes_existing_config | to_json }}\n"}},
            ] + reads + [install, {'ansible.builtin.assert': {'that': [
                'not configuration_write.changed', "hermes_existing_config.web.search_backend == 'searxng'",
                'hermes_existing_config.personal.keep',
            ]}}]
            tasks += [{'ansible.builtin.file': {'path': str(config), 'mode': '0644'}}] + reads + [install]
            tasks += [{'ansible.builtin.assert': {'that': ['configuration_write.changed']}}]
            variables = {'hermes_home': directory, 'hermes_user': pwd.getpwuid(os.getuid()).pw_name,
                         'fixture_group': grp.getgrgid(os.getgid()).gr_name,
                         'hermes_managed_config': {'model': {'provider': 'fixture', 'default': 'fixture'},
                                                   'web': {'search_backend': 'auto'}},
                         'hermes_external_skill_dirs': [], 'vps_hermes': {'config': {}},
                         'vps_deploy': {'features': {'workspace_ui': False}},
                         'vps_runtime': {'capabilities': {'searxng_search': {'set': {'web.search_backend': 'searxng'}}}},
                         'hermes_runtime_capabilities': ['searxng_search']}
            result = run_play(root, tasks, variables, [
                {'name': 'restart Hermes gateway', 'ansible.builtin.debug': {'msg': 'restart requested'}}])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_matching_serve_state_never_resets_or_republishes(self):
        services = [{'name': 'dashboard', 'protocol': 'https', 'port': 443, 'target': 'http://127.0.0.1:9119'}]
        all_services = services + [{'protocol': 'https', 'port': 3002, 'target': 'http://127.0.0.1:3002'}]
        current = {'TCP': {str(s['port']): {'HTTPS': True} for s in all_services},
                   'Web': {f"host.example.ts.net:{s['port']}": {'Handlers': {'/': {'Proxy': s['target']}}}
                           for s in all_services}}
        tasks = yaml.safe_load((ANSIBLE / 'tasks/tailscale-serve.yml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / 'tailscale'
            command.write_text('#!' + sys.executable + '\n' +
                'import json, sys\n' +
                'replies = ' + repr({'ip -4': '100.64.0.1', 'serve status --json': json.dumps(current),
                                    'status --json': json.dumps({'Self': {'DNSName': 'host.example.ts.net.'}})}) + '\n' +
                "key = ' '.join(sys.argv[1:])\n" +
                "if key not in replies: sys.exit('Unexpected mutation: ' + key)\n" +
                'print(replies[key])\n')
            command.chmod(0o700)
            for task in tasks:
                if 'ansible.builtin.command' in task:
                    task['environment'] = {'PATH': str(root) + os.pathsep + os.environ['PATH']}
            result = run_play(root, tasks, {'hermes_enable_tailscale': True,
                'vps_tailscale': {'serve': {'enabled': True, 'services': services}},
                'vps_deploy': {'features': {'workspace_ui': True}}, 'vps_workspace_ui': {'port': 3002}})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('changed=0', result.stdout)

