"""Exercise the native ownership repair without touching real Hermes data."""

import grp
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).with_name('normalize-ownership.py')


class NormalizeOwnershipTests(unittest.TestCase):
    def run_helper(self, root):
        return subprocess.run([sys.executable, str(HELPER), str(root),
                               pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name],
                              capture_output=True, text=True, timeout=10)

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

    def test_rejects_option_like_owner_arguments_before_spawning_children(self):
        # S8705 hardening: owner arguments reach find/chown unvalidated, so a
        # value such as 'a:b' or 'a b' is handed to the child processes.
        # The helper must reject them itself before any child runs.
        group = grp.getgrgid(os.getgid()).gr_name
        for value in ('a:b', 'a b', 'a$b', 'a;b'):
            with self.subTest(value=value):
                result = subprocess.run(
                    [sys.executable, str(HELPER), '/tmp', value, group],
                    capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('ownership-changed=', result.stdout)
                self.assertNotIn('find:', result.stderr)

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
            result = subprocess.run(
                [sys.executable, str(HELPER), str(root),
                 pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name],
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH']},
                capture_output=True, text=True, timeout=10)
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
            result = subprocess.run(
                [sys.executable, str(HELPER), str(directory), impossible, impossible],
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('ownership-changed=', result.stdout)
