#!/usr/bin/env python3
"""Additional coverage tests for install-agent-reach.py"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys
import os
import subprocess
import yaml

SCRIPT = Path(__file__).parent / 'install-agent-reach.py'

class InstallAgentReachAdditionalCoverageTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('agent_reach_install', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        # Don't mock installed_versions globally - let individual tests mock it
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.pin = 'a' * 40
        self.url = f'https://github.com/Panniantong/Agent-Reach/archive/{self.pin}.zip'
        self.venv = self.home / '.local/share/hermes-tools/agent-reach'
        self.uv_path = '/usr/bin/uv'

    def provision(self):
        (self.venv / 'bin').mkdir(parents=True, exist_ok=True)
        for name in ('python', 'agent-reach', 'yt-dlp'):
            (self.venv / 'bin' / name).write_text('fixture')
        # Make them executable
        for name in ('python', 'agent-reach', 'yt-dlp'):
            (self.venv / 'bin' / name).chmod(0o755)

    def _make_run_mock(self, mock_run, provision_venv=True):
        """Create a side_effect function for mocking run() that handles all the internal calls."""
        def run_side_effect(args, timeout=60):
            # provision: uv venv --python <python> --no-python-downloads <venv>
            if args[1:3] == ['venv', '--python']:
                if provision_venv:
                    self.provision()
                return ''
            # pip install
            if args[1:3] == ['pip', 'install']:
                return json.dumps({'url': self.url})
            # installed_from METADATA check: python -c "from importlib.metadata import distribution; print(distribution('agent-reach').read_text('direct_url.json') or '{}')"
            if '-c' in args and 'importlib.metadata' in args[2] and 'direct_url.json' in args[2]:
                return json.dumps({'url': self.url})
            # installed_versions: python -c "import json; from importlib.metadata import distributions; print(json.dumps({d.metadata['Name']: d.version for d in distributions()}))"
            if '-c' in args and 'importlib.metadata' in args[2] and 'distributions' in args[2]:
                return json.dumps({'pkg': '1.0.0'})
            # version probe: agent-reach version or yt-dlp --version
            if len(args) >= 2 and args[1] in ('version', '--version'):
                return '1.0.0'
            return ''
        mock_run.side_effect = run_side_effect

    # ---- installed_from line 27: python.exists() == False ----
    def test_installed_from_python_not_exists(self):
        python_mock = self.home / 'bin' / 'python'
        self.assertFalse(python_mock.exists())
        result = self.module.installed_from(python_mock, 'http://example.com')
        self.assertFalse(result)

    # ---- install lines 54 and 56: symlink mismatch and existing non-symlink ----
    def test_install_symlink_mismatch(self):
        launcher = self.home / '.local/bin' / 'agent-reach'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        wrong_target = self.home / 'wrong'
        wrong_target.symlink_to(self.home / 'somewhere')
        launcher.symlink_to(wrong_target)
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run)
                with self.assertRaises(RuntimeError) as cm:
                    self.module.install(self.home, self.pin, self.uv_path)
                self.assertIn('unmanaged launcher already exists', str(cm.exception))

    def test_install_existing_non_symlink_launcher(self):
        launcher = self.home / '.local/bin' / 'agent-reach'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text('not a symlink')
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run)
                with self.assertRaises(RuntimeError) as cm:
                    self.module.install(self.home, self.pin, self.uv_path)
                self.assertIn('unmanaged launcher already exists', str(cm.exception))

    # ---- install lines 74-83: changed calculation, version probes, symlink creation ----
    def test_install_changed_via_version_diff(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run, provision_venv=True)
                # For this test we need VERSIONS to alternate
                call_count = 0
                original_side_effect = mock_run.side_effect
                def custom_side_effect(args, timeout=60):
                    nonlocal call_count
                    if '-c' in args and 'importlib.metadata' in args[2] and 'distributions' in args[2]:
                        call_count += 1
                        if call_count % 2 == 1:
                            return json.dumps({'pkg': '1.0.0'})
                        else:
                            return json.dumps({'pkg': '2.0.0'})
                    return original_side_effect(args, timeout)
                mock_run.side_effect = custom_side_effect
                changed = self.module.install(self.home, self.pin, self.uv_path)
                self.assertTrue(changed)

    def test_install_version_probes_and_symlink_creation(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run, provision_venv=True)
                changed = self.module.install(self.home, self.pin, self.uv_path)
                self.assertTrue(changed)

    # ---- installed_versions line 87 ----
    def test_installed_versions(self):
        with mock.patch.object(self.module, 'run') as mock_run:
            mock_run.return_value = json.dumps({'pkg': '1.0.0'})
            python_mock = self.home / 'bin' / 'python'
            python_mock.parent.mkdir(parents=True, exist_ok=True)
            python_mock.write_text('#!/bin/sh')
            python_mock.chmod(0o755)
            result = self.module.installed_versions(python_mock)
            self.assertEqual(result, {'pkg': '1.0.0'})

    # ---- resolve_uv lines 92-95 ----
    def test_resolve_uv_found(self):
        managed_uv = self.home / 'bin' / 'uv'
        managed_uv.parent.mkdir(parents=True, exist_ok=True)
        managed_uv.write_text('#!/bin/sh')
        managed_uv.chmod(0o755)
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'Path') as mock_path:
                mock_path.home.return_value = self.home
                result = self.module.resolve_uv(self.home)
                self.assertEqual(result, str(managed_uv))

    def test_resolve_uv_not_executable(self):
        managed_uv = self.home / 'bin' / 'uv'
        managed_uv.parent.mkdir(parents=True, exist_ok=True)
        managed_uv.write_text('#!/bin/sh')
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'Path') as mock_path:
                mock_path.home.return_value = self.home
                result = self.module.resolve_uv(self.home)
                self.assertEqual(result, self.uv_path)

    def test_resolve_uv_from_which(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'Path') as mock_path:
                mock_path.home.return_value = self.home
                result = self.module.resolve_uv(self.home)
                self.assertEqual(result, self.uv_path)

    # ---- main lines 114-115: settings not a file ----
    def test_main_settings_not_file(self):
        settings_dir = self.home / 'settings_dir'
        settings_dir.mkdir()
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_dir)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 118-119: wrong extension ----
    def test_main_settings_wrong_extension(self):
        settings_file = self.home / 'settings.txt'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: "latest"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 124: vps_tools not dict ----
    def test_main_settings_vps_tools_not_dict(self):
        settings_file = self.home / 'settings.yml'
        settings_file.write_text('''vps_tools: "not a dict"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 128: agent_reach not dict ----
    def test_main_settings_agent_reach_not_dict(self):
        settings_file = self.home / 'settings.yml'
        settings_file.write_text('''vps_tools:
  agent_reach: "not a dict"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 131: revision not string ----
    def test_main_settings_revision_not_string(self):
        settings_file = self.home / 'settings.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: 12345
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 133-134: revision invalid sha ----
    def test_main_settings_revision_invalid_sha(self):
        settings_file = self.home / 'settings.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: "notashort"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit):
                self.module.main()

    # ---- main lines 137-138: missing uv ----
    def test_main_missing_uv(self):
        with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', self.pin]):
            with mock.patch.dict('os.environ', {'HERMES_HOME': str(self.home)}):
                with mock.patch.object(self.module.shutil, 'which', return_value=None):
                    # Also ensure the managed uv doesn't exist
                    managed_uv = self.home / 'bin/uv'
                    if managed_uv.exists():
                        managed_uv.unlink()
                    with self.assertRaises(SystemExit):
                        self.module.main()

    # ---- main lines 145-146: print and return 0 (success) ----
    def test_main_success_changed_true(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run, provision_venv=True)
                with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', self.pin]):
                    result = self.module.main()
                    self.assertEqual(result, 0)

    def test_main_success_changed_false(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'run') as mock_run:
                self._make_run_mock(mock_run, provision_venv=True)
                # First call to set up the installation
                self.module.install(self.home, self.pin, self.uv_path)
                # Second call should detect no changes
                with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', self.pin]):
                    result = self.module.main()
                    self.assertEqual(result, 0)

    # ---- main lines 141-144: exception handling ----
    def test_main_exception_handling(self):
        with mock.patch('shutil.which', return_value=self.uv_path):
            with mock.patch.object(self.module, 'install', side_effect=ValueError('test')):
                with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
                    result = self.module.main()
                    self.assertEqual(result, 1)

    # ---- line 150: sys.exit(main()) ----
    def test_main_as_script_calls_sys_exit(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=5
        )
        self.assertNotEqual(result.returncode, 0)
        # Check for the actual error message format
        self.assertIn('one of the arguments --settings --revision is required', result.stderr)


if __name__ == '__main__':
    unittest.main()