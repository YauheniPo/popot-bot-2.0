#!/usr/bin/env python3
"""Coverage tests for install-agent-reach.py."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys
import os
import subprocess

ORIGINAL_SCRIPT = Path(__file__).parent / 'install-agent-reach.py'


class InstallAgentReachCoverageTests(unittest.TestCase):
    def setUp(self):
        # Create a temporary home directory (for HERMES_HOME and uv lookup)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Create a temporary directory for the copied script and its support files
        self.base_temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Create the directory structure mirroring the original: base_temp_dir/hermes/runtime/
        self.runtime_dir = self.base_temp_dir / 'hermes' / 'runtime'
        self.runtime_dir.mkdir(parents=True)
        # Point path validation at an isolated fixture tree
        self.copied_script = self.runtime_dir / 'install-agent-reach.py'
        # Create the config directory: base_temp_dir/hermes/config/
        self.config_dir = self.base_temp_dir / 'hermes' / 'config'
        self.config_dir.mkdir(parents=True)
        # Execute the original source so coverage is attributed to production code
        spec = importlib.util.spec_from_file_location('install_agent_reach', ORIGINAL_SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.__file__ = str(self.copied_script)
        self.enterContext(mock.patch.object(self.module.Path, 'home', return_value=self.home))
        self.enterContext(mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home / '.hermes')}))
        # Set up test-specific paths
        self.pin = 'a' * 40
        self.url = f'https://github.com/Panniantong/Agent-Reach/archive/{self.pin}.zip'
        self.venv = self.home / '.local/share/hermes-tools/agent-reach'

    def provision(self):
        (self.venv / 'bin').mkdir(parents=True, exist_ok=True)
        for name in ('python', 'agent-reach', 'yt-dlp'):
            (self.venv / 'bin' / name).write_text('fixture')

    def test_run_function(self):
        with mock.patch.object(self.module, 'subprocess') as mock_subprocess:
            mock_subprocess.run.return_value.stdout = 'test output'
            mock_subprocess.run.return_value.stderr = ''
            result = self.module.run(['echo', 'test'])
            self.assertEqual(result, 'test output')

    def test_installed_from_true(self):
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.return_value = json.dumps({'url': 'http://example.com'})
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_from(python_mock, 'http://example.com')
            self.assertTrue(result)

    def test_installed_from_false(self):
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.return_value = json.dumps({'url': 'http://other.com'})
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_from(python_mock, 'http://example.com')
            self.assertFalse(result)

    def test_installed_from_exception(self):
        with mock.patch.object(self.module, 'run', side_effect=subprocess.CalledProcessError(1, 'cmd')):
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_from(python_mock, 'http://example.com')
            self.assertFalse(result)

    def fake_run(self, args, timeout=60):
        if len(args) >= 2 and args[1].endswith('resolve-tool-version.py'):
            return self.pin
        if args[1:3] == ['venv', '--python']:
            self.provision()
        if args[1:3] == ['-c', self.module.METADATA]:
            return json.dumps({'url': self.url})
        if args[1:3] == ['-c', self.module.VERSIONS]:
            return json.dumps({})
        return ''

    def test_install_valid_revision_latest(self):
        with mock.patch.object(self.module, 'run', side_effect=self.fake_run) as runner:
            self.assertTrue(self.module.install(self.home, 'latest', 'uv'))
        self.assertEqual(runner.call_args_list[0].args[0][2:],
                         ['--package', 'agent-reach', '--requested', 'latest'])

    def test_install_invalid_revision_format(self):
        with self.assertRaises(ValueError) as cm:
            self.module.install(self.home, 'invalid-revision', 'uv')
        self.assertIn('Agent-Reach revision must be a full commit SHA or "latest"', str(cm.exception))

    def test_install_invalid_revision_after_latest_resolution(self):
        def mock_run(args, timeout=60):
            if args[1:3] == ['pip', 'install']:
                return json.dumps({'url': 'https://github.com/Panniantong/Agent-Reach/archive/notasha40.zip'})
            return ''

        with mock.patch.object(self.module, 'run', side_effect=mock_run):
            with self.assertRaises(ValueError) as cm:
                self.module.install(self.home, 'latest', 'uv')
            self.assertIn('Agent-Reach revision must be a full commit SHA', str(cm.exception))

    def test_main_with_settings(self):
        settings = self.config_dir / 'settings.yml'
        settings.write_text('vps_tools:\n  agent_reach:\n    revision: latest\n')
        with mock.patch.object(self.module, 'resolve_uv', return_value='uv'), \
                mock.patch.object(self.module, 'run', side_effect=self.fake_run), \
                mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--settings', str(settings)]):
            self.assertEqual(self.module.main(), 0)
        self.assertTrue((self.home / '.local/bin/agent-reach').is_symlink())

    def test_main_with_revision(self):
        with mock.patch.object(self.module, 'resolve_uv', return_value='uv'), \
                mock.patch.object(self.module, 'run', side_effect=self.fake_run), \
                mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--revision', self.pin]):
            self.assertEqual(self.module.main(), 0)
        self.assertTrue((self.home / '.local/bin/yt-dlp').is_symlink())

    def test_main_invalid_revision(self):
        with mock.patch.object(self.module, 'resolve_uv', return_value='uv'), \
                mock.patch.object(self.module, 'run') as runner, \
                mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--revision', 'invalid']):
            self.assertEqual(self.module.main(), 1)
        runner.assert_not_called()

    def test_main_missing_uv(self):
        with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
            with mock.patch.object(self.module, 'shutil', autospec=True) as mock_shutil:
                mock_shutil.which.return_value = None
                with self.assertRaises(SystemExit):
                    self.module.main()

    def test_resolve_uv_found(self):
        managed_uv = self.home / 'bin' / 'uv'
        managed_uv.parent.mkdir(parents=True, exist_ok=True)
        managed_uv.write_text('#!/bin/sh')
        managed_uv.chmod(0o755)
        with mock.patch.object(self.module, 'Path') as mock_path:
            mock_path.home.return_value = self.home
            result = self.module.resolve_uv(self.home)
            self.assertEqual(result, str(managed_uv))

    def test_resolve_uv_not_executable(self):
        managed_uv = self.home / 'bin' / 'uv'
        managed_uv.parent.mkdir(parents=True, exist_ok=True)
        managed_uv.write_text('#!/bin/sh')
        managed_uv.chmod(0o644)
        with mock.patch.object(self.module.shutil, 'which', return_value=None):
            self.assertIsNone(self.module.resolve_uv(self.home))

    def test_resolve_uv_from_path(self):
        with mock.patch.object(self.module, 'Path') as mock_path:
            mock_path.home.return_value = self.home
            with mock.patch.object(self.module, 'shutil', autospec=True) as mock_shutil:
                mock_shutil.which.return_value = '/usr/bin/uv'
                result = self.module.resolve_uv(self.home)
                self.assertEqual(result, '/usr/bin/uv')

    def test_installed_versions(self):
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.return_value = json.dumps({'package': '1.0.0'})
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_versions(python_mock)
            self.assertEqual(result, {'package': '1.0.0'})


if __name__ == '__main__':
    unittest.main()