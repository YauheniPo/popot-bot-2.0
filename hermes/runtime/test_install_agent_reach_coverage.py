#!/usr/bin/env python3
"""Coverage tests for install-agent-reach.py."""

import importlib.util
import json
import shutil
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
        # Copy the original script to the runtime directory
        self.copied_script = self.runtime_dir / 'install-agent-reach.py'
        shutil.copy(ORIGINAL_SCRIPT, self.copied_script)
        # Create the config directory: base_temp_dir/hermes/config/
        self.config_dir = self.base_temp_dir / 'hermes' / 'config'
        self.config_dir.mkdir(parents=True)
        # Load the module from the copied script (so __file__ matches)
        spec = importlib.util.spec_from_file_location('install_agent_reach', self.copied_script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        # Patch installed_versions to return empty dict (as original setUp did)
        self.enterContext(mock.patch.object(self.module, 'installed_versions', return_value={}))
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

    def test_install_valid_revision_latest(self):
        def mock_run(args, timeout=60):
            if args[1:3] == ['venv', '--python']:
                self.provision()
                return ''
            if args[1:3] == ['pip', 'install']:
                return json.dumps({'url': self.url})
            if '-c' in args and 'importlib.metadata' in args[2]:
                cmd_str = ' '.join(args)
                if "distribution('agent-reach').read_text('direct_url.json')" in cmd_str:
                    return json.dumps({'url': self.url})
                else:
                    return json.dumps({})
            return ''
        with mock.patch.object(self.module, 'run', side_effect=mock_run):
            changed = self.module.install(self.home, self.pin, 'uv')
            self.assertTrue(changed)
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
        settings_content = """vps_tools:
  agent_reach:
    revision: "latest"""
        # Place settings file under config directory to satisfy path validation
        settings_file = self.config_dir / 'settings.yml'
        with open(settings_file, 'w') as f:
            f.write(settings_content)

        with mock.patch.object(self.module, 'resolve_uv', return_value='uv'):
            with mock.patch.object(self.module, 'run') as mock_run:
                def run_side_effect(args, timeout=60):
                    if args[1:3] == ['venv', '--python']:
                        self.provision()
                        return ''
                    if args[1:3] == ['pip', 'install']:
                        return json.dumps({'url': self.url})
                    if '-c' in args and 'importlib.metadata' in args[2]:
                        cmd_str = ' '.join(args)
                        if "distribution('agent-reach').read_text('direct_url.json')" in cmd_str:
                            # METADATA call: return direct_url.json content with url
                            return json.dumps({'url': self.url})
                        else:
                            # VERSIONS call: return empty dict (no packages)
                            return json.dumps({})
                    # Handle resolve-tool-version.py call for latest revision
                    if len(args) >= 2 and args[0].endswith('resolve-tool-version.py'):
                        if '--package' in args and '--requested' in args:
                            pkg_idx = args.index('--package')
                            req_idx = args.index('--requested')
                            if pkg_idx + 1 < len(args) and req_idx + 1 < len(args):
                                if args[pkg_idx + 1] == 'agent-reach' and args[req_idx + 1] == 'latest':
                                    return self.pin  # return the fake SHA
                    return ''
                mock_run.side_effect = run_side_effect
                with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
                    result = self.module.main()
                    self.assertEqual(result, 0)
    def test_main_with_revision(self):
        with mock.patch.object(self.module, 'resolve_uv', return_value='uv'):
            with mock.patch.object(self.module, 'run') as mock_run:
                def run_side_effect(args, timeout=60):
                    if args[1:3] == ['venv', '--python']:
                        self.provision()
                        return ''
                    if args[1:3] == ['pip', 'install']:
                        return json.dumps({'url': self.url})
                    if '-c' in args and 'importlib.metadata' in args[2]:
                        cmd_str = ' '.join(args)
                        if "distribution('agent-reach').read_text('direct_url.json')" in cmd_str:
                            return json.dumps({'url': self.url})
                        else:
                            return json.dumps({})
                    return ''
                mock_run.side_effect = run_side_effect
                with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', self.pin]):
                    result = self.module.main()
                    self.assertEqual(result, 0)
    def test_main_invalid_revision(self):
        with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'invalid']):
            with mock.patch.object(self.module, 'resolve_uv', return_value='uv'):
                with mock.patch.object(self.module, 'run') as mock_run:
                    mock_run.return_value = ''
                    result = self.module.main()
                    self.assertEqual(result, 1)
    def test_main_missing_uv(self):
        with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
            with mock.patch.object(self.module, 'shutil', autospec=True) as mock_shutil:
                mock_shutil.which.return_value = None
                with mock.patch.object(self.module, 'Path') as mock_path:
                    mock_path.home.return_value = self.home
                    with mock.patch.object(self.module, 'run') as mock_run:
                        mock_run.return_value = ''
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
        # Not executable
        with mock.patch.object(self.module, 'Path') as mock_path:
            mock_path.home.return_value = self.home
            with mock.patch.object(self.module, 'shutil') as mock_shutil:
                mock_shutil.which.return_value = None
                result = self.module.resolve_uv(self.home)
                self.assertIsNone(result)
    def test_resolve_uv_from_path(self):
        with mock.patch.object(self.module, 'Path') as mock_path:
            mock_path.home.return_value = self.home
            with mock.patch.object(self.module, 'shutil', autospec=True) as mock_shutil:
                mock_shutil.which.return_value = '/usr/bin/uv'
                result = self.module.resolve_uv(self.home)
                self.assertEqual(result, '/usr/bin/uv')

        def mock_run(args, timeout=60):
        def mock_run(args, timeout=60):
            if "-c" in args and "importlib.metadata" in args[2]:
                cmd_str = " ".join(args)
                if "distribution('agent-reach').read_text('direct_url.json')" in cmd_str:
                    return json.dumps({'url': 'http://example.com'})
                else:
                    return json.dumps({'package': '1.0.0'})
            return ""
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.side_effect = mock_run
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_versions(python_mock)
            self.assertEqual(result, {'package': '1.0.0'})
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.side_effect = mock_run
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_versions(python_mock)
            self.assertEqual(result, {'package': '1.0.0'})
            mock_run.side_effect = mock_run
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_versions(python_mock)
            self.assertEqual(result, {'package': '1.0.0'})

if __name__ == '__main__':
    unittest.main()