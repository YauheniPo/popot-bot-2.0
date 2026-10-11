"""The last durable chat selection supplies one non-secret route for all cron runs."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('cron_shared_model', Path(__file__).with_name('cron_shared_model.py'))
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)


class SharedCronModelTests(unittest.TestCase):
    def setUp(self):
        self.config = {'model': {'default': 'main', 'provider': 'main-provider'},
                       'cron': {'follow_chat_model': True, 'timeout': 123,
                                'shared_model': {'model': 'old', 'provider': 'old-provider', 'base_url': None}},
                       'secret': 'keep-private'}
        self.root = Path('/fixture/home')
        self.selection = SimpleNamespace(new_model='selected', target_provider='selected-provider',
                                         base_url='https://api.example.test/v1', api_key='never-copy-this')
        self.writes = []
        def write(path, key, value):
            self.writes.append((path, key, copy.deepcopy(value)))
            self.config['cron']['shared_model'] = copy.deepcopy(value)
        self.modules = {
            'hermes_constants': SimpleNamespace(get_default_hermes_root=lambda: self.root),
            'hermes_cli.config': SimpleNamespace(
                require_readable_config_before_write=lambda path: copy.deepcopy(self.config)),
            'utils': SimpleNamespace(atomic_roundtrip_yaml_update=write),
        }
        self.patch = mock.patch.dict('sys.modules', self.modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_durable_choice_updates_only_shared_route_idempotently_without_credentials(self):
        before = copy.deepcopy(self.config)
        self.assertTrue(shared.persist_chat_model(self.selection))
        expected = {'model': 'selected', 'provider': 'selected-provider', 'base_url': self.selection.base_url}
        self.assertEqual(self.writes, [(self.root / 'config.yaml', 'cron.shared_model', expected)])
        before['cron']['shared_model'] = expected
        self.assertEqual(self.config, before)
        self.assertTrue(shared.persist_chat_model(self.selection))
        self.assertEqual(len(self.writes), 1)

    def test_next_run_uses_latest_route_over_old_pins_without_rewriting_job_or_active_run(self):
        job = {'id': 'a', 'provider': 'pinned', 'model': 'pinned', 'base_url': 'https://old.invalid',
               'origin': {'chat_id': '123'}, 'deliver': 'telegram:456', 'state': 'paused'}
        before = copy.deepcopy(job)
        first_run = shared.routed_job(job)
        self.assertEqual(first_run['model'], 'old')
        shared.persist_chat_model(self.selection)
        second_run = shared.routed_job(job)
        self.assertEqual(second_run, {**before, **self.config['cron']['shared_model']})
        self.assertEqual(job, before)
        self.assertEqual(first_run['model'], 'old')
        self.assertIsNone(first_run['base_url'])

    def test_disabled_policy_and_script_jobs_keep_native_routing(self):
        job = {'id': 'a', 'model': 'pinned'}
        self.config['cron']['follow_chat_model'] = False
        self.assertFalse(shared.persist_chat_model(self.selection))
        self.assertIs(shared.routed_job(job), job)
        self.config['cron']['follow_chat_model'] = True
        job['no_agent'] = True
        self.assertIs(shared.routed_job(job), job)
        self.assertFalse(self.writes)

    def test_invalid_or_unreadable_shared_route_fails_without_silent_fallback(self):
        for value in ({}, {'provider': 'p', 'model': ''}, {'provider': None, 'model': 'm'}, []):
            self.config['cron']['shared_model'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'shared cron model'):
                shared.routed_job({'id': 'a'})
        reader = self.modules['hermes_cli.config']
        reader.require_readable_config_before_write = mock.Mock(side_effect=RuntimeError('unreadable'))
        with self.assertRaisesRegex(RuntimeError, 'unreadable'):
            shared.persist_chat_model(self.selection)
        self.assertFalse(self.writes)

    def test_write_failure_propagates_and_keeps_old_route(self):
        self.modules['utils'].atomic_roundtrip_yaml_update = mock.Mock(side_effect=OSError('denied'))
        with self.assertRaises(OSError):
            shared.persist_chat_model(self.selection)
        self.assertEqual(self.config['cron']['shared_model']['model'], 'old')

    def test_invalid_urls_and_symlinked_configuration_are_rejected(self):
        for url in (['invalid'], 'ftp://api.example.test', 'https://user:password@example.test', 'https:///'):
            self.selection.base_url = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                shared.persist_chat_model(self.selection)
        with mock.patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                shared.routed_job({'id': 'a'})
        self.assertFalse(self.writes)


if __name__ == '__main__':
    unittest.main()
