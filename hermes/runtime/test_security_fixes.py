"""Security contracts exercised through the managed CLI implementation."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml


class TestSecurityFixes(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = self.home / 'hermes/config'
        self.config.mkdir(parents=True)
        self.script = self.home / 'hermes/runtime/install-agent-reach.py'
        self.script.parent.mkdir()
        self.installer = self.load_module('install-agent-reach.py')
        self.resolver = self.load_module('resolve-tool-version.py')
        self.enterContext(mock.patch.object(self.installer, '__file__', str(self.script)))
        self.enterContext(mock.patch.object(self.installer.Path, 'home', return_value=self.home))
        self.enterContext(mock.patch.dict(os.environ, {'HERMES_HOME': str(self.home / '.hermes')}))

    def load_module(self, name):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_settings(self, data):
        settings = self.config / 'settings.yml'
        settings.write_text(yaml.safe_dump(data))
        with mock.patch.object(self.installer.sys, 'argv', [str(self.script), '--settings', str(settings)]):
            return self.installer.main()

    def test_revision_format_validation(self):
        for revision in ('main', '123456', 'f' * 64, '', 'latest-extra', 'g' * 40):
            with self.subTest(revision=revision), mock.patch.object(self.installer, 'run') as runner:
                with self.assertRaises(ValueError):
                    self.installer.install(self.home, revision, 'uv')
                runner.assert_not_called()

    def test_revision_type_validation(self):
        for revision in (12345, None, [], {}):
            with self.subTest(revision=revision), mock.patch.object(self.installer, 'install') as install:
                with self.assertRaisesRegex(ValueError, 'revision must be a string'):
                    self.run_settings({'vps_tools': {'agent_reach': {'revision': revision}}})
                install.assert_not_called()

    def test_settings_structure_validation(self):
        for data in (None, [], 'invalid', {}, {'vps_tools': []}, {'vps_tools': {'agent_reach': []}}):
            with self.subTest(data=data), mock.patch.object(self.installer, 'install') as install:
                with self.assertRaisesRegex(ValueError, 'mapping'):
                    self.run_settings(data)
                install.assert_not_called()

    def test_error_message_sanitization(self):
        secret = 'SYNTHETIC_INSTALLER_SECRET'
        error = subprocess.CalledProcessError(1, ['uv'], output=secret, stderr=secret)
        with mock.patch.object(self.installer.sys, 'argv', [str(self.script), '--revision', 'a' * 40]), \
                mock.patch.object(self.installer, 'resolve_uv', return_value='uv'), \
                mock.patch.object(self.installer, 'install', side_effect=error), \
                mock.patch.object(self.installer.sys, 'stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(self.installer.main(), 1)
            self.assertEqual(stderr.getvalue(), 'Agent-Reach installation failed\n')
            self.assertNotIn(secret, stderr.getvalue())

    def test_symlink_escape_prevention(self):
        outside = self.home / 'outside.yml'
        outside.write_text('vps_tools: {}')
        link = self.config / 'escape.yml'
        link.symlink_to(outside)
        with mock.patch.object(self.installer.sys, 'argv', [str(self.script), '--settings', str(link)]), \
                mock.patch.object(self.installer, 'install') as install, \
                mock.patch.object(self.installer.sys, 'stderr', new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                self.installer.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn('--settings path must be under', stderr.getvalue())
            install.assert_not_called()

    def test_response_size_capping(self):
        max_size = 5 * 1024 * 1024
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b' ' * (max_size + 1)
        with mock.patch.object(self.resolver, 'urlopen', return_value=response) as opener, \
                mock.patch.object(self.resolver.time, 'sleep'), \
                mock.patch.object(self.resolver.json, 'loads') as loads:
            with self.assertRaisesRegex(RuntimeError, 'latest version lookup failed'):
                self.resolver.fetch_json('https://example.invalid')
            self.assertEqual(opener.call_count, 3)
            response.__enter__.return_value.read.assert_called_with(max_size + 1)
            loads.assert_not_called()

    def test_uv_executable_validation(self):
        uv = self.home / '.hermes/bin/uv'
        uv.parent.mkdir(parents=True)
        uv.write_text('not executable')
        uv.chmod(0o644)
        with mock.patch.object(self.installer.shutil, 'which', return_value=None):
            self.assertIsNone(self.installer.resolve_uv(self.home / '.hermes'))
        uv.chmod(0o755)
        with mock.patch.object(self.installer.shutil, 'which', return_value='/other/uv'):
            self.assertEqual(self.installer.resolve_uv(self.home / '.hermes'), str(uv))

    def test_launcher_validation(self):
        launcher = self.home / '.local/bin/agent-reach'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('owner tool')
        with mock.patch.object(self.installer, 'run') as runner:
            with self.assertRaisesRegex(RuntimeError, 'unmanaged launcher'):
                self.installer.install(self.home, 'a' * 40, 'uv')
            runner.assert_not_called()
        self.assertEqual(launcher.read_text(), 'owner tool')

    def test_npm_dist_tags_must_be_a_mapping(self):
        for tags in (None, [], 'latest'):
            with self.subTest(tags=tags), mock.patch.object(self.resolver, 'fetch_json', return_value={'dist-tags': tags}):
                with self.assertRaises(ValueError):
                    self.resolver.resolve('agent-browser', 'latest')
