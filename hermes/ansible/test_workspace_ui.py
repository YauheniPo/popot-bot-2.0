"""Deployment contracts for Workspace sharing the existing Hermes backend."""

from pathlib import Path
import hashlib
import json
import unittest

import yaml

try:
    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment
except ImportError:
    FilterModule = None


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / 'ansible'


class WorkspaceDeploymentTests(unittest.TestCase):
    def test_mcp_ui_build_wrapper_is_fingerprinted_and_preserves_upstream(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/workspace-ui.yml').read_text())
        identity = next(task for task in tasks if task['name'] == 'Define the managed Workspace UI build identity')
        expression = identity['ansible.builtin.set_fact']['hermes_workspace_ui_build_hash']
        self.assertIn('workspace-mcp-ui.mjs', expression)
        self.assertIn('workspace-model-policy.mjs', expression)
        self.assertIn('workspace-profile-ui.mjs', expression)
        self.assertIn('workspace-profile-prompts.mjs', expression)
        self.assertIn('workspace-voice-ui.mjs', expression)
        self.assertIn('workspace-background-runs.mjs', expression)
        wrapper = (ANSIBLE / 'templates/workspace-vite.config.mjs.j2').read_text()
        self.assertIn('workspaceBackgroundRuns()', wrapper)
        self.assertIn('workspace-vite.config.mjs.j2', expression)
        marker = next(task for task in tasks if task['name'] == 'Inspect successful Workspace build marker')
        self.assertIn('hermes_workspace_ui_build_hash', marker['ansible.builtin.stat']['path'])
        build = next(task for task in tasks if task['name'] == 'Build the Workspace companion without starting another gateway')
        command = next(task for task in build['block'] if task['name'] == 'Build Workspace production assets')
        self.assertEqual(command['ansible.builtin.command']['argv'][1:],
                         ['exec', 'vite', 'build', '--config', '.hermes-vite.config.mjs'])

    def test_existing_mcp_repair_is_opt_in_and_precedes_normal_config_write(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/runtime.yml').read_text())
        index, repair = next((i, task) for i, task in enumerate(tasks)
                             if task['name'].startswith('Repair known Workspace MCP presets'))
        self.assertTrue(repair['no_log'])
        self.assertIn('hermes_repair_workspace_mcp_presets | default(false) | bool', repair['when'])
        self.assertIn('vps_deploy.features.workspace_ui | default(false) | bool', repair['when'])
        config_index = next(i for i, task in enumerate(tasks)
                            if task['name'] == 'Apply the managed Hermes model and voice configuration')
        self.assertLess(index, config_index)

    def test_bridge_update_triggers_workspace_restart_without_rebuilding_upstream(self):
        unit = (ANSIBLE / 'templates/hermes-workspace.service.j2').read_text()
        self.assertIn("'../runtime/workspace-dashboard-bridge.mjs') | hash('sha256')", unit)
        self.assertIn("'../runtime/workspace-mcp-adapter.mjs') | hash('sha256')", unit)
        tasks = yaml.safe_load((ANSIBLE / 'tasks/workspace-ui.yml').read_text())
        install = next(task for task in tasks if task['name'] == 'Install Workspace companion service')
        self.assertEqual(install['notify'], 'restart Hermes Workspace')
        self.assertNotIn('when', install)

    def test_workspace_terminal_dependencies_are_installed_before_service_start(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/workspace-ui.yml').read_text())
        installs = [(i, task) for i, task in enumerate(tasks)
                    if {'zsh', 'tmux'}.issubset(
                        task.get('ansible.builtin.apt', {}).get('name', []))]
        self.assertEqual(len(installs), 1)
        index, task = installs[0]
        self.assertEqual(task['ansible.builtin.apt']['state'], 'present')
        self.assertNotIn('become_user', task)
        self.assertNotIn('when', task)  # Also runs with an existing build marker.
        start = next(i for i, task in enumerate(tasks)
                     if 'ansible.builtin.systemd_service' in task)
        self.assertLess(index, start)
        playbook = yaml.safe_load((ANSIBLE / 'playbook.yml').read_text())
        # The existing include gates ALL these tasks when Workspace is disabled.
        def find_imports(tasks):
            for task in tasks:
                if task.get('ansible.builtin.import_tasks') == 'tasks/workspace-ui.yml':
                    yield task
                for section in ('block', 'rescue', 'always'):
                    yield from find_imports(task.get(section, []))
        imports = list(find_imports(playbook[0]['tasks']))
        self.assertEqual(len(imports), 1)
        self.assertIn('vps_deploy.features.workspace_ui', imports[0]['when'])

    def test_workspace_terminal_probes_use_service_user_and_path_before_handlers(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/workspace-ui.yml').read_text())
        probe_index, probe = next((i, task) for i, task in enumerate(tasks)
                                 if task['name'] == 'Verify Workspace terminal dependencies')
        self.assertEqual(probe['become_user'], '{{ hermes_user }}')
        self.assertEqual(probe['ansible.builtin.command']['argv'], '{{ item }}')
        self.assertIn(['zsh', '--version'], probe['loop'])
        self.assertIn(['tmux', '-V'], probe['loop'])
        self.assertIs(probe['changed_when'], False)
        self.assertNotIn('failed_when', probe)
        self.assertNotIn('ignore_errors', probe)
        self.assertGreater(probe['timeout'], 0)
        unit = (ANSIBLE / 'templates/hermes-workspace.service.j2').read_text()
        self.assertIn(f'Environment="PATH={probe["environment"]["PATH"]}"', unit)
        self.assertEqual(probe['environment']['HOME'], '{{ hermes_user_home }}')
        install_index = next(i for i, task in enumerate(tasks)
                             if 'zsh' in task.get('ansible.builtin.apt', {}).get('name', []))
        flush_index = next(i for i, task in enumerate(tasks)
                           if task.get('ansible.builtin.meta') == 'flush_handlers')
        self.assertLess(install_index, probe_index)
        self.assertLess(probe_index, flush_index)

    @unittest.skipIf(FilterModule is None, 'Requires the Ansible controller Python')
    def test_four_character_vault_inputs_are_validated_and_api_token_is_derived(self):
        env = Environment()
        env.filters.update(FilterModule().filters())
        pre_tasks = yaml.safe_load((ANSIBLE / 'playbook.yml').read_text())[0]['pre_tasks']
        validation = next(task for task in pre_tasks if task['name'] ==
                          'Check shared Workspace credentials without exposing values')
        expression = env.compile_expression(validation['ansible.builtin.set_fact'][
            'hermes_workspace_credentials_valid'].strip()[2:-2])
        template = env.from_string((ANSIBLE / 'templates/hermes.env.j2').read_text())
        for value in ('1234', 'a!$#', 'я🔑字!', '    ', ' a! ', '"\\$!'):
            secrets = {'API_SERVER_KEY': value, 'HERMES_WORKSPACE_PASSWORD': value}
            with self.subTest(value=value):
                self.assertTrue(expression(hermes_secret_env=secrets))
                rendered = template.render(
                    hermes_secret_env=secrets,
                    vps_deploy={'features': {'workspace_ui': True}},
                    vps_workspace_ui={'api_port': 8642},
                    vps_browser={'launch_args': ''}, hermes_home='/tmp/test-hermes',
                )
                entries = dict(line.split('=', 1) for line in rendered.splitlines()
                               if line and not line.startswith('#'))
                self.assertEqual(json.loads(entries['API_SERVER_KEY']),
                                 hashlib.sha256(value.encode('utf-8')).hexdigest())
                self.assertEqual(json.loads(entries['HERMES_WORKSPACE_PASSWORD']), value)
                self.assertNotIn('\\u', entries['HERMES_WORKSPACE_PASSWORD'])
        # Deployments without the companion retain their existing API token.
        rendered = template.render(
            hermes_secret_env={'API_SERVER_KEY': 'existing-api-token'},
            vps_deploy={'features': {'workspace_ui': False}},
            vps_browser={'launch_args': ''}, hermes_home='/tmp/test-hermes',
        )
        self.assertIn('API_SERVER_KEY="existing-api-token"', rendered)
        for key in ('API_SERVER_KEY', 'HERMES_WORKSPACE_PASSWORD'):
            for invalid in ('', 'abc', 1234, None):
                values = {'API_SERVER_KEY': 'abcd', 'HERMES_WORKSPACE_PASSWORD': 'abcd'}
                values[key] = invalid
                self.assertFalse(expression(hermes_secret_env=values))

    @unittest.skipIf(FilterModule is None, 'Requires the Ansible controller Python')
    def test_voice_deploy_removes_english_hint_without_changing_provider(self):
        settings = yaml.safe_load((ROOT / 'config/vps-defaults.yml').read_text())
        tasks = yaml.safe_load((ANSIBLE / 'tasks/runtime.yml').read_text())
        task = next(task for task in tasks if task['name'] ==
                    'Apply the managed Hermes model and voice configuration')
        env = Environment()
        env.filters.update(FilterModule().filters())
        template = env.from_string(task['ansible.builtin.copy']['content'])
        for workspace_enabled in (True, False):
            for provider in ('nous', 'local', 'openai', 'groq'):
                with self.subTest(workspace=workspace_enabled, provider=provider):
                    existing = {'stt': {'enabled': True, 'language': 'en',
                        'provider': provider, 'prompt': 'Personal vocabulary',
                        provider: {'model': 'operator-stt-model'}}}
                    values = dict(hermes_existing_config=existing,
                        hermes_managed_config=settings['vps_hermes']['config']['managed_overlay'],
                        hermes_external_skill_dirs=[], vps_hermes=settings['vps_hermes'],
                        vps_deploy={'features': {'workspace_ui': workspace_enabled}})
                    result = yaml.safe_load(template.render(**values))
                    self.assertEqual(result['stt']['language'], '')
                    self.assertEqual(result['stt']['provider'], provider)
                    self.assertEqual(result['stt'][provider]['model'], 'operator-stt-model')
                    self.assertEqual(result['stt']['prompt'], 'Personal vocabulary')
                    self.assertTrue(result['stt']['enabled'])
                    values['hermes_existing_config'] = result
                    self.assertEqual(yaml.safe_load(template.render(**values)), result)

    @unittest.skipIf(FilterModule is None, 'Requires the Ansible controller Python')
    def test_actual_config_template_preserves_ui_choices_and_enforces_policy(self):
        tasks = yaml.safe_load((ANSIBLE / 'tasks/runtime.yml').read_text())
        task = next(task for task in tasks if task['name'] ==
                    'Apply the managed Hermes model and voice configuration')
        env = Environment()
        env.filters.update(FilterModule().filters())
        template = env.from_string(task['ansible.builtin.copy']['content'])
        values = {
            'hermes_existing_config': {
                'model': {'default': 'operator-choice'},
                'approvals': {'mode': 'off'}, 'workspace_extension': {'keep': True},
            },
            'hermes_managed_config': {
                'model': {'default': 'repo-seed', 'provider': 'fixture-cloud', 'max_tokens': 999},
                'approvals': {'mode': 'manual'},
            },
            'hermes_external_skill_dirs': ['/opt/test-skills'],
            'vps_hermes': {'config': {'ui_owned_sections': ['model']}},
            'vps_deploy': {'features': {'workspace_ui': True}},
        }
        result = yaml.safe_load(template.render(**values))
        self.assertEqual(result['model'], {'default': 'repo-seed', 'provider': 'fixture-cloud', 'max_tokens': 999})
        self.assertEqual(result['delegation']['model'], 'repo-seed')
        self.assertEqual(result['cron']['model_provider'], 'fixture-cloud')
        self.assertEqual(result['approvals']['mode'], 'manual')
        self.assertTrue(result['platforms']['api_server']['enabled'])
        self.assertEqual(result['workspace_extension'], {'keep': True})
        values['vps_deploy']['features']['workspace_ui'] = False
        result = yaml.safe_load(template.render(**values))
        self.assertEqual(result['model']['default'], 'repo-seed')
        self.assertNotIn('platforms', result)

    def test_service_uses_existing_home_and_only_starts_workspace(self):
        unit = (ANSIBLE / 'templates/hermes-workspace.service.j2').read_text()
        self.assertIn('User={{ hermes_user }}', unit)
        self.assertIn('HERMES_HOME={{ hermes_home }}', unit)
        self.assertIn('workspace-launch.py', unit)
        self.assertNotIn('gateway run', unit)
        self.assertNotIn('start:all', unit)
        self.assertIn('UMask=0077', unit)

    def test_workspace_is_private_and_source_is_pinned(self):
        settings = yaml.safe_load((ROOT / 'config/vps-defaults.yml').read_text())
        config = settings['vps_workspace_ui']
        self.assertRegex(config['revision'], r'^[0-9a-f]{40}$')
        ports = [settings['vps_observability'][name]['port'] for name in
                 ('dashboard', 'grafana', 'prometheus', 'node_exporter')]
        ports.append(settings['vps_vscode']['host_port'])
        self.assertNotIn(config['port'], ports)
        self.assertEqual(config['bind_address'], '127.0.0.1')

    def test_installation_does_not_install_another_hermes(self):
        tasks = (ANSIBLE / 'tasks/workspace-ui.yml').read_text()
        self.assertIn('--frozen-lockfile', tasks)
        self.assertIn('vps_workspace_ui.revision', tasks)
        self.assertNotIn('start:all', tasks)
        self.assertNotIn('curl', tasks)
        self.assertIn('force: false', tasks)
        self.assertIn('hermes-workspace.service', tasks)

    def test_environment_owns_api_listener_but_not_secret_values(self):
        template = (ANSIBLE / 'templates/hermes.env.j2').read_text()
        self.assertIn('API_SERVER_HOST="127.0.0.1"', template)
        self.assertIn('API_SERVER_ENABLED="true"', template)
        self.assertNotIn('API_SERVER_KEY="', template)


if __name__ == '__main__':
    unittest.main()
