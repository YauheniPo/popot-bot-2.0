#!/usr/bin/env python3
"""Export Hermes and basic VPS health as Prometheus text metrics."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional, Union


def home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def label(value: object) -> str:
    return str(value or "unknown").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")[:180]


def metric(
    name: str,
    value: Union[float, int],
    labels: Optional[dict[str, object]] = None,
) -> str:
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{key}="{label(item)}"' for key, item in sorted(labels.items())) + "}"
    return f"{name}{suffix} {value}"


def rows(database: Path, sql: str) -> Iterable[tuple]:
    if not database.exists():
        return []
    try:
        # Read-only mode prevents the exporter from writing the database while
        # still letting SQLite include recently committed records in its WAL.
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
        try:
            return list(connection.execute(sql).fetchall())
        finally:
            connection.close()
    except sqlite3.Error:
        # A concurrently created or rotated database must not stop host health
        # metrics from being exported; the next collector interval retries.
        return []


def memory_ratio() -> float:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) / max(values.get("MemTotal", 0), 1)
    except (OSError, ValueError):
        return 0.0


def gateway_up() -> int:
    service = os.environ.get("HERMES_GATEWAY_SERVICE", "hermes-gateway.service")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return int(result.returncode == 0)
    except (OSError, subprocess.TimeoutExpired):
        return 0


def backup_ages() -> tuple[int, float, float]:
    configured = Path(os.environ.get("HERMES_BACKUP_DIR", str(home().parent / "hermes-backups"))).expanduser()
    snapshots = home() / "state-snapshots"
    if not configured.is_dir() and not snapshots.is_dir():
        return 0, -1, -1
    newest = 0.0
    newest_full = 0.0
    try:
        if configured.is_dir():
            for path in configured.iterdir():
                if path.is_file() and not path.name.endswith(".partial.zip"):
                    newest = max(newest, path.stat().st_mtime)
                    if path.name.startswith("scheduled-full-") and path.name.endswith(".zip"):
                        newest_full = max(newest_full, path.stat().st_mtime)
        if snapshots.is_dir():
            for path in snapshots.iterdir():
                if path.is_dir():
                    newest = max(newest, path.stat().st_mtime)
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
    lines = [
        "# HELP hermes_gateway_up Whether the Hermes gateway systemd service is active.",
        "# TYPE hermes_gateway_up gauge",
        metric("hermes_gateway_up", gateway_up()),
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
    for provider, model, status, calls, input_tokens, output_tokens, cache_tokens, total_tokens, cost, duration_ms in rows(
        database,
        "SELECT COALESCE(provider,'unknown'),COALESCE(model,'unknown'),status,COUNT(*),"
        "COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(cache_read_tokens),0),"
        "COALESCE(SUM(total_tokens),0),COALESCE(SUM(cost_usd),0),COALESCE(SUM(duration_ms),0) "
        "FROM api_calls GROUP BY provider,model,status",
    ):
        tags = {"provider": provider, "model": model, "status": status}
        lines.extend(
            [
                metric("hermes_api_calls_total", calls, tags),
                metric("hermes_input_tokens_total", input_tokens, tags),
                metric("hermes_output_tokens_total", output_tokens, tags),
                metric("hermes_cache_read_tokens_total", cache_tokens, tags),
                metric("hermes_tokens_total", total_tokens, tags),
                metric("hermes_cost_usd_total", cost, tags),
                metric("hermes_api_duration_ms_total", duration_ms, tags),
            ]
        )
    for tool, status, calls, average_ms, duration_ms in rows(
        database,
        "SELECT COALESCE(tool_name,'unknown'),status,COUNT(*),COALESCE(AVG(duration_ms),0),COALESCE(SUM(duration_ms),0) "
        "FROM tool_calls GROUP BY tool_name,status",
    ):
        tags = {"tool": tool, "status": status}
        lines.append(metric("hermes_tool_calls_total", calls, tags))
        lines.append(metric("hermes_tool_duration_ms_average", average_ms, tags))
        lines.append(metric("hermes_tool_duration_ms_total", duration_ms, tags))
    for command, calls in rows(database, "SELECT COALESCE(command,'unknown'),COUNT(*) FROM commands GROUP BY command"):
        lines.append(metric("hermes_commands_total", calls, {"command": command}))
    for model, platform, completed, failed, interrupted, turns in rows(
        database,
        "SELECT COALESCE(model,'unknown'),COALESCE(platform,'unknown'),completed,failed,interrupted,COUNT(*) "
        "FROM sessions WHERE event='end' GROUP BY model,platform,completed,failed,interrupted",
    ):
        outcome = "completed" if completed else ("interrupted" if interrupted else "failed")
        lines.append(metric("hermes_turns_total", turns, {"model": model, "platform": platform, "outcome": outcome}))
    for choice, responses in rows(
        database,
        "SELECT COALESCE(choice,'unknown'),COUNT(*) FROM approvals WHERE event='response' GROUP BY choice",
    ):
        lines.append(metric("hermes_approval_responses_total", responses, {"choice": choice}))
    audit = root / "logs" / "ops-audit.jsonl"
    lines.append(metric("hermes_audit_log_bytes", audit.stat().st_size if audit.exists() else 0))
    database_bytes = sum(
        path.stat().st_size for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")) if path.exists()
    )
    lines.append(metric("hermes_metrics_database_bytes", database_bytes))

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, metrics_file_mode())
    temporary.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
