#!/usr/bin/env python3
"""Coverage tests for resolve_tool_version.py"""

import importlib.util
import json
import io
import runpy
import subprocess
import unittest
from pathlib import Path
from unittest import mock
import sys

SCRIPT = Path(__file__).parent / 'resolve_tool_version.py'

class ToolVersionCoverageTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('tool_version', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    # ----- fetch_json -----
    def test_fetch_json_success(self):
        with mock.patch.object(self.module, 'urlopen') as mock_urlopen:
            mock_resp = mock.Mock()
            mock_resp.read.return_value = b'{"ok": true}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = self.module.fetch_json('http://example.com')
            self.assertEqual(result, {'ok': True})

    def test_fetch_json_value_error_on_last_attempt(self):
        """Lines 31-34: ValueError on last attempt raises RuntimeError."""
        with mock.patch.object(self.module, 'urlopen') as mock_urlopen:
            mock_resp = mock.Mock()
            mock_resp.read.return_value = b'not valid json'
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            with mock.patch.object(self.module.time, 'sleep'):
                with self.assertRaises(RuntimeError) as cm:
                    self.module.fetch_json('http://example.com')
                self.assertIn('latest version lookup failed', str(cm.exception))
                self.assertIn('JSONDecodeError', str(cm.exception))

    def test_fetch_json_raises_after_retries(self):
        """Line 25: raise RuntimeError after 3 attempts"""
        from urllib.error import URLError
        with mock.patch.object(self.module, 'urlopen', side_effect=URLError('fail')):
            with mock.patch.object(self.module.time, 'sleep'):
                with self.assertRaises(RuntimeError) as cm:
                    self.module.fetch_json('http://example.com')
                self.assertIn('latest version lookup failed', str(cm.exception))

    def test_fetch_json_retry_then_success(self):
        """Lines 24-26: retry logic, then success on third attempt"""
        from urllib.error import URLError
        call_count = 0
        def urlopen_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise URLError('temporary failure')
            mock_resp = mock.Mock()
            mock_resp.read.return_value = b'{}'
            # Return a context manager mock
            cm = mock.MagicMock()
            cm.__enter__.return_value = mock_resp
            cm.__exit__.return_value = None
            return cm
        with mock.patch.object(self.module, 'urlopen', side_effect=urlopen_side_effect):
            with mock.patch.object(self.module.time, 'sleep'):
                result = self.module.fetch_json('http://example.com')
                self.assertEqual(result, {})
                self.assertEqual(call_count, 3)

    # ----- resolve -----
    def test_unsupported_package(self):
        """Line 30-31: if package not in PACKAGES: raise ValueError"""
        with self.assertRaises(ValueError) as cm:
            self.module.resolve('unsupported-package', 'latest')
        self.assertIn('unsupported managed CLI', str(cm.exception))

    def test_agent_reach_latest_non_dict_response(self):
        """Line 37-38: if not isinstance(data, dict): raise ValueError"""
        with mock.patch.object(self.module, 'fetch_json', return_value='not a dict'):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-reach', 'latest')
            self.assertIn('unexpected response structure for agent-reach latest', str(cm.exception))

    def test_agent_reach_latest_wrong_repo(self):
        """Reject a commit with wrong repository."""
        with mock.patch.object(self.module, 'fetch_json', return_value={
                'sha': 'a' * 40, 'repository': {'full_name': 'wrong/repo'}, 'ref': 'refs/heads/main'}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-reach', 'latest')
            self.assertIn('agent-reach response repository mismatch', str(cm.exception))

    def test_agent_reach_latest_wrong_ref(self):
        """Reject a commit with wrong ref."""
        with mock.patch.object(self.module, 'fetch_json', return_value={
                'sha': 'a' * 40, 'repository': {'full_name': 'Panniantong/Agent-Reach'}, 'ref': 'refs/heads/develop'}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-reach', 'latest')
            self.assertIn('agent-reach response ref mismatch', str(cm.exception))

    def test_agent_reach_latest_missing_sha(self):
        """Line 46: version = data.get('sha', '') -> empty string -> fails pattern"""
        with mock.patch.object(self.module, 'fetch_json', return_value={
            'repository': {'full_name': 'Panniantong/Agent-Reach'}, 'ref': 'refs/heads/main'}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-reach', 'latest')
            self.assertIn('invalid resolved version for agent-reach', str(cm.exception))

    def test_npm_latest_non_dict_response(self):
        """Line 50-51: if not isinstance(data, dict): raise ValueError"""
        with mock.patch.object(self.module, 'fetch_json', return_value='not a dict'):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-browser', 'latest')
            self.assertIn('unexpected npm response structure', str(cm.exception))

    def test_npm_latest_missing_dist_tags(self):
        """Line 52: version = data.get('dist-tags', {}).get('latest', '') -> empty string -> fails pattern"""
        with mock.patch.object(self.module, 'fetch_json', return_value={}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-browser', 'latest')
            self.assertIn('invalid resolved version for agent-browser', str(cm.exception))

    def test_invalid_resolved_version_agent_reach(self):
        """Line 67-69: pattern check fails - need valid repo/ref but invalid SHA"""
        with mock.patch.object(self.module, 'fetch_json', return_value={
            'sha': 'nothex', 'repository': {'full_name': 'Panniantong/Agent-Reach'}, 'ref': 'refs/heads/main'}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-reach', 'latest')
            self.assertIn('invalid resolved version for agent-reach', str(cm.exception))

    def test_invalid_resolved_version_npm(self):
        """Line 54-55: pattern check fails for npm"""
        with mock.patch.object(self.module, 'fetch_json', return_value={'dist-tags': {'latest': 'not-a-version'}}):
            with self.assertRaises(ValueError) as cm:
                self.module.resolve('agent-browser', 'latest')
            self.assertIn('invalid resolved version for agent-browser', str(cm.exception))

    # ----- main -----
    def test_main_help(self):
        with mock.patch('sys.argv', ['resolve-tool-version.py', '--help']):
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertEqual(cm.exception.code, 0)

    def test_main_missing_package(self):
        with mock.patch('sys.argv', ['resolve-tool-version.py', '--requested', 'latest']):
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_main_missing_requested(self):
        with mock.patch('sys.argv', ['resolve-tool-version.py', '--package', 'agent-reach']):
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertNotEqual(cm.exception.code, 0)

    def test_main_value_error(self):
        with mock.patch.object(self.module, 'resolve', side_effect=ValueError('test error')):
            with mock.patch('sys.argv', ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']):
                result = self.module.main()
                self.assertEqual(result, 1)

    def test_main_runtime_error(self):
        with mock.patch.object(self.module, 'resolve', side_effect=RuntimeError('test error')):
            with mock.patch('sys.argv', ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']):
                result = self.module.main()
                self.assertEqual(result, 1)

    def test_main_success(self):
        with mock.patch.object(self.module, 'resolve', return_value='1.2.3'):
            with mock.patch('sys.argv', ['resolve-tool-version.py', '--package', 'agent-reach', '--requested', 'latest']):
                with mock.patch('sys.stdout') as mock_stdout:
                    result = self.module.main()
                    self.assertEqual(result, 0)
                    # stdout.write is called twice: '1.2.3' then '\n'
                    self.assertEqual(mock_stdout.write.call_count, 2)

    def test_main_as_script_calls_sys_exit(self):
        """Line 73: sys.exit(main()) - run as subprocess"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=5
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('the following arguments are required: --package', result.stderr)

    def test_entry_point_prints_explicit_pin_and_exits_successfully(self):
        with mock.patch.object(sys, 'argv', [str(SCRIPT), '--package', 'agent-browser', '--requested', '1.2.3']), \
                mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as stdout, \
                mock.patch.object(sys, 'exit') as exit_call:
            runpy.run_path(str(SCRIPT), run_name='__main__')
        exit_call.assert_called_once_with(0)
        self.assertEqual(stdout.getvalue(), '1.2.3\n')


if __name__ == '__main__':
    unittest.main()
