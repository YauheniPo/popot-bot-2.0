"""Tests for scheduled Hermes backup retention."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prune-backups.py")
SPEC = importlib.util.spec_from_file_location("prune_backups", MODULE_PATH)
assert SPEC and SPEC.loader
prune_backups = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune_backups)


class PruneBackupsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
