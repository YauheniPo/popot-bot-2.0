"""Tests for scheduled Hermes backup retention."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("prune-backups.py")
SPEC = importlib.util.spec_from_file_location("prune_backups", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
prune_backups = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune_backups)


class PruneBackupsTests(unittest.TestCase):
    def test_deployment_retention_handles_unique_suffixes_and_legacy_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefixes = ["pre-config-deploy-20261003T120000",
                        "pre-config-deploy-20261003T120000-" + "a" * 16,
                        "pre-config-deploy-20261003T120000-" + "b" * 16]
            for index, prefix in enumerate(prefixes):
                archive = root / (prefix + ".zip")
                archive.touch()
                os.utime(archive, (index + 1, index + 1))
                (root / (prefix + "-state.json")).touch()
            manual = root / "pre-config-deploy-20261003T120000-manual.zip"
            manual.touch()
            self.assertEqual(prune_backups.prune_deployment_backups(root, keep=1), 2)
            self.assertEqual(sorted(path.name for path in root.iterdir()), sorted([
                prefixes[-1] + ".zip", prefixes[-1] + "-state.json", manual.name,
            ]))
            self.assertEqual(prune_backups.prune_deployment_backups(root, keep=1), 0)

    def test_full_retention_keeps_only_five_newest_scheduled_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            backup_dir = Path(temporary_directory)
            for index in range(7):
                path = backup_dir / f"scheduled-full-202608{index + 1:02d}-000000.zip"
                path.touch()
                os.utime(path, (index + 1, index + 1))
            manual = backup_dir / "pre-deploy-20260801-000000.zip"
            manual.touch()

            removed = prune_backups.prune_scheduled_full_backups(backup_dir, keep=5)

            self.assertEqual(removed, 2)
            self.assertEqual(
                sorted(path.name for path in backup_dir.glob("scheduled-full-*.zip")),
                [
                    "scheduled-full-20260803-000000.zip",
                    "scheduled-full-20260804-000000.zip",
                    "scheduled-full-20260805-000000.zip",
                    "scheduled-full-20260806-000000.zip",
                    "scheduled-full-20260807-000000.zip",
                ],
            )
            self.assertTrue(manual.exists())

    def test_quick_retention_deletes_only_old_scheduled_snapshots(self) -> None:
        now = 2_000_000_000.0
        day = 24 * 60 * 60
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshots_dir = Path(temporary_directory)
            old_scheduled = self.create_snapshot(snapshots_dir, "old-scheduled", "scheduled", now - 15 * day)
            recent_scheduled = self.create_snapshot(snapshots_dir, "recent-scheduled", "scheduled", now - 13 * day)
            old_manual = self.create_snapshot(snapshots_dir, "old-manual", "manual", now - 30 * day)
            malformed = snapshots_dir / "malformed"
            malformed.mkdir()
            (malformed / "manifest.json").write_text("not-json", encoding="utf-8")
            os.utime(malformed, (now - 30 * day, now - 30 * day))

            removed = prune_backups.prune_scheduled_quick_snapshots(
                snapshots_dir,
                retention_days=14,
                now=now,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(old_scheduled.exists())
            self.assertTrue(recent_scheduled.exists())
            self.assertTrue(old_manual.exists())
            self.assertTrue(malformed.exists())

    def test_deployment_retention_removes_old_archive_groups_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            backup_dir = Path(temporary_directory)
            prefixes = [
                "pre-deploy-20260820-010101",
                "pre-config-deploy-20260821T010101",
                "pre-deploy-20260822-010101",
            ]
            for index, prefix in enumerate(prefixes):
                archive = backup_dir / f"{prefix}.zip"
                archive.touch()
                os.utime(archive, (index + 1, index + 1))
                suffixes = (
                    ("-kanban-before.json", "-kanban-after.json")
                    if prefix.startswith("pre-deploy-")
                    else ("-state.json",)
                )
                for suffix in suffixes:
                    (backup_dir / f"{prefix}{suffix}").touch()
            manual = backup_dir / "manual-backup.zip"
            manual.touch()
            symlink = backup_dir / "pre-deploy-20260819-010101.zip"
            symlink.symlink_to(manual)

            removed = prune_backups.prune_deployment_backups(backup_dir, keep=2)

            self.assertEqual(removed, 1)
            self.assertFalse((backup_dir / f"{prefixes[0]}.zip").exists())
            self.assertFalse((backup_dir / f"{prefixes[0]}-kanban-before.json").exists())
            self.assertFalse((backup_dir / f"{prefixes[0]}-kanban-after.json").exists())
            self.assertTrue((backup_dir / f"{prefixes[1]}.zip").exists())
            self.assertTrue((backup_dir / f"{prefixes[2]}.zip").exists())
            self.assertTrue(manual.exists())
            self.assertTrue(symlink.is_symlink())

    @staticmethod
    def create_snapshot(root: Path, name: str, label: str, modified: float) -> Path:
        path = root / name
        path.mkdir()
        (path / "manifest.json").write_text(json.dumps({"label": label}), encoding="utf-8")
        os.utime(path, (modified, modified))
        return path

    def test_repeated_deployments_remain_bounded_and_retention_is_idempotent(self):
        for keep in (2, 4):
            with self.subTest(keep=keep), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for index in range(9):
                    prefix = f"pre-config-deploy-20260919T1200{index:02d}"
                    archive = root / f"{prefix}.zip"
                    archive.touch()
                    os.utime(archive, (index + 1, index + 1))
                    (root / f"{prefix}-state.json").touch()
                    prune_backups.prune_deployment_backups(root, keep)
                    self.assertEqual(len(list(root.glob("*.zip"))), min(index + 1, keep))
                    self.assertEqual(len(list(root.glob("*-state.json"))), min(index + 1, keep))
                    self.assertTrue(archive.exists())
                    self.assertEqual(prune_backups.prune_deployment_backups(root, keep), 0)

    def test_quick_retention_keeps_all_recent_snapshots_not_a_count_limit(self):
        now, day = 2_000_000_000, 86400
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(21):
                self.create_snapshot(root, str(index), "scheduled", now - index * day / 3)
            self.assertEqual(prune_backups.prune_scheduled_quick_snapshots(root, 14, now=now), 0)
            self.assertEqual(len(list(root.iterdir())), 21)


    def test_positive_integer_valid(self) -> None:
        self.assertEqual(prune_backups.positive_integer("5"), 5)

    def test_positive_integer_invalid(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            prune_backups.positive_integer("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            prune_backups.positive_integer("-1")

    def test_prune_scheduled_full_backups_dir_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backup_dir = Path(temp) / "missing"
            removed = prune_backups.prune_scheduled_full_backups(backup_dir, keep=5)
            self.assertEqual(removed, 0)

    def test_prune_deployment_backups_dir_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backup_dir = Path(temp) / "missing"
            removed = prune_backups.prune_deployment_backups(backup_dir, keep=5)
            self.assertEqual(removed, 0)

    def test_is_scheduled_snapshot_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot"
            path.mkdir()
            self.assertFalse(prune_backups.is_scheduled_snapshot(path))

    def test_is_scheduled_snapshot_not_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot"
            path.mkdir()
            (path / "manifest.json").write_text('{"label": "manual"}', encoding="utf-8")
            self.assertFalse(prune_backups.is_scheduled_snapshot(path))

    def test_is_scheduled_snapshot_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot"
            path.mkdir()
            (path / "manifest.json").write_text("not json", encoding="utf-8")
            self.assertFalse(prune_backups.is_scheduled_snapshot(path))

    def test_prune_scheduled_quick_snapshots_dir_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            snapshots_dir = Path(temp) / "missing"
            removed = prune_backups.prune_scheduled_quick_snapshots(
                snapshots_dir, retention_days=7, now=2_000_000_000.0,
            )
            self.assertEqual(removed, 0)

    def test_prune_scheduled_quick_snapshots_skips_non_scheduled(self) -> None:
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            snapshots_dir = Path(temp)
            manual = snapshots_dir / "manual-snapshot"
            manual.mkdir()
            (manual / "manifest.json").write_text(
                json.dumps({"label": "manual"}), encoding="utf-8",
            )
            os.utime(manual, (now - 30 * 86400, now - 30 * 86400))
            removed = prune_backups.prune_scheduled_quick_snapshots(
                snapshots_dir, retention_days=14, now=now,
            )
            self.assertEqual(removed, 0)
            self.assertTrue(manual.exists())

    def test_build_parser_has_expected_arguments(self) -> None:
        parser = prune_backups.build_parser()
        args = parser.parse_args([
            "--backup-dir", "/tmp/b", "--snapshots-dir", "/tmp/s",
            "--quick-retention-days", "7", "--full-keep", "5",
            "--deployment-keep", "3",
        ])
        self.assertEqual(args.quick_retention_days, 7)
        self.assertEqual(args.full_keep, 5)
        self.assertEqual(args.deployment_keep, 3)

    def test_main_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backup_dir = Path(temp) / "backups"
            backup_dir.mkdir()
            snapshots_dir = Path(temp) / "snapshots"
            snapshots_dir.mkdir()
            argv = ["prune-backups.py", "--backup-dir", str(backup_dir),
                    "--snapshots-dir", str(snapshots_dir),
                    "--quick-retention-days", "7", "--full-keep", "5",
                    "--deployment-keep", "3"]
            with mock.patch("sys.argv", argv):
                self.assertEqual(prune_backups.main(), 0)

    def test_quick_snapshots_removes_scheduled_old(self) -> None:
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            snapshots_dir = Path(temp)
            old_scheduled = self.create_snapshot(snapshots_dir, "old-scheduled", "scheduled", now - 15 * 86400)
            removed = prune_backups.prune_scheduled_quick_snapshots(
                snapshots_dir, retention_days=14, now=now,
            )
            self.assertEqual(removed, 1)
            self.assertFalse(old_scheduled.exists())

    def test_quick_snapshots_removes_scheduled_old_when_not_symlink_or_mount(self) -> None:
        """Cover the continue branch for symlink/mount check."""
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            snapshots_dir = Path(temp)
            # Create a scheduled snapshot that is old enough to be removed
            old_scheduled = self.create_snapshot(snapshots_dir, "old-scheduled", "scheduled", now - 15 * 86400)
            # Create a symlink to a different location that is NOT scheduled (should be skipped)
            outside = Path(temp) / "outside"
            outside.mkdir()
            (outside / "manifest.json").write_text(json.dumps({"label": "manual"}), encoding="utf-8")
            os.utime(outside, (now - 15 * 86400, now - 15 * 86400))
            symlink = snapshots_dir / "symlink-snapshot"
            symlink.symlink_to(outside)
            
            removed = prune_backups.prune_scheduled_quick_snapshots(
                snapshots_dir, retention_days=14, now=now,
            )
            # Only the directory should be removed, symlink should be skipped
            self.assertEqual(removed, 1)
            self.assertFalse(old_scheduled.exists())
            self.assertTrue(symlink.exists())
            self.assertTrue(outside.exists())

    def test_main_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backup_dir = Path(temp) / "backups"
            backup_dir.mkdir()
            snapshots_dir = Path(temp) / "snapshots"
            snapshots_dir.mkdir()
            argv = ["prune-backups.py", "--backup-dir", str(backup_dir),
                    "--snapshots-dir", str(snapshots_dir),
                    "--quick-retention-days", "7", "--full-keep", "5",
                    "--deployment-keep", "3"]
            with mock.patch("sys.argv", argv):
                self.assertEqual(prune_backups.main(), 0)


if __name__ == "__main__":
    unittest.main()
