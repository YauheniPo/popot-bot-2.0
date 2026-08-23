"""Read-only dashboard API for the Hermes observability SQLite database."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query


router = APIRouter()
_PERIOD_RE = re.compile(r"([1-9][0-9]*)([hd])$")
_MAX_PERIOD_SECONDS = 366 * 86400


def _home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _period(value: str) -> tuple[str, int]:
    match = _PERIOD_RE.fullmatch(value.strip().lower())
    if not match:
        raise HTTPException(status_code=422, detail="period must be 24h, 7d, or 30d")
    seconds = int(match.group(1)) * (3600 if match.group(2) == "h" else 86400)
    if seconds > _MAX_PERIOD_SECONDS:
        raise HTTPException(status_code=422, detail="period must not exceed 366d")
    return value.strip().lower(), seconds


def _cutoff(seconds: int) -> str:
    return datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc).isoformat(timespec="milliseconds")


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _number(value: Any) -> int | float:
    return value if isinstance(value, (int, float)) else 0


@router.get("/summary")
def summary(period: str = Query("24h", max_length=8)) -> dict[str, Any]:
    """Return aggregated, non-sensitive observability data for the dashboard."""

    label, seconds = _period(period)
    database = _home() / "ops" / "metrics.db"
    health_state = _home() / "ops" / "health-state"
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    health_issues: list[str] = []
    try:
        if health_state.is_file():
            health_issues = [line[:80] for line in health_state.read_text(encoding="utf-8").splitlines() if line.strip()][:20]
    except OSError:
        health_issues = ["health-state-unreadable"]

    try:
        database_exists = database.is_file()
    except OSError as error:
        raise HTTPException(status_code=503, detail="metrics database is temporarily unavailable") from error

    if not database_exists:
        return {
            "available": False,
            "period": label,
            "generated_at": generated_at,
            "summary": {},
            "models": [],
            "tools": [],
            "timeline": [],
            "health": {"issues": health_issues, "database_bytes": 0},
        }

    cutoff = _cutoff(seconds)
    bucket = "substr(ts,1,10)" if seconds > 48 * 3600 else "substr(ts,1,13) || ':00:00Z'"
    try:
        connection = _connect(database)
        try:
            aggregate = _rows(
                connection,
                "SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens, "
                "COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, "
                "COALESCE(SUM(cost_usd),0) cost_usd, "
                "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) errors "
                "FROM api_calls WHERE ts>=?",
                (cutoff,),
            )[0]
            tools_summary = _rows(
                connection,
                "SELECT COUNT(*) calls, COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) errors "
                "FROM tool_calls WHERE ts>=?",
                (cutoff,),
            )[0]
            models = _rows(
                connection,
                "SELECT COALESCE(provider,'?') provider, COALESCE(model,'?') model, COUNT(*) calls, "
                "COALESCE(SUM(total_tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost_usd, "
                "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) errors "
                "FROM api_calls WHERE ts>=? GROUP BY provider, model "
                "ORDER BY SUM(cost_usd) DESC, SUM(total_tokens) DESC LIMIT 12",
                (cutoff,),
            )
            tools = _rows(
                connection,
                "SELECT COALESCE(tool_name,'?') name, COUNT(*) calls, "
                "COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) errors, "
                "COALESCE(AVG(duration_ms),0) avg_duration_ms FROM tool_calls WHERE ts>=? "
                "GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 12",
                (cutoff,),
            )
            timeline = _rows(
                connection,
                f"SELECT {bucket} bucket, COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens, "
                "COALESCE(SUM(cost_usd),0) cost_usd FROM api_calls WHERE ts>=? "
                f"GROUP BY {bucket} ORDER BY {bucket}",
                (cutoff,),
            )
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise HTTPException(status_code=503, detail="metrics database is temporarily unavailable") from error

    try:
        database_bytes = database.stat().st_size
    except OSError:
        database_bytes = 0

    return {
        "available": True,
        "period": label,
        "generated_at": generated_at,
        "summary": {
            "api_calls": _number(aggregate.get("calls")),
            "tokens": _number(aggregate.get("tokens")),
            "input_tokens": _number(aggregate.get("input_tokens")),
            "output_tokens": _number(aggregate.get("output_tokens")),
            "cost_usd": _number(aggregate.get("cost_usd")),
            "api_errors": _number(aggregate.get("errors")),
            "tool_calls": _number(tools_summary.get("calls")),
            "tool_errors": _number(tools_summary.get("errors")),
        },
        "models": models,
        "tools": tools,
        "timeline": timeline,
        "health": {
            "issues": health_issues,
            "database_bytes": database_bytes,
        },
    }
