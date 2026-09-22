#!/usr/bin/env python3
"""Export Hermes and basic VPS health as Prometheus text metrics."""

from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Iterable


def home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def label(value: object) -> str:
    escaped = str(value or "unknown").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    escaped = escaped[:180]
    # Do not emit a truncated escape sequence at the label boundary.
    trailing = len(escaped) - len(escaped.rstrip("\\"))
    return escaped[:-1] if trailing % 2 else escaped


def metric(
    name: str,
    value: float | int,
    labels: dict[str, object] | None = None,
) -> str:
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{key}="{label(item)}"' for key, item in sorted(labels.items())) + "}"
    rendered = "NaN" if isinstance(value, float) and math.isnan(value) else str(value)
    return f"{name}{suffix} {rendered}"


def rows(database: Path, sql: str) -> Iterable[tuple]:
    if not database.exists():
        return []
    try:
        # Read-only mode prevents the exporter from writing the database while
        # still letting SQLite include recently committed records in its WAL.
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
        try:
            # Group on exactly the labels we expose, including unknown values
            # and length normalization, rather than distinct raw DB values.
            connection.create_function("metric_label", 1, label, deterministic=True)
            return list(connection.execute(sql).fetchall())
        finally:
            connection.close()
    except sqlite3.Error:
        # A concurrently created or rotated database must not stop host health
        # metrics from being exported; the next collector interval retries.
        return []


RATE_LIMIT_SQL = (
    "(status_code=429 OR lower(finish_reason) LIKE '%rate%limit%' OR lower(finish_reason) LIKE '%too many requests%')"
)
API_CALL_COLUMNS = (
    "provider", "model", "status", "status_code", "finish_reason", "weight", "input_tokens", "output_tokens",
    "cache_read_tokens", "total_tokens", "cost_usd", "duration_ms", "retry_count",
)


def existing_tables(database: Path) -> set[str]:
    return {name for (name,) in rows(database, "SELECT name FROM sqlite_master WHERE type='table'")}


def with_rollup(tables: set[str], table: str, live: str, columns: tuple[str, ...]) -> str:
    """Live rows plus the pruner's pre-aggregated rows, so counters never drop."""
    if f"{table}_rollup" not in tables:
        return live
    return f"{live} UNION ALL SELECT NULL AS ts, {', '.join(columns)} FROM {table}_rollup"


def api_calls_source(tables: set[str]) -> str:
    live = (
        "SELECT ts, COALESCE(provider,'') AS provider, COALESCE(model,'') AS model, COALESCE(status,'') AS status, "
        "COALESCE(status_code,0) AS status_code, COALESCE(finish_reason,'') AS finish_reason, 1 AS weight, "
        "COALESCE(input_tokens,0) AS input_tokens, COALESCE(output_tokens,0) AS output_tokens, "
        "COALESCE(cache_read_tokens,0) AS cache_read_tokens, COALESCE(total_tokens,0) AS total_tokens, "
        "COALESCE(cost_usd,0) AS cost_usd, COALESCE(duration_ms,0) AS duration_ms, "
        "COALESCE(retry_count,0) AS retry_count FROM api_calls"
    )
    return with_rollup(tables, "api_calls", live, API_CALL_COLUMNS)


