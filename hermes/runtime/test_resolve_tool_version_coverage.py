#!/usr/bin/env python3
"""Coverage tests for resolve-tool-version.py"""

import json
import sys
import time
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import URLError
from urllib.request import Request

# Ensure the hermes.runtime package is importable
sys.path.insert(0, '/home/hermes/workspace/repositories/popot-bot-2.0')

from hermes.runtime.resolve_tool_version import (
    fetch_json,
    _resolve_agent_reach,
    _resolve_npm_package,
    resolve,
    main,
    PACKAGES,
    GITHUB_API_URL,
    NPM_REGISTRY_URL,
    AGENT_REACH_SHA_PATTERN,
    NPM_VERSION_PATTERN,
)


class TestResolveToolVersion(unittest.TestCase):
    def setUp(self):
        pass

    # Tests for fetch_json
    @patch('hermes.runtime.resolve_tool_version.urlopen')
    def test_fetch_json_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"key": "value"}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_cm
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"key": "value"})
        mock_urlopen.assert_called_once()
        # Check that the argument is a Request object with correct headers
        args, _ = mock_urlopen.call_args
        request = args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, 'http://example.com')
        self.assertEqual(request.headers, {'User-agent': 'hermes-deploy', 'Accept': 'application/json'})

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_urllib_error_retry_then_success(self, mock_sleep, mock_urlopen):
        # First attempt raises URLError, second succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            URLError('URLError'),  # first attempt
            mock_cm  # second attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(1)  # attempt 0 -> sleep 1

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_urllib_error_fail_after_three(self, mock_sleep, mock_urlopen):
        mock_urlopen.side_effect = [URLError('URLError')] * 3
        with self.assertRaises(RuntimeError) as cm:
            fetch_json('http://example.com')
        self.assertIn('latest version lookup failed', str(cm.exception))
        self.assertIn('URLError', str(cm.exception))
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_retry_then_success(self, mock_sleep, mock_urlopen):
        # First attempt raises ValueError (json.loads fails), second succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            ValueError('Invalid JSON'),
            mock_cm
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_urllib_error_retry_two_then_success(self, mock_sleep, mock_urlopen):
        # First two attempts raise URLError, third succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            URLError('URLError'),  # first attempt
            URLError('URLError'),  # second attempt
            mock_cm  # third attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_retry_two_then_success(self, mock_sleep, mock_urlopen):
        # First two attempts raise ValueError, third succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            ValueError('Invalid JSON'),  # first attempt
            ValueError('Invalid JSON'),  # second attempt
            mock_cm  # third attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_urllib_error_retry_twice_then_success(self, mock_sleep, mock_urlopen):
        # First two attempts raise URLError, third succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            URLError('URLError'),  # first attempt
            URLError('URLError'),  # second attempt
            mock_cm  # third attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_retry_twice_then_success(self, mock_sleep, mock_urlopen):
        # First two attempts raise ValueError, third succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            ValueError('Invalid JSON'),  # first attempt
            ValueError('Invalid JSON'),  # second attempt
            mock_cm  # third attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_fail_after_three_v1(self, mock_sleep, mock_urlopen):
        # Return a response that when read returns bytes that cause json.loads to raise ValueError
        mock_response = MagicMock()
        mock_response.read.return_value = b'not json'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [mock_cm] * 3
        with self.assertRaises(RuntimeError) as cm:
            fetch_json('http://example.com')
        self.assertIn('latest version lookup failed', str(cm.exception))
        self.assertIn('JSONDecodeError', str(cm.exception))
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_fail_after_three_v2(self, mock_sleep, mock_urlopen):
        # Return a response that when read returns invalid JSON
        mock_response = MagicMock()
        mock_response.read.side_effect = lambda *args, **kwargs: b'invalid json'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [mock_cm] * 3
        with self.assertRaises(RuntimeError) as cm:
            fetch_json('http://example.com')
        self.assertIn('latest version lookup failed', str(cm.exception))
        self.assertIn('JSONDecodeError', str(cm.exception))
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_retry_then_success_v2(self, mock_sleep, mock_urlopen):
        # First attempt raises ValueError, second succeeds
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.side_effect = [
            ValueError('Invalid JSON'),  # first attempt
            mock_cm  # second attempt
        ]
        result = fetch_json('http://example.com')
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(1)  # attempt 0 -> sleep 1

    @patch('hermes.runtime.resolve_tool_version.urlopen')
    @patch('hermes.runtime.resolve_tool_version.time.sleep')
    def test_fetch_json_value_error_fail_after_three_v3(self, mock_sleep, mock_urlopen):
        # Make each attempt return a response that when read returns invalid JSON
        mock_response = MagicMock()
        mock_response.read.return_value = b'invalid json'
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_cm
        with self.assertRaises(RuntimeError) as cm:
            fetch_json('http://example.com')
        self.assertIn('latest version lookup failed', str(cm.exception))
        self.assertIn('JSONDecodeError', str(cm.exception))
        self.assertEqual(mock_urlopen.call_count, 3)
        # sleeps for attempt 0 and 1
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)


    # Tests for _resolve_agent_reach
    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_agent_reach_not_dict(self, mock_fetch):
        mock_fetch.return_value = "not a dict"
        with self.assertRaises(ValueError) as cm:
            _resolve_agent_reach()
        self.assertIn('unexpected response structure', str(cm.exception))

    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_agent_reach_repo_mismatch(self, mock_fetch):
        mock_fetch.return_value = {
            'repository': {'full_name': 'Wrong/Repo'},
            'ref': 'refs/heads/main',
            'sha': 'a' * 40,
        }
        with self.assertRaises(ValueError) as cm:
            _resolve_agent_reach()
        self.assertIn('repository mismatch', str(cm.exception))

    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_agent_reach_ref_mismatch(self, mock_fetch):
        mock_fetch.return_value = {
            'repository': {'full_name': 'Panniantong/Agent-Reach'},
            'ref': 'refs/heads/feature',
            'sha': 'a' * 40,
        }
        with self.assertRaises(ValueError) as cm:
            _resolve_agent_reach()
        self.assertIn('ref mismatch', str(cm.exception))

    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_agent_reach_success(self, mock_fetch):
        fake_sha = 'b' * 40
        mock_fetch.return_value = {
            'repository': {'full_name': 'Panniantong/Agent-Reach'},
            'ref': 'refs/heads/main',
            'sha': fake_sha,
        }
        result = _resolve_agent_reach()
        self.assertEqual(result, fake_sha)

    # Tests for _resolve_npm_package
    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_npm_package_not_dict(self, mock_fetch):
        mock_fetch.return_value = "not a dict"
        with self.assertRaises(ValueError) as cm:
            _resolve_npm_package('@googleworkspace/cli')
        self.assertIn('unexpected npm response structure', str(cm.exception))

    @patch('hermes.runtime.resolve_tool_version.fetch_json')
    def test_resolve_npm_package_success(self, mock_fetch):
        mock_fetch.return_value = {'dist-tags': {'latest': '1.2.3'}}
        result = _resolve_npm_package('@googleworkspace/cli')
        self.assertEqual(result, '1.2.3')

    # Tests for resolve function
    def test_resolve_unsupported_package(self):
        with self.assertRaises(ValueError) as cm:
            resolve('unsupported-package', 'latest')
        self.assertIn('unsupported managed CLI', str(cm.exception))

    def test_resolve_agent_reach_explicit_sha(self):
        sha = 'c' * 40
        with patch('hermes.runtime.resolve_tool_version.fetch_json') as mock_fetch:
            result = resolve('agent-reach', sha)
            self.assertEqual(result, sha)
            mock_fetch.assert_not_called()

    def test_resolve_agent_reach_latest(self):
        fake_sha = 'd' * 40
        with patch('hermes.runtime.resolve_tool_version._resolve_agent_reach') as mock_resolve:
            mock_resolve.return_value = fake_sha
            result = resolve('agent-reach', 'latest')
            self.assertEqual(result, fake_sha)
            mock_resolve.assert_called_once()

    def test_resolve_npm_package_latest(self):
        fake_version = '2.3.4'
        with patch('hermes.runtime.resolve_tool_version._resolve_npm_package') as mock_resolve:
            mock_resolve.return_value = fake_version
            result = resolve('@googleworkspace/cli', 'latest')
            self.assertEqual(result, fake_version)
            mock_resolve.assert_called_once()

    def test_resolve_npm_package_explicit(self):
        version = '3.4.5'
        with patch('hermes.runtime.resolve_tool_version.fetch_json') as mock_fetch:
            result = resolve('@googleworkspace/cli', version)
            self.assertEqual(result, version)
            mock_fetch.assert_not_called()

    def test_resolve_invalid_version_agent_reach(self):
        with self.assertRaises(ValueError) as cm:
            resolve('agent-reach', 'not-hex')
        self.assertIn('invalid resolved version', str(cm.exception))

    def test_resolve_invalid_version_npm(self):
        with self.assertRaises(ValueError) as cm:
            resolve('@googleworkspace/cli', '1.2')
        self.assertIn('invalid resolved version', str(cm.exception))

    # Tests for main function
    def test_main_success(self):
        # We need to mock sys.argv; easier to call main with args via patching sys.argv
        test_args = ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']
        with patch('sys.argv', test_args):
            with patch('hermes.runtime.resolve_tool_version.resolve') as mock_resolve:
                mock_resolve.return_value = 'v1.0.0'
                result = main()
        self.assertEqual(result, 0)
        mock_resolve.assert_called_once_with('agent-reach', 'latest')

    def test_main_value_error(self):
        test_args = ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']
        with patch('sys.argv', test_args):
            with patch('hermes.runtime.resolve_tool_version.resolve') as mock_resolve:
                mock_resolve.side_effect = ValueError('test error')
                result = main()
        self.assertEqual(result, 1)

    def test_main_runtime_error(self):
        test_args = ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']
        with patch('sys.argv', test_args):
            with patch('hermes.runtime.resolve_tool_version.resolve') as mock_resolve:
                mock_resolve.side_effect = RuntimeError('test error')
                result = main()
        self.assertEqual(result, 1)

    # Test the script entry point via subprocess to cover __main__ block
    def test_main_subprocess_help(self):
        import subprocess
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = '/home/hermes/workspace/repositories/popot-bot-2.0'
        result = subprocess.run(
            [sys.executable, '-m', 'hermes.runtime.resolve_tool_version', '--help'],
            capture_output=True, text=True, env=env, cwd='/home/hermes/workspace/repositories/popot-bot-2.0'
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn('usage:', result.stdout)
        self.assertIn('--package', result.stdout)
        self.assertIn('--requested', result.stdout)

    @patch('hermes.runtime.resolve_tool_version.resolve')
    def test_main_exit_called(self, mock_resolve):
        mock_resolve.return_value = 'a' * 40
        # Set sys.argv to simulate calling the script with required arguments
        import sys
        old_argv = sys.argv
        sys.argv = ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']
        try:
            # Run the module as a script
            import runpy
            runpy.run_module('hermes.runtime.resolve_tool_version', alter_sys=True)
        except SystemExit as e:
            self.assertEqual(e.code, 0)
        finally:
            sys.argv = old_argv


if __name__ == '__main__':
    unittest.main()