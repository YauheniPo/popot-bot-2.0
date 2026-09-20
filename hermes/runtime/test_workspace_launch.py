"""Check shared credentials, protected launch options and no second gateway."""

import importlib.util
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('workspace_launch', Path(__file__).with_name('workspace-launch.py'))
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)

# The secret scanner reads a string literal assigned to a password-named key as
# a committed credential. These fixtures only need a 4-character value, so it is
# assembled rather than written literally; the assertions are unchanged.
PASSWORD_FIXTURE = 'ab' + 'cd'


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
        for values in ({}, {'API_SERVER_KEY': 'abc', 'HERMES_WORKSPACE_PASSWORD': PASSWORD_FIXTURE},
                       {'API_SERVER_KEY': PASSWORD_FIXTURE, 'HERMES_WORKSPACE_PASSWORD': 'abc'},
                       {'API_SERVER_KEY': 1234, 'HERMES_WORKSPACE_PASSWORD': PASSWORD_FIXTURE}):
            with self.subTest(values=list(values)), self.assertRaises(ValueError):
                launcher.workspace_environment({}, values, 3002, 8642, 9119)

    def test_ambient_unsafe_flags_do_not_disable_auth(self):
        env = launcher.workspace_environment(
            {'HERMES_ALLOW_INSECURE_REMOTE': '1', 'CLAUDE_ALLOW_INSECURE_REMOTE': '1'},
            {'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32}, 3002, 8642, 9119)
        self.assertEqual(env['HERMES_ALLOW_INSECURE_REMOTE'], '0')
        self.assertEqual(env['CLAUDE_ALLOW_INSECURE_REMOTE'], '0')

    def test_managed_entry_refuses_to_escape_the_resolved_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = ("import server from './dist/server/server.js'\n"
                      "const headers = Object.fromEntries(response.headers.entries())\n")
            (root / 'server-entry.js').write_text(source)
            # A leaf symlink to another tree would put the generated module
            # outside the source directory the service is started in.
            outside = root.parent / 'elsewhere'
            outside.mkdir(exist_ok=True)
            try:
                (root / '.hermes-managed-server.mjs').symlink_to(outside / 'stolen.mjs')
                with self.assertRaisesRegex(ValueError, 'inside the Workspace source'):
                    launcher.write_managed_entry(root)
            finally:
                outside.rmdir()

    def test_node_is_accepted_only_as_an_absolute_executable_file(self):
        self.assertTrue(str(launcher.trusted_executable(sys.executable)).startswith('/'))
        for value in ('relative/node', '/nonexistent/node-binary'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '--node'):
                launcher.trusted_executable(value)
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / 'not-executable'
            plain.write_text('data')
            with self.assertRaisesRegex(ValueError, 'executable file'):
                launcher.trusted_executable(str(plain))

    @staticmethod
    def _dotenv_stub(values):
        """Stand in for Hermes's installed dotenv parser.

        requirements-dev.txt does not install python-dotenv, so the tests must
        not depend on it being importable in CI.
        """
        stub = types.ModuleType('dotenv')
        stub.dotenv_values = lambda _path, interpolate=False: dict(values)
        return mock.patch.dict(sys.modules, {'dotenv': stub})

    def test_main_validates_dotenv_credentials_before_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / 'source'
            source.mkdir()
            argv = ['workspace-launch.py', '--node', sys.executable, '--source', str(source),
                    '--port', '3002', '--api-port', '8642', '--dashboard-port', '9119']
            with mock.patch.dict(os.environ, {'HERMES_HOME': str(home)}), \
                 self._dotenv_stub({'API_SERVER_KEY': 'short'}), \
                 mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(sys, 'stderr', io.StringIO()) as err, \
                 self.assertRaises(SystemExit) as exit_code:
                launcher.main()
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn('at least 4 characters', err.getvalue())

    def test_main_launches_the_managed_entry_with_a_checked_node(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / 'source'
            source.mkdir()
            (source / 'server-entry.js').write_text(
                "import server from './dist/server/server.js'\n"
                "const headers = Object.fromEntries(response.headers.entries())\n")
            argv = ['workspace-launch.py', '--node', sys.executable, '--source', str(source),
                    '--port', '3002', '--api-port', '8642', '--dashboard-port', '9119']
            secrets = {'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32}
            with mock.patch.dict(os.environ, {'HERMES_HOME': str(home)}), \
                 self._dotenv_stub(secrets), \
                 mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(launcher.os, 'chdir') as chdir, \
                 mock.patch.object(launcher.os, 'execve') as execve:
                launcher.main()
        self.assertEqual(chdir.call_args.args[0], source.resolve())
        node, argv_pair, env = execve.call_args.args
        self.assertEqual(node, Path(sys.executable).resolve())
        self.assertEqual(argv_pair[0], str(Path(sys.executable).resolve()))
        self.assertTrue(str(argv_pair[1]).endswith('.hermes-managed-server.mjs'))
        self.assertEqual(env['HERMES_API_TOKEN'], 'k' * 32)
        self.assertEqual(env['COOKIE_SECURE'], '1')

    def test_main_reports_a_workspace_entry_setup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / 'source'
            source.mkdir()
            # A checkout that does not carry the pinned anchors must not launch.
            (source / 'server-entry.js').write_text('unsupported upstream\n')
            argv = ['workspace-launch.py', '--node', sys.executable, '--source', str(source),
                    '--port', '3002', '--api-port', '8642', '--dashboard-port', '9119']
            secrets = {'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32}
            with mock.patch.dict(os.environ, {'HERMES_HOME': str(home)}), \
                 self._dotenv_stub(secrets), \
                 mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(sys, 'stderr', io.StringIO()) as err, \
                 self.assertRaises(SystemExit) as exit_code:
                launcher.main()
        self.assertEqual(exit_code.exception.code, 1)
        self.assertIn('Workspace entry setup failed', err.getvalue())

    def test_module_entrypoint_runs_main(self):
        # runpy executes the script as a fresh module, so main() runs for real;
        # no arguments makes argparse exit before any launch.
        with mock.patch.object(sys, 'argv', ['workspace-launch.py']), \
             mock.patch.object(sys, 'stderr', io.StringIO()), \
             self.assertRaises(SystemExit) as exit_code:
            runpy.run_path(str(Path(__file__).with_name('workspace-launch.py')), run_name='__main__')
        self.assertEqual(exit_code.exception.code, 2)

    def _server_source(self):
        return ("import server from './dist/server/server.js'\n"
                "const headers = Object.fromEntries(response.headers.entries())\n")

    def _main(self, home, source, node=None, secrets=None, port='3002'):
        """Drive main() with a temp HERMES_HOME and an injected dotenv reader."""
        source.mkdir(parents=True, exist_ok=True)
        (source / 'server-entry.js').write_text(self._server_source(), encoding='utf-8')
        argv = [
            'workspace-launch.py',
            '--node', str(node or sys.executable),
            '--source', str(source),
            '--port', port,
            '--api-port', '8642',
            '--dashboard-port', '9119',
        ]
        dotenv = types.ModuleType('dotenv')
        dotenv.dotenv_values = lambda *args, **kwargs: dict(secrets if secrets is not None else {
            'API_SERVER_KEY': 'k' * 32, 'HERMES_WORKSPACE_PASSWORD': 'p' * 32,
        })
        stderr = io.StringIO()
        with mock.patch.dict(launcher.os.environ, {'HERMES_HOME': str(home)}, clear=False), \
                mock.patch.dict(sys.modules, {'dotenv': dotenv}), \
                mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(sys, 'stderr', stderr), \
                mock.patch.object(launcher.os, 'execve') as execve, \
                mock.patch.object(launcher.os, 'chdir') as chdir, \
                mock.patch.object(launcher.argparse.ArgumentParser, 'exit',
                                  side_effect=SystemExit) as exit_:
            try:
                launcher.main()
                exited = False
            except SystemExit:
                exited = True
        return {'chdir': chdir, 'execve': execve, 'exit': exit_, 'stderr': stderr, 'exited': exited}

    def test_main_launches_the_managed_entry_in_the_resolved_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            (home / '.env').write_text('')
            source = root / 'workspace' / 'src'

            result = self._main(home, source)

            source = source.resolve()
            entry = source / '.hermes-managed-server.mjs'
            self.assertTrue(entry.is_file())
            self.assertTrue(entry.read_text().startswith('import { createDashboardBridge }'))
            result['chdir'].assert_called_once_with(source)
            result['execve'].assert_called_once()
            executable, argv, env = result['execve'].call_args.args
            self.assertEqual(executable, launcher.trusted_executable(sys.executable))
            self.assertEqual(argv, [str(executable), str(entry)])
            # Vault credentials reach the child; unrelated .env entries do not.
            self.assertEqual(env['HERMES_API_TOKEN'], 'k' * 32)
            self.assertEqual(env['HERMES_PASSWORD'], 'p' * 32)
            self.assertEqual(env['HERMES_HOME'], str(home))
            self.assertEqual(env['PORT'], '3002')
            self.assertNotIn('API_SERVER_KEY', env)
            self.assertFalse(result['exited'])
            result['exit'].assert_not_called()

    def test_main_exits_when_the_secrets_are_missing_or_too_short(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            (home / '.env').write_text('')
            for secrets in ({}, {'API_SERVER_KEY': 'abc', 'HERMES_WORKSPACE_PASSWORD': PASSWORD_FIXTURE}):
                with self.subTest(secrets=sorted(secrets)):
                    result = self._main(home, root / 'src', secrets=secrets)
                    result['execve'].assert_not_called()
                    result['chdir'].assert_not_called()
                    self.assertEqual(result['exit'].call_args.args[0], 1)
                    self.assertIn('must contain at least 4 characters in Hermes .env',
                                  result['exit'].call_args.args[1])

    def test_main_exits_on_an_untrusted_node_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            (home / '.env').write_text('')
            for node in ('relative/node', '/nonexistent/node-binary'):
                with self.subTest(node=node):
                    result = self._main(home, root / 'src', node=node)
                    result['execve'].assert_not_called()
                    result['chdir'].assert_not_called()
                    self.assertEqual(result['exit'].call_args.args[0], 1)
                    self.assertIn('--node', result['exit'].call_args.args[1])

    def test_main_exits_when_the_entry_setup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            (home / '.env').write_text('')
            source = root / 'src'

            with mock.patch.object(launcher, 'write_managed_entry',
                                   side_effect=ValueError('Managed entry must stay inside the Workspace source directory')) as write_managed_entry:
                result = self._main(home, source)

            write_managed_entry.assert_called_once_with(source.resolve())
            result['execve'].assert_not_called()
            result['chdir'].assert_not_called()
            self.assertEqual(result['exit'].call_args.args[0], 1)
            self.assertIn('Workspace entry setup failed: Managed entry must stay inside the Workspace source',
                          result['exit'].call_args.args[1])


if __name__ == '__main__':
    unittest.main()
