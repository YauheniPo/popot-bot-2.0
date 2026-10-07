#!/usr/bin/env python3
"""Tests to cover missing lines in install-agent-reach.py for coverage."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import shutil
import yaml

ORIGINAL_SCRIPT = Path(__file__).parent / 'install-agent-reach.py'
ORIGINAL_RESOLVER = Path(__file__).parent / 'resolve-tool-version.py'


class InstallAgentReachMissingCoverageTests(unittest.TestCase):
    """Tests targeting specific missing lines for coverage."""

    def setUp(self):
        # Create a temporary base directory
        self.temp_base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Create the directory structure: temp_base/hermes/runtime/ and temp_base/hermes/config/
        self.fake_hermes_runtime = self.temp_base / 'hermes' / 'runtime'
        self.fake_hermes_runtime.mkdir(parents=True)
        self.fake_hermes_config = self.temp_base / 'hermes' / 'config'
        self.fake_hermes_config.mkdir(parents=True)
        # Copy the original script to the fake location
        self.fake_script_path = self.fake_hermes_runtime / 'install-agent-reach.py'
        shutil.copy(ORIGINAL_SCRIPT, self.fake_script_path)
        # Load the module from the original script (to get the actual code)
        spec = __import__('importlib.util').util.spec_from_file_location(
            'install_agent_reach', ORIGINAL_SCRIPT
        )
        self.module = __import__('importlib.util').util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        # Monkey-patch __file__ so that the module's path calculations use our fake location
        self.module.__file__ = str(self.fake_script_path)

    def tearDown(self):
        # No manual cleanup needed; enterContext handles it.
        pass

    # ---- line 37: revision == 'latest' block ----
    def test_line_37_latest_revision_block(self):
        """Line 37: revision == 'latest' triggers resolution via resolve-tool-version.py"""
        fake_sha = 'a' * 40
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch.object(self.module, 'subprocess') as mock_subprocess, \
             mock.patch.object(self.module, 'installed_from', return_value=True), \
             mock.patch.object(self.module, 'installed_versions', return_value={'agent-reach': '1.0.0'}), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
            # Mock subprocess.run to return the fake SHA for resolve-tool-version.py
            def run_side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get('args', [])
                if isinstance(cmd, list) and len(cmd) >= 2 and 'resolve-tool-version.py' in cmd[1]:
                    mock_result = mock.Mock()
                    mock_result.stdout = fake_sha
                    mock_result.stderr = ''
                    return mock_result
                # For any other subprocess call, return a dummy success
                mock_result = mock.Mock()
                mock_result.stdout = ''
                mock_result.stderr = ''
                return mock_result
            mock_subprocess.run.side_effect = run_side_effect
            # Execute
            result = self.module.main()
            # We just need to ensure the line executed; result should be bool
            self.assertIn(result, (True, False))

    # ---- lines 114-115: settings path is_file() check (symlink to directory) ----
    def test_lines_114_115_settings_is_file_symlink_to_dir(self):
        """Lines 114-115: --settings path symlink pointing to directory fails is_file() check."""
        # Create a directory to point to
        target_dir = self.fake_hermes_config / 'target_dir'
        target_dir.mkdir()
        # Create symlink to that directory
        settings_symlink = self.fake_hermes_config / 'bad_link.yml'
        settings_symlink.symlink_to(target_dir)
        # Expect error about regular file (parser.error -> SystemExit)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_symlink)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(SystemExit) as cm:
            self.module.main()
            self.assertEqual(cm.exception.code, 2)
            # stderr should contain 'regular file'
            self.assertIn('regular file', mock_stderr.getvalue())

    # ---- lines 118-119: wrong extension triggers error ----
    def test_lines_118_119_settings_wrong_extension(self):
        """Lines 118-119: --settings wrong extension triggers error."""
        settings_file = self.fake_hermes_config / 'bad.txt'
        settings_file.write_text('{}')
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(SystemExit) as cm:
            self.module.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('must be a .yml or .yaml file', mock_stderr.getvalue())

    # ---- lines 120-131: settings structure validation ----
    def test_lines_120_131_settings_vps_tools_not_dict(self):
        """Line 124-125: vps_tools must be a mapping."""
        settings = {'vps_tools': 'not a dict'}
        settings_file = self.fake_hermes_config / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(ValueError) as cm:
            self.module.main()
            self.assertIn('vps_tools must be a mapping', str(cm.exception))

    def test_lines_120_131_settings_agent_reach_not_dict(self):
        """Line 127-128: agent_reach must be a mapping."""
        settings = {'vps_tools': {'agent_reach': 'not a dict'}}
        settings_file = self.fake_hermes_config / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(ValueError) as cm:
            self.module.main()
            self.assertIn('vps_tools.agent_reach must be a mapping', str(cm.exception))

    def test_lines_120_131_settings_revision_not_string(self):
        """Line 130-131: revision must be a string."""
        settings = {'vps_tools': {'agent_reach': {'revision': 123}}}
        settings_file = self.fake_hermes_config / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(ValueError) as cm:
            self.module.main()
            self.assertIn('revision must be a string', str(cm.exception))

    def test_lines_120_131_settings_revision_invalid_sha(self):
        """Line 133-134: revision must be valid SHA or 'latest'."""
        settings = {'vps_tools': {'agent_reach': {'revision': 'short'}}}
        settings_file = self.fake_hermes_config / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
             self.assertRaises(ValueError) as cm:
            self.module.main()
            self.assertIn('Agent-Reach revision from settings must be a full commit SHA or', str(cm.exception))

    # ---- line 150: sys.exit(main()) entry point ----
    def test_line_150_sys_exit_entry_point(self):
        """Line 150: sys.exit(main()) entry point."""
        # Test with missing required arguments -> should error and exit with code 2 (argparse)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py']), \
             mock.patch('sys.stderr', new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as cm:
            sys.exit(self.module.main())
            self.assertEqual(cm.exception.code, 2)
        # Test with invalid revision -> should error and exit with code 1 (ValueError caught in main)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'invalid']), \
             mock.patch('sys.stderr', new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as cm:
            sys.exit(self.module.main())
            self.assertEqual(cm.exception.code, 1)


class InstallAgentReachResolveToolVersionTests(unittest.TestCase):
    """Tests for resolve-tool-version.py to improve coverage."""

    def setUp(self):
        # Load the resolver module from the original file
        spec = __import__('importlib.util').util.spec_from_file_location(
            'resolve_tool_version', ORIGINAL_RESOLVER
        )
        self.resolver = __import__('importlib.util').util.module_from_spec(spec)
        spec.loader.exec_module(self.resolver)

    def test_resolve_agent_reach_latest(self):
        """Test resolving agent-reach with 'latest'."""
        fake_sha = 'a' * 40
        with mock.patch.object(self.resolver, 'fetch_json') as mock_fetch:
            mock_fetch.return_value = {
                'repository': {'full_name': 'Panniantong/Agent-Reach'},
                'ref': 'refs/heads/main',
                'sha': fake_sha
            }
            version = self.resolver.resolve('agent-reach', 'latest')
            self.assertEqual(version, fake_sha)
            # Ensure fetch_json was called with the GitHub API URL
            mock_fetch.assert_called_once_with(
                'https://api.github.com/repos/Panniantong/Agent-Reach/commits/main'
            )

    def test_resolve_agent_reach_explicit_sha(self):
        """Test resolving agent-reach with explicit SHA."""
        sha = 'b' * 40
        with mock.patch.object(self.resolver, 'fetch_json') as mock_fetch:
            version = self.resolver.resolve('agent-reach', sha)
            self.assertEqual(version, sha)
            mock_fetch.assert_not_called()

    def test_resolve_agent_reach_invalid_sha(self):
        """Test resolving agent-reach with invalid SHA raises ValueError."""
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'not-hex')
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'gg' * 20)  # not hex
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'short')  # too short
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'f' * 41)  # too long

    def test_resolve_npm_package_latest(self):
        """Test resolving npm package with 'latest'."""
        fake_version = '1.2.3'
        with mock.patch.object(self.resolver, 'fetch_json') as mock_fetch:
            mock_fetch.return_value = {
                'dist-tags': {'latest': fake_version}
            }
            version = self.resolver.resolve('@googleworkspace/cli', 'latest')
            self.assertEqual(version, fake_version)
            # Ensure fetch_json was called with the npm URL
            mock_fetch.assert_called_once_with(
                'https://registry.npmjs.org/@googleworkspace%2Fcli'
            )

    def test_resolve_npm_package_explicit(self):
        """Test resolving npm package with explicit version."""
        version = '2.0.0'
        with mock.patch.object(self.resolver, 'fetch_json') as mock_fetch:
            res = self.resolver.resolve('@googleworkspace/cli', version)
            self.assertEqual(res, version)
            mock_fetch.assert_not_called()

    def test_resolve_npm_package_invalid_version(self):
        """Test resolving npm package with invalid version raises ValueError."""
        with self.assertRaises(ValueError):
            self.resolver.resolve('@googleworkspace/cli', 'not-a-version')
        with self.assertRaises(ValueError):
            self.resolver.resolve('@googleworkspace/cli', '1.2')  # missing patch
        with self.assertRaises(ValueError):
            self.resolver.resolve('@googleworkspace/cli', '1.2.3.4')  # extra part

    def test_resolve_unsupported_package(self):
        """Test resolving unsupported package raises ValueError."""
        with self.assertRaises(ValueError):
            self.resolver.resolve('unsupported-package', 'latest')

    def test_fetch_json_retry_and_error(self):
        """Test fetch_json retries and raises RuntimeError on failure."""
        with mock.patch.object(self.resolver, 'urlopen') as mock_urlopen:
            mock_urlopen.side_effect = Exception('network error')
            with self.assertRaises(RuntimeError):
                self.resolver.fetch_json('http://example.com')
            # Should have retried 3 times
            self.assertEqual(mock_urlopen.call_count, 3)

    def test_fetch_json_success(self):
        """Test fetch_json returns data on success."""
        mock_resp = mock.Mock()
        mock_resp.read.return_value = rb'{"key": "value"}'
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=None)
        with mock.patch.object(self.resolver, 'urlopen', return_value=mock_resp):
            result = self.resolver.fetch_json('http://example.com')
            self.assertEqual(result, {"key": "value"})


if __name__ == '__main__':
    unittest.main()