def api_metrics(database: Path) -> list[str]:
    """Route counters and timestamps for Prometheus alerts and long-range graphs.

    Per-call analytics (latency percentiles, finish reasons, first-attempt
    success, model mismatch, fallbacks) are queried by Grafana directly from
    the SQLite snapshot written by analytics_snapshot().
    """
    tables = existing_tables(database)
    if "api_calls" not in tables:
        return []
    source = api_calls_source(tables)
    epoch = "MAX(CAST(strftime('%s',ts) AS REAL))"
    lines: list[str] = []
    route_status = "metric_label(provider), metric_label(model), metric_label(status)"
    for provider, model, status, calls, input_tokens, output_tokens, cache_tokens, total_tokens, cost, duration_ms in rows(
        database,
        f"WITH c AS ({source}) SELECT {route_status}, SUM(weight), SUM(input_tokens), SUM(output_tokens), "
        f"SUM(cache_read_tokens), SUM(total_tokens), SUM(cost_usd), SUM(duration_ms) FROM c GROUP BY {route_status}",
    ):
        tags = {"provider": provider, "model": model, "status": status}
        lines.extend([
            metric("hermes_api_calls_total", calls, tags),
            metric("hermes_input_tokens_total", input_tokens, tags),
            metric("hermes_output_tokens_total", output_tokens, tags),
            metric("hermes_cache_read_tokens_total", cache_tokens, tags),
            metric("hermes_tokens_total", total_tokens, tags),
            metric("hermes_cost_usd_total", cost, tags),
            metric("hermes_api_duration_ms_total", duration_ms, tags),
        ])
    lines.extend([
        '# HELP hermes_api_rate_limits_total API requests rejected by provider rate limiting, grouped by provider and model.',
        '# TYPE hermes_api_rate_limits_total counter',
        '# HELP hermes_api_last_rate_limit_timestamp_seconds Unix timestamp of the latest provider rate-limit response.',
        '# TYPE hermes_api_last_rate_limit_timestamp_seconds gauge',
    ])
    route = "metric_label(provider), metric_label(model)"
    for provider, model, rate_limits, last_rate_limit in rows(
        database,
        f"WITH c AS ({source}) SELECT {route}, SUM(weight), COALESCE({epoch},0) FROM c "
        f"WHERE {RATE_LIMIT_SQL} GROUP BY {route}",
    ):
        tags = {"provider": provider, "model": model}
        lines.append(metric("hermes_api_rate_limits_total", rate_limits, tags))
        lines.append(metric("hermes_api_last_rate_limit_timestamp_seconds", last_rate_limit, tags))
    lines.extend([
        '# HELP hermes_api_success_total Successful model API responses by provider and model.',
        '# TYPE hermes_api_success_total counter',
        '# HELP hermes_api_errors_total Failed model API responses by provider and model.',
        '# TYPE hermes_api_errors_total counter',
        '# HELP hermes_api_retries_total Provider retry attempts recorded for model API requests.',
        '# TYPE hermes_api_retries_total counter',
        '# HELP hermes_api_last_success_timestamp_seconds Unix timestamp of the latest successful model response.',
        '# TYPE hermes_api_last_success_timestamp_seconds gauge',
        '# HELP hermes_api_last_error_timestamp_seconds Unix timestamp of the latest failed model response.',
        '# TYPE hermes_api_last_error_timestamp_seconds gauge',
    ])
    for provider, model, successes, errors, retries, last_success, last_error in rows(
        database,
        f"WITH c AS ({source}) SELECT {route}, "
        "SUM(CASE WHEN status='ok' THEN weight ELSE 0 END), SUM(CASE WHEN status!='ok' THEN weight ELSE 0 END), "
        "SUM(retry_count), "
        "COALESCE(MAX(CASE WHEN status='ok' THEN CAST(strftime('%s',ts) AS REAL) END),0), "
        "COALESCE(MAX(CASE WHEN status!='ok' THEN CAST(strftime('%s',ts) AS REAL) END),0) "
        f"FROM c GROUP BY {route}",
    ):
        tags = {"provider": provider, "model": model}
        lines.extend([
            metric("hermes_api_success_total", successes, tags),
            metric("hermes_api_errors_total", errors, tags),
            metric("hermes_api_retries_total", retries, tags),
            metric("hermes_api_last_success_timestamp_seconds", last_success, tags),
            metric("hermes_api_last_error_timestamp_seconds", last_error, tags),
        ])
    return lines


