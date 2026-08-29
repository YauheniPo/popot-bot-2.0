"""Bounded asynchronous audit and SQLite persistence."""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import _COMMAND_VALUE_KEYS, _SENSITIVE_KEY, _command_program, _safe_audit_value

_EVENT_QUEUE: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False

def _home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _paths() -> tuple[Path, Path, Path]:
    root = _home()
    return root / "ops", root / "ops" / "metrics.db", root / "logs" / "ops-audit.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    _, db_path, _ = _paths()
    connection = sqlite3.connect(str(db_path), timeout=3)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=3000")
    return connection


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _db() -> sqlite3.Connection:
    """Create the schema (if missing) and return a connection. Call once at register()."""
    ops_dir, db_path, audit_path = _paths()
    ops_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = _connect()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_calls (
          ts TEXT NOT NULL, request_id TEXT, session_id TEXT, turn_id TEXT,
          provider TEXT, model TEXT, platform TEXT, status TEXT,
          duration_ms REAL DEFAULT 0, input_tokens INTEGER DEFAULT 0,
          output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
          total_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
          cost_source TEXT DEFAULT 'unavailable', finish_reason TEXT,
          status_code INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts);
        CREATE INDEX IF NOT EXISTS idx_api_calls_model ON api_calls(provider, model);
        CREATE TABLE IF NOT EXISTS tool_calls (
          ts TEXT NOT NULL, session_id TEXT, turn_id TEXT, tool_name TEXT,
          status TEXT, duration_ms REAL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_name ON tool_calls(tool_name);
        CREATE TABLE IF NOT EXISTS sessions (
          ts TEXT NOT NULL, session_id TEXT, model TEXT, platform TEXT,
          event TEXT, completed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
          interrupted INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts);
        CREATE TABLE IF NOT EXISTS approvals (
          ts TEXT NOT NULL, session_id TEXT, turn_id TEXT, surface TEXT,
          event TEXT, choice TEXT, pattern_key TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_ts ON approvals(ts);
        CREATE TABLE IF NOT EXISTS commands (
          ts TEXT NOT NULL, session_id TEXT, surface TEXT, platform TEXT,
          command TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts);
        """
    )
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return connection


def _write_audit(payload: bytes) -> None:
    """Append a privacy-filtered audit event and keep its size bounded."""

    _, _, path = _paths()
    max_bytes = _bounded_int("HERMES_OBSERVABILITY_AUDIT_MAX_BYTES", 5 * 1024 * 1024, 64 * 1024, 100 * 1024 * 1024)
    rotated_files = _bounded_int("HERMES_OBSERVABILITY_AUDIT_ROTATED_FILES", 2, 1, 10)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            return
        if path.exists() and path.stat().st_size + len(payload) > max_bytes:
            for index in range(rotated_files, 0, -1):
                source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
                target = path.with_name(f"{path.name}.{index}")
                if not source.exists() or source.is_symlink():
                    continue
                if target.exists() or target.is_symlink():
                    target.unlink()
                source.replace(target)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        # Best-effort second copy in the root-managed system journal. The local
        # JSONL remains the convenient query source; journald is harder for the
        # unprivileged agent account to erase.
        if Path("/dev/log").exists():
            try:
                import syslog

                syslog.syslog(syslog.LOG_INFO, "hermes_audit " + payload.decode().rstrip())
            except Exception:
                pass
    except Exception:
        # Observability must never break the worker or an agent turn.
        pass


def _write_batch(events: list[tuple[str, object]]) -> None:
    """Persist a batch outside the agent execution path."""

    sql_events = [event[1] for event in events if event[0] == "sql"]
    if sql_events:
        try:
            connection = _connect()
            try:
                for sql, params in sql_events:
                    connection.execute(sql, params)
                connection.commit()
            finally:
                connection.close()
        except Exception:
            # Dropping telemetry is preferable to retaining an SQLite lock or
            # retry loop that could consume agent resources.
            pass
    for kind, payload in events:
        if kind == "audit" and isinstance(payload, bytes):
            _write_audit(payload)


def _worker() -> None:
    while True:
        event = _EVENT_QUEUE.get()
        batch = [event]
        # Bound both latency and memory: normal activity is committed within
        # 50 ms, while bursts need just one SQLite transaction.
        deadline = time.monotonic() + 0.05
        while len(batch) < 64 and time.monotonic() < deadline:
            try:
                batch.append(_EVENT_QUEUE.get_nowait())
            except queue.Empty:
                break
        _write_batch(batch)


def _start_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        threading.Thread(target=_worker, name="hermes-observability", daemon=True).start()
        _WORKER_STARTED = True


def _enqueue(kind: str, payload: object) -> None:
    try:
        _EVENT_QUEUE.put_nowait((kind, payload))
    except queue.Full:
        # A bounded queue guarantees observability cannot exhaust memory or
        # block the agent during a tool/API burst.
        pass


def _execute(sql: str, params: tuple[Any, ...]) -> None:
    _enqueue("sql", (sql, params))


def _audit(event: str, **metadata: Any) -> None:
    record: dict[str, Any] = {"ts": _now(), "event": event}
    for key, value in metadata.items():
        if value is None or _SENSITIVE_KEY.search(str(key)):
            continue
        key_text = str(key)[:80]
        if key_text.lower() in _COMMAND_VALUE_KEYS:
            record["command_program"] = _command_program(value)
            continue
        sanitized = _safe_audit_value(value, 800)
        if sanitized is not None:
            record[key_text] = sanitized
    _enqueue("audit", (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


