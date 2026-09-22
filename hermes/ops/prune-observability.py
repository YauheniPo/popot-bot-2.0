#!/usr/bin/env python3
"""Delete expired rows from the local Hermes observability SQLite database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TABLES = ("api_calls", "tool_calls", "sessions", "approvals", "commands", "route_fallbacks")

# Latency histogram upper bounds in milliseconds; -1 encodes +Inf. The exporter
# must bucket live rows identically (see export-metrics.py DURATION_BUCKETS_MS).
DURATION_BUCKETS_MS = (250, 500, 1000, 2000, 5000, 10000, 20000, 30000, 60000, 120000, 300000)

# Columns the observability plugin adds to older databases; the pruner adds
# them too so a retention run never fails on a database the gateway has not
# reopened since the upgrade.
API_CALLS_ADDED_COLUMNS = {"requested_model": "TEXT", "call_index": "INTEGER DEFAULT 0"}
ROW_COUNT_SQL = "COUNT(*)"


def duration_bucket_sql(column: str) -> str:
    cases = " ".join(f"WHEN {column} <= {bound} THEN {bound}" for bound in DURATION_BUCKETS_MS)
    return f"CASE {cases} ELSE -1 END"


# A success is a first-attempt success unless an error row of the same logical
# call (session + api_call_count) precedes it. call_index 0 means the
# correlation is unavailable, so the row counts as a first attempt.
FIRST_ATTEMPT_SQL = (
    "CASE WHEN status='ok' AND (COALESCE(call_index,0)=0 OR NOT EXISTS ("
    "SELECT 1 FROM api_calls e WHERE e.status!='ok' AND e.session_id=a.session_id "
    "AND e.call_index=a.call_index AND e.ts<=a.ts)) THEN 1 ELSE 0 END"
)
# A provider "success" without any output while usage was reported.
EMPTY_SUCCESS_SQL = (
    "CASE WHEN status='ok' AND COALESCE(output_tokens,0)=0 "
    "AND (COALESCE(input_tokens,0)>0 OR COALESCE(total_tokens,0)>0) THEN 1 ELSE 0 END"
)
# Tool calls carry only the session id; the model comes from the latest
# session start recorded before the call.
TOOL_MODEL_SQL = (
    "COALESCE((SELECT s.model FROM sessions s WHERE s.session_id=t.session_id AND s.event='start' "
    "AND s.ts<=t.ts ORDER BY s.ts DESC LIMIT 1),'')"
)

# Pre-aggregated counters that survive row deletion. Prometheus counters are
# computed as live rows plus these rollups, so pruning never lowers a counter.
# keys: rollup column -> SQL over the source table (aliased a/t); sums likewise.
ROLLUPS: dict[str, dict[str, Any]] = {
    "api_calls": {
        "alias": "a",
        "keys": {
            "provider": "COALESCE(provider,'')", "model": "COALESCE(model,'')",
            "requested_model": "COALESCE(requested_model,'')", "status": "COALESCE(status,'')",
            "status_code": "COALESCE(status_code,0)", "finish_reason": "COALESCE(finish_reason,'')",
            "duration_bucket": duration_bucket_sql("COALESCE(duration_ms,0)"),
        },
        "sums": {
            "weight": ROW_COUNT_SQL, "input_tokens": "COALESCE(SUM(input_tokens),0)",
            "output_tokens": "COALESCE(SUM(output_tokens),0)",
            "cache_read_tokens": "COALESCE(SUM(cache_read_tokens),0)",
            "total_tokens": "COALESCE(SUM(total_tokens),0)", "cost_usd": "COALESCE(SUM(cost_usd),0)",
            "duration_ms": "COALESCE(SUM(duration_ms),0)", "retry_count": "COALESCE(SUM(retry_count),0)",
            "first_attempt": f"SUM({FIRST_ATTEMPT_SQL})", "empty_success": f"SUM({EMPTY_SUCCESS_SQL})",
        },
    },
    "tool_calls": {
        "alias": "t",
        "keys": {"tool_name": "COALESCE(tool_name,'')", "status": "COALESCE(status,'')", "model": TOOL_MODEL_SQL},
        "sums": {"weight": ROW_COUNT_SQL, "duration_ms": "COALESCE(SUM(duration_ms),0)"},
    },
    "sessions": {
        "alias": "a",
        "keys": {
            "model": "COALESCE(model,'')", "platform": "COALESCE(platform,'')", "event": "COALESCE(event,'')",
            "completed": "COALESCE(completed,0)", "failed": "COALESCE(failed,0)",
            "interrupted": "COALESCE(interrupted,0)",
        },
        "sums": {"weight": ROW_COUNT_SQL},
    },
    "approvals": {
        "alias": "a",
        "keys": {"event": "COALESCE(event,'')", "choice": "COALESCE(choice,'')"},
        "sums": {"weight": ROW_COUNT_SQL},
    },
    "commands": {
        "alias": "a",
        "keys": {"command": "COALESCE(command,'')"},
        "sums": {"weight": ROW_COUNT_SQL},
    },
    "route_fallbacks": {
        "alias": "a",
        "keys": {
            "from_provider": "COALESCE(from_provider,'')", "from_model": "COALESCE(from_model,'')",
            "to_provider": "COALESCE(to_provider,'')", "to_model": "COALESCE(to_model,'')",
        },
        "sums": {"weight": ROW_COUNT_SQL},
    },
}


def rollup_table(table: str) -> str:
    return f"{table}_rollup"


def rollup_create_sql(table: str) -> str:
    spec = ROLLUPS[table]
    keys = ", ".join(spec["keys"])
    columns = ", ".join([*spec["keys"], *(f"{name} REAL NOT NULL DEFAULT 0" for name in spec["sums"])])
    return f"CREATE TABLE IF NOT EXISTS {rollup_table(table)} ({columns}, UNIQUE({keys}))"


def rollup_upsert_sql(table: str) -> str:
    spec = ROLLUPS[table]
    selected = ", ".join(f"{sql} AS {name}" for name, sql in [*spec["keys"].items(), *spec["sums"].items()])
    columns = ", ".join([*spec["keys"], *spec["sums"]])
    keys = ", ".join(spec["keys"])
    updates = ", ".join(f"{name}={name}+excluded.{name}" for name in spec["sums"])
    return (
        f"INSERT INTO {rollup_table(table)} ({columns}) SELECT {selected} FROM {table} {spec['alias']} "
        f"WHERE ts < ? GROUP BY {keys} ON CONFLICT({keys}) DO UPDATE SET {updates}"
    )


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _upgrade_api_schema(connection: sqlite3.Connection, existing: set[str]) -> None:
    if "api_calls" not in existing:
        return
    present = {row[1] for row in connection.execute("PRAGMA table_info(api_calls)")}
    for name, definition in API_CALLS_ADDED_COLUMNS.items():
        if name not in present:
            connection.execute(f"ALTER TABLE api_calls ADD COLUMN {name} {definition}")


def _fold_expired_rows(connection: sqlite3.Connection, existing: set[str], cutoff: str) -> None:
    for table in TABLES:
        if table not in existing:
            continue
        connection.execute(rollup_create_sql(table))
        connection.execute(rollup_upsert_sql(table), (cutoff,))


def _delete_expired_rows(connection: sqlite3.Connection, existing: set[str], cutoff: str) -> int:
    removed = 0
    for table in TABLES:
        if table not in existing:
            continue
        cursor = connection.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        removed += max(cursor.rowcount, 0)
    return removed


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

    # Encode literal filename characters; rw must not create a fresh database
    # if the file disappears between validation and opening the connection.
    database_uri = database.resolve().as_uri() + "?mode=rw"
    connection = sqlite3.connect(database_uri, uri=True, timeout=30)
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
        with connection:
            _upgrade_api_schema(connection, existing)
            # Fold expiring rows into rollups before deleting them.
            _fold_expired_rows(connection, existing, cutoff)
            removed = _delete_expired_rows(connection, existing, cutoff)
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
    # Resolve the directory, not the leaf: a symlinked database is not allowed.
    expected_database = (hermes_home() / "ops").resolve() / "metrics.db"
    if args.database.is_symlink() or args.database.resolve() != expected_database:
        print(f"--database must be {expected_database}", file=sys.stderr)
        return 2
    removed = prune_database(expected_database, args.retention_days)
    print(f"Pruned observability rows: {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
