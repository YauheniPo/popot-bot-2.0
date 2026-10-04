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
        self.enterContext(mock.patch.object(self.module, 'installed_versions', return_value={}))
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
                json.dumps({'url': 'https://wrong.test/pkg.zip'}), '',
                json.dumps({'url': self.url}), '', '']) as runner:
            self.assertTrue(self.module.install(self.home, self.pin, 'uv'))
        self.assertIn('--reinstall-package', runner.call_args_list[1].args[0])

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
        with mock.patch.dict(os.environ, {'HERMES_HOME': str(custom_home)}), mock.patch.object(self.module.sys, 'argv', ['install-agent-reach.py', '--revision', self.pin]), mock.patch.object(self.module, 'resolve_uv', return_value='/managed/uv') as resolver, mock.patch.object(self.module, 'install', return_value=False):
            self.assertEqual(self.module.main(), 0)
        resolver.assert_called_once_with(custom_home)
