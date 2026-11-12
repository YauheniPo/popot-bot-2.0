"""Safe file operations keep link and permission rules behind one interface."""

import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("safe_files", Path(__file__).with_name("safe_files.py"))
safe_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_files)


class SafeFilesTests(unittest.TestCase):
    def test_read_write_and_probe_share_the_same_file_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / "nested/state.json"
            self.assertIsNone(safe_files.read_optional(path))
            self.assertFalse(safe_files.regular_file(path))
            safe_files.write_atomic(path, "personal state")
            self.assertEqual(safe_files.read_optional(path), "personal state")
            self.assertTrue(safe_files.regular_file(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            safe_files.write_atomic(path, "updated", mode=0o640)
            self.assertEqual(safe_files.read_optional(path), "updated")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_read_and_probe_reject_links_and_nonregular_files(self):
        for kind in ("symlink", "dangling", "hardlink", "directory", "fifo", "parent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                victim = root / "victim"
                victim.write_text("untouched")
                path = root / "state"
                if kind == "hardlink":
                    os.link(victim, path)
                elif kind in ("symlink", "dangling"):
                    path.symlink_to(victim if kind == "symlink" else root / "missing")
                elif kind == "directory":
                    path.mkdir()
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    path.symlink_to(root, target_is_directory=True)
                    path = path / "victim"
                for operation in (safe_files.read_optional, safe_files.regular_file):
                    with self.assertRaises((OSError, safe_files.UnsafeFileError)):
                        operation(path)
                self.assertEqual(victim.read_text(), "untouched")

    def test_private_parent_permissions_do_not_follow_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / "state/backup"
            safe_files.ensure_private_parent(path)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                safe_files.ensure_private_parent(alias / "backup")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_all_operations_reject_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / "child/../state"
            for operation in (safe_files.read_optional, safe_files.regular_file,
                              safe_files.ensure_private_parent,
                              lambda target: safe_files.write_atomic(target, "state")):
                with self.assertRaises(safe_files.UnsafeFileError):
                    operation(path)
            self.assertFalse((root / "state").exists())

    def test_atomic_replace_is_not_redirected_by_parent_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "AGENTS.md"
            target.write_text("Original\n")
            outside = root / "outside"
            outside.mkdir()
            (outside / "AGENTS.md").write_text("Outside notes\n")
            moved = root / "original-workspace"
            original_replace = os.replace

            def swap_parent(*args, **kwargs):
                workspace.rename(moved)
                workspace.symlink_to(outside, target_is_directory=True)
                return original_replace(*args, **kwargs)

            with mock.patch.object(os, "replace", side_effect=swap_parent):
                safe_files.write_atomic(target, "Updated\n")
            self.assertEqual((outside / "AGENTS.md").read_text(), "Outside notes\n")
            self.assertEqual((moved / "AGENTS.md").read_text(), "Updated\n")
            self.assertEqual(set(moved.iterdir()), {moved / "AGENTS.md"})

    def test_atomic_write_failure_preserves_original_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "AGENTS.md"
            target.write_text("Original\n")
            with mock.patch.object(os, "replace", side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    safe_files.write_atomic(target, "Updated\n")
            self.assertEqual(target.read_text(), "Original\n")
            self.assertEqual(set(root.iterdir()), {target})

    def test_atomic_writer_itself_rejects_linked_destinations(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                victim = root / "victim.md"
                victim.write_text("Outside\n")
                target = root / "AGENTS.md"
                if kind == "symlink":
                    target.symlink_to(victim)
                else:
                    os.link(victim, target)
                with self.assertRaises(safe_files.UnsafeFileError):
                    safe_files.write_atomic(target, "New content\n")
                self.assertEqual(victim.read_text(), "Outside\n")

    def test_atomic_write_keeps_workspace_instructions_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp).resolve() / "AGENTS.md"
            safe_files.write_atomic(target, "Private operator notes\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_text(), "Private operator notes\n")
