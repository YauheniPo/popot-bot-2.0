"""Check shared credentials, protected launch options and no second gateway."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location('workspace_launch', Path(__file__).with_name('workspace-launch.py'))
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class WorkspaceLaunchTests(unittest.TestCase):
    def test_entry_is_generated_without_changing_upstream_or_following_leaf_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = ("import server from './dist/server/server.js'\n"
                      "const headers = Object.fromEntries(response.headers.entries())\n")
            original = root / 'server-entry.js'
            original.write_text(source)
            generated = root / '.hermes-managed-server.mjs'
            generated.symlink_to(original)
            self.assertEqual(launcher.write_managed_entry(root), generated)
            self.assertFalse(generated.is_symlink())
            self.assertEqual(original.read_text(), source)
            first = generated.read_text()
            launcher.write_managed_entry(root)
            self.assertEqual(generated.read_text(), first)
            self.assertIn('createDashboardBridge', first)

    def test_managed_entry_wraps_server_and_preserves_multiple_cookies(self):
        source = ("import server from './dist/server/server.js'\n"
                  "const headers = Object.fromEntries(response.headers.entries())\n")
        result = launcher.managed_entry(source, Path('/opt/runtime/workspace-dashboard-bridge.mjs'))
        self.assertIn('bridge.handle', result)
        self.assertIn('globalThis.fetch = bridge.fetch', result)
        self.assertIn('getSetCookie()', result)
        self.assertIn('await import', result)
        with self.assertRaisesRegex(ValueError, 'entry'):
            launcher.managed_entry('unsupported upstream', Path('/opt/runtime/bridge.mjs'))

    def test_shared_credentials_are_mapped_without_exporting_other_secrets(self):
        result = launcher.workspace_environment(
            {'PATH': '/bin', 'HERMES_HOME': '/srv/hermes'},
            {'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32,
             'TELEGRAM_BOT_TOKEN': 'must-not-export'}, 3002, 8642, 9119)
        self.assertEqual(result['HERMES_API_TOKEN'], 'k' * 32)
        self.assertEqual(result['HERMES_PASSWORD'], 'p' * 32)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', result)
        self.assertEqual(result['HOST'], '127.0.0.1')
        self.assertEqual(result['COOKIE_SECURE'], '1')
        self.assertEqual(result['HERMES_API_URL'], 'http://127.0.0.1:8642')
        self.assertEqual(result['HERMES_HOME'], '/srv/hermes')

    def test_four_character_credentials_preserve_arbitrary_characters(self):
        for value in ('1234', 'a!$#', 'я🔑字!', '    ', 'abcd ef'):
            with self.subTest(value=value):
                result = launcher.workspace_environment({}, {
                    'API_SERVER_KEY': value, 'HERMES_WORKSPACE_PASSWORD': value,
                }, 3002, 8642, 9119)
                self.assertEqual(result['HERMES_API_TOKEN'], value)
                self.assertEqual(result['HERMES_PASSWORD'], value)

    def test_missing_or_too_short_credentials_fail_closed_without_values(self):
        for values in ({}, {'API_SERVER_KEY': 'abc', 'HERMES_WORKSPACE_PASSWORD': 'abcd'},
                       {'API_SERVER_KEY': 'abcd', 'HERMES_WORKSPACE_PASSWORD': 'abc'},
                       {'API_SERVER_KEY': 1234, 'HERMES_WORKSPACE_PASSWORD': 'abcd'}):
            with self.subTest(values=list(values)), self.assertRaises(ValueError):
                launcher.workspace_environment({}, values, 3002, 8642, 9119)

    def test_ambient_unsafe_flags_do_not_disable_auth(self):
        env = launcher.workspace_environment(
            {'HERMES_ALLOW_INSECURE_REMOTE': '1', 'CLAUDE_ALLOW_INSECURE_REMOTE': '1'},
            {'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32}, 3002, 8642, 9119)
        self.assertEqual(env['HERMES_ALLOW_INSECURE_REMOTE'], '0')
        self.assertEqual(env['CLAUDE_ALLOW_INSECURE_REMOTE'], '0')


if __name__ == '__main__':
    unittest.main()
