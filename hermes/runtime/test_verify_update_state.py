"""Tests for fail-closed Hermes update-state verification."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile


MODULE_PATH = Path(__file__).with_name("verify-update-state.py")
SPEC = importlib.util.spec_from_file_location("verify_update_state", MODULE_PATH)
assert SPEC and SPEC.loader
verify_update_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_update_state)


def create_kanban_db(path: Path, statuses: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO tasks (id, status) VALUES (?, ?)",
            [(f"task-{index}", status) for index, status in enumerate(statuses)],
        )
        connection.commit()
    finally:
        connection.close()


def create_backup(home: Path, backup_path: Path, *, omit: str | None = None) -> None:
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("config.yaml", "model: test\n")
        for database in sorted(home.rglob("kanban.db")):
            relative_path = database.relative_to(home).as_posix()
            if relative_path != omit:
                archive.write(database, relative_path)


class VerifyUpdateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / ".hermes"
        self.home.mkdir()
        create_kanban_db(self.home / "kanban.db", ["todo", "done", "done"])
        create_kanban_db(
            self.home / "kanban" / "boards" / "project-a" / "kanban.db",
            ["ready", "blocked"],
        )
        create_kanban_db(
            self.home / "kanban" / "boards" / "_archived" / "old" / "kanban.db",
            ["archived"],
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_snapshot_records_every_board_and_status_count(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)

        self.assertEqual(
            set(snapshot["databases"]),
            {
                "kanban.db",
                "kanban/boards/project-a/kanban.db",
                "kanban/boards/_archived/old/kanban.db",
            },
        )
        self.assertEqual(
            snapshot["databases"]["kanban.db"]["status_counts"],
            {"done": 2, "todo": 1},
        )

    def test_verified_backup_matches_live_snapshot(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        create_backup(self.home, backup_path)

        verify_update_state.verify_backup(backup_path, snapshot)

    def test_backup_missing_a_board_fails_closed(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        create_backup(
            self.home,
            backup_path,
            omit="kanban/boards/project-a/kanban.db",
        )

        with self.assertRaisesRegex(verify_update_state.VerificationError, "missing"):
            verify_update_state.verify_backup(backup_path, snapshot)

    def test_corrupt_database_inside_backup_fails_closed(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("config.yaml", "model: test\n")
            for database in sorted(self.home.rglob("kanban.db")):
                relative_path = database.relative_to(self.home).as_posix()
                if relative_path == "kanban.db":
                    archive.writestr(relative_path, b"not a SQLite database")
                else:
                    archive.write(database, relative_path)

        with self.assertRaisesRegex(verify_update_state.VerificationError, "Kanban database"):
            verify_update_state.verify_backup(backup_path, snapshot)

    def test_corrupt_live_database_fails_closed(self) -> None:
        (self.home / "kanban.db").write_bytes(b"not a SQLite database")

        with self.assertRaisesRegex(verify_update_state.VerificationError, "Kanban database"):
            verify_update_state.create_snapshot(self.home)

    def test_post_update_status_change_fails_closed(self) -> None:
        before = verify_update_state.create_snapshot(self.home)
        connection = sqlite3.connect(self.home / "kanban.db")
        try:
            connection.execute("UPDATE tasks SET status = 'archived' WHERE id = 'task-0'")
            connection.commit()
        finally:
            connection.close()
        after = verify_update_state.create_snapshot(self.home)

        with self.assertRaisesRegex(verify_update_state.VerificationError, "status counts"):
            verify_update_state.compare_snapshots(before, after)

    def test_snapshot_file_round_trip(self) -> None:
        snapshot_path = self.root / "snapshot.json"
        snapshot = verify_update_state.create_snapshot(self.home)

        verify_update_state.write_snapshot(snapshot, snapshot_path)

        self.assertEqual(verify_update_state.load_snapshot(snapshot_path), snapshot)
        self.assertEqual(json.loads(snapshot_path.read_text()), snapshot)

    def test_cli_parser_accepts_all_update_guard_commands(self) -> None:
        parser = verify_update_state.build_parser()

        snapshot_args = parser.parse_args(
            ["snapshot", "--hermes-home", str(self.home), "--output", "snapshot.json"]
        )
        backup_args = parser.parse_args(
            ["verify-backup", "--backup", "backup.zip", "--snapshot", "before.json"]
        )
        compare_args = parser.parse_args(
            ["compare", "--before", "before.json", "--after", "after.json"]
        )

        self.assertEqual(snapshot_args.command, "snapshot")
        self.assertEqual(backup_args.command, "verify-backup")
        self.assertEqual(compare_args.command, "compare")


if __name__ == "__main__":
    unittest.main()