def activity_metrics(database: Path) -> list[str]:
    """Tool, command, session and approval counters, including pruned rollups."""
    tables = existing_tables(database)
    lines: list[str] = []
    tools = with_rollup(
        tables, "tool_calls",
        "SELECT ts, COALESCE(tool_name,'') AS tool_name, COALESCE(status,'') AS status, 1 AS weight, "
        "COALESCE(duration_ms,0) AS duration_ms FROM tool_calls",
        ("tool_name", "status", "weight", "duration_ms"),
    )
    for tool, status, calls, duration_ms in rows(
        database,
        f"WITH c AS ({tools}) SELECT metric_label(tool_name), metric_label(status), SUM(weight), SUM(duration_ms) "
        "FROM c GROUP BY metric_label(tool_name), metric_label(status)",
    ):
        tags = {"tool": tool, "status": status}
        lines.append(metric("hermes_tool_calls_total", calls, tags))
        lines.append(metric("hermes_tool_duration_ms_average", duration_ms / calls if calls else 0, tags))
        lines.append(metric("hermes_tool_duration_ms_total", duration_ms, tags))
    commands = with_rollup(
        tables, "commands", "SELECT ts, COALESCE(command,'') AS command, 1 AS weight FROM commands", ("command", "weight"),
    )
    for command, calls in rows(
        database, f"WITH c AS ({commands}) SELECT metric_label(command), SUM(weight) FROM c GROUP BY metric_label(command)",
    ):
        lines.append(metric("hermes_commands_total", calls, {"command": command}))
    sessions = with_rollup(
        tables, "sessions",
        "SELECT ts, COALESCE(model,'') AS model, COALESCE(platform,'') AS platform, COALESCE(event,'') AS event, "
        "COALESCE(completed,0) AS completed, COALESCE(failed,0) AS failed, COALESCE(interrupted,0) AS interrupted, "
        "1 AS weight FROM sessions",
        ("model", "platform", "event", "completed", "failed", "interrupted", "weight"),
    )
    for model, platform, outcome, turns in rows(
        database,
        f"WITH c AS ({sessions}) SELECT metric_label(model), metric_label(platform), "
        "CASE WHEN completed THEN 'completed' WHEN interrupted THEN 'interrupted' ELSE 'failed' END AS outcome, SUM(weight) "
        "FROM c WHERE event='end' GROUP BY metric_label(model), metric_label(platform), outcome",
    ):
        lines.append(metric("hermes_turns_total", turns, {"model": model, "platform": platform, "outcome": outcome}))
    approvals = with_rollup(
        tables, "approvals",
        "SELECT ts, COALESCE(event,'') AS event, COALESCE(choice,'') AS choice, 1 AS weight FROM approvals",
        ("event", "choice", "weight"),
    )
    for choice, responses in rows(
        database,
        f"WITH c AS ({approvals}) SELECT metric_label(choice), SUM(weight) FROM c WHERE event='response' "
        "GROUP BY metric_label(choice)",
    ):
        lines.append(metric("hermes_approval_responses_total", responses, {"choice": choice}))
    return lines


def analytics_file_mode() -> int:
    """Snapshot mode: group-readable for Grafana by default, never wider than 0644."""
    value = os.environ.get("HERMES_ANALYTICS_MODE", "0640").strip()
    try:
        mode = int(value, 8)
    except ValueError:
        return 0o640
    return mode if mode in (0o600, 0o640, 0o644) else 0o640


def analytics_snapshot(database: Path) -> list[str]:
    """Publish a consistent, rollback-journal copy of metrics.db for Grafana.

    Grafana runs as another user and must not read the live WAL database in
    the private Hermes home. VACUUM INTO writes a compact copy in DELETE
    journal mode, so a read-only directory is enough for the SQLite datasource;
    the atomic rename keeps every query on a complete file.
    """
    configured = os.environ.get("HERMES_ANALYTICS_FILE", "").strip()
    if not configured:
        return []
    target = Path(configured).expanduser()
    lines = [
        "# HELP hermes_analytics_snapshot_timestamp_seconds Unix time of the latest metrics.db snapshot for Grafana; 0 means the snapshot failed.",
        "# TYPE hermes_analytics_snapshot_timestamp_seconds gauge",
    ]
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        if not database.exists():
            raise FileNotFoundError(database)
        temporary.unlink(missing_ok=True)
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
        try:
            connection.execute("VACUUM INTO ?", (str(temporary),))
        finally:
            connection.close()
        os.chmod(temporary, analytics_file_mode())
        temporary.replace(target)
        lines.append(metric("hermes_analytics_snapshot_timestamp_seconds", time.time()))
    except (OSError, sqlite3.Error):
        temporary.unlink(missing_ok=True)
        lines.append(metric("hermes_analytics_snapshot_timestamp_seconds", 0))
    return lines


