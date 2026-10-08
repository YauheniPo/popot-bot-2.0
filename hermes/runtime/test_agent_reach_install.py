"""Managed CLI installation: isolation, repeat runs and failures."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name('install-agent-reach.py')


class AgentReachInstallTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('agent_reach_install', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.pin = 'a' * 40
        self.url = f'https://github.com/Panniantong/Agent-Reach/archive/{self.pin}.zip'
        self.venv = self.home / '.local/share/hermes-tools/agent-reach'

    def provision(self):
        (self.venv / 'bin').mkdir(parents=True, exist_ok=True)
        for name in ('python', 'agent-reach', 'yt-dlp'):
            (self.venv / 'bin' / name).write_text('fixture')

    def test_first_install_and_repeat_do_not_reinstall(self):
        installed = False
        def run(args, **kwargs):
            nonlocal installed
            if args[1:3] == ['venv', '--python']:
                self.provision()
            if args[1:3] == ['pip', 'install']:
                installed = True
            if '-c' in args:
                if 'VERSIONS' in args[2] or 'installed_versions' in args[2] or 'distributions' in args[2]:
                    return json.dumps({'yt-dlp': '1.0'})
                if 'METADATA' in args[2] or 'direct_url.json' in args[2]:
                    return json.dumps({'url': self.url} if installed else {})
            return ''
        with mock.patch.object(self.module, 'run', side_effect=run) as runner:
            self.assertTrue(self.module.install(self.home, self.pin, 'uv'))
            self.assertFalse(self.module.install(self.home, self.pin, 'uv'))
        installs = [call.args[0] for call in runner.call_args_list
                    if call.args[0][1:3] == ['pip', 'install']]
        self.assertEqual(len(installs), 2)
        self.assertTrue(all('--upgrade' in args for args in installs))
        self.assertNotIn('--reinstall-package', installs[1])
        self.assertIn(self.url, installs[0])
        self.assertIn(str(self.venv / 'bin/python'), installs[0])
        for name in ('agent-reach', 'yt-dlp'):
            self.assertEqual((self.home / '.local/bin' / name).resolve(), self.venv / 'bin' / name)
        self.assertFalse(any('--system' in call.args[0] for call in runner.call_args_list))

    def test_same_version_different_source_is_reinstalled(self):
        self.provision()
        with mock.patch.object(self.module, 'run', side_effect=[
                json.dumps({'url': 'https://wrong.test/pkg.zip'}),  # installed_from
                json.dumps({'yt-dlp': '1.0'}),  # installed_versions
                '',  # pip install
                json.dumps({'url': self.url}),  # installed_from (verify)
                json.dumps({'yt-dlp': '1.0'}),  # installed_versions
                '', '',  # version probes
                ]) as runner:
            self.assertTrue(self.module.install(self.home, self.pin, 'uv'))
        pip_calls = [call.args[0] for call in runner.call_args_list
                     if len(call.args[0]) >= 3 and call.args[0][1:3] == ['pip', 'install']]
        self.assertGreaterEqual(len(pip_calls), 1)
        self.assertIn('--reinstall-package', pip_calls[0])

    def test_failed_install_does_not_publish_launcher(self):
        self.provision()
        with mock.patch.object(self.module, 'run', side_effect=[
                '{}', RuntimeError('download failed')]):
            with self.assertRaisesRegex(RuntimeError, 'download failed'):
                self.module.install(self.home, self.pin, 'uv')
        self.assertFalse((self.home / '.local/bin/agent-reach').exists())

    def test_failed_environment_creation_does_not_publish_launcher(self):
        with mock.patch.object(self.module, 'run', side_effect=RuntimeError('download failed')):
            with self.assertRaisesRegex(RuntimeError, 'download failed'):
                self.module.install(self.home, self.pin, 'uv')
        self.assertFalse((self.home / '.local/bin/agent-reach').exists())

    def test_missing_dependency_command_is_repaired(self):
        self.provision()
        (self.venv / 'bin/yt-dlp').unlink()
        def run(args, **kwargs):
            if args[1:3] == ['pip', 'install']:
                self.provision()
            return json.dumps({'url': self.url}) if '-c' in args else ''
        with mock.patch.object(self.module, 'run', side_effect=run) as runner:
            self.assertTrue(self.module.install(self.home, self.pin, 'uv'))
        installs = [call.args[0] for call in runner.call_args_list
                    if call.args[0][1:3] == ['pip', 'install']]
        self.assertEqual(len(installs), 1)
        self.assertIn('yt-dlp', installs[0])

    def test_existing_unmanaged_launcher_is_preserved(self):
        target = self.home / '.local/bin/agent-reach'
        target.parent.mkdir(parents=True)
        target.write_text('personal tool')
        with self.assertRaisesRegex(RuntimeError, 'unmanaged launcher'):
            self.module.install(self.home, self.pin, 'uv')
        self.assertEqual(target.read_text(), 'personal tool')

    def test_invalid_revision_is_rejected_before_commands(self):
        with mock.patch.object(self.module, 'run') as runner:
            with self.assertRaises(ValueError):
                self.module.install(self.home, 'main', 'uv')
        runner.assert_not_called()

    def test_dependency_upgrade_is_reported_as_change(self):
        self.provision()
        with mock.patch.object(self.module, 'installed_versions', side_effect=[{'yt-dlp': 'old'}, {'yt-dlp': 'new'}]), mock.patch.object(self.module, 'run', side_effect=[json.dumps({'url': self.url}), '', json.dumps({'url': self.url}), '', '']):
            self.assertTrue(self.module.install(self.home, self.pin, 'uv'))

    def test_managed_uv_is_found_without_path_and_preferred_over_global(self):
        managed_home = self.home / 'custom-state'
        managed_uv = managed_home / 'bin/uv'
        managed_uv.parent.mkdir(parents=True)
        managed_uv.write_text('#!/bin/sh\nexit 0\n')
        managed_uv.chmod(0o755)
        for global_uv in (None, '/usr/local/bin/uv'):
            with self.subTest(global_uv=global_uv), mock.patch.object(self.module.shutil, 'which', return_value=global_uv):
                self.assertEqual(self.module.resolve_uv(managed_home), str(managed_uv))

    def test_missing_or_non_executable_managed_uv_uses_path(self):
        managed_home = self.home / '.hermes'
        with mock.patch.object(self.module.shutil, 'which', return_value='/usr/bin/uv'):
            self.assertEqual(self.module.resolve_uv(managed_home), '/usr/bin/uv')
            managed_uv = managed_home / 'bin/uv'
            managed_uv.parent.mkdir(parents=True)
            managed_uv.write_text('not executable')
            self.assertEqual(self.module.resolve_uv(managed_home), '/usr/bin/uv')

    def test_main_passes_custom_hermes_home_to_uv_resolution(self):
        custom_home = self.home / 'custom-state'
        with mock.patch.dict(os.environ, {'HERMES_HOME': str(custom_home)}), \
                mock.patch.object(self.module.Path, 'home', return_value=self.home), \
                mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--revision', self.pin]), \
                mock.patch.object(self.module, 'resolve_uv', return_value='/managed/uv') as resolver, \
                mock.patch.object(self.module, 'install', return_value=False) as installer:
            self.assertEqual(self.module.main(), 0)
        resolver.assert_called_once_with(custom_home)
        installer.assert_called_once_with(self.home, self.pin, '/managed/uv')

    def test_installed_false_when_python_missing(self):
        # installed_from should return False when python executable does not exist
        from pathlib import Path
        python = Path('/non/existent/python')
        url = 'http://example.com/test.zip'
        self.assertFalse(self.module.installed_from(python, url))

    def test_invalid_revision_raises(self):
        with mock.patch.object(self.module, 'run') as runner:
            with self.assertRaisesRegex(ValueError, 'full commit SHA'):
                self.module._validate_and_prepare(self.home, 'invalid')
        runner.assert_not_called()

    def test_check_launchers_raises_on_target_mismatch(self):
        # Test that _check_launchers raises RuntimeError when symlink points to wrong target
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            real_target = tmp_path / 'real'
            real_target.mkdir()
            wrong_target = tmp_path / 'wrong'
            wrong_target.mkdir()
            link = tmp_path / 'link'
            link.symlink_to(real_target)
            # Change the symlink target to wrong_target
            link.unlink()
            link.symlink_to(wrong_target)
            # Now call _check_launchers with link pointing to wrong_target but expecting real_target
            with self.assertRaises(RuntimeError) as cm:
                self.module._check_launchers({link: real_target})
            self.assertIn('unmanaged launcher already exists', str(cm.exception))

    def test_check_launchers_raises_on_unmanaged(self):
        # _check_launchers should raise RuntimeError if an unmanaged launcher exists
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            venv = home / '.local/share/hermes-tools/agent-reach'
            venv.mkdir(parents=True)
            bin_dir = venv / 'bin'
            bin_dir.mkdir(parents=True, exist_ok=True)
            python = bin_dir / 'python'
            python.write_text('#!/bin/sh\\necho python')
            launchers = {home / '.local/bin' / 'agent-reach': venv / 'bin' / 'agent-reach'}
            # Create a regular file at the launcher location (not a symlink)
            launcher = home / '.local/bin' / 'agent-reach'
            launcher.parent.mkdir(parents=True)
            launcher.write_text('not a symlink')
            with self.assertRaises(RuntimeError) as cm:
                self.module._check_launchers(launchers)
            self.assertIn('unmanaged launcher already exists', str(cm.exception))

    def test_verify_installation_raises_on_mismatch(self):
        # _verify_installation should raise RuntimeError if installed_from returns False
        from unittest import mock
        with mock.patch.object(self.module, 'installed_from', return_value=False):
            with self.assertRaises(RuntimeError) as cm:
                self.module._verify_installation('/fake/python', 'http://example.com/test.zip')
            self.assertIn('Agent-Reach installed source does not match the requested revision', str(cm.exception))

    def test_settings_path_outside_config_root_error(self):
        outside_settings = self.home / 'outside.yml'
        outside_settings.write_text('dummy')
        with mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--settings', str(outside_settings)]), \
                mock.patch.object(self.module, 'install') as install:
            with self.assertRaises(SystemExit) as error:
                self.module.main()
        self.assertEqual(error.exception.code, 2)
        install.assert_not_called()

    def test_validate_and_prepare_raises_on_sha_mismatch(self):
        with mock.patch.object(self.module, '_resolve_latest_revision', return_value='notasha'):
            with self.assertRaisesRegex(ValueError, 'full commit SHA'):
                self.module._validate_and_prepare(self.home, 'latest')

    def test_sys_exit_called_via_runpy(self):
        import runpy
        # runpy creates a fresh module: mock its external calls, not the old module's functions.
        def run(args, **kwargs):
            if args[1:3] == ['venv', '--python']:
                self.provision()
            if args[1:3] == ['-c', self.module.METADATA]:
                return mock.Mock(stdout=json.dumps({'url': self.url}))
            if args[1:3] == ['-c', self.module.VERSIONS]:
                return mock.Mock(stdout='{}')
            return mock.Mock(stdout='')
        with mock.patch.object(self.module.subprocess, 'run', side_effect=run), \
                mock.patch.object(self.module.shutil, 'which', return_value='/fake/uv'), \
                mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home / '.hermes')}), \
                mock.patch.object(self.module.sys, 'exit') as exit_call, \
                mock.patch.object(self.module.Path, 'home', return_value=self.home), \
                mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--revision', self.pin]):
            runpy.run_path(str(SCRIPT), run_name='__main__')
        exit_call.assert_called_once_with(0)
        self.assertTrue((self.home / '.local/bin/agent-reach').is_symlink())
