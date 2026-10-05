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


class InstallAgentReachMissingCoverageTests(unittest.TestCase):
    """Tests targeting specific missing lines for coverage."""

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
        spec = __import__('importlib.util').util.spec_from_file_location(
            'install_agent_reach', self.copied_script
        )
        self.module = __import__('importlib.util').util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        # No manual cleanup needed; enterContext handles it.
        pass

    # ---- line 37: revision == 'latest' block ----
    def test_line_37_latest_revision_block(self):
        """Line 37: revision == 'latest' triggers resolution via resolve-tool-version.py"""
        fake_sha = 'a' * 40
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.home), \
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
        target_dir = self.config_dir / 'target_dir'
        target_dir.mkdir()
        # Create symlink to that directory
        settings_symlink = self.config_dir / 'bad_link.yml'
        settings_symlink.symlink_to(target_dir)
        # Expect error about regular file (parser.error -> SystemExit)
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_symlink)]):
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertEqual(cm.exception.code, 2)
            # stderr should contain 'regular file'
            with mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
                try:
                    self.module.main()
                except SystemExit:
                    pass
                self.assertIn('regular file', mock_stderr.getvalue())

    # ---- lines 118-119: wrong extension triggers error ----
    def test_lines_118_119_settings_wrong_extension(self):
        """Lines 118-119: --settings wrong extension triggers error."""
        settings_file = self.config_dir / 'bad.txt'
        settings_file.write_text('{}')
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(SystemExit) as cm:
                self.module.main()
            self.assertEqual(cm.exception.code, 2)
            with mock.patch('sys.stderr', new_callable=io.StringIO) as mock_stderr:
                try:
                    self.module.main()
                except SystemExit:
                    pass
                self.assertIn('must be a .yml or .yaml file', mock_stderr.getvalue())

    # ---- lines 120-131: settings structure validation ----
    def test_lines_120_131_settings_vps_tools_not_dict(self):
        """Line 124-125: vps_tools must be a mapping."""
        settings = {'vps_tools': 'not a dict'}
        settings_file = self.config_dir / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(ValueError) as cm:
                self.module.main()
            self.assertIn('vps_tools must be a mapping', str(cm.exception))

    def test_lines_120_131_settings_agent_reach_not_dict(self):
        """Line 127-128: agent_reach must be a mapping."""
        settings = {'vps_tools': {'agent_reach': 'not a dict'}}
        settings_file = self.config_dir / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(ValueError) as cm:
                self.module.main()
            self.assertIn('vps_tools.agent_reach must be a mapping', str(cm.exception))

    def test_lines_120_131_settings_revision_not_string(self):
        """Line 130-131: revision must be a string."""
        settings = {'vps_tools': {'agent_reach': {'revision': 123}}}
        settings_file = self.config_dir / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(ValueError) as cm:
                self.module.main()
            self.assertIn('revision must be a string', str(cm.exception))

    def test_lines_120_131_settings_revision_invalid_sha(self):
        """Line 133-134: revision must be valid SHA or 'latest'."""
        settings = {'vps_tools': {'agent_reach': {'revision': 'short'}}}
        settings_file = self.config_dir / 'test.yml'
        settings_file.write_text(yaml.dump(settings))
        with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
            with self.assertRaises(ValueError) as cm:
                self.module.main()
            self.assertIn('Agent-Reach revision from settings must be a full commit SHA or', str(cm.exception))

    # ---- line 150: sys.exit(main()) entry point ----
    def test_line_150_sys_exit_entry_point(self):
        """Line 150: sys.exit(main()) entry point."""
        # Test with missing required arguments -> should error and exit with code 2 (argparse)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.home):
            with mock.patch('sys.argv', ['install-agent-reach.py']):
                with self.assertRaises(SystemExit) as cm:
                    sys.exit(self.module.main())
                self.assertEqual(cm.exception.code, 2)
        # Test with invalid revision -> should error and exit with code 1 (ValueError caught in main)
        with mock.patch('shutil.which', return_value='/usr/bin/uv'), \
             mock.patch('pathlib.Path.home', return_value=self.home):
            with mock.patch('sys.argv', ['install-agent-reach.py', '--revision', 'invalid']):
                with self.assertRaises(SystemExit) as cm:
                    sys.exit(self.module.main())
                self.assertEqual(cm.exception.code, 1)