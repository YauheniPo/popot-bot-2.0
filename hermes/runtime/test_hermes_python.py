"""Managed Python chooses PM at launch time and preserves Python argument semantics."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class HermesPythonTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.launcher = self.home / 'python'
        shutil.copyfile(ROOT / 'hermes-python.py', self.launcher)
        self.repo = self.home / 'hermes-agent'
        self.env = {**os.environ, 'HERMES_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('HERMES_INSTALL_DIR', None)

    def call(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(self.launcher), *args], env=self.env,
                              capture_output=True, text=True, timeout=10, **kwargs)

    def legacy(self):
        binary = self.repo / 'venv/bin/python'
        binary.parent.mkdir(parents=True)
        binary.symlink_to(sys.executable)

    def managed(self):
        (self.repo / 'pm').mkdir(parents=True)
        (self.repo / 'pm/environments.py').touch()
        (self.repo / 'hermes_cli').mkdir()
        (self.repo / 'hermes_cli/__init__.py').touch()
        (self.repo / 'hermes_cli/_launchers.py').write_text('''import sys
from pathlib import Path
def resolve_store_python(root):
    return Path(sys.executable) if (root / 'ready').exists() else None
def runtime_command(root, args, *, code, python, home):
    # Exercise the bridge in another interpreter, as the real native launcher does.
    return [str(python), '-I', '-c', 'import runpy, sys; ' + code, *args]
''')
        (self.repo / 'ready').touch()

    def test_legacy_interpreter_is_available_for_pre_upgrade_backup(self):
        self.legacy()
        result = self.call('-c', 'import sys; print(sys.argv[1])', 'keep spaces')
        self.assertEqual((result.returncode, result.stdout), (0, 'keep spaces\n'), result.stderr)

    def test_managed_launch_handles_code_script_module_and_stdin(self):
        self.managed()
        source = 'import json, sys; print(json.dumps(sys.argv)); raise SystemExit(7)'
        script = self.home / 'script with spaces.py'
        script.write_text(source)
        for args, stdin, expected in (
            (('-c', source, 'a b'), None, ['-c', 'a b']),
            ((str(script), 'a b'), None, [str(script), 'a b']),
            (('-', 'a b'), source, ['-', 'a b']),
        ):
            with self.subTest(args=args):
                result = self.call(*args, input=stdin)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
        result = self.call('-m', 'json.tool', input='{"ok":true}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': True})

    def test_broken_pm_never_falls_back_to_stale_legacy_dependencies(self):
        self.legacy()
        self.managed()
        (self.repo / 'ready').unlink()
        result = self.call('-c', 'print("must not run")')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('runtime', result.stderr)

    def test_missing_runtime_fails_closed(self):
        result = self.call('-c', 'print("must not run")')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')


class ManagedNodeTests(unittest.TestCase):
    def test_wrappers_preserve_global_packages_and_repeat_without_changes(self):
        from unittest import mock
        from types import SimpleNamespace
        import runpy
        module = runpy.run_path(str(ROOT / 'publish-managed-node.py'))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            node = home / 'store/node/bin/node'
            npm = home / 'store/npm'
            node.parent.mkdir(parents=True)
            node.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            node.chmod(0o755)
            for name in ('npm', 'npx'):
                cli = npm / f'lib/node_modules/npm/bin/{name}-cli.js'
                cli.parent.mkdir(parents=True, exist_ok=True)
                cli.touch()
            user_package = home / 'node/lib/node_modules/private/package.json'
            user_package.parent.mkdir(parents=True)
            user_package.write_text('{"keep": true}')
            packages = {'node': SimpleNamespace(binary=node), 'npm': SimpleNamespace(path=npm)}
            with mock.patch.dict(sys.modules, {'pm': SimpleNamespace(installed_package=packages.get)}):
                self.assertTrue(module['publish'](home))
                before = {p: p.stat().st_mtime_ns for p in (home / 'node/bin').iterdir()}
                self.assertFalse(module['publish'](home))
                self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
                result = subprocess.run([str(home / 'node/bin/npm'), '--version'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [str(npm / 'lib/node_modules/npm/bin/npm-cli.js'), '--version'])
                self.assertEqual(user_package.read_text(), '{"keep": true}')
                packages.clear()
                with self.assertRaises(RuntimeError):
                    module['publish'](home)


if __name__ == '__main__':
    unittest.main()
