"""Privacy-aware local observability for Hermes.

The plugin deliberately stores metadata, not prompts, responses, tool output,
email bodies, browser contents, or provider error bodies.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


# Hooks execute in the agent's request path. Observability must never make a
# response wait for disk, SQLite locks, journald, or a slow filesystem, so they
# only enqueue tiny metadata records. A single daemon worker batches writes.
_EVENT_QUEUE: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False
_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s&;]+"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--(?:password|passwd|secret|token|api[-_]?key|authorization|auth)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_URL_USERINFO = re.compile(r"(?i)(https?://)[^/@\s]+@")
_SAFE_ARG_KEYS = {
    "command",
    "path",
    "file_path",
    "directory",
    "url",
    "repo",
    "branch",
    "operation",
    "action",
}
_METRICS_PERIODS = {"1h", "24h", "7d", "30d"}
_PROMETHEUS_HOSTS = {"127.0.0.1", "localhost", "prometheus"}

OPS_METRICS_SCHEMA: dict[str, Any] = {
    "name": "ops_metrics",
    "description": (
        "Read-only Hermes operational metrics. Use when the user asks about "
        "current gateway, memory, disk, load, model usage, token usage, tool "
        "errors, or cost. Data comes only from the local Hermes SQLite database "
        "and the private Prometheus instance; it cannot modify Hermes, Grafana, "
        "or the VPS."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "enum": ["overview", "resources", "activity", "models", "tools"],
                "description": "overview combines current health and selected-period activity.",
            },
            "period": {
                "type": "string",
                "enum": ["1h", "24h", "7d", "30d"],
                "description": "Activity window. Default: 24h.",
            },
        },
        "additionalProperties": False,
    },
}


def _home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _paths() -> tuple[Path, Path, Path]:
    root = _home()
    return root / "ops", root / "ops" / "metrics.db", root / "logs" / "ops-audit.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _short(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    text = _SECRET_TEXT.sub(lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]", text)
    text = _SECRET_FLAG.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = re.sub(r"-----BEGIN [^-]+ PRIVATE KEY-----.*", "[REDACTED PRIVATE KEY]", text, flags=re.IGNORECASE)
    return text[:limit]


def _id(value: Any) -> str:
    return _short(value, 160)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            result = value.model_dump()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        try:
            return dict(vars(value))
        except Exception:
            return {}
    return {}


def _nested_number(data: dict[str, Any], *names: str) -> float | None:
    for name in names:
        if name in data:
            return _number(data.get(name))
    for nested_name in ("usage", "token_usage", "details", "input_tokens_details", "output_tokens_details"):
        nested = _mapping(data.get(nested_name))
        if nested:
            result = _nested_number(nested, *names)
            if result is not None:
                return result
    return None


def _safe_args(args: Any) -> dict[str, Any]:
    data = _mapping(args)
    summary: dict[str, Any] = {"arg_keys": sorted(str(key)[:80] for key in data.keys())[:40]}
    for key, value in data.items():
        key_text = str(key)
        if _SENSITIVE_KEY.search(key_text) or key_text.lower() not in _SAFE_ARG_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)):
            if key_text.lower() == "url":
                try:
                    parsed = urlsplit(str(value))
                    value = f"{parsed.scheme}://{parsed.hostname or ''}"
                except ValueError:
                    value = "[invalid-url]"
            summary[key_text[:80]] = _short(value, 600 if key_text == "command" else 240)
    return summary


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
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and path.stat().st_size + len(payload) > max_bytes:
            # Keep one previous file only. Audit is diagnostic metadata, not a
            # compliance archive, and bounded retention protects the VPS disk.
            path.replace(path.with_name(path.name + ".1"))
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
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
        if isinstance(value, dict):
            record[str(key)[:80]] = value
        elif isinstance(value, (str, int, float, bool)):
            record[str(key)[:80]] = _short(value, 800)
    _enqueue("audit", (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode())


def _price(provider: str, model: str, input_tokens: int, output_tokens: int, cache_tokens: int) -> tuple[float, str]:
    price_file = _home() / "ops" / "model-prices.json"
    try:
        prices = json.loads(price_file.read_text(encoding="utf-8"))
        entry = prices.get(f"{provider}/{model}") or prices.get(model)
        if not isinstance(entry, dict):
            return 0.0, "unavailable"
        uncached_input = max(input_tokens - cache_tokens, 0)
        cost = (
            uncached_input * _number(entry.get("input_per_million"))
            + output_tokens * _number(entry.get("output_per_million"))
            + cache_tokens * _number(entry.get("cache_read_per_million"))
        ) / 1_000_000
        return cost, "price-file"
    except Exception:
        return 0.0, "unavailable"


def _usage_values(usage: Any) -> tuple[int, int, int, int, float, str]:
    data = _mapping(usage)
    input_tokens = int(_nested_number(data, "input_tokens", "prompt_tokens") or 0)
    output_tokens = int(_nested_number(data, "output_tokens", "completion_tokens") or 0)
    cache_tokens = int(_nested_number(data, "cache_read_tokens", "cached_tokens", "cache_tokens") or 0)
    total_tokens = int(_nested_number(data, "total_tokens") or 0) or input_tokens + output_tokens
    provider_cost = _nested_number(data, "cost_usd", "total_cost", "cost")
    if provider_cost is not None:
        return input_tokens, output_tokens, cache_tokens, total_tokens, provider_cost, "provider"
    return input_tokens, output_tokens, cache_tokens, total_tokens, 0.0, "unavailable"


def _pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", turn_id: str = "", **kwargs: Any) -> None:
    _audit("tool.start", tool=_id(tool_name), session_id=_id(task_id), turn_id=_id(turn_id), args=_safe_args(args))


def _post_tool_call(
    tool_name: str = "", args: Any = None, result: Any = None, task_id: str = "",
    turn_id: str = "", duration_ms: Any = 0, status: str = "", **kwargs: Any
) -> None:
    final_status = _id(status or "ok")
    duration = _number(duration_ms or kwargs.get("duration") or kwargs.get("elapsed_ms"))
    _execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), _id(task_id or kwargs.get("session_id")), _id(turn_id), _id(tool_name), final_status, duration),
    )
    _audit("tool.end", tool=_id(tool_name), session_id=_id(task_id), turn_id=_id(turn_id), status=final_status, duration_ms=duration)


def _pre_api_request(**kwargs: Any) -> None:
    _audit(
        "api.start",
        request_id=_id(kwargs.get("api_request_id")),
        session_id=_id(kwargs.get("session_id")),
        turn_id=_id(kwargs.get("turn_id")),
        provider=_id(kwargs.get("provider")),
        model=_id(kwargs.get("model")),
        platform=_id(kwargs.get("platform")),
        approximate_input_tokens=int(_number(kwargs.get("approx_input_tokens"))),
        retry_count=int(_number(kwargs.get("retry_count"))),
    )


def _post_api_request(**kwargs: Any) -> None:
    provider = _id(kwargs.get("provider"))
    model = _id(kwargs.get("response_model") or kwargs.get("model"))
    input_tokens, output_tokens, cache_tokens, total_tokens, cost, source = _usage_values(kwargs.get("usage"))
    if source == "unavailable":
        cost, source = _price(provider, model, input_tokens, output_tokens, cache_tokens)
    duration_ms = _number(kwargs.get("api_duration"))
    # Hermes currently reports API duration in seconds. Accept explicit *_ms if added later.
    if "api_duration_ms" in kwargs:
        duration_ms = _number(kwargs.get("api_duration_ms"))
    elif duration_ms and duration_ms < 10_000:
        duration_ms *= 1000
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), provider, model, _id(kwargs.get("platform")), "ok",
        duration_ms, input_tokens, output_tokens, cache_tokens, total_tokens, cost, source,
        _id(kwargs.get("finish_reason")), 0, 0,
    )
    _execute("INSERT INTO api_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    _audit(
        "api.end", request_id=values[1], session_id=values[2], turn_id=values[3],
        provider=provider, model=model, platform=values[6], status="ok",
        duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_tokens, total_tokens=total_tokens, cost_usd=round(cost, 8),
        cost_source=source,
    )


def _api_request_error(**kwargs: Any) -> None:
    duration_ms = _number(kwargs.get("api_duration"))
    if duration_ms and duration_ms < 10_000:
        duration_ms *= 1000
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), _id(kwargs.get("provider")), _id(kwargs.get("model")),
        _id(kwargs.get("platform")), "error", duration_ms, 0, 0, 0, 0, 0.0,
        "unavailable", _id(kwargs.get("reason")), int(_number(kwargs.get("status_code"))),
        int(_number(kwargs.get("retry_count"))),
    )
    _execute("INSERT INTO api_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    _audit(
        "api.error", request_id=values[1], session_id=values[2], turn_id=values[3],
        provider=values[4], model=values[5], platform=values[6], status_code=values[16],
        retry_count=values[17], retryable=bool(kwargs.get("retryable")), reason=values[15],
    )


def _on_session_start(session_id: str = "", model: str = "", platform: str = "", **kwargs: Any) -> None:
    _execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'start', 0, 0, 0)",
        (_now(), _id(session_id), _id(model), _id(platform)),
    )
    _audit("session.start", session_id=_id(session_id), model=_id(model), platform=_id(platform))


def _on_session_end(session_id: str = "", model: str = "", platform: str = "", **kwargs: Any) -> None:
    completed = int(bool(kwargs.get("completed")))
    interrupted = int(bool(kwargs.get("interrupted")))
    failed = int(bool(kwargs.get("failed")) or (not completed and not interrupted))
    _execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'end', ?, ?, ?)",
        (_now(), _id(session_id), _id(model), _id(platform), completed, failed, interrupted),
    )
    _audit(
        "session.end", session_id=_id(session_id), model=_id(model), platform=_id(platform),
        completed=completed, failed=failed, interrupted=interrupted,
    )


def _pre_approval_request(**kwargs: Any) -> None:
    _execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, 'request', '', ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("turn_id")),
         _id(kwargs.get("surface")), _id(kwargs.get("pattern_key"))),
    )
    _audit(
        "approval.request", session_id=_id(kwargs.get("session_key")),
        turn_id=_id(kwargs.get("turn_id")), surface=_id(kwargs.get("surface")),
        pattern_key=_id(kwargs.get("pattern_key")), command=_short(kwargs.get("command"), 600),
    )


def _post_approval_response(**kwargs: Any) -> None:
    choice = _id(kwargs.get("choice"))
    _execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, 'response', ?, ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("turn_id")),
         _id(kwargs.get("surface")), choice, _id(kwargs.get("pattern_key"))),
    )
    _audit(
        "approval.response", session_id=_id(kwargs.get("session_key")),
        turn_id=_id(kwargs.get("turn_id")), surface=_id(kwargs.get("surface")),
        pattern_key=_id(kwargs.get("pattern_key")), choice=choice,
        command=_short(kwargs.get("command"), 600), decided_by=_id(kwargs.get("decided_by")),
    )


def _pre_command(**kwargs: Any) -> None:
    command = _id(kwargs.get("command"))
    _execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("surface")),
         _id(kwargs.get("platform")), command),
    )
    # args_raw is intentionally omitted: slash-command arguments can contain secrets.
    _audit(
        "command", session_id=_id(kwargs.get("session_key")), surface=_id(kwargs.get("surface")),
        platform=_id(kwargs.get("platform")), command=command, alias=_id(kwargs.get("alias_used")),
    )


def _period(raw: str) -> tuple[str, str]:
    text = (raw or "24h").strip().lower()
    match = re.fullmatch(r"([1-9][0-9]*)([hd])", text)
    if not match:
        raise ValueError("period must look like 24h, 7d, or 30d")
    amount = int(match.group(1))
    seconds = amount * (3600 if match.group(2) == "h" else 86400)
    if seconds > 366 * 86400:
        raise ValueError("maximum period is 366d")
    cutoff = datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc).isoformat(timespec="milliseconds")
    return cutoff, text


def _query(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    try:
        connection = _connect()
        try:
            return list(connection.execute(sql, params).fetchall())
        finally:
            connection.close()
    except Exception:
        return []


def _prometheus_base_url() -> str | None:
    """Return only the local/internal Prometheus endpoint, never a user URL."""

    raw = os.environ.get("HERMES_PROMETHEUS_URL", "http://127.0.0.1:9090").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in _PROMETHEUS_HOSTS or parsed.username or parsed.password:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return raw.rstrip("/")


def _prometheus_vector(expression: str) -> list[tuple[dict[str, Any], float]] | None:
    base_url = _prometheus_base_url()
    if base_url is None:
        return None
    url = base_url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=1.0) as response:  # noqa: S310 - URL is host allowlisted above.
            body = json.loads(response.read(256 * 1024).decode("utf-8"))
        if body.get("status") != "success":
            return None
        result = body.get("data", {}).get("result", [])
        if not isinstance(result, list):
            return None
        values: list[tuple[dict[str, Any], float]] = []
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("metric"), dict) or not isinstance(item.get("value"), list):
                continue
            values.append((item["metric"], float(item["value"][1])))
        return values
    except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _activity_snapshot(period: str) -> dict[str, Any]:
    window = period if period in _METRICS_PERIODS else "24h"
    cutoff, _ = _period(window)
    api_rows = _query(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0), "
        "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) FROM api_calls WHERE ts>=?",
        (cutoff,),
    )
    tool_rows = _query(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) "
        "FROM tool_calls WHERE ts>=?",
        (cutoff,),
    )
    api_calls, tokens, cost_usd, api_errors = api_rows[0] if api_rows else (0, 0, 0.0, 0)
    tool_calls, tool_errors = tool_rows[0] if tool_rows else (0, 0)
    return {
        "api_calls": api_calls,
        "api_errors": api_errors,
        "tokens": tokens,
        "cost_usd": round(float(cost_usd), 6),
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
    }


def _resources_snapshot() -> dict[str, Any]:
    values: dict[str, float | None] = {
        "gateway_up": None,
        "disk_used_ratio": None,
        "memory_available_ratio": None,
        "load_1m": None,
        "load_5m": None,
        "load_15m": None,
    }
    rows = _prometheus_vector('{__name__=~"hermes_gateway_up|hermes_host_disk_used_ratio|hermes_host_memory_available_ratio|hermes_host_load"}')
    for labels, value in rows or []:
        name = labels.get("__name__")
        if name == "hermes_gateway_up":
            values["gateway_up"] = value
        elif name == "hermes_host_disk_used_ratio":
            values["disk_used_ratio"] = value
        elif name == "hermes_host_memory_available_ratio":
            values["memory_available_ratio"] = value
        elif name == "hermes_host_load" and labels.get("window") in {"1m", "5m", "15m"}:
            values[f"load_{labels['window']}"] = value
    return {key: (round(value, 6) if value is not None else None) for key, value in values.items()}


def _database_breakdown(period: str, kind: str) -> list[dict[str, Any]]:
    try:
        cutoff, _ = _period(period)
    except ValueError:
        cutoff, _ = _period("24h")
    if kind == "models":
        rows = _query(
            "SELECT COALESCE(provider,'?'), COALESCE(model,'?'), COUNT(*), "
            "COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0), "
            "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) "
            "FROM api_calls WHERE ts>=? GROUP BY provider,model "
            "ORDER BY SUM(total_tokens) DESC LIMIT 12",
            (cutoff,),
        )
        return [
            {"provider": provider, "model": model, "calls": calls, "tokens": tokens, "cost_usd": cost, "errors": errors}
            for provider, model, calls, tokens, cost, errors in rows
        ]
    rows = _query(
        "SELECT COALESCE(tool_name,'?'), COUNT(*), "
        "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0), "
        "COALESCE(AVG(duration_ms),0) FROM tool_calls WHERE ts>=? "
        "GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 12",
        (cutoff,),
    )
    return [
        {"tool": name, "calls": calls, "errors": errors, "average_duration_ms": round(float(duration), 2)}
        for name, calls, errors, duration in rows
    ]


def _metrics_snapshot(view: str, period: str) -> dict[str, Any]:
    selected_view = view if view in {"overview", "resources", "activity", "models", "tools"} else "overview"
    window = period if period in _METRICS_PERIODS else "24h"
    result: dict[str, Any] = {
        "source": "local Hermes SQLite and private Prometheus",
        "period": window,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if selected_view in {"overview", "resources"}:
        result["resources"] = _resources_snapshot()
    if selected_view in {"overview", "activity"}:
        result["activity"] = _activity_snapshot(window)
    if selected_view in {"overview", "models"}:
        result["models"] = _database_breakdown(window, "models")
    if selected_view in {"overview", "tools"}:
        result["tools"] = _database_breakdown(window, "tools")
    return result


def _ops_metrics_tool(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    """Expose a deliberately small, read-only observability surface to the LLM."""

    values = args if isinstance(args, dict) else {}
    return json.dumps(_metrics_snapshot(str(values.get("view", "overview")), str(values.get("period", "24h"))), ensure_ascii=False)


def _ops_command(raw_args: str) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError as error:
        return f"Invalid arguments: {error}"
    topic = parts[0].lower() if parts else "summary"
    if topic == "help":
        return "Usage: /ops [summary|system|models|tools|costs|commands|health] [1h|24h|7d|30d]"
    if topic == "health":
        state = _home() / "ops" / "health-state"
        issues = state.read_text(encoding="utf-8").strip() if state.exists() else ""
        return "VPS health: OK" if not issues else "VPS health alerts:\n" + issues
    try:
        cutoff, label = _period(parts[1] if len(parts) > 1 else "24h")
    except ValueError as error:
        return str(error)

    if topic == "system":
        snapshot = _metrics_snapshot("overview", label)
        resources = snapshot.get("resources", {})
        activity = snapshot.get("activity", {})
        return (
            f"Hermes system ({label})\n"
            f"Gateway: {resources.get('gateway_up')}\n"
            f"Disk used: {resources.get('disk_used_ratio')}\n"
            f"Memory available: {resources.get('memory_available_ratio')}\n"
            f"Load 1m/5m/15m: {resources.get('load_1m')} / {resources.get('load_5m')} / {resources.get('load_15m')}\n"
            f"API calls: {activity.get('api_calls')} ({activity.get('api_errors')} errors)\n"
            f"Tokens: {activity.get('tokens')}; tools: {activity.get('tool_calls')} ({activity.get('tool_errors')} errors)\n"
            f"Cost USD: {activity.get('cost_usd')}"
        )

    if topic == "summary":
        api = _query(
            "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0), "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM api_calls WHERE ts>=?", (cutoff,),
        )
        tools = _query(
            "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM tool_calls WHERE ts>=?", (cutoff,),
        )
        commands = _query("SELECT COUNT(*) FROM commands WHERE ts>=?", (cutoff,))
        a = api[0] if api else (0, 0, 0, 0)
        t = tools[0] if tools else (0, 0)
        c = commands[0][0] if commands else 0
        return (
            f"Hermes activity ({label})\n"
            f"API calls: {a[0]} ({a[3] or 0} errors)\nTokens: {a[1] or 0:,}\n"
            f"Estimated/provider cost: ${float(a[2] or 0):.4f}\n"
            f"Tool calls: {t[0]} ({t[1] or 0} errors)\nSlash commands: {c}"
        )
    if topic in {"models", "costs"}:
        rows = _query(
            "SELECT COALESCE(provider,'?'), COALESCE(model,'?'), COUNT(*), "
            "COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0) "
            "FROM api_calls WHERE ts>=? GROUP BY provider,model ORDER BY SUM(cost_usd) DESC, SUM(total_tokens) DESC LIMIT 15",
            (cutoff,),
        )
        lines = [f"Models ({label})", "provider/model — calls | tokens | cost"]
        lines.extend(f"{p}/{m} — {calls} | {tokens:,} | ${float(cost):.4f}" for p, m, calls, tokens, cost in rows)
        return "\n".join(lines) if rows else f"No model activity for {label}"
    if topic == "tools":
        rows = _query(
            "SELECT tool_name, COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), "
            "COALESCE(AVG(duration_ms),0) FROM tool_calls WHERE ts>=? GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 20",
            (cutoff,),
        )
        lines = [f"Tools ({label})", "tool — calls | errors | avg ms"]
        lines.extend(f"{name} — {calls} | {errors or 0} | {float(avg):.0f}" for name, calls, errors, avg in rows)
        return "\n".join(lines) if rows else f"No tool activity for {label}"
    if topic == "commands":
        rows = _query(
            "SELECT command, COUNT(*) FROM commands WHERE ts>=? GROUP BY command ORDER BY COUNT(*) DESC LIMIT 20", (cutoff,),
        )
        lines = [f"Commands ({label})"]
        lines.extend(f"/{name} — {count}" for name, count in rows)
        return "\n".join(lines) if rows else f"No slash commands for {label}"
    return "Usage: /ops [summary|system|models|tools|costs|commands|health] [1h|24h|7d|30d]"


def register(ctx: Any) -> None:
    """Register metadata-only observers and a no-LLM reporting command."""
    _db().close()
    _start_worker()
    hooks = {
        "pre_tool_call": _pre_tool_call,
        "post_tool_call": _post_tool_call,
        "pre_api_request": _pre_api_request,
        "post_api_request": _post_api_request,
        "api_request_error": _api_request_error,
        "on_session_start": _on_session_start,
        "on_session_end": _on_session_end,
        "pre_approval_request": _pre_approval_request,
        "post_approval_response": _post_approval_response,
        "pre_command": _pre_command,
    }
    for name, callback in hooks.items():
        ctx.register_hook(name, callback)
    ctx.register_command(
        "ops",
        handler=_ops_command,
        description="Local VPS/model/tool activity report (no LLM tokens)",
        args_hint="[summary|system|models|tools|costs|commands|health] [1h|24h|7d|30d]",
    )
    ctx.register_tool(
        name="ops_metrics",
        toolset="observability",
        schema=OPS_METRICS_SCHEMA,
        handler=_ops_metrics_tool,
        description=OPS_METRICS_SCHEMA["description"],
        emoji="📊",
    )
