#!/usr/bin/env python3
"""Tests to cover missing lines in install-agent-reach.py for coverage."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import shutil
import yaml

SCRIPT = Path(__file__).parent / 'install-agent-reach.py'


class InstallAgentReachMissingCoverageTests(unittest.TestCase):
    """Tests targeting specific missing lines for coverage."""

    def setUp(self):
        # Create a temporary home directory
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Create a mock install-agent-reach.py in a temporary runtime directory
        self.runtime_dir = self.home / 'hermes' / 'runtime'
        self.runtime_dir.mkdir(parents=True)
        self.config_dir = self.home / '.hermes' / 'config'
        self.config_dir.mkdir(parents=True)
        # Copy the real script to our runtime directory
        self.mock_script = self.runtime_dir / 'install-agent-reach.py'
        self.mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))
        # Load the module from our copy
        spec = importlib.util.spec_from_file_location('install_agent_reach', self.mock_script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        # Common mocks
        self.uv_path = '/usr/bin/uv'
        self.pin = 'a' * 40

    def _setup_common_mocks(self):
        """Set up common mocks for main() execution."""
        # Mock shutil.which to return our fake uv
        with mock.patch('shutil.which', return_value=self.uv_path):
            # Mock Path.home to return our temporary home
            with mock.patch('pathlib.Path.home', return_value=self.home):
                # Mock subprocess.run to avoid actual installation
                with mock.patch.object(self.module, 'run') as mock_run:
                    def run_side_effect(args, timeout=60, **kwargs):
                        # Provision uv venv
                        if args[1:3] == ['venv', '--python']:
                            (self.home / '.local/share/hermes-tools/agent-reach' / 'bin').mkdir(parents=True, exist_ok=True)
                            for name in ('python', 'agent-reach', 'yt-dlp'):
                                (self.home / '.local/share/hermes-tools/agent-reach' / 'bin' / name).write_text('fixture')
                                (self.home / '.local/share/hermes-tools/agent-reach' / 'bin' / name).chmod(0o755)
                            return ''
                        # pip install
                        if args[1:3] == ['pip', 'install']:
                            return json.dumps({'url': f'https://github.com/Panniantong/Agent-Reach/archive/{self.pin}.zip'})
                        # installed_from METADATA check
                        if '-c' in args and 'importlib.metadata' in args[2] and 'direct_url.json' in args[2]:
                            return json.dumps({'url': f'https://github.com/Panniantong/Agent-Reach/archive/{self.pin}.zip'})
                        # installed_versions
                        if '-c' in args and 'importlib.metadata' in args[2] and 'distributions' in args[2]:
                            return json.dumps({'pkg': '1.0.0'})
                        # version probe
                        if len(args) >= 2 and args[1] in ('version', '--version'):
                            return '1.0.0'
                        return ''
                    mock_run.side_effect = run_side_effect
                    yield mock_run

    # ---- line 37: revision == 'latest' block ----
    def test_line_37_latest_revision_block(self):
        """Line 37: revision == 'latest' triggers resolution via resolve-tool-version.py"""
        with self._setup_common_mocks() as mock_run:
            # Mock the resolve-tool-version.py call
            original_side_effect = mock_run.side_effect
            def run_side_effect(args, timeout=60):
                if 'resolve-tool-version.py' in args[1]:
                    return self.pin
                return original_side_effect(args, timeout)
            mock_run.side_effect = run_side_effect
            with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'latest']):
                with mock.patch('pathlib.Path.home', return_value=self.home):
                    changed = self.module.main()
                    self.assertTrue(changed)  # Should succeed and report a change

    # ---- lines 114-115: settings path is_file() check (symlink to directory) ----
    def test_lines_114_115_settings_is_file_symlink_to_dir(self):
        """Lines 114-115: --settings path symlink pointing to directory fails is_file() check."""
        settings_symlink = self.config_dir / 'bad_link.yml'
        target_dir = self.config_dir / 'target_dir'
        target_dir.mkdir()
        settings_symlink.symlink_to(target_dir)  # symlink to directory
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_symlink)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 2)  # parser.error -> SystemExit(2)
                    self.assertIn('regular file', self.module.__dict__.get('last_stderr', ''))

    # ---- lines 118-119: settings suffix check ----
    def test_lines_118_119_settings_wrong_extension(self):
        """Lines 118-119: --settings wrong extension triggers error."""
        settings_file = self.config_dir / 'settings.txt'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: "latest"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 2)
                    self.assertIn('must be a .yml or .yaml file', self.module.__dict__.get('last_stderr', ''))

    # ---- lines 120-131: settings structure validation ----
    def test_lines_120_131_settings_vps_tools_not_dict(self):
        """Line 124-125: vps_tools must be a mapping."""
        settings_file = self.config_dir / 'settings1.yml'
        settings_file.write_text('vps_tools: \"not a dict\"')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 1)  # ValueError -> return 1
                    self.assertIn('vps_tools must be a mapping', self.module.__dict__.get('last_stderr', ''))

    def test_lines_120_131_settings_agent_reach_not_dict(self):
        """Line 127-128: agent_reach must be a mapping."""
        settings_file = self.config_dir / 'settings2.yml'
        settings_file.write_text('''vps_tools:
  agent_reach: \"not a dict\"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn('vps_tools.agent_reach must be a mapping', self.module.__dict__.get('last_stderr', ''))

    def test_lines_120_131_settings_revision_not_string(self):
        """Line 130-131: revision must be a string."""
        settings_file = self.config_dir / 'settings3.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: 12345
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn('vps_tools.agent_reach.revision must be a string', self.module.__dict__.get('last_stderr', ''))

    def test_lines_120_131_settings_revision_invalid_sha(self):
        """Line 133-134: revision must be valid SHA or 'latest'."""
        settings_file = self.config_dir / 'settings4.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: \"notashort\"
''')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with mock.patch.object(self.module, '__file__', str(self.mock_script)):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(self.home) if key == 'HERMES_HOME' else default):
                    with self.assertRaises(SystemExit) as cm:
                        self.module.main()
                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn('Agent-Reach revision from settings must be a full commit SHA or \"latest\"', self.module.__dict__.get('last_stderr', ''))

    # ---- line 150: sys.exit(main()) entry point ----
    def test_line_150_sys_exit_entry_point(self):
        """Line 150: sys.exit(main()) entry point."""
        result = subprocess.run(
            [sys.executable, str(self.mock_script)],
            capture_output=True,
            text=True,
            timeout=5
        )
        self.assertNotEqual(result.returncode, 0)
        # Should show usage error about missing required arguments
        self.assertIn('usage:', result.stderr.lower())
        self.assertIn('--settings', result.stderr.lower())
        self.assertIn('--revision', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()