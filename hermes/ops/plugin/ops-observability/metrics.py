"""Read-only SQLite and private Prometheus metrics queries."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .storage import _connect

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