def memory_ratio() -> float:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) / max(values.get("MemTotal", 0), 1)
    except (OSError, ValueError):
        return 0.0


def _request_timestamps(database: Path, now: float) -> list:
    """Read retention-window user timestamps in timestamp order."""
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        deadline = time.monotonic() + 1
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
        records = connection.execute(
            'SELECT role, timestamp FROM messages '
            "WHERE typeof(timestamp) IN ('real','integer') AND timestamp>0 AND timestamp<=? "
            + ("AND COALESCE(_compressed_summary,0)=0 " if '_compressed_summary' in columns else '')
            + 'ORDER BY timestamp, rowid', (now,)
        ).fetchall()
    finally:
        connection.close()
    return records


def _profile_last_activity(records: list, latest_user: float) -> float:
    """Latest user or assistant timestamp; the latest user record seeds it."""
    activity = latest_user
    for role, timestamp in records:
        if role in {'user', 'assistant'}:
            activity = max(activity, timestamp)
    return activity


def _timed_durations(records: list, now: float) -> tuple:
    """Pair user records with the next assistant record, bounded by 24 hours.

    Returns the retained, 1h, 24h and 7d duration sums and the latest completed
    response window (duration, start, end). The latest window starts unmatched
    so the first completed pair always defines it.
    """
    windows = (3600, 86400, 604800)
    durations = [0.0, 0.0, 0.0, 0.0]
    latest_duration = 0.0
    latest_start = 0.0
    latest_end = 0.0
    pending_user = None
    for role, timestamp in records:
        if role == 'user':
            pending_user = timestamp
        elif role == 'assistant' and pending_user is not None:
            duration = max(0.0, min(float(timestamp - pending_user), 86400.0))
            if pending_user >= latest_start:
                latest_duration = duration
                latest_start = pending_user
                latest_end = timestamp
            durations[3] += duration
            for index, window in enumerate(windows):
                if pending_user >= now - window:
                    durations[index] += duration
            pending_user = None
    return (*durations, latest_duration, latest_start, latest_end)


def _profile_request_counts(database: Path, now: float) -> tuple:
    """Read aggregate profile activity metadata without loading message text."""
    if database.is_symlink() or not database.is_file():
        raise OSError('Profile history unavailable')
    records = _request_timestamps(database, now)
    user_timestamps = [timestamp for role, timestamp in records if role == 'user']
    retained = len(user_timestamps)
    latest = max(user_timestamps, default=0)
    counts = tuple(sum(timestamp >= now - window for timestamp in user_timestamps)
                   for window in (3600, 86400, 604800))
    durations = _timed_durations(records, now)
    return (retained, latest, *counts, _profile_last_activity(records, latest), *durations)


