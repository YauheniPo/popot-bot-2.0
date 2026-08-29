#!/usr/bin/env python3
"""Compact, no-LLM status report for Hermes quick_commands.status."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path


def hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def gateway_state() -> str:
    service = os.environ.get("HERMES_GATEWAY_SERVICE", "hermes-gateway.service")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    state = result.stdout.strip()
    return state if state else "unknown"


def token_totals(database: Path) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    if not database.exists():
        return None, None
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
    try:
        active = connection.execute(
            """
            SELECT session_id FROM sessions AS started
            WHERE event = 'start'
              AND NOT EXISTS (
                SELECT 1 FROM sessions AS ended
                WHERE ended.session_id = started.session_id
                  AND ended.event = 'end' AND ended.ts >= started.ts
              )
            ORDER BY ts DESC LIMIT 1
            """
        ).fetchone()
        all_tokens = connection.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(total_tokens), 0) FROM api_calls"
        ).fetchone()
        if active is None:
            return None, all_tokens
        session_tokens = connection.execute(
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cache_read_tokens), 0), COALESCE(SUM(total_tokens), 0) "
            "FROM api_calls WHERE session_id = ?",
            (active[0],),
        ).fetchone()
        return session_tokens, all_tokens
    finally:
        connection.close()


def format_tokens(label: str, totals: tuple[int, int, int, int] | None) -> str:
    if totals is None:
        return f"{label}: unavailable"
    input_tokens, output_tokens, cache_read_tokens, total_tokens = totals
    return (
        f"{label}: {total_tokens:,} "
        f"(in {input_tokens:,}; out {output_tokens:,}; cache {cache_read_tokens:,})"
    )


def main() -> int:
    session_tokens, all_tokens = token_totals(hermes_home() / "ops" / "metrics.db")
    print(f"Gateway: {gateway_state()}")
    print(format_tokens("Active session tokens", session_tokens))
    print(format_tokens("All tracked tokens", all_tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
