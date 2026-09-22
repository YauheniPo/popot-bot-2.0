"""Tests for fail-closed Hermes update-state verification."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile


MODULE_PATH = Path(__file__).with_name("verify-update-state.py")
SPEC = importlib.util.spec_from_file_location("verify_update_state", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
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


def create_backup(
    home: Path,
    backup_path: Path,
    *,
    omit: str | None = None,
    replacements: dict[str, bytes] | None = None,
) -> None:
    replacements = replacements or {}
    resolved_home = home.resolve()
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in verify_update_state.discover_backup_files(home):
            relative_path = path.relative_to(resolved_home).as_posix()
            if relative_path == omit:
                continue
            if relative_path in replacements:
                archive.writestr(relative_path, replacements[relative_path])
            else:
                archive.write(path, relative_path)


class VerifyUpdateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / ".hermes"
        self.home.mkdir()
        (self.home / "config.yaml").write_text("model: test\n", encoding="utf-8")
        (self.home / "SOUL.md").write_text("personal identity\n", encoding="utf-8")
        custom_skill = self.home / "skills" / "custom-review" / "SKILL.md"
        custom_skill.parent.mkdir(parents=True)
        custom_skill.write_text("custom skill\n", encoding="utf-8")
        generated_dependency = self.home / "skills" / "custom-review" / "venv" / "dependency.py"
        generated_dependency.parent.mkdir(parents=True)
        generated_dependency.write_text("generated\n", encoding="utf-8")
        installed_source = self.home / "hermes-agent" / "gateway" / "run.py"
        installed_source.parent.mkdir(parents=True)
        installed_source.write_text("replaceable source\n", encoding="utf-8")
        session = self.home / "sessions" / "telegram" / "session.json"
        session.parent.mkdir(parents=True)
        session.write_text("{}\n", encoding="utf-8")
        quick_snapshot = self.home / "state-snapshots" / "scheduled" / "state.db"
        quick_snapshot.parent.mkdir(parents=True)
        quick_snapshot.write_bytes(b"backup artifact\n")
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
        self.assertIn("SOUL.md", snapshot["files"])
        self.assertIn("skills/custom-review/SKILL.md", snapshot["files"])
        self.assertIn("sessions/telegram/session.json", snapshot["files"])
        self.assertNotIn("skills/custom-review/venv/dependency.py", snapshot["files"])
        self.assertNotIn("hermes-agent/gateway/run.py", snapshot["files"])
        self.assertNotIn("state-snapshots/scheduled/state.db", snapshot["files"])

    def test_verified_backup_matches_live_snapshot(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        create_backup(self.home, backup_path)

        with zipfile.ZipFile(backup_path) as archive:
            self.assertNotIn("state-snapshots/scheduled/state.db", archive.namelist())

        verify_update_state.verify_backup(backup_path, snapshot)

    def test_gateway_lock_disappearing_after_inventory_does_not_abort_snapshot(self) -> None:
        for relative in ("gateway.lock", "profiles/custom/gateway.lock"):
            with self.subTest(path=relative):
                lock = self.home / relative
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text("temporary process lock")
                discover = verify_update_state.discover_backup_files

                def discover_then_release(home):
                    paths = discover(home)
                    lock.unlink()
                    return paths

                with mock.patch.object(verify_update_state, "discover_backup_files",
                                       side_effect=discover_then_release):
                    snapshot = verify_update_state.create_snapshot(self.home)

                self.assertNotIn(relative, snapshot["files"])
                self.assertNotIn(relative, snapshot["file_hashes"])
                self.assertIn("SOUL.md", snapshot["files"])
                backup_path = self.root / "backup.zip"
                create_backup(self.home, backup_path)
                verify_update_state.verify_backup(backup_path, snapshot)

    def test_disappearing_personal_file_still_aborts_snapshot(self) -> None:
        personal = self.home / "SOUL.md"
        discover = verify_update_state.discover_backup_files

        def discover_then_remove(home):
            paths = discover(home)
            personal.unlink()
            return paths

        with mock.patch.object(verify_update_state, "discover_backup_files",
                               side_effect=discover_then_remove):
            with self.assertRaises(FileNotFoundError):
                verify_update_state.create_snapshot(self.home)

    def test_unrelated_lock_files_remain_in_backup_inventory(self) -> None:
        dependency_lock = self.home / "skills/custom-review/package.lock"
        dependency_lock.write_text("persistent dependency state")
        snapshot = verify_update_state.create_snapshot(self.home)
        self.assertIn("skills/custom-review/package.lock", snapshot["files"])

    def test_worktree_git_files_are_not_required_but_working_files_are(self) -> None:
        worktrees = ["swarm/worktrees/builder", "swarm/worktrees/researcher",
                     "swarm/worktrees/reviewer", "skills/custom-worktree"]
        for relative in worktrees:
            directory = self.home / relative
            directory.mkdir(parents=True)
            (directory / ".git").write_text("gitdir: /external/repo/.git/worktrees/worker\n")
            (directory / "uncommitted.py").write_text("# preserve local changes\n")
            (directory / ".gitignore").write_text("*.pyc\n")

        snapshot = verify_update_state.create_snapshot(self.home)
        archive = self.root / "worktree-backup.zip"
        # Model the native archive independently: .git files are omitted even
        # when our verifier mistakenly expects them (the reported regression).
        with zipfile.ZipFile(archive, "w") as backup:
            for path in verify_update_state.discover_backup_files(self.home):
                if path.name != ".git":
                    backup.write(path, path.relative_to(self.home).as_posix())
        verify_update_state.verify_backup(archive, snapshot)
        for relative in worktrees:
            self.assertNotIn(f"{relative}/.git", snapshot["files"])
            self.assertNotIn(f"{relative}/.git", snapshot["file_hashes"])
            self.assertIn(f"{relative}/uncommitted.py", snapshot["files"])
            self.assertIn(f"{relative}/.gitignore", snapshot["files"])

        create_backup(self.home, archive, omit="swarm/worktrees/builder/uncommitted.py")
        with self.assertRaisesRegex(verify_update_state.VerificationError, "missing"):
            verify_update_state.verify_backup(archive, snapshot)

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

    def test_backup_missing_a_custom_skill_fails_closed(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        create_backup(
            self.home,
            backup_path,
            omit="skills/custom-review/SKILL.md",
        )

        with self.assertRaisesRegex(verify_update_state.VerificationError, "live Hermes files"):
            verify_update_state.verify_backup(backup_path, snapshot)

    def test_full_deployment_backup_verifies_profile_memory_and_workspace_manifest(self) -> None:
        required = ["profiles/custom/SOUL.md", "profiles/custom/memories/MEMORY.md",
                    "swarm/swarm.yaml", "operator-state/workspace-instructions.json"]
        for name in required:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("personal state")
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        for missing in required:
            with self.subTest(missing=missing):
                create_backup(self.home, backup_path, omit=missing)
                with self.assertRaisesRegex(verify_update_state.VerificationError, "missing"):
                    verify_update_state.verify_backup(backup_path, snapshot)

    def test_corrupt_database_inside_backup_fails_closed(self) -> None:
        snapshot = verify_update_state.create_snapshot(self.home)
        backup_path = self.root / "backup.zip"
        create_backup(
            self.home,
            backup_path,
            replacements={"kanban.db": b"not a SQLite database"},
        )

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

    def test_post_update_custom_skill_loss_fails_closed(self) -> None:
        before = verify_update_state.create_snapshot(self.home)
        (self.home / "skills" / "custom-review" / "SKILL.md").unlink()
        after = verify_update_state.create_snapshot(self.home)

        with self.assertRaisesRegex(verify_update_state.VerificationError, "files disappeared"):
            verify_update_state.compare_snapshots(before, after)

    def test_post_update_bundled_skill_rename_is_not_a_loss(self) -> None:
        # Hermes's own skill-sync renames/recategorizes bundled skills on
        # every update (e.g. skills/github/foo -> skills/web/foo). Same
        # content at a new path must not trip the missing-files guard.
        before = verify_update_state.create_snapshot(self.home)
        old_path = self.home / "skills" / "custom-review" / "SKILL.md"
        new_path = self.home / "skills" / "renamed-category" / "custom-review" / "SKILL.md"
        new_path.parent.mkdir(parents=True)
        new_path.write_bytes(old_path.read_bytes())
        old_path.unlink()
        after = verify_update_state.create_snapshot(self.home)

        verify_update_state.compare_snapshots(before, after)  # must not raise

    def test_deleting_one_of_two_identical_files_still_fails_closed(self) -> None:
        # Matching content alone must not excuse a deletion: every surviving
        # copy accounts for at most one moved file.
        duplicate = self.home / "skills" / "custom-review" / "SKILL.copy.md"
        duplicate.write_bytes((self.home / "skills" / "custom-review" / "SKILL.md").read_bytes())
        before = verify_update_state.create_snapshot(self.home)
        duplicate.unlink()
        after = verify_update_state.create_snapshot(self.home)

        with self.assertRaisesRegex(verify_update_state.VerificationError, "files disappeared"):
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

    def test_archivable_file_skips_symlinks_excluded_names_and_directories(self) -> None:
        # symlink file
        link = self.home / "config-link.yaml"
        link.symlink_to(self.home / "config.yaml")
        self.assertIsNone(
            verify_update_state._archivable_file(self.home, self.home, "config-link.yaml")
        )
        # excluded file name
        (self.home / ".backup.lock").write_text("lock", encoding="utf-8")
        self.assertIsNone(
            verify_update_state._archivable_file(self.home, self.home, ".backup.lock")
        )
        # excluded file suffix
        (self.home / "data.db-wal").write_text("wal", encoding="utf-8")
        self.assertIsNone(
            verify_update_state._archivable_file(self.home, self.home, "data.db-wal")
        )
        # a directory, not a file
        subdir = self.home / "subdir"
        subdir.mkdir()
        self.assertIsNone(
            verify_update_state._archivable_file(self.home, self.home, "subdir")
        )
        # a file that resolves outside the inventory root
        outside = self.root / "outside.txt"
        outside.write_text("escape", encoding="utf-8")
        with self.assertRaisesRegex(verify_update_state.VerificationError, "escapes HERMES_HOME"):
            verify_update_state._archivable_file(self.home, self.root, "outside.txt")


if __name__ == "__main__":
    unittest.main()
