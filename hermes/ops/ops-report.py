#!/usr/bin/env python3
"""Read-only reports from the Hermes local observability database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def cutoff(period: str) -> tuple[str, str]:
    match = re.fullmatch(r"([1-9][0-9]*)([hd])", period.strip().lower())
    if not match:
        raise ValueError("period must look like 24h, 7d, or 30d")
    seconds = int(match.group(1)) * (3600 if match.group(2) == "h" else 86400)
    if seconds > 366 * 86400:
        raise ValueError("maximum period is 366d")
    value = datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc).isoformat(timespec="milliseconds")
    return value, period.lower()


def query(connection: sqlite3.Connection, sql: str, since: str) -> list[sqlite3.Row]:
    return list(connection.execute(sql, (since,)).fetchall())


def report(connection: sqlite3.Connection, since: str, period: str) -> dict[str, Any]:
    api = query(
        connection,
        "SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens, "
        "COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, "
        "COALESCE(SUM(cache_read_tokens),0) cache_read_tokens, COALESCE(SUM(cost_usd),0) cost_usd, "
        "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors FROM api_calls WHERE ts>=?",
        since,
    )[0]
    tools = query(
        connection,
        "SELECT COUNT(*) calls, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors "
        "FROM tool_calls WHERE ts>=?",
        since,
    )[0]
    commands = query(connection, "SELECT COUNT(*) calls FROM commands WHERE ts>=?", since)[0]
    models = query(
        connection,
        "SELECT COALESCE(provider,'?') provider, COALESCE(model,'?') model, COUNT(*) calls, "
        "COALESCE(SUM(total_tokens),0) tokens, COALESCE(SUM(input_tokens),0) input_tokens, "
        "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(cache_read_tokens),0) cache_read_tokens, "
        "COALESCE(SUM(cost_usd),0) cost_usd, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors "
        "FROM api_calls WHERE ts>=? GROUP BY provider,model ORDER BY SUM(cost_usd) DESC,SUM(total_tokens) DESC",
        since,
    )
    tool_rows = query(
        connection,
        "SELECT tool_name, COUNT(*) calls, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errors, "
        "COALESCE(AVG(duration_ms),0) avg_duration_ms FROM tool_calls WHERE ts>=? "
        "GROUP BY tool_name ORDER BY COUNT(*) DESC",
        since,
    )
    command_rows = query(
        connection,
        "SELECT command, COUNT(*) calls FROM commands WHERE ts>=? GROUP BY command ORDER BY COUNT(*) DESC",
        since,
    )
    return {
        "period": period,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": dict(api) | {
            "tool_calls": tools["calls"],
            "tool_errors": tools["errors"] or 0,
            "slash_commands": commands["calls"],
        },
        "models": [dict(row) for row in models],
        "tools": [dict(row) for row in tool_rows],
        "commands": [dict(row) for row in command_rows],
    }


def as_markdown(data: dict[str, Any]) -> str:
    summary = data["summary"]
    lines = [
        f"# Hermes activity — {data['period']}",
        "",
        f"API calls: {summary['calls']} ({summary['errors'] or 0} errors)",
        f"Tokens: {summary['tokens']:,} (input {summary['input_tokens']:,}, output {summary['output_tokens']:,}, cache-read {summary['cache_read_tokens']:,})",
        f"Provider/estimated cost: ${float(summary['cost_usd']):.6f}",
        f"Tool calls: {summary['tool_calls']} ({summary['tool_errors']} errors)",
        f"Slash commands: {summary['slash_commands']}",
        "",
        "## Models",
        "",
        "| Provider / model | Calls | Errors | Tokens | Cost USD |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in data["models"]:
        lines.append(
            f"| {row['provider']} / {row['model']} | {row['calls']} | {row['errors'] or 0} | "
            f"{row['tokens']:,} | {float(row['cost_usd']):.6f} |"
        )
    lines.extend(["", "## Tools", "", "| Tool | Calls | Errors | Average ms |", "|---|---:|---:|---:|"])
    for row in data["tools"]:
        lines.append(
            f"| {row['tool_name']} | {row['calls']} | {row['errors'] or 0} | {float(row['avg_duration_ms']):.0f} |"
        )
    lines.extend(["", "## Slash commands", "", "| Command | Calls |", "|---|---:|"])
    for row in data["commands"]:
        lines.append(f"| /{row['command']} | {row['calls']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Hermes model, token, cost, tool, and command activity")
    parser.add_argument("--period", default="24h", help="report window, for example 24h, 7d, or 30d")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--database", type=Path, default=hermes_home() / "ops" / "metrics.db")
    args = parser.parse_args()
    if not args.database.exists():
        print(f"No metrics database yet: {args.database}", file=sys.stderr)
        return 2
    try:
        since, label = cutoff(args.period)
    except ValueError as error:
        parser.error(str(error))
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        data = report(connection, since, label)
    finally:
        connection.close()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.format == "json" else as_markdown(data), end="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

