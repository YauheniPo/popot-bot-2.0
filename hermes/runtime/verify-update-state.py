#!/usr/bin/env python3
"""Fail-closed state and backup checks for managed Hermes deployments."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any
import zipfile


SNAPSHOT_SCHEMA_VERSION = 2
BACKUP_MARKERS = {".env", "config.yaml", "state.db"}
EXCLUDED_DIRECTORIES = {
    "hermes-agent",
    "__pycache__",
    ".git",
    "node_modules",
    "backups",
    "state-snapshots",
    "checkpoints",
    ".venv",
    "venv",
    "site-packages",
    ".cache",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_FILE_NAMES = {".backup.lock", "gateway.pid", "cron.pid"}
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo", ".db-wal", ".db-shm", ".db-journal")


class VerificationError(RuntimeError):
    """Raised when an update safety invariant is not satisfied."""


def discover_backup_files(hermes_home: Path) -> list[Path]:
    """Return every live file the pinned Hermes full-backup walker must archive."""
    root = hermes_home.expanduser().resolve()
    files: list[Path] = []
    try:
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(directory)
            directory_names[:] = [
                name
                for name in directory_names
                if name not in EXCLUDED_DIRECTORIES and not (current / name).is_symlink()
            ]
            for name in file_names:
                path = current / name
                if path.is_symlink():
                    continue
                if name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_FILE_SUFFIXES):
                    continue
                if not path.is_file():
                    continue
                try:
                    path.resolve().relative_to(root)
                except ValueError as exc:
                    raise VerificationError(f"backup file escapes HERMES_HOME: {path}") from exc
                files.append(path)
    except OSError as exc:
        raise VerificationError(f"cannot inventory HERMES_HOME for backup: {exc}") from exc
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def discover_kanban_databases(hermes_home: Path) -> list[Path]:
    """Return every default, named, and archived Kanban database."""
    root = hermes_home.expanduser().resolve()
    candidates = [root / "kanban.db"]
    boards_root = root / "kanban" / "boards"
    if boards_root.is_dir():
        candidates.extend(boards_root.rglob("kanban.db"))

    databases: list[Path] = []
    for path in candidates:
        if path.is_symlink():
            raise VerificationError(f"refusing symlinked Kanban database: {path}")
        if not path.exists():
            continue
        if not path.is_file():
            raise VerificationError(f"Kanban database path is not a file: {path}")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise VerificationError(f"Kanban database escapes HERMES_HOME: {path}") from exc
        databases.append(path)
    return sorted(set(databases), key=lambda item: item.relative_to(root).as_posix())


def inspect_kanban_database(path: Path) -> dict[str, Any]:
    """Run a read-only integrity check and return task counts by status."""
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot open Kanban database {path}: {exc}") from exc

    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            details = "; ".join(integrity) if integrity else "no result"
            raise VerificationError(f"Kanban integrity check failed for {path}: {details}")

        tasks_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if tasks_table is None:
            raise VerificationError(f"Kanban tasks table is missing in {path}")

        rows = connection.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY status"
        ).fetchall()
    except sqlite3.Error as exc:
        raise VerificationError(f"cannot inspect Kanban database {path}: {exc}") from exc
    finally:
        connection.close()

    status_counts = {str(status): int(count) for status, count in rows}
    return {
        "integrity": "ok",
        "total_tasks": sum(status_counts.values()),
        "status_counts": status_counts,
    }


def create_snapshot(hermes_home: Path) -> dict[str, Any]:
    root = hermes_home.expanduser().resolve()
    if not root.is_dir():
        raise VerificationError(f"HERMES_HOME is not a directory: {root}")

    databases: dict[str, dict[str, Any]] = {}
    for path in discover_kanban_databases(root):
        relative_path = path.relative_to(root).as_posix()
        databases[relative_path] = inspect_kanban_database(path)

    files = [path.relative_to(root).as_posix() for path in discover_backup_files(root)]

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "databases": databases,
        "files": files,
    }


def write_snapshot(snapshot: dict[str, Any], output: Path) -> None:
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".partial",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read Hermes state snapshot {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise VerificationError(f"unsupported Hermes state snapshot format: {path}")
    if not isinstance(value.get("databases"), dict):
        raise VerificationError(f"Hermes state snapshot has no database mapping: {path}")
    files = value.get("files")
    if (
        not isinstance(files, list)
        or any(not isinstance(item, str) or not item for item in files)
        or len(files) != len(set(files))
    ):
        raise VerificationError(f"Hermes state snapshot has an invalid file inventory: {path}")
    return value


def _is_kanban_archive_path(name: str) -> bool:
    return name == "kanban.db" or (
        name.startswith("kanban/boards/") and name.endswith("/kanban.db")
    )


def compare_database_maps(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    actual_label: str,
) -> None:
    expected_paths = set(expected)
    actual_paths = set(actual)
    if expected_paths != actual_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise VerificationError(
            f"Kanban database set changed in {actual_label}; "
            f"missing={missing}, unexpected={unexpected}"
        )

    for relative_path in sorted(expected_paths):
        expected_state = expected[relative_path]
        actual_state = actual[relative_path]
        if expected_state.get("total_tasks") != actual_state.get("total_tasks"):
            raise VerificationError(
                f"Kanban task total changed for {relative_path} in {actual_label}: "
                f"expected {expected_state.get('total_tasks')}, "
                f"got {actual_state.get('total_tasks')}"
            )
        if expected_state.get("status_counts") != actual_state.get("status_counts"):
            raise VerificationError(
                f"Kanban status counts changed for {relative_path} in {actual_label}: "
                f"expected {expected_state.get('status_counts')}, "
                f"got {actual_state.get('status_counts')}"
            )


def verify_backup(backup_path: Path, before_snapshot: dict[str, Any]) -> None:
    archive = backup_path.expanduser().resolve()
    if not archive.is_file() or archive.stat().st_size == 0:
        raise VerificationError(f"backup archive is missing or empty: {archive}")

    expected_databases = before_snapshot["databases"]
    try:
        with zipfile.ZipFile(archive, "r") as backup:
            bad_entry = backup.testzip()
            if bad_entry is not None:
                raise VerificationError(f"backup CRC check failed for {bad_entry}")

            file_names = [item.filename for item in backup.infolist() if not item.is_dir()]
            duplicates = sorted(name for name, count in Counter(file_names).items() if count > 1)
            if duplicates:
                raise VerificationError(f"backup contains duplicate entries: {duplicates}")
            if not any(Path(name).name in BACKUP_MARKERS for name in file_names):
                raise VerificationError("backup has no Hermes config, environment, or state marker")

            expected_files = set(before_snapshot["files"])
            missing_files = sorted(expected_files - set(file_names))
            if missing_files:
                raise VerificationError(
                    "backup is missing live Hermes files; "
                    f"missing={missing_files}"
                )

            archived_kanban_paths = {name for name in file_names if _is_kanban_archive_path(name)}
            expected_paths = set(expected_databases)
            if archived_kanban_paths != expected_paths:
                missing = sorted(expected_paths - archived_kanban_paths)
                unexpected = sorted(archived_kanban_paths - expected_paths)
                raise VerificationError(
                    "backup Kanban database set does not match live state; "
                    f"missing={missing}, unexpected={unexpected}"
                )

            archived_databases: dict[str, dict[str, Any]] = {}
            with tempfile.TemporaryDirectory(prefix="hermes-kanban-backup-check-") as temp_dir:
                for index, relative_path in enumerate(sorted(expected_paths)):
                    extracted = Path(temp_dir) / f"kanban-{index}.db"
                    with backup.open(relative_path, "r") as source, extracted.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    archived_databases[relative_path] = inspect_kanban_database(extracted)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"cannot validate backup archive {archive}: {exc}") from exc

    compare_database_maps(
        expected_databases,
        archived_databases,
        actual_label="backup archive",
    )


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> None:
    compare_database_maps(
        before["databases"],
        after["databases"],
        actual_label="post-update state",
    )
    missing_files = sorted(set(before["files"]) - set(after["files"]))
    if missing_files:
        raise VerificationError(
            "live Hermes files disappeared during the update; "
            f"missing={missing_files}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--hermes-home", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)

    backup_parser = subparsers.add_parser("verify-backup")
    backup_parser.add_argument("--backup", type=Path, required=True)
    backup_parser.add_argument("--snapshot", type=Path, required=True)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--after", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "snapshot":
            snapshot = create_snapshot(args.hermes_home)
            write_snapshot(snapshot, args.output)
            print(
                "Hermes state snapshot verified: "
                f"{len(snapshot['files'])} file(s), "
                f"{len(snapshot['databases'])} Kanban database(s)"
            )
        elif args.command == "verify-backup":
            verify_backup(args.backup, load_snapshot(args.snapshot))
            print("Backup archive, live file inventory, and Kanban snapshots verified")
        else:
            compare_snapshots(load_snapshot(args.before), load_snapshot(args.after))
            print("Post-update Kanban state matches the pre-update snapshot")
    except VerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
