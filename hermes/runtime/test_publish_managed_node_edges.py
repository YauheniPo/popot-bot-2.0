"""Edge cases for publishing the managed Node/npm wrappers."""

import contextlib
import io
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'publish-managed-node.py'


def store(home: Path):
    node = home / 'store/node/bin/node'
    npm = home / 'store/npm'
    node.parent.mkdir(parents=True)
    node.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    node.chmod(0o755)
    for name in ('npm', 'npx'):
        cli = npm / f'lib/node_modules/npm/bin/{name}-cli.js'
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.touch()
    packages = {'node': SimpleNamespace(binary=node), 'npm': SimpleNamespace(path=npm)}
    return SimpleNamespace(installed_package=packages.get), npm


class PublishManagedNodeEdgeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.module = runpy.run_path(str(SCRIPT))
        self.pm, self.npm = store(self.home)

    def publish(self):
        with mock.patch.dict(sys.modules, {'pm': self.pm}):
            return self.module['publish'](self.home)

    def test_missing_npm_cli_entries_are_reported(self):
        for name in ('npm', 'npx'):
            with self.subTest(entry=name):
                cli = self.npm / f'lib/node_modules/npm/bin/{name}-cli.js'
                cli.unlink()
                with self.assertRaisesRegex(RuntimeError, f'PM {name} entry is missing'):
                    self.publish()
                cli.touch()

    def test_symlinked_prefix_is_rejected_before_writing(self):
        (self.home / 'node').symlink_to(self.home / 'store')
        with self.assertRaisesRegex(RuntimeError, 'Managed Node prefix must not be a symlink'):
            self.publish()
        self.assertFalse((self.home / 'store/bin').exists())

    def test_symlinked_bin_directory_is_rejected(self):
        (self.home / 'node').mkdir()
        (self.home / 'node/bin').symlink_to(self.home / 'store')
        with self.assertRaisesRegex(RuntimeError, 'Managed Node prefix must not be a symlink'):
            self.publish()

    def test_loose_wrapper_mode_is_repaired_without_rewriting(self):
        self.assertTrue(self.publish())
        wrapper = self.home / 'node/bin/npm'
        wrapper.chmod(0o644)
        stamp = wrapper.stat().st_mtime_ns
        self.assertTrue(self.publish())
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o750)
        self.assertEqual(wrapper.stat().st_mtime_ns, stamp)

    def test_cli_reports_changed_then_unchanged(self):
        outputs = []
        for _ in range(2):
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {'pm': self.pm}), \
                    mock.patch.object(sys, 'argv', ['publish-managed-node.py', '--hermes-home', str(self.home)]), \
                    contextlib.redirect_stdout(output):
                runpy.run_path(str(SCRIPT), run_name='__main__')
            outputs.append(output.getvalue().strip())
        self.assertEqual(outputs, ['changed', 'unchanged'])


if __name__ == '__main__':
    unittest.main()