def profile_request_metrics(root: Path, now: float | None = None) -> list[str]:
    """Counts of retained input records, not monotonic invocation counters."""
    now = time.time() if now is None else now
    profiles = [('default', root)]
    directory = root / 'profiles'
    if directory.is_dir() and not directory.is_symlink():
        profiles.extend((p.name, p) for p in sorted(directory.iterdir())
                        if p.is_dir() and not p.is_symlink() and (p / 'config.yaml').is_file()
                        and p.name != 'default' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', p.name))
    lines = [
        '# HELP hermes_profile_user_requests Retained user input records by profile and lookback window; excludes compression summaries. Not LLM calls or lifetime totals.',
        '# TYPE hermes_profile_user_requests gauge',
        '# HELP hermes_profile_last_request_timestamp_seconds Latest retained user input time; zero means empty history.',
        '# TYPE hermes_profile_last_request_timestamp_seconds gauge',
        '# HELP hermes_profile_last_activity_timestamp_seconds Latest retained profile activity time; zero means empty history.',
        '# TYPE hermes_profile_last_activity_timestamp_seconds gauge',
        '# HELP hermes_profile_response_duration_seconds Sum of user-to-assistant response durations by lookback window.',
        '# TYPE hermes_profile_response_duration_seconds gauge',
        '# HELP hermes_profile_last_request_duration_seconds Duration of the latest completed profile call.',
        '# TYPE hermes_profile_last_request_duration_seconds gauge',
        '# HELP hermes_profile_last_request_start_timestamp_seconds Start timestamp of the latest completed profile call.',
        '# TYPE hermes_profile_last_request_start_timestamp_seconds gauge',
        '# HELP hermes_profile_last_request_end_timestamp_seconds End timestamp of the latest completed profile call.',
        '# TYPE hermes_profile_last_request_end_timestamp_seconds gauge',
        '# HELP hermes_profile_history_readable Whether this profile history could be collected; missing or unreadable is not zero usage.',
        '# TYPE hermes_profile_history_readable gauge',
    ]
    for profile, path in profiles:
        tags = {'profile': profile}
        try:
            (retained, latest, hour, day, week, last_activity, hour_duration,
             day_duration, week_duration, retained_duration, latest_duration,
             latest_start, latest_end) = _profile_request_counts(path / 'state.db', now)
        except (OSError, sqlite3.Error):
            lines.append(metric('hermes_profile_history_readable', 0, tags))
            continue
        lines.append(metric('hermes_profile_history_readable', 1, tags))
        lines.append(metric('hermes_profile_last_request_timestamp_seconds', latest, tags))
        lines.append(metric('hermes_profile_last_activity_timestamp_seconds', last_activity, tags))
        lines.append(metric('hermes_profile_last_request_duration_seconds', latest_duration, tags))
        lines.append(metric('hermes_profile_last_request_start_timestamp_seconds', latest_start, tags))
        lines.append(metric('hermes_profile_last_request_end_timestamp_seconds', latest_end, tags))
        for window, count in [('1h', hour), ('24h', day), ('7d', week), ('retained', retained)]:
            lines.append(metric('hermes_profile_user_requests', count, {**tags, 'window': window}))
        for window, duration in [('1h', hour_duration), ('24h', day_duration),
                                 ('7d', week_duration), ('retained', retained_duration)]:
            lines.append(metric('hermes_profile_response_duration_seconds', duration,
                                {**tags, 'window': window}))
    return lines


def gateway_up() -> float:
    state = os.environ.get("HERMES_GATEWAY_STATE", "").strip().lower()
    if state == "unavailable":
        return float("nan")
    if state == "up":
        return 1.0
    if state == "down":
        return 0.0

    service = os.environ.get("HERMES_GATEWAY_SERVICE", "hermes-gateway.service")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return float(result.returncode == 0)
    except (OSError, subprocess.TimeoutExpired):
        return 0


def gateway_process_metrics() -> tuple[float, float]:
    """Return gateway CPU time and RSS from /proc, or NaN when unavailable."""
    service = os.environ.get("HERMES_GATEWAY_SERVICE", "hermes-gateway.service")
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=MainPID", "--value", service],
            check=False, capture_output=True, text=True, timeout=5,
        )
        pid = int(result.stdout.strip())
        if result.returncode or pid <= 0:
            raise ValueError("gateway has no main PID")
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        cpu_seconds = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
        rss_kib = next(
            int(line.split()[1]) for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines()
            if line.startswith("VmRSS:")
        )
        return cpu_seconds, rss_kib * 1024
    except (OSError, ValueError, IndexError, StopIteration, subprocess.TimeoutExpired):
        return float("nan"), float("nan")


def _newest_archive_time(configured: Path) -> float:
    """Return the newest non-partial archive mtime, or 0.0 when none exists."""
    newest = 0.0
    if not configured.is_dir():
        return newest
    for path in configured.iterdir():
        if not path.is_file() or path.name.endswith(".partial.zip"):
            continue
        newest = max(newest, path.stat().st_mtime)
    return newest


def _newest_scheduled_full_time(configured: Path) -> float:
    newest = 0.0
    if not configured.is_dir():
        return newest
    for path in configured.iterdir():
        if path.is_file() and path.name.startswith("scheduled-full-") and path.name.endswith(".zip"):
            newest = max(newest, path.stat().st_mtime)
    return newest


