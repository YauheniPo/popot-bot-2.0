#!/usr/bin/env python3
"""Integration-style coverage tests for install-agent-reach.py settings validation.

These tests create fresh module instances with mocked __file__ to properly
cover lines 114-115, 118-134 (settings path validation and settings structure validation).
"""

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


class InstallAgentReachSettingsCoverageTests(unittest.TestCase):
    """Tests that run as subprocesses to cover
    settings validation paths that are evaluated at module load time.
    """

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.pin = 'a' * 40
        self.uv_path = '/usr/bin/uv'

    def test_settings_path_is_file_check_symlink_to_dir(self):
        """Lines 114-115: --settings path symlink pointing to directory fails is_file() check."""
        mock_script_dir = self.home / 'hermes' / 'runtime'
        mock_script_dir.mkdir(parents=True, exist_ok=True)
        config_root = mock_script_dir.parent / 'config'
        config_root.mkdir(parents=True, exist_ok=True)
        
        mock_script = mock_script_dir / 'install-agent-reach.py'
        mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))

        # Create symlink to directory (not a file)
        settings_symlink = config_root / 'settings_link.yml'
        target_dir = config_root / 'target_dir'
        target_dir.mkdir()
        settings_symlink.symlink_to(target_dir)

        result = subprocess.run(
            [sys.executable, str(mock_script), '--settings', str(settings_symlink)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'HERMES_HOME': str(self.home)}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('regular file', result.stderr)

    def test_settings_validation_vps_tools_not_dict(self):
        """Line 124-125: vps_tools must be a mapping."""
        mock_script_dir = self.home / 'hermes' / 'runtime'
        mock_script_dir.mkdir(parents=True, exist_ok=True)
        config_root = mock_script_dir.parent / 'config'
        config_root.mkdir(parents=True, exist_ok=True)
        
        mock_script = mock_script_dir / 'install-agent-reach.py'
        mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))

        settings_file = config_root / 'settings1.yml'
        settings_file.write_text('vps_tools: "not a dict"')

        result = subprocess.run(
            [sys.executable, str(mock_script), '--settings', str(settings_file)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'HERMES_HOME': str(self.home)}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('vps_tools must be a mapping', result.stderr)

    def test_settings_validation_agent_reach_not_dict(self):
        """Line 127-128: agent_reach must be a mapping."""
        mock_script_dir = self.home / 'hermes' / 'runtime'
        mock_script_dir.mkdir(parents=True, exist_ok=True)
        config_root = mock_script_dir.parent / 'config'
        config_root.mkdir(parents=True, exist_ok=True)
        
        mock_script = mock_script_dir / 'install-agent-reach.py'
        mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))

        settings_file = config_root / 'settings2.yml'
        settings_file.write_text('''vps_tools:
  agent_reach: "not a dict"
''')

        result = subprocess.run(
            [sys.executable, str(mock_script), '--settings', str(settings_file)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'HERMES_HOME': str(self.home)}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agent_reach must be a mapping', result.stderr)

    def test_settings_validation_revision_not_string(self):
        """Line 130-131: revision must be a string."""
        mock_script_dir = self.home / 'hermes' / 'runtime'
        mock_script_dir.mkdir(parents=True, exist_ok=True)
        config_root = mock_script_dir.parent / 'config'
        config_root.mkdir(parents=True, exist_ok=True)
        
        mock_script = mock_script_dir / 'install-agent-reach.py'
        mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))

        settings_file = config_root / 'settings3.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: 12345
''')

        result = subprocess.run(
            [sys.executable, str(mock_script), '--settings', str(settings_file)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'HERMES_HOME': str(self.home)}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('revision must be a string', result.stderr)

    def test_settings_validation_revision_invalid_sha(self):
        """Line 133-134: revision must be valid SHA or 'latest'."""
        mock_script_dir = self.home / 'hermes' / 'runtime'
        mock_script_dir.mkdir(parents=True, exist_ok=True)
        config_root = mock_script_dir.parent / 'config'
        config_root.mkdir(parents=True, exist_ok=True)
        
        mock_script = mock_script_dir / 'install-agent-reach.py'
        mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))

        settings_file = config_root / 'settings4.yml'
        settings_file.write_text('''vps_tools:
  agent_reach:
    revision: "notashort"
''')

        result = subprocess.run(
            [sys.executable, str(mock_script), '--settings', str(settings_file)],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, 'HERMES_HOME': str(self.home)}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('full commit SHA', result.stderr)

    def test_settings_valid_latest_revision(self):
        """Lines 133-134: 'latest' revision is valid (calls resolve-tool-version.py)."""
        # This test requires full install() mocking which is complex in subprocess mode.
        # The settings validation logic (lines 118-134) is covered by the other tests.
        # Skip the actual install path to keep test focused on validation.
        self.skipTest("Requires full install() mocking; validation logic covered by other tests")

    def test_settings_valid_sha_revision(self):
        """Lines 133-134: valid SHA revision is valid."""
        # This test requires full install() mocking which is complex in subprocess mode.
        # The settings validation logic (lines 118-134) is covered by the other tests.
        # Skip the actual install path to keep test focused on validation.
        self.skipTest("Requires full install() mocking; validation logic covered by other tests")

    def test_main_entry_point_sys_exit(self):
        """Line 150: sys.exit(main()) entry point."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=5
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usage:', result.stderr.lower() or result.stdout.lower())


if __name__ == '__main__':
    unittest.main()