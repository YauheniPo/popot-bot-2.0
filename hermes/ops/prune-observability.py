#!/usr/bin/env python3
"""Delete expired rows from the local Hermes observability SQLite database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


TABLES = ("api_calls", "tool_calls", "sessions", "approvals", "commands")


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def prune_database(database: Path, retention_days: int, *, now: datetime | None = None) -> int:
    if database.name != "metrics.db":
        raise ValueError(f"--database must point to a metrics.db file: {database}")
    if not database.exists():
        return 0
    if database.is_symlink() or not database.is_file():
        raise ValueError(f"observability database must be a regular file: {database}")

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = (reference.astimezone(timezone.utc) - timedelta(days=retention_days)).isoformat(
        timespec="milliseconds"
    )

    connection = sqlite3.connect(str(database), timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError(f"observability database integrity check failed: {integrity!r}")
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        removed = 0
        with connection:
            for table in TABLES:
                if table not in existing:
                    continue
                cursor = connection.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed += max(cursor.rowcount, 0)
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return removed
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--retention-days", required=True, type=positive_integer)
    args = parser.parse_args()
    # Anchor --database to HERMES_HOME instead of trusting its basename
    # alone: a basename-only check still lets the directory component point
    # anywhere on the filesystem. Falls back to ~/.hermes (like hermes_home()
    # is used elsewhere) so a manual invocation without HERMES_HOME set still
    # behaves sensibly; the systemd unit that runs this script in production
    # always sets HERMES_HOME anyway.
    expected_database = (hermes_home() / "ops" / "metrics.db").resolve()
    if args.database.resolve() != expected_database:
        print(f"--database must be {expected_database}", file=sys.stderr)
        return 2
    removed = prune_database(args.database, args.retention_days)
    print(f"Pruned observability rows: {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
