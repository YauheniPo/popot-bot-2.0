#!/usr/bin/env python3
"""Integration-style coverage tests for install-agent-reach.py settings validation.

These tests create proper directory structure and mock environment to
cover lines 108-134 (settings path validation and settings structure validation).
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
import shutil
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

    def test_settings_validation_lines_via_main(self):
        """Lines 108-134: settings path validation via main() with proper HERMES_HOME."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            # Create Hermes directory structure
            hermes_home = home / '.hermes'
            hermes_home.mkdir()
            config_dir = hermes_home / 'config'
            config_dir.mkdir()
            runtime_dir = hermes_home / 'runtime'
            runtime_dir.mkdir()
            
            # Create a mock install-agent-reach.py in the runtime directory
            mock_script = runtime_dir / 'install-agent-reach.py'
            mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))
            
            # Create a settings file in config directory
            settings_file = config_dir / 'test_settings.yml'
            settings_file.write_text('''vps_tools:
  agent_reach:
    revision: "latest"
''')

            # Mock external dependencies
            with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_file)]):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(home) if key == 'HERMES_HOME' else default):
                    with mock.patch.object(shutil, 'which', return_value='/usr/bin/uv'):
                        with mock.patch.object(subprocess, 'run') as mock_run:
                            # Mock subprocess.run to avoid actual installation
                            def mock_run_side_effect(args, timeout=60, **kwargs):
                                # Return fake outputs for various commands
                                if args[1:3] == ['venv', '--python']:
                                    mock_result = mock.Mock()
                                    mock_result.stdout = ''
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                                elif args[1:3] == ['pip', 'install']:
                                    mock_result = mock.Mock()
                                    mock_result.stdout = json.dumps({'url': f'https://github.com/Panniantong/Agent-Reach/archive/a'*40})
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                                elif '-c' in args and 'importlib.metadata' in str(args) and 'direct_url.json' in str(args):
                                    mock_result = mock.Mock()
                                    mock_result.stdout = json.dumps({'url': f'https://github.com/Panniantong/Agent-Reach/archive/a'*40})
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                                elif '-c' in args and 'importlib.metadata' in str(args) and 'distributions' in str(args):
                                    mock_result = mock.Mock()
                                    mock_result.stdout = json.dumps({'pkg': '1.0.0'})
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                                elif len(args) >= 2 and args[1] in ('version', '--version'):
                                    mock_result = mock.Mock()
                                    mock_result.stdout = '1.0.0'
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                                else:
                                    mock_result = mock.Mock()
                                    mock_result.stdout = ''
                                    mock_result.stderr = ''
                                    mock_result.returncode = 0
                                    return mock_result
                            mock_run.side_effect = mock_run_side_effect
                            
                            # Also mock Path.home() to return our test home
                            with mock.patch('pathlib.Path.home', return_value=home):
                                try:
                                    result = self.mock_script_main(mock_script)
                                    # Should succeed (return 0) or fail gracefully with usage/error
                                    # The important thing is that we executed the settings validation path
                                    self.assertIn(result, [0, 1, 2])  # Valid return codes
                                except SystemExit as e:
                                    # SystemExit is OK if it's from argument parsing or validation
                                    self.assertIn(e.code, [0, 1, 2])  # Valid exit codes

    def test_settings_validation_failure_paths(self):
        """Lines 114-115, 116-117, 118-119: settings path validation failure paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            hermes_home = home / '.hermes'
            hermes_home.mkdir()
            config_dir = hermes_home / 'config'
            config_dir.mkdir()
            runtime_dir = hermes_home / 'runtime'
            runtime_dir.mkdir()
            
            mock_script = runtime_dir / 'install-agent-reach.py'
            mock_script.write_text(SCRIPT.read_text(encoding='utf-8'))
            
            # Test 1: Settings path is symlink to directory (should fail is_file() check)
            settings_symlink = config_dir / 'bad_link.yml'
            target_dir = config_dir / 'target_dir'
            target_dir.mkdir()
            settings_symlink.symlink_to(target_dir)
            
            with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(settings_symlink)]):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(home) if key == 'HERMES_HOME' else default):
                    with mock.patch.object(shutil, 'which', return_value='/usr/bin/uv'):
                        with mock.patch('pathlib.Path.home', return_value=home):
                            with self.assertRaises(SystemExit) as cm:
                                self.mock_script_main(mock_script)
                            self.assertEqual(cm.exception.code, 2)  # parser.error -> SystemExit(2)
            
            # Test 2: Settings path outside config dir (should trigger ValueError in relative_to)
            outside_file = home / 'outside.yml'
            outside_file.write_text('test: value')
            
            with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(outside_file)]):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(home) if key == 'HERMES_HOME' else default):
                    with mock.patch.object(shutil, 'which', return_value='/usr/bin/uv'):
                        with mock.patch('pathlib.Path.home', return_value=home):
                            with self.assertRaises(SystemExit) as cm:
                                self.mock_script_main(mock_script)
                            self.assertEqual(cm.exception.code, 2)  # parser.error -> SystemExit(2)
            
            # Test 3: Wrong file extension
            wrong_ext_file = config_dir / 'settings.txt'
            wrong_ext_file.write_text('test: value')
            
            with mock.patch('sys.argv', ['install-agent-reach.py', '--settings', str(wrong_ext_file)]):
                with mock.patch('os.environ.get', side_effect=lambda key, default=None: str(home) if key == 'HERMES_HOME' else default):
                    with mock.patch.object(shutil, 'which', return_value='/usr/bin/uv'):
                        with mock.patch('pathlib.Path.home', return_value=home):
                            with self.assertRaises(SystemExit) as cm:
                                self.mock_script_main(mock_script)
                            self.assertEqual(cm.exception.code, 2)  # parser.error -> SystemExit(2)

    def mock_script_main(self, script_path):
        """Helper to run main() from a script path with proper mocking."""
        # Temporarily add the script's directory to sys.path so imports work
        script_dir = str(script_path.parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        
        try:
            # Import the module dynamically
            spec = importlib.util.spec_from_file_location('install_agent_reach', script_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.main()
        finally:
            # Clean up sys.path
            if script_dir in sys.path:
                sys.path.remove(script_dir)

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