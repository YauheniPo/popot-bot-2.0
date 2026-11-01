"""Exercise the native ownership repair without touching real Hermes data."""

import contextlib
import grp
import importlib.util
import io
import os
from pathlib import Path
import pwd
import runpy
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


HELPER = Path(__file__).with_name('normalize-ownership.py')


def load_helper():
    """Import the helper file into this process so coverage sees its lines."""
    spec = importlib.util.spec_from_file_location('normalize_ownership', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call_main(root, user=None, group=None, argv_override=None):
    helper = load_helper()
    argv = argv_override if argv_override is not None else [
        str(HELPER), str(root),
        user or pwd.getpwuid(os.getuid()).pw_name,
        group or grp.getgrgid(os.getgid()).gr_name,
    ]
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        with mock.patch.object(sys, 'argv', argv):
            try:
                helper.main()
                code = 0
            except SystemExit as error:
                code = error.code
    return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


class NormalizeOwnershipTests(unittest.TestCase):
    def run_helper(self, root):
        return call_main(root)

    def second_group(self):
        groups = [gid for gid in os.getgroups() if gid != os.getgid()]
        if not groups and os.geteuid() == 0:
            groups = [group.gr_gid for group in grp.getgrall() if group.gr_gid != os.getgid()]
        if not groups:
            self.skipTest('A second permitted group is needed to create ownership drift')
        return groups[0]

    def test_correct_tree_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chown(directory, -1, os.getgid())
            path = Path(directory) / 'file'
            path.write_text('unchanged')
            os.chown(path, -1, os.getgid())
            metadata = path.stat()
            result = self.run_helper(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ownership-changed=false')
            self.assertEqual(path.stat().st_ctime_ns, metadata.st_ctime_ns)

    def test_repairs_nested_files_and_links_without_following_targets(self):
        groups = [gid for gid in os.getgroups() if gid != os.getgid()]
        if not groups and os.geteuid() == 0:
            groups = [group.gr_gid for group in grp.getgrall() if group.gr_gid != os.getgid()]
        if not groups:
            self.skipTest('A second permitted group is needed to create ownership drift')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed, outside = root / 'home', root / 'outside'
            (managed / 'nested').mkdir(parents=True)
            outside.mkdir()
            (managed / 'nested/file').write_text('keep')
            (outside / 'file').write_text('outside')
            (managed / 'file-link').symlink_to(outside / 'file')
            (managed / 'directory-link').symlink_to(outside, target_is_directory=True)
            paths = [managed, *managed.rglob('*'), outside, outside / 'file']
            for path in paths:
                os.chown(path, -1, groups[0], follow_symlinks=False)
            result = self.run_helper(managed)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ownership-changed=true')
            for path in paths[:-2]:
                self.assertEqual(path.lstat().st_gid, os.getgid(), path)
            self.assertEqual(outside.stat().st_gid, groups[0])
            self.assertEqual((outside / 'file').stat().st_gid, groups[0])
            repeated = self.run_helper(managed)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(repeated.stdout.strip(), 'ownership-changed=false')

    def test_failed_scan_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_helper(Path(directory) / 'missing')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('ownership-changed=false', result.stdout)

    def test_root_symlink_is_resolved_before_repair(self):
        # S8707 hardening: the root is canonicalized with realpath, so the
        # traversal repairs the real directory even when the root is reached
        # through a symlink, and the link itself is never rewritten.
        drift = self.second_group()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / 'real'
            (real / 'nested').mkdir(parents=True)
            (real / 'nested/file').write_text('keep')
            os.chown(real, -1, drift, follow_symlinks=False)
            os.chown(real / 'nested', -1, drift, follow_symlinks=False)
            os.chown(real / 'nested/file', -1, drift, follow_symlinks=False)
            link = base / 'link'
            link.symlink_to(real, target_is_directory=True)
            result = self.run_helper(link)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ownership-changed=true')
            self.assertEqual(real.stat().st_gid, os.getgid())
            self.assertEqual((real / 'nested').stat().st_gid, os.getgid())
            self.assertEqual((real / 'nested/file').stat().st_gid, os.getgid())
            self.assertEqual(link.lstat().st_gid, os.getgid())

    def test_rejects_option_like_owner_arguments_before_spawning_children(self):
        # S8705 hardening: owner arguments reach find/chown unvalidated, so a
        # value such as 'a:b' or 'a b' is handed to the child processes.
        # The helper must reject them itself before any child runs.
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        for operand in ('user', 'group'):
            for value in ('a:b', 'a b', 'a$b', 'a;b'):
                with self.subTest(operand=operand, value=value):
                    argv = [str(HELPER), '/tmp', user, group]
                    argv[2 if operand == 'user' else 3] = value
                    result = call_main(None, argv_override=argv)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('ownership-changed=', result.stdout)
                    self.assertNotIn('find:', result.stderr)

    def test_unknown_owner_operand_is_rejected(self):
        result = call_main(None, argv_override=[
            str(HELPER), '/tmp', 'no_such_user_zzz', grp.getgrgid(os.getgid()).gr_name])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown owner operand', result.stderr)
        self.assertNotIn('ownership-changed=', result.stdout)

    def test_repair_reports_lstat_failures(self):
        helper = load_helper()
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file'
            path.write_text('keep')
            repaired = helper.repair(str(path / 'child'), os.getuid(), os.getgid(), errors)
        self.assertFalse(repaired)
        self.assertEqual(len(errors), 1)
        self.assertIn(str(path / 'child'), errors[0])

    def test_repair_reports_success_when_chown_succeeds(self):
        # A no-op ownership change (-1, -1) is permitted for non-root users,
        # so the successful os.lchown path can be exercised deterministically.
        helper = load_helper()
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file'
            path.write_text('keep')
            repaired = helper.repair(str(path), -1, -1, errors)
        self.assertTrue(repaired)
        self.assertEqual(errors, [])

    def test_repair_does_not_spawn_external_find_or_chown(self):
        # The repair must stay inside the interpreter: argv values used to
        # reach find/chown (Sonar S8705). Any external child would be
        # observable through PATH shims that mark their invocation.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file').write_text('unchanged')
            probe = root / 'probe'
            for name in ('find', 'chown'):
                shim = root / name
                shim.write_text(f'#!/bin/sh\ntouch "{probe}"\nexit 0\n')
                shim.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': str(root) + os.pathsep + os.environ['PATH']}):
                result = self.run_helper(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ownership-changed=false')
            self.assertFalse(probe.exists(), 'repair must not spawn external find/chown')

    def test_failed_chown_is_not_hidden_by_other_repairs(self):
        if os.geteuid() == 0:
            self.skipTest('requires a non-root user so chown fails with EPERM')
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'file').write_text('keep')
            # An impossible owner id selects every entry, while chown as a
            # non-root user cannot apply it: the failure must be reported.
            impossible = '2147483646'
            result = call_main(directory, user=impossible, group=impossible)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('ownership-changed=', result.stdout)

    def test_module_entry_point_runs_main(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chown(directory, -1, os.getgid())
            argv = [str(HELPER), str(directory),
                    pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with mock.patch.object(sys, 'argv', argv):
                    runpy.run_path(str(HELPER), run_name='__main__')
            self.assertEqual(stdout.getvalue().strip(), 'ownership-changed=false')
            self.assertEqual(stderr.getvalue(), '')