def _newest_snapshot_time(snapshots: Path) -> float:
    newest = 0.0
    if not snapshots.is_dir():
        return newest
    for path in snapshots.iterdir():
        if path.is_dir():
            newest = max(newest, path.stat().st_mtime)
    return newest


def backup_ages() -> tuple[int, float, float]:
    configured = Path(os.environ.get("HERMES_BACKUP_DIR", str(home().parent / "hermes-backups"))).expanduser()
    snapshots = home() / "state-snapshots"
    if not configured.is_dir() and not snapshots.is_dir():
        return 0, -1, -1
    newest = 0.0
    newest_full = 0.0
    try:
        newest = _newest_archive_time(configured)
        newest_full = _newest_scheduled_full_time(configured)
        newest = max(newest, _newest_snapshot_time(snapshots))
    except OSError:
        return 1, -1, -1
    return (
        1,
        time.time() - newest if newest else -1,
        time.time() - newest_full if newest_full else -1,
    )


def metrics_file_mode() -> int:
    """Return a deliberately narrow mode for the generated textfile."""

    value = os.environ.get("HERMES_METRICS_MODE", "0600").strip()
    try:
        mode = int(value, 8)
    except ValueError:
        return 0o600
    # Metrics contain aggregate operational data, never prompts or secrets.
    # The only wider supported mode is for an internal read-only collector.
    return mode if mode in (0o600, 0o640, 0o644) else 0o600


def main() -> int:
    root = home()
    database = root / "ops" / "metrics.db"
    target = Path(os.environ.get("HERMES_METRICS_FILE", str(root / "ops" / "metrics" / "hermes.prom"))).expanduser()
    disk_path = Path(os.environ.get("HERMES_DISK_PATH", str(root))).expanduser()
    disk = shutil.disk_usage(disk_path if disk_path.exists() else root.parent)
    filesystem = os.statvfs(disk_path if disk_path.exists() else root.parent)
    inode_total = filesystem.f_files
    inode_used_ratio = (
        (inode_total - filesystem.f_ffree) / inode_total if inode_total > 0 else 0.0
    )
    backup_configured, backup_seconds, full_backup_seconds = backup_ages()
    load1, load5, load15 = os.getloadavg()
    gateway_cpu_seconds, gateway_rss_bytes = gateway_process_metrics()
    lines = [
        "# HELP hermes_gateway_up Whether the Hermes gateway is active; NaN means this collector cannot observe it.",
        "# TYPE hermes_gateway_up gauge",
        metric("hermes_gateway_up", gateway_up()),
        metric("hermes_gateway_process_cpu_seconds_total", gateway_cpu_seconds),
        metric("hermes_gateway_process_resident_memory_bytes", gateway_rss_bytes),
        "# HELP hermes_host_disk_used_ratio Filesystem used fraction for the Hermes disk.",
        "# TYPE hermes_host_disk_used_ratio gauge",
        metric("hermes_host_disk_used_ratio", (disk.total - disk.free) / max(disk.total, 1)),
        metric("hermes_host_inode_used_ratio", inode_used_ratio),
        "# HELP hermes_host_memory_available_ratio Available memory fraction.",
        "# TYPE hermes_host_memory_available_ratio gauge",
        metric("hermes_host_memory_available_ratio", memory_ratio()),
        metric("hermes_host_load", load1, {"window": "1m"}),
        metric("hermes_host_load", load5, {"window": "5m"}),
        metric("hermes_host_load", load15, {"window": "15m"}),
        metric("hermes_backup_configured", backup_configured),
        metric("hermes_backup_age_seconds", backup_seconds),
        metric("hermes_full_backup_age_seconds", full_backup_seconds),
    ]
    lines.extend(api_metrics(database))
    lines.extend(activity_metrics(database))
    lines.extend(analytics_snapshot(database))
    audit = root / "logs" / "ops-audit.jsonl"
    lines.append(metric("hermes_audit_log_bytes", audit.stat().st_size if audit.exists() else 0))
    database_bytes = sum(
        path.stat().st_size for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")) if path.exists()
    )
    lines.append(metric("hermes_metrics_database_bytes", database_bytes))
    lines.extend(profile_request_metrics(root))

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, metrics_file_mode())
    temporary.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
