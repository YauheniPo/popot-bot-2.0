"""Managed chat fallback commands and provider-scoped quota failover."""

import ast
import asyncio
import copy
import errno
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch('sys.path', [str(ROOT / 'ops'), *sys.path]):
        spec.loader.exec_module(module)
    return module


class FallbackPolicyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict('sys.modules', {
            'hermes_cli.providers': SimpleNamespace(ALIASES={'nim': 'nvidia'})}))
        self.policy = load_module('managed_fallback', ROOT / 'runtime/fallback-policy.py')
        self.config = {
            'model': {'provider': 'nvidia', 'default': 'primary'},
            'fallback_policy': {'allowed_providers': ['nvidia', 'openrouter', 'nous', 'ollama-cloud'],
                                'default_routes': [{'provider': 'nous', 'model': 'default'}]},
            'fallback_providers': [{'provider': 'openrouter', 'model': 'test/model:free'}],
            'fallback_model': {'provider': 'nous', 'model': 'legacy'},
            'unrelated': {'keep': True},
        }

    def test_excluded_pairs_filter_current_and_legacy_routes_without_mutation(self):
        self.config['fallback_policy']['excluded_routes'] = [
            {'provider': 'openrouter', 'model': 'blocked:free'},
            {'provider': 'nim', 'model': 'blocked'}]
        chain = [
            {'provider': 'openrouter', 'model': 'blocked:free', 'api_key': 'private'},
            {'provider': 'nous', 'model': 'blocked:free'},
            {'provider': 'openrouter', 'model': 'other:free'},
            {'provider': 'nvidia', 'model': 'blocked'}]
        before = copy.deepcopy(chain)
        result = self.policy.filter_fallback_routes(self.config, chain)
        self.assertEqual(result, chain[1:3])
        self.assertEqual(chain, before)
        self.config['fallback_providers'] = chain[:3]
        self.config['fallback_model'] = chain[3]
        patcher = load_module('fallback_exclusion_patches', ROOT / 'runtime/apply-hermes-patches.py')
        patch = next(p for p in patcher._PATCHES
                     if p[1] == '# Local Hermes: managed fallback policy')
        native = '''def get_fallback_chain(config):
    chain = list(config.get('fallback_providers', []))
    if config.get('fallback_model'):
        chain.append(config['fallback_model'])
    return chain
'''
        namespace = {}
        exec(native.replace(patch[2], patch[3]), namespace)
        self.assertEqual(namespace['get_fallback_chain'](self.config), chain[1:3])
        self.config['fallback_policy']['excluded_routes'] = []
        self.assertEqual(namespace['get_fallback_chain'](self.config), chain)

    def test_invalid_exclusions_fail_closed_without_exposing_values(self):
        for excluded in (None, 'secret', [{}], ['secret'],
                         [{'provider': 'nous', 'model': ''}],
                         [{'provider': None, 'model': 'a'}],
                         [{'provider': 'Bad Provider', 'model': 'a'}],
                         [{'provider': 'nous', 'model': 42}],
                         [{'provider': 'nous', 'model': 'https://secret'}],
                         [{'provider': 'nous', 'model': 'a' * 201}],
                         [{'provider': 'nous', 'model': 'a', 'api_key': 'secret'}],
                         [{'provider': 'nous', 'model': 'a'}] * 2):
            with self.subTest(excluded=excluded):
                self.config['fallback_policy']['excluded_routes'] = excluded
                with self.assertRaises(ValueError) as error:
                    self.policy.filter_fallback_routes(self.config, self.config['fallback_providers'])
                self.assertNotIn('secret', str(error.exception))

    def test_paid_openrouter_is_filtered_before_pre_agent_provider_resolution(self):
        chain = [{'provider': 'openrouter', 'model': 'paid-model'},
                 {'provider': 'openrouter', 'model': 'backup:free'},
                 {'provider': 'nous', 'model': 'backup'}]
        self.assertEqual(self.policy.filter_fallback_routes(self.config, chain), chain[1:])

    def test_openrouter_validation_error_shows_the_required_suffix(self):
        with self.assertRaises(self.policy.FallbackCommandError) as error:
            self.policy.validate_fallback_routes(
                [{'provider': 'openrouter', 'model': 'paid-model'}],
                self.config['fallback_policy']['allowed_providers'],
            )
        self.assertIn(':free', str(error.exception))
        self.assertNotIn('FREE_SUFFIX', str(error.exception))

    def test_chat_edits_preserve_exclusions_and_list_explains_skipped_routes(self):
        excluded = [{'provider': 'openrouter', 'model': 'blocked:free'}]
        self.config['fallback_policy']['excluded_routes'] = excluded
        updated, _ = self.policy.edit_fallback_config(self.config, 'add openrouter blocked:free')
        self.assertEqual(updated['fallback_policy']['excluded_routes'], excluded)
        self.assertEqual(self.policy.filter_fallback_routes(updated, updated['fallback_providers']),
                         self.config['fallback_providers'])
        _, reply = self.policy.edit_fallback_config(updated, 'list')
        self.assertIn('Исключены из переключения:', reply)
        self.assertIn('openrouter blocked:free', reply)
        self.config['fallback_policy'] = 'secret'
        with self.assertRaisesRegex(ValueError, 'fallback_policy must be a mapping'):
            self.policy.filter_fallback_routes(self.config, [])

    def test_set_add_remove_off_reset_preserve_other_config(self):
        updated, reply = self.policy.edit_fallback_config(self.config, 'set nous model-a; nvidia model-b')
        self.assertEqual(updated['fallback_providers'], [
            {'provider': 'nous', 'model': 'model-a'}, {'provider': 'nvidia', 'model': 'model-b'}])
        self.assertNotIn('fallback_model', updated)
        self.assertEqual(updated['model'], self.config['model'])
        self.assertEqual(updated['unrelated'], self.config['unrelated'])
        self.assertIn('следующего', reply)
        updated, _ = self.policy.edit_fallback_config(updated, 'add ollama-cloud model-c')
        updated, _ = self.policy.edit_fallback_config(updated, 'remove 2')
        self.assertEqual([r['provider'] for r in updated['fallback_providers']], ['nous', 'ollama-cloud'])
        updated, _ = self.policy.edit_fallback_config(updated, 'off')
        self.assertEqual(updated['fallback_providers'], [])
        updated, _ = self.policy.edit_fallback_config(updated, 'reset')
        self.assertEqual(updated['fallback_providers'], self.config['fallback_policy']['default_routes'])
        self.assertIn('fallback_model', self.config)  # no in-place mutation

    def test_invalid_routes_never_mutate_config(self):
        before = copy.deepcopy(self.config)
        for args in ('set', 'set openrouter paid-model', 'add unknown model',
                     'set nous a; nous a', 'remove 0', 'remove 99',
                     'set nous a api_key=secret', 'set nous https://bad',
                     'off extra', 'reset extra', 'set nous a\n/run danger',
                     'set nous ' + 'x' * 201,
                     'set ' + '; '.join(f'nous model-{n}' for n in range(9))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.policy.edit_fallback_config(self.config, args)
        self.assertEqual(before, self.config)

    def test_same_provider_with_different_models_is_allowed_but_duplicate_pair_is_not(self):
        routes = [{'provider': 'nous', 'model': 'a'}, {'provider': 'nous', 'model': 'b'}]
        self.config['fallback_policy']['default_routes'] = routes
        updated, _ = self.policy.edit_fallback_config(self.config, 'reset')
        self.assertEqual(updated['fallback_providers'], routes)
        with self.assertRaises(ValueError):
            self.policy.edit_fallback_config(updated, 'add nous a')

    def test_reset_rejects_malformed_managed_routes_without_mutating_config(self):
        for route in (None, 'nous model', {}, {'provider': 'nous'},
                      {'provider': 'nous', 'model': 'a', 'api_key': 'secret'}):
            with self.subTest(route=route):
                self.config['fallback_policy']['default_routes'] = [route]
                before = copy.deepcopy(self.config)
                with self.assertRaisesRegex(self.policy.FallbackCommandError, 'Маршрут: provider model'):
                    self.policy.edit_fallback_config(self.config, 'reset')
                self.assertEqual(self.config, before)

    def test_invalid_provider_policy_fails_closed(self):
        for allowed in ('nous', None, ['nous', 1]):
            with self.subTest(allowed=allowed):
                self.config['fallback_policy']['allowed_providers'] = allowed
                before = copy.deepcopy(self.config)
                with self.assertRaisesRegex(ValueError, 'Invalid managed provider policy'):
                    self.policy.edit_fallback_config(self.config, 'off')
                self.assertEqual(self.config, before)

    def test_invalid_chat_command_returns_authored_feedback_without_writing(self):
        writer = mock.Mock()
        config_module = SimpleNamespace(_CONFIG_LOCK=threading.RLock(),
            read_user_config_raw=mock.Mock(return_value=self.config), atomic_config_write=writer)
        with mock.patch.dict('sys.modules', {'hermes_cli.config': config_module}):
            reply = self.policy.run_fallback_command(Path('/profile/config.yaml'), 'remove 99')
        self.assertIn('Используйте /fallback', reply)
        writer.assert_not_called()

    def test_list_does_not_write_or_expose_route_secrets(self):
        self.config['fallback_providers'][0]['api_key'] = 'never-show-this'
        updated, reply = self.policy.edit_fallback_config(self.config, '')
        self.assertIsNone(updated)
        self.assertIn('openrouter', reply)
        self.assertIn('legacy fallback_model', reply)
        self.assertNotIn('never-show-this', reply)

    def test_account_quota_skips_entire_failed_provider_and_keeps_native_order(self):
        agent = SimpleNamespace(provider='nvidia', _fallback_index=0, _fallback_activated=False)
        self.policy.begin_fallback_walk(agent, 'billing')
        for candidate, allowed in (({'provider': 'nvidia', 'model': 'other'}, False),
                                   ({'provider': 'nous', 'model': 'next'}, True),
                                   ({'provider': 'openrouter', 'model': 'paid'}, False),
                                   ({'provider': 'openrouter', 'model': 'test:free'}, True)):
            self.assertEqual(self.policy.allow_fallback_candidate(agent, candidate), allowed)
        agent.provider, agent._fallback_index, agent._fallback_activated = 'nous', 2, True
        self.policy.begin_fallback_walk(agent, 'billing')
        self.assertFalse(self.policy.allow_fallback_candidate(agent, {'provider': 'nvidia', 'model': 'again'}))
        self.assertFalse(self.policy.allow_fallback_candidate(agent, {'provider': 'nous', 'model': 'again'}))
        self.assertTrue(self.policy.allow_fallback_candidate(agent, {'provider': 'ollama-cloud', 'model': 'last'}))
        # A new primary recovery cycle is eligible again; no permanent blacklist.
        agent.provider, agent._fallback_index, agent._fallback_activated = 'nvidia', 0, False
        self.policy.begin_fallback_walk(agent, 'timeout')
        self.assertTrue(self.policy.allow_fallback_candidate(agent, {'provider': 'nvidia', 'model': 'other'}))

    def test_rate_limit_can_try_another_free_model_on_same_provider(self):
        agent = SimpleNamespace(provider='nvidia', model='ultra',
                                _fallback_index=0, _fallback_activated=False)
        self.policy.begin_fallback_walk(agent, 'rate_limit')
        self.assertFalse(self.policy.allow_fallback_candidate(
            agent, {'provider': 'nvidia', 'model': 'ultra'}))
        self.assertTrue(self.policy.allow_fallback_candidate(
            agent, {'provider': 'nvidia', 'model': 'super'}))
        agent.provider, agent.model = 'openrouter', 'gemma:free'
        agent._fallback_index, agent._fallback_activated = 1, True
        self.policy.begin_fallback_walk(agent, 'rate_limit')
        self.assertFalse(self.policy.allow_fallback_candidate(
            agent, {'provider': 'openrouter', 'model': 'gemma:free'}))
        self.assertTrue(self.policy.allow_fallback_candidate(
            agent, {'provider': 'openrouter', 'model': 'north:free'}))
        self.assertFalse(self.policy.allow_fallback_candidate(
            agent, {'provider': 'openrouter', 'model': 'north'}))

    def test_eager_fallback_patch_skips_pool_wait_after_fallback_429(self):
        patcher = load_module('fallback_patches', ROOT / 'runtime/apply-hermes-patches.py')
        patches = [item for item in patcher._PATCHES
                   if item[0] == 'agent/turn_recovery.py'
                   and item[1] == '# Local Hermes: eager fallback after fallback 429']
        self.assertEqual(len(patches), 1)
        path, marker, old, new = patches[0]
        self.assertIn('pool_may_recover', old)
        namespace = {}
        exec('def should_wait(agent, is_rate_limited, _is_upstream, _ra):\n'
             + textwrap.indent(textwrap.dedent(new), '    ')
             + '    return pool_may_recover\n', namespace)
        pool = SimpleNamespace(_pool_may_recover_from_rate_limit=lambda _: True)
        route = SimpleNamespace(_credential_pool=object(), _provider_fallback_active=True)
        self.assertFalse(namespace['should_wait'](route, True, False, lambda: pool))
        self.assertTrue(namespace['should_wait'](route, False, False, lambda: pool))
        route._provider_fallback_active = False
        self.assertTrue(namespace['should_wait'](route, True, False, lambda: pool))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'agent/turn_recovery.py'
            target.parent.mkdir()
            target.write_text(old)
            state = {}
            with mock.patch.object(patcher, 'HERMES_AGENT_DIR', root):
                first = patcher._apply_one_patch(path, marker, old, new, state)
                second = patcher._apply_one_patch(path, marker, old, new, state)
            self.assertEqual((first[0], second[0]), (1, 0))
            self.assertEqual(target.read_text(), new)

    def test_enum_reasons_and_provider_aliases(self):
        from enum import Enum
        class Reason(Enum):
            quota = 'upstream_rate_limit'
        agent = SimpleNamespace(provider='nim', _fallback_index=0, _fallback_activated=False)
        with mock.patch.dict('sys.modules', {'hermes_cli.providers': SimpleNamespace(ALIASES={'nim': 'nvidia'})}):
            self.policy.begin_fallback_walk(agent, Reason.quota)
            self.assertFalse(self.policy.allow_fallback_candidate(agent, {'provider': 'nvidia', 'model': 'other'}))

    def test_scoped_atomic_write_failure_and_read_only_command(self):
        reader = mock.Mock(return_value=self.config)
        writer = mock.Mock()
        path = Path('/profile/config.yaml')
        config_module = SimpleNamespace(_CONFIG_LOCK=threading.RLock(),
            read_user_config_raw=reader, atomic_config_write=writer)
        with mock.patch.dict('sys.modules', {'hermes_cli.config': config_module}):
            self.policy.run_fallback_command(path, 'list')
            writer.assert_not_called()
            self.policy.run_fallback_command(path, 'off')
            reader.assert_called_with(path)
            writer.assert_called_once()
            self.assertEqual(writer.call_args.args[0], path)
            self.assertEqual(writer.call_args.args[1]['fallback_providers'], [])
            self.assertNotIn('fallback_model', writer.call_args.args[1])
            writer.side_effect = OSError('do-not-expose-this')
            reply = self.policy.run_fallback_command(path, 'reset')
            self.assertIn('Не удалось', reply)
            self.assertNotIn('do-not-expose-this', reply)

    def test_config_errors_are_redacted_and_never_report_success(self):
        reader = mock.Mock(return_value=self.config)
        writer = mock.Mock()
        config_module = SimpleNamespace(_CONFIG_LOCK=threading.RLock(),
            read_user_config_raw=reader, atomic_config_write=writer)
        with mock.patch.dict('sys.modules', {'hermes_cli.config': config_module}):
            for failing in (reader, writer):
                for error in (yaml.YAMLError('secret'), ValueError('secret'), RuntimeError('secret')):
                    with self.subTest(operation=failing, error=type(error)):
                        reader.side_effect = writer.side_effect = None
                        writer.reset_mock()
                        failing.side_effect = error
                        reply = self.policy.run_fallback_command(Path('/profile/config.yaml'), 'off')
                        self.assertIn('Не удалось', reply)
                        self.assertNotIn('secret', reply)
                        if failing is reader:
                            writer.assert_not_called()

    def test_deploy_reapplies_managed_fallback_routes_with_or_without_workspace(self):
        from ansible.plugins.filter.core import FilterModule
        from jinja2 import Environment
        tasks = yaml.safe_load((ROOT / 'ansible/tasks/runtime.yml').read_text())
        task = next(t for t in tasks if t['name'] == 'Apply the managed Hermes model and voice configuration')
        env = Environment()
        env.filters.update(FilterModule().filters())
        template = env.from_string(task['ansible.builtin.copy']['content'])
        settings = yaml.safe_load((ROOT / 'config/vps-defaults.yml').read_text())
        managed = settings['vps_hermes']['config']['managed_overlay']
        defaults = [{'provider': 'nous', 'model': 'a'}, {'provider': 'nous', 'model': 'b'}]
        managed.pop('fallback_providers', None)
        managed['fallback_policy']['default_routes'] = defaults
        for workspace in (False, True):
            for selected in ([], [{'provider': 'nous', 'model': 'chosen'}]):
                values = dict(hermes_existing_config={'fallback_providers': selected},
                    hermes_managed_config=managed, hermes_external_skill_dirs=[],
                    vps_hermes=settings['vps_hermes'], vps_deploy={'features': {'workspace_ui': workspace}})
                result = yaml.safe_load(template.render(**values))
                self.assertEqual(result['fallback_providers'], defaults)
                self.assertEqual(result['fallback_policy']['default_routes'], defaults)
                values['hermes_existing_config'] = result
                self.assertEqual(yaml.safe_load(template.render(**values)), result)
        values['hermes_existing_config'] = {}
        result = yaml.safe_load(template.render(**values))
        self.assertEqual(result['fallback_providers'], defaults)

    def test_deploy_rejects_duplicate_pairs_before_writing_config(self):
        from ansible.plugins.filter.core import FilterModule
        from ansible.plugins.filter.mathstuff import FilterModule as MathFilters
        from jinja2 import Environment
        tasks = yaml.safe_load((ROOT / 'ansible/tasks/runtime.yml').read_text())
        name = 'Validate managed fallback provider and model pairs'
        validation = next(t for t in tasks if t['name'] == name)
        copy_index = next(i for i, t in enumerate(tasks)
                          if t['name'] == 'Apply the managed Hermes model and voice configuration')
        self.assertLess(tasks.index(validation), copy_index)
        env = Environment()
        env.filters.update(FilterModule().filters())
        env.filters.update(MathFilters().filters())
        a = {'provider': 'nous', 'model': 'a'}
        for routes, valid in (([a, {'provider': 'nous', 'model': 'b'}], True),
                              ([a, dict(a)], False), ([], True), ([{}], False)):
            checks = validation['ansible.builtin.assert']['that']
            self.assertEqual(all(env.compile_expression(check)(fallback_routes=routes)
                                 for check in checks), valid)


@unittest.skipUnless(os.environ.get('HERMES_UPSTREAM_DIR'), 'Pinned upstream source required')
class FallbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.patches = load_module('fallback_patches', ROOT / 'runtime/apply-hermes-patches.py')
        self.upstream = Path(os.environ['HERMES_UPSTREAM_DIR'])

    def patched(self, path):
        import subprocess
        source = subprocess.check_output(['git', '-C', str(self.upstream), 'show', f'HEAD:{path}'], text=True)
        for target, marker, old, new in self.patches._PATCHES:
            if target == path:
                self.assertIn(old, source, marker)
                source = source.replace(old, new, 1)
        ast.parse(source)
        return source

    def cron_runtime(self):
        # Execute the pinned scheduler's real setup/constructor and fallback
        # reader. Only credentials, clients, MCP and unrelated prompt I/O are stubbed.
        fallback = {}
        exec(self.patched('hermes_cli/fallback_config.py'), fallback)
        config = {'fallback_policy': {'excluded_routes': [{'provider': 'nvidia', 'model': 'blocked'}]},
                  'fallback_providers': [{'provider': 'nvidia', 'model': 'blocked'},
                                         {'provider': 'openrouter', 'model': 'paid'},
                                         {'provider': 'nvidia', 'model': 'backup-a'},
                                         {'provider': 'nous', 'model': 'backup-b'}]}
        auth_error = type('AuthError', (Exception,), {})
        resolver = mock.Mock()
        ns = {'logger': mock.Mock(), 'contextlib': __import__('contextlib'), 'errno': errno,
              '_CronAgentSetup': lambda **kw: SimpleNamespace(blocked=None, fallback_notice=None, **kw),
              'get_fallback_chain': fallback['get_fallback_chain'],
              '_guard_job_credential_exfil': mock.Mock(), '_cron_preflight_enabled': lambda cfg: True,
              '_load_prefill_messages': lambda *a: None, '_load_credential_pool': lambda *a: None,
              '_resolve_job_reasoning_config': lambda *a: None, '_init_cron_mcp_tools': mock.Mock(),
              '_empty_requested_mcp_toolsets': lambda *a: None,
              '_resolve_cron_enabled_toolsets': lambda *a: [], '_resolve_cron_disabled_toolsets': lambda *a: [],
              '_blocked_config_result': lambda *a: (False, '', '', 'blocked'),
              '_sched': SimpleNamespace(get_fallback_chain=fallback['get_fallback_chain']),
              'cron_env_setting': lambda key: '', '_credential_store_scope_label': lambda: 'fixture'}
        names = {'_preflight_check_provider_key', '_preflight_or_block', '_resolve_job_runtime',
                 '_is_transient_provider_resolve_error',
                 '_resolve_cron_agent_setup', '_construct_cron_agent', '_fallback_entries', '_init_fallback_chain'}
        constants = {'_TRANSIENT_NET_EXC_NAMES', '_DNS_FAILURE_NEEDLES', '_TRANSIENT_OSERROR_NEEDLES',
                     '_TRANSIENT_HTTP_NEEDLES', '_TRANSIENT_ERRNOS'}
        for path in ('cron/scheduler_preflight.py', 'cron/scheduler.py', 'agent/agent_init.py'):
            tree = ast.parse(self.patched(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    exec('from __future__ import annotations\n' + ast.unparse(node), ns)
                elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in constants for target in node.targets
                ):
                    exec(ast.unparse(node), ns)
        ns['_preflight_job_config'] = ns['_preflight_check_provider_key']
        self.enterContext(mock.patch.dict('sys.modules', {
            'hermes_cli.fallback_config': SimpleNamespace(**fallback),
            'hermes_cli.providers': SimpleNamespace(ALIASES={}),
            'hermes_cli.runtime_provider': SimpleNamespace(resolve_runtime_provider=resolver,
                                                         format_runtime_provider_error=lambda exc: 'unavailable'),
            'hermes_cli.auth': SimpleNamespace(AuthError=auth_error, is_rate_limited_auth_error=lambda exc: False),
            'hermes_cli.config': SimpleNamespace(resolve_turn_limit=lambda value: 10),
            'agent.agent_runtime_helpers': SimpleNamespace(sync_credential_pool_entry_id=lambda agent: None),
        }))
        jc = SimpleNamespace(cfg=config, model='selected', model_cfg={'provider': 'primary'}, cron_default_provider='')
        job = {'id': 'cron-fixture', 'model': 'selected', 'provider': 'primary'}
        return ns, resolver, auth_error, jc, job

    def test_cron_primary_auth_or_network_failure_tries_eligible_backups_in_order(self):
        ns, resolver, auth_error, jc, job = self.cron_runtime()
        for primary_error in (auth_error('missing primary credential'),
                              ConnectionError(errno.ECONNRESET, 'connection reset')):
            with self.subTest(error=type(primary_error).__name__):
                resolver.reset_mock()
                resolver.side_effect = [primary_error, auth_error('missing backup credential'), {'provider': 'nous'}]
                setup = ns['_resolve_cron_agent_setup'](job, job['id'], 'fixture', jc)
                self.assertIsNone(setup.blocked)
                self.assertEqual((setup.runtime['provider'], setup.model), ('nous', 'backup-b'))
                self.assertEqual([c.kwargs['requested'] for c in resolver.call_args_list],
                                 ['primary', 'nvidia', 'nous'])
                self.assertIn('fallback', setup.fallback_notice)
                self.assertEqual(job['model'], 'selected')
        resolver.reset_mock()
        resolver.side_effect = auth_error('all providers unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            ns['_resolve_cron_agent_setup'](job, job['id'], 'fixture', jc)
        self.assertEqual(resolver.call_count, 3)

    def test_cron_agent_receives_the_chain_for_failures_during_generation(self):
        ns, resolver, _auth_error, jc, job = self.cron_runtime()
        resolver.return_value = {'provider': 'primary', 'base_url': 'https://primary.example.test', 'api_key': 'fixture'}
        setup = ns['_resolve_cron_agent_setup'](job, job['id'], 'fixture', jc)
        expected = jc.cfg['fallback_providers'][2:]
        self.assertEqual(setup.fallback_model, expected)
        constructor = mock.Mock()
        ns['_construct_cron_agent'](constructor, job, jc.cfg, setup, workdir=None, session_id='session', session_db=None)
        supplied = constructor.call_args.kwargs
        self.assertEqual(supplied['fallback_model'], expected)
        self.assertEqual(supplied['platform'], 'cron')
        self.assertTrue(supplied['quiet_mode'])
        # Quiet cron execution must still initialize the native failover chain.
        agent = SimpleNamespace(quiet_mode=True)
        ns['_init_fallback_chain'](agent, supplied['fallback_model'])
        self.assertEqual(agent._fallback_chain, expected)
        self.assertEqual(agent._fallback_index, 0)

    def test_registered_idle_handler_checks_authorization_and_uses_routed_home(self):
        registry = self.patched('hermes_cli/commands.py')
        self.assertIn('CommandDef("fallback"', registry)
        busy = ast.parse(self.patched('gateway/run_busy.py'))
        idle = next(n for n in ast.walk(busy) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == '_IDLE_COMMANDS' for t in n.targets))
        self.assertIn('fallback', ast.literal_eval(idle.value))
        tree = ast.parse(self.patched('gateway/slash_commands_model.py'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == '_handle_fallback_command')
        ns = {}
        exec('from __future__ import annotations\n' + ast.unparse(method), ns)
        command = mock.Mock(return_value='saved')
        runner = SimpleNamespace(_is_user_authorized_for_source=mock.Mock(return_value=False))
        event = SimpleNamespace(source=object(), get_command_args=lambda: 'off')
        modules = {'hermes_cli.fallback_config': SimpleNamespace(run_fallback_command=command),
                   'gateway.run': SimpleNamespace(_gateway_config_home=lambda: Path('/routed/profile'))}
        with mock.patch.dict('sys.modules', modules):
            asyncio.run(ns['_handle_fallback_command'](runner, event))
            command.assert_not_called()
            runner._is_user_authorized_for_source.return_value = True
            self.assertEqual(asyncio.run(ns['_handle_fallback_command'](runner, event)), 'saved')
            command.assert_called_once_with(Path('/routed/profile/config.yaml'), 'off')

    def test_quota_filter_precedes_provider_resolution_and_preserves_loop(self):
        source = self.patched('agent/chat_completion_helpers.py')
        tree = ast.parse(source)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'try_activate_fallback')
        code = ast.unparse(fn)
        self.assertIn('begin_fallback_walk(agent, reason)', code)
        self.assertLess(code.index('allow_fallback_candidate(agent, fb)'), code.index('resolve_provider_client('))
        self.assertNotIn('_execute_tool_calls', code)
        compiled = self.patched('hermes_cli/fallback_config.py')
        self.assertIn('def run_fallback_command(', compiled)

    def test_real_fallback_walk_never_resolves_quota_provider_or_paid_route(self):
        policy = load_module('fallback_runtime', ROOT / 'runtime/fallback-policy.py')
        tree = ast.parse(self.patched('agent/chat_completion_helpers.py'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'try_activate_fallback')
        import logging
        ns = {'logger': logging.getLogger(__name__), '_fallback_chain_exhausted': lambda *a: False,
              '_fallback_entry_key': lambda fb: tuple(fb.items()),
              '_should_skip_fallback_candidate': lambda *a: False,
              '_fallback_api_mode_hint': lambda *a: (False, 'chat_completions')}
        exec('from __future__ import annotations\n' + ast.unparse(fn), ns)
        resolver = mock.Mock(return_value=(None, None))
        modules = {
            'hermes_cli.providers': SimpleNamespace(ALIASES={}),
            'agent.fallback_cooldown': SimpleNamespace(_arm_rate_limit_cooldown=lambda *a, **kw: 60,
                                                       switch_deferred_by_reset=lambda *a: False),
            'hermes_cli.fallback_config': SimpleNamespace(begin_fallback_walk=policy.begin_fallback_walk,
                allow_fallback_candidate=policy.allow_fallback_candidate, resolve_entry_api_key=lambda fb: None),
            'agent.auxiliary_client': SimpleNamespace(resolve_provider_client=resolver),
        }
        agent = SimpleNamespace(provider='nvidia', _fallback_index=0, _fallback_activated=False,
            _fallback_chain=[{'provider': p, 'model': m} for p, m in (
                ('nvidia', 'other'), ('openrouter', 'paid'), ('nous', 'a'), ('ollama-cloud', 'b'))])
        with mock.patch.dict('sys.modules', modules):
            # Provider-level billing/quota blocks all its models; a model 429 does not.
            self.assertFalse(ns['try_activate_fallback'](agent, 'billing'))
        self.assertEqual([call.args[0] for call in resolver.call_args_list], ['nous', 'ollama-cloud'])
        self.assertEqual(agent._fallback_index, len(agent._fallback_chain))

    def test_cron_in_request_fallback_switches_provider_without_resetting_history(self):
        policy = load_module('cron_fallback_runtime', ROOT / 'runtime/fallback-policy.py')
        tree = ast.parse(self.patched('agent/chat_completion_helpers.py'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'try_activate_fallback')
        ns = {'logger': mock.Mock(), '_fallback_chain_exhausted': lambda *a: False,
              '_fallback_entry_key': lambda fb: (fb['provider'], fb['model']),
              '_should_skip_fallback_candidate': lambda *a: False,
              '_fallback_api_mode_hint': lambda *a: (True, 'chat_completions'),
              '_fallback_reason_text': lambda reason: str(reason),
              **{name: mock.Mock() for name in ('_rebind_fallback_credential_pool',
                  '_update_fallback_context_compressor', '_reresolve_fallback_reasoning_config',
                  '_rescope_fallback_extra_body', 'rewrite_prompt_model_identity',
                  '_buffer_fallback_notice', '_log_fallback_activated', '_reset_stale_streak')}}
        exec('from __future__ import annotations\n' + ast.unparse(fn), ns)
        client = SimpleNamespace(base_url='https://backup.example.test', api_key='backup-private')
        resolver = mock.Mock(side_effect=[(None, None), (client, 'backup-b')])
        def swap(agent, replacement, *args):
            agent.client, agent.api_key = replacement, replacement.api_key
        modules = {
            'hermes_cli.providers': SimpleNamespace(ALIASES={}, is_actual_route=lambda *a: False),
            'agent.fallback_cooldown': SimpleNamespace(_arm_rate_limit_cooldown=lambda *a, **kw: None,
                                                      switch_deferred_by_reset=lambda *a: False),
            'hermes_cli.fallback_config': SimpleNamespace(begin_fallback_walk=policy.begin_fallback_walk,
                allow_fallback_candidate=policy.allow_fallback_candidate, resolve_entry_api_key=lambda fb: None),
            'agent.auxiliary_client': SimpleNamespace(resolve_provider_client=resolver),
            'hermes_cli.model_normalize': SimpleNamespace(normalize_model_for_provider=lambda model, provider: model),
            'agent.client_lifecycle': SimpleNamespace(_swap_fallback_clients=swap),
            'agent.agent_runtime_helpers': SimpleNamespace(sync_credential_pool_entry_id=lambda agent: None),
            'agent.native_compaction': SimpleNamespace(resolve_native_compaction_capabilities=lambda **kw: {}),
        }
        history = [{'role': 'user', 'content': 'Run the scheduled digest'}]
        primary = {'model': 'selected', 'provider': 'ollama-cloud'}
        agent = SimpleNamespace(platform='cron', quiet_mode=True, model='selected', provider='ollama-cloud',
            base_url='https://primary.example.test', _fallback_index=0, _fallback_activated=False,
            _primary_runtime=dict(primary), messages=history,
            _anthropic_prompt_cache_policy=lambda **kw: (False, False), _ensure_lmstudio_runtime_loaded=mock.Mock(),
            _fallback_chain=[{'provider': 'nvidia', 'model': 'backup-a'}, {'provider': 'nous', 'model': 'backup-b'}])
        with mock.patch.dict('sys.modules', modules):
            self.assertTrue(ns['try_activate_fallback'](agent, 'rate_limit'))
        self.assertEqual((agent.provider, agent.model), ('nous', 'backup-b'))
        self.assertIs(agent.client, client)
        self.assertEqual(agent.api_key, client.api_key)
        self.assertIs(agent.messages, history)
        self.assertEqual(agent._primary_runtime, primary)
        self.assertTrue(agent._provider_fallback_active)
        self.assertEqual(resolver.call_count, 2)
        self.assertIn('using backup-b via nous', ns['_buffer_fallback_notice'].call_args.args[1])

    def test_chat_edit_refreshes_cached_chain_during_cooldown_without_replaying_turn(self):
        tree = ast.parse(self.patched('gateway/run_config_loaders.py'))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == '_apply_fallback_chain_to_agent')
        fn.decorator_list = []
        ns = {'time': time}
        exec('from __future__ import annotations\n' + ast.unparse(fn), ns)
        agent = SimpleNamespace(_fallback_activated=True, _rate_limited_until=time.monotonic() + 600,
            _fallback_chain=[{'provider': 'nous', 'model': 'old'}], _fallback_index=1,
            _unavailable_fallback_keys={'old'}, provider='nous', model='old')
        ns['_apply_fallback_chain_to_agent'](agent, copy.deepcopy(agent._fallback_chain))
        self.assertEqual(agent._fallback_index, 1)
        self.assertEqual(agent._unavailable_fallback_keys, {'old'})
        ns['_apply_fallback_chain_to_agent'](agent, [])
        self.assertEqual(agent._fallback_chain, [])
        self.assertEqual(agent._fallback_index, 0)
        self.assertEqual((agent.provider, agent.model), ('nous', 'old'))
        self.assertFalse(agent._unavailable_fallback_keys)


if __name__ == '__main__':
    unittest.main()
