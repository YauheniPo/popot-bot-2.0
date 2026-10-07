"""Latest tool resolution must fail closed and return concrete stable versions."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


class ToolVersionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('tool_versions', Path(__file__).with_name('resolve-tool-version.py'))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_npm_latest_uses_stable_dist_tag(self):
        with mock.patch.object(self.module, 'fetch_json', return_value={'dist-tags': {'latest': '2.3.4', 'next': '3.0.0-beta.1'}}) as fetch:
            self.assertEqual(self.module.resolve('agent-browser', 'latest'), '2.3.4')
        self.assertEqual(fetch.call_args.args[0], 'https://registry.npmjs.org/agent-browser')

    def test_scoped_package_and_git_source(self):
        with mock.patch.object(self.module, 'fetch_json', return_value={'dist-tags': {'latest': '1.2.3'}}) as fetch:
            self.assertEqual(self.module.resolve('@googleworkspace/cli', 'latest'), '1.2.3')
        self.assertIn('%2F', fetch.call_args.args[0])
        commit_url = 'https://api.github.com/repos/Panniantong/Agent-Reach/commits/' + 'a' * 40
        with mock.patch.object(self.module, 'fetch_json', return_value={'sha': 'a' * 40, 'url': commit_url}) as fetch:
            self.assertEqual(self.module.resolve('agent-reach', 'latest'), 'a' * 40)
        fetch.assert_called_once_with('https://api.github.com/repos/Panniantong/Agent-Reach/commits/main')

    def test_git_commit_response_must_bind_valid_sha_to_the_expected_repository(self):
        for commit_url in (None, 123, '',
                           'https://api.github.com/repos/other/repo/commits/' + 'a' * 40,
                           'https://api.github.com/repos/Panniantong/Agent-Reach/commits/' + 'b' * 40,
                           'http://api.github.com/repos/Panniantong/Agent-Reach/commits/' + 'a' * 40):
            with self.subTest(url=commit_url), mock.patch.object(self.module, 'fetch_json',
                    return_value={'sha': 'a' * 40, 'url': commit_url}):
                with self.assertRaisesRegex(ValueError, 'commit URL mismatch'):
                    self.module.resolve('agent-reach', 'latest')

    def test_invalid_remote_response_is_rejected(self):
        for package, payload in [('agent-browser', {'dist-tags': {'latest': 'next'}}), ('agent-reach', {'sha': 'main'})]:
            with self.subTest(package=package), mock.patch.object(self.module, 'fetch_json', return_value=payload):
                with self.assertRaises(ValueError):
                    self.module.resolve(package, 'latest')

    def test_explicit_pin_is_preserved_without_network(self):
        with mock.patch.object(self.module, 'fetch_json') as fetch:
            self.assertEqual(self.module.resolve('agent-browser', '1.2.3'), '1.2.3')
            self.assertEqual(self.module.resolve('agent-reach', 'b' * 40), 'b' * 40)
        fetch.assert_not_called()

    def test_network_failure_has_bounded_attempts_and_no_old_version_fallback(self):
        with mock.patch.object(self.module, 'urlopen', side_effect=TimeoutError('offline')) as request, mock.patch.object(self.module.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'latest version lookup failed'):
                self.module.resolve('agent-browser', 'latest')
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.kwargs['timeout'] == 15 for call in request.call_args_list))
