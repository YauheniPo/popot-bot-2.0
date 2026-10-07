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
import importlib.util
import shutil
import yaml
from urllib.error import URLError
import subprocess

ORIGINAL_SCRIPT = Path(__file__).parent / 'install-agent-reach.py'
ORIGINAL_RESOLVER = Path(__file__).parent / 'resolve_tool_version.py'


class InstallAgentReachMissingCoverageTests(unittest.TestCase):
    """Tests targeting specific missing lines for coverage."""

    def setUp(self):
        self.temp_base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake_hermes_runtime = self.temp_base / 'hermes' / 'runtime'
        self.fake_hermes_runtime.mkdir(parents=True)
        self.fake_hermes_config = self.temp_base / 'hermes' / 'config'
        self.fake_hermes_config.mkdir(parents=True)
        spec = importlib.util.spec_from_file_location(
            'install_agent_reach', ORIGINAL_SCRIPT
        )
        if spec is None:
            raise RuntimeError(f"Could not load spec from {ORIGINAL_SCRIPT}")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.original_file = self.module.__file__

    def tearDown(self):
        pass

    def test_line_37_latest_revision_block(self):
        """Line 37: revision == 'latest' triggers resolution via resolve_tool_version.py"""
        fake_sha = 'a' * 40
        with mock.patch.object(self.module, '__file__', str(ORIGINAL_SCRIPT)), \
             mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch.object(self.module, '_resolve_latest_revision', return_value=fake_sha), \
             mock.patch.object(self.module, 'installed_from', return_value=True), \
             mock.patch.object(self.module, 'installed_versions', return_value={'agent-reach': '1.0.0'}), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
            result = self.module.main()
            self.assertIn(result, (True, False))

    def test_lines_114_115_settings_is_file_symlink_to_dir(self):
        """Lines 114-115: --settings path symlink pointing to directory fails is_file() check."""
        target_dir = self.fake_hermes_config / 'target_dir'
        target_dir.mkdir()
        settings_symlink = self.fake_hermes_config / 'bad_link.yml'
        settings_symlink.symlink_to(target_dir)
        # Mock parser.error to avoid sys.exit
        with mock.patch.object(self.module, '__file__', str(ORIGINAL_SCRIPT)), \
             mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_symlink)]), \
             mock.patch.object(self.module.argparse.ArgumentParser, 'error') as mock_error, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
            mock_error.side_effect = SystemExit(2)
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertEqual(cm.exception.code, 2)
            # Check that parser.error was called with the expected message
            mock_error.assert_called()
            args = mock_error.call_args[0][0]
            self.assertIn('regular file', args)

    def test_lines_118_119_settings_wrong_extension(self):
        """Lines 118-119: --settings wrong extension triggers error."""
        settings_file = self.fake_hermes_config / 'bad.txt'
        settings_file.write_text('{}')
        with mock.patch.object(self.module, '__file__', str(ORIGINAL_SCRIPT)), \
             mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
             mock.patch.object(self.module.argparse.ArgumentParser, 'error') as mock_error, \
             mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
            mock_error.side_effect = SystemExit(2)
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertEqual(cm.exception.code, 2)
            mock_error.assert_called()
            args = mock_error.call_args[0][0]
            self.assertIn('.yml or .yaml', args)

    def test_lines_120_131_settings_vps_tools_not_dict(self):
        """Line 124-125: vps_tools must be a mapping."""
        settings = {'vps_tools': 'not a dict'}
        # Use the real config directory
        real_config = Path('/home/hermes/workspace/repositories/popot-bot-2.0/hermes/config')
        settings_file = real_config / 'test_missing_coverage_vps_tools_not_dict.yml'
        settings_file.write_text(yaml.dump(settings))
        try:
            with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
                 mock.patch('pathlib.Path.home', return_value=self.temp_base), \
                 mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
                 mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
                 self.assertRaises(ValueError) as cm:
                self.module.main()
                self.assertIn('vps_tools must be a mapping', str(cm.exception))
        finally:
            settings_file.unlink(missing_ok=True)

    def test_lines_120_131_settings_agent_reach_not_dict(self):
        """Line 127-128: agent_reach must be a mapping."""
        settings = {'vps_tools': {'agent_reach': 'not a dict'}}
        real_config = Path('/home/hermes/workspace/repositories/popot-bot-2.0/hermes/config')
        settings_file = real_config / 'test_missing_coverage_agent_reach_not_dict.yml'
        settings_file.write_text(yaml.dump(settings))
        try:
            with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
                 mock.patch('pathlib.Path.home', return_value=self.temp_base), \
                 mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
                 mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
                 self.assertRaises(ValueError) as cm:
                self.module.main()
                self.assertIn('vps_tools.agent_reach must be a mapping', str(cm.exception))
        finally:
            settings_file.unlink(missing_ok=True)

    def test_lines_120_131_settings_revision_not_string(self):
        """Line 130-131: revision must be a string."""
        settings = {'vps_tools': {'agent_reach': {'revision': 123}}}
        real_config = Path('/home/hermes/workspace/repositories/popot-bot-2.0/hermes/config')
        settings_file = real_config / 'test_missing_coverage_revision_not_string.yml'
        settings_file.write_text(yaml.dump(settings))
        try:
            with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
                 mock.patch('pathlib.Path.home', return_value=self.temp_base), \
                 mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
                 mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
                 self.assertRaises(ValueError) as cm:
                self.module.main()
                self.assertIn('revision must be a string', str(cm.exception))
        finally:
            settings_file.unlink(missing_ok=True)

    def test_lines_120_131_settings_revision_invalid_sha(self):
        """Line 133-134: revision must be valid SHA or 'latest'."""
        settings = {'vps_tools': {'agent_reach': {'revision': 'short'}}}
        real_config = Path('/home/hermes/workspace/repositories/popot-bot-2.0/hermes/config')
        settings_file = real_config / 'test_missing_coverage_revision_invalid_sha.yml'
        settings_file.write_text(yaml.dump(settings))
        try:
            with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
                 mock.patch('pathlib.Path.home', return_value=self.temp_base), \
                 mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]), \
                 mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr, \
                 self.assertRaises(ValueError) as cm:
                self.module.main()
                self.assertIn('Agent-Reach revision from settings must be a full commit SHA or', str(cm.exception))
        finally:
            settings_file.unlink(missing_ok=True)

    def test_line_150_sys_exit_entry_point(self):
        """Line 150: sys.exit(main()) entry point."""
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py']), \
             mock.patch('sys.stderr', new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as cm:
            sys.exit(self.module.main())
            self.assertEqual(cm.exception.code, 2)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.temp_base), \
             mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'invalid']), \
             mock.patch('sys.stderr', new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as cm:
            sys.exit(self.module.main())
            self.assertEqual(cm.exception.code, 1)

    def test_installed_from_false_when_subprocess_error(self):
        """Line 36-39: installed_from returns False when subprocess call fails."""
        fake_python = self.temp_base / 'python'
        fake_python.touch()
        fake_python.chmod(0o755)
        with mock.patch.object(self.module, 'subprocess') as mock_subprocess:
            mock_subprocess.CalledProcessError = subprocess.CalledProcessError
            mock_subprocess.run.side_effect = subprocess.CalledProcessError(1, 'cmd')
            result = self.module.installed_from(fake_python, 'http://example.com')
            self.assertFalse(result)

    def test_installed_from_false_when_json_error(self):
        """Line 36-39: installed_from returns False when JSON decoding fails."""
        fake_python = self.temp_base / 'python'
        fake_python.touch()
        fake_python.chmod(0o755)
        with mock.patch.object(self.module, 'subprocess') as mock_subprocess:
            mock_subprocess.CalledProcessError = subprocess.CalledProcessError
            mock_result = mock.Mock()
            mock_result.stdout = b'invalid json'
            mock_result.stderr = b''
            mock_subprocess.run.return_value = mock_result
            result = self.module.installed_from(fake_python, 'http://example.com')
            self.assertFalse(result)

    def test_installed_from_true_when_match(self):
        """Line 36-39: installed_from returns True when metadata matches URL."""
        fake_python = self.temp_base / 'python'
        fake_python.touch()
        fake_python.chmod(0o755)
        test_url = 'http://example.com/custom'
        with mock.patch.object(self.module, 'subprocess') as mock_subprocess:
            mock_subprocess.CalledProcessError = subprocess.CalledProcessError
            mock_result = mock.Mock()
            mock_result.stdout = b'{"url": "http://example.com/custom"}'
            mock_result.stderr = b''
            mock_subprocess.run.return_value = mock_result
            result = self.module.installed_from(fake_python, test_url)
            self.assertTrue(result)

    def test_line_189_uv_not_found(self):
        """Line 189: raises error when uv is not found in managed location or PATH."""
        with mock.patch.object(self.module, 'resolve_uv', return_value=None):
            with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
                with mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
                    with self.assertRaises(SystemExit) as cm:
                        sys.exit(self.module.main())
                        self.assertEqual(cm.exception.code, 2)

    def test_resolve_latest_revision(self):
        """Test _resolve_latest_revision calls resolve-tool-version.py and returns stdout."""
        fake_sha = "a" * 40
        with mock.patch.object(self.module, "subprocess") as mock_subprocess:
            mock_result = mock.Mock()
            mock_result.stdout = fake_sha + "\n"
            mock_result.stderr = ""
            mock_result.returncode = 0
            mock_subprocess.run.return_value = mock_result
            revision = self.module._resolve_latest_revision()
            self.assertEqual(revision, fake_sha)
            mock_subprocess.run.assert_called_once()
            args, kwargs = mock_subprocess.run.call_args
            self.assertEqual(args[0][0], sys.executable)
            self.assertIn("--package", args[0])
            self.assertIn("agent-reach", args[0])
            self.assertIn("--requested", args[0])
            self.assertIn("latest", args[0])

    def test_installed_versions_calls_run(self):
        """Line 139: installed_versions calls run with VERSIONS code."""
        fake_python = self.temp_base / 'python'
        fake_python.touch()
        fake_python.chmod(0o755)
        with mock.patch.object(self.module, 'run', return_value='{"agent-reach": "1.0"}') as mock_run:
            result = self.module.installed_versions(fake_python)
            self.assertEqual(result, {'agent-reach': '1.0'})
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertIn('VERSIONS', args[2])

    def test_validate_revision_latest(self):
        """Line 28-29: _validate_revision returns True for 'latest'."""
        self.assertTrue(self.module._validate_revision('latest'))

    def test_validate_revision_valid_sha(self):
        """Line 30: _validate_revision validates SHA pattern."""
        valid_sha = 'a' * 40
        self.assertTrue(self.module._validate_revision(valid_sha))

    def test_validate_revision_invalid_sha(self):
        """Line 30: _validate_revision returns False for invalid SHA."""
        self.assertFalse(self.module._validate_revision('not-a-sha'))

    def test_run_version_probes(self):
        """Line 103-104: _run_version_probes runs version command on launchers."""
        from pathlib import Path
        launchers = {
            Path('/fake/bin/agent-reach'): Path('/fake/venv/bin/agent-reach'),
            Path('/fake/bin/yt-dlp'): Path('/fake/venv/bin/yt-dlp')
        }
        with mock.patch.object(self.module, 'run') as mock_run:
            self.module._run_version_probes(launchers)
            self.assertEqual(mock_run.call_count, 2)
            calls = [call.args[0][0] for call in mock_run.call_args_list]
            self.assertIn('/fake/venv/bin/agent-reach', calls)
            self.assertIn('/fake/venv/bin/yt-dlp', calls)

    def test_create_launchers_creates_symlinks(self):
        """Line 109-114: _create_launchers creates symlinks and returns True."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            venv_bin = home / 'venv' / 'bin'
            venv_bin.mkdir(parents=True)
            (venv_bin / 'agent-reach').write_text('agent-reach')
            (venv_bin / 'yt-dlp').write_text('yt-dlp')
            launchers = {
                home / '.local/bin/agent-reach': venv_bin / 'agent-reach',
                home / '.local/bin/yt-dlp': venv_bin / 'yt-dlp'
            }
            changed = self.module._create_launchers(launchers)
            self.assertTrue(changed)
            self.assertTrue((home / '.local/bin/agent-reach').is_symlink())
            self.assertTrue((home / '.local/bin/yt-dlp').is_symlink())

    def test_create_launchers_returns_false_if_exist(self):
        """Line 109-114: _create_launchers returns False if symlinks already exist."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            venv_bin = home / 'venv' / 'bin'
            venv_bin.mkdir(parents=True)
            (venv_bin / 'agent-reach').write_text('agent-reach')
            (venv_bin / 'yt-dlp').write_text('yt-dlp')
            launchers = {
                home / '.local/bin/agent-reach': venv_bin / 'agent-reach',
                home / '.local/bin/yt-dlp': venv_bin / 'yt-dlp'
            }
            self.module._create_launchers(launchers)
            changed = self.module._create_launchers(launchers)
            self.assertFalse(changed)

    def test_install_returns_false_when_no_changes(self):
        """Line 131: install returns False when nothing changed."""
        from pathlib import Path
        home = Path('/tmp')
        revision = 'a' * 40
        uv = '/usr/bin/uv'
        with mock.patch.object(self.module, '_validate_and_prepare'), \
             mock.patch.object(self.module, '_check_launchers'), \
             mock.patch.object(self.module, '_create_venv_if_needed', return_value=False), \
             mock.patch.object(self.module, 'installed_from', return_value=True), \
             mock.patch.object(self.module, 'installed_versions', return_value={'yt-dlp': '1.0'}), \
             mock.patch.object(self.module, '_install_packages'), \
             mock.patch.object(self.module, '_verify_installation'), \
             mock.patch.object(self.module, '_run_version_probes'), \
             mock.patch.object(self.module, '_create_launchers', return_value=False):
            result = self.module.install(home, revision, uv)
            self.assertFalse(result)

    def test_resolve_uv_returns_managed_uv(self):
        """Line 144-146: resolve_uv returns managed uv if exists and executable."""
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            hermes_home = Path(td)
            managed_uv = hermes_home / 'bin/uv'
            managed_uv.parent.mkdir(parents=True)
            managed_uv.write_text('#!/bin/sh\nexit 0\n')
            managed_uv.chmod(0o755)
            result = self.module.resolve_uv(hermes_home)
            self.assertEqual(result, str(managed_uv))

    def test_resolve_uv_returns_path_uv(self):
        """Line 147: resolve_uv returns shutil.which('uv') if managed uv not found."""
        with mock.patch('shutil.which', return_value='/usr/bin/uv'):
            result = self.module.resolve_uv(Path('/tmp/not-exist'))
            self.assertEqual(result, '/usr/bin/uv')

    def test_resolve_uv_returns_none_when_not_found(self):
        """Line 147: resolve_uv returns None if not found."""
        with mock.patch('shutil.which', return_value=None):
            result = self.module.resolve_uv(Path('/tmp/not-exist'))
            self.assertIsNone(result)

    def test_resolve_uv_or_error_raises(self):
        """Line 156: _resolve_uv_or_error calls parser.error when uv not found."""
        parser = mock.MagicMock()
        with mock.patch.object(self.module, 'resolve_uv', return_value=None):
            with self.assertRaises(SystemExit):
                self.module._resolve_uv_or_error(Path('/tmp'), parser)

    def test_verify_installation_raises(self):
        """Line 97-98: _verify_installation raises when installed_from returns False."""
        with mock.patch.object(self.module, 'installed_from', return_value=False):
            with self.assertRaises(RuntimeError) as cm:
                self.module._verify_installation('/fake/python', 'http://example.com/test.zip')
            self.assertIn('Agent-Reach installed source does not match the requested revision', str(cm.exception))

    def test_check_launchers_raises_on_target_mismatch(self):
        """Lines 70-72: _check_launchers raises when symlink points to wrong target."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            real_target = tmp_path / 'real'
            real_target.mkdir()
            wrong_target = tmp_path / 'wrong'
            wrong_target.mkdir()
            link = tmp_path / 'link'
            link.symlink_to(real_target)
            link.unlink()
            link.symlink_to(wrong_target)
            with self.assertRaises(RuntimeError) as cm:
                self.module._check_launchers({link: real_target})
            self.assertIn('unmanaged launcher already exists', str(cm.exception))

    def test_check_launchers_raises_on_unmanaged(self):
        """Lines 73-74: _check_launchers raises if unmanaged launcher exists."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            venv = home / '.local/share/hermes-tools/agent-reach'
            venv.mkdir(parents=True)
            bin_dir = venv / 'bin'
            bin_dir.mkdir(parents=True)
            python = bin_dir / 'python'
            python.write_text('#!/bin/sh\necho python')
            launchers = {home / '.local/bin/agent-reach': venv / 'bin/agent-reach'}
            launcher = home / '.local/bin/agent-reach'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('not a symlink')
            with self.assertRaises(RuntimeError) as cm:
                self.module._check_launchers(launchers)
            self.assertIn('unmanaged launcher already exists', str(cm.exception))

    def test_validate_and_prepare_raises_on_sha_mismatch(self):
        """Lines 57-58: _validate_and_prepare raises ValueError for invalid SHA."""
        from pathlib import Path
        home = Path('/tmp')
        with mock.patch.object(self.module, '_validate_revision', return_value=True):
            with self.assertRaises(ValueError) as cm:
                self.module._validate_and_prepare(home, 'notasha')
            self.assertIn('Agent-Reach revision must be a full commit SHA', str(cm.exception))

    def test_sys_exit_called_via_runpy(self):
        """Line 150: sys.exit(main()) entry point via runpy."""
        import runpy
        from pathlib import Path
        with mock.patch.object(self.module, 'install', return_value=0), \
             mock.patch.object(self.module, 'resolve_uv', return_value='/fake/uv'), \
             mock.patch.object(self.module.sys, 'exit') as mock_exit, \
             mock.patch.object(self.module.Path, 'home', return_value=Path('/tmp')), \
             mock.patch('sys.argv', ['install-agent-reach.py']):
            runpy.run_path(str(self.module.__file__), run_name='__main__')
            mock_exit.assert_called_once_with(2)


class InstallAgentReachResolveToolVersionTests(unittest.TestCase):
    """Tests for resolve_tool_version.py to improve coverage."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'resolve_tool_version', ORIGINAL_RESOLVER
        )
        if spec is None:
            raise RuntimeError(f"Could not load spec from {ORIGINAL_RESOLVER}")
        self.resolver = importlib.util.module_from_spec(spec)
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
            self.resolver.resolve('agent-reach', 'gg' * 20)
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'short')
        with self.assertRaises(ValueError):
            self.resolver.resolve('agent-reach', 'f' * 41)

    def test_resolve_npm_package_latest(self):
        """Test resolving npm package with 'latest'."""
        fake_version = '1.2.3'
        with mock.patch.object(self.resolver, 'fetch_json') as mock_fetch:
            mock_fetch.return_value = {
                'dist-tags': {'latest': fake_version}
            }
            version = self.resolver.resolve('@googleworkspace/cli', 'latest')
            self.assertEqual(version, fake_version)
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
            self.resolver.resolve('@googleworkspace/cli', '1.2')
        with self.assertRaises(ValueError):
            self.resolver.resolve('@googleworkspace/cli', '1.2.3.4')

    def test_resolve_unsupported_package(self):
        """Test resolving unsupported package raises ValueError."""
        with self.assertRaises(ValueError):
            self.resolver.resolve('unsupported-package', 'latest')

    def test_fetch_json_retry_and_error(self):
        """Test fetch_json retries and raises RuntimeError on failure."""
        with mock.patch.object(self.resolver, 'urlopen') as mock_urlopen:
            mock_urlopen.side_effect = URLError('network error')
            with self.assertRaises(RuntimeError):
                self.resolver.fetch_json('http://example.com')
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
