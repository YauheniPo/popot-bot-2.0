#!/usr/bin/env python3
"""Import published GitHub AI-review metadata into Hermes' local metrics DB.

Only review metadata is stored: no prompts, findings, comments, or tokens.
The importer is intentionally idempotent and can be run from a timer or by
hand after a PR review (``GITHUB_TOKEN=... extract-review-metrics.py --pr 43``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any


METADATA_RE = re.compile(r"Technical metadata", re.I)
# Published comments render the metadata block as a Markdown blockquote with
# backtick-quoted values ("> Connection: `nous` · ..."); the plain format is
# kept for older records and tests. Legacy DirectAPI bodies used "Requests:"
# and "API time:" aliases before the shared metadata line was introduced.
FIELD_RE = {
    "connection": re.compile(r"(?:^|> )Connection:\s*([^·\n]+)", re.M),
    "model": re.compile(r"(?:^|> )Successful models?:\s*([^\n]+)", re.M),
    "attempts": re.compile(r"(?:Attempts|Requests):\s*(\d+)", re.I),
    "validated_chunks": re.compile(r"Validated:\s*(\d+)", re.I),
    "retries": re.compile(r"Retries:\s*(\d+)", re.I),
    "fallback_successes": re.compile(r"Fallback successes:\s*(\d+)", re.I),
    "provider_seconds": re.compile(r"(?:Provider|API) time:\s*([\d.]+)s", re.I),
    "outcome": re.compile(r"(?:^|> )Result:\s*([^\n]+)", re.M | re.I),
    "scope": re.compile(r"Validated chunks:\s*(\d+)\s*/\s*(\d+)", re.I),
}


def parse_review(body: str, reviewer_hint: str = "") -> dict[str, Any] | None:
    match = METADATA_RE.search(body or "")
    if not match:
        return None
    block = re.split(r"\n\s*(?:Review scope|CI run)\b", body[match.end():], maxsplit=1, flags=re.I)[0]
    connection = FIELD_RE["connection"].search(block)
    model = FIELD_RE["model"].search(block)
    if not connection or not model:
        return None
    provider = connection.group(1).strip().strip("`").strip()
    model_name = model.group(1).strip().split(",", 1)[0].strip("`").strip()
    reviewer = reviewer_hint.strip() or next(
        (candidate for candidate in ("DirectAPI", "ClaudeCodePlugin", "ObservableMessagesReview")
         if candidate.lower() in body.lower()),
        "unknown",
    )
    numbers = {
        name: int(found.group(1)) if (found := FIELD_RE[name].search(block)) else 0
        for name in ("attempts", "validated_chunks", "retries", "fallback_successes")
    }
    seconds = FIELD_RE["provider_seconds"].search(block)
    outcome = FIELD_RE["outcome"].search(block)
    scope = FIELD_RE["scope"].search(body)
    # Published review bodies carry the metadata block; Observable failure
    # and partial reports carry it too, with an explicit Result line that
    # overrides the default. Markdown emphasis ("**success**") is stripped.
    raw_outcome = outcome.group(1).strip().strip("*_").strip() if outcome else "success"
    return {
        "reviewer": reviewer,
        "provider": provider,
        "model": model_name,
        "attempts": numbers["attempts"],
        "validated_chunks": numbers["validated_chunks"],
        "total_chunks": int(scope.group(2)) if scope else 0,
        "retries": numbers["retries"],
        "fallback_successes": numbers["fallback_successes"],
        "provider_seconds": float(seconds.group(1)) if seconds else 0.0,
        "outcome": raw_outcome.lower() if raw_outcome else "success",
    }


def _api_url(repository: str, path: str) -> str:
    return f"https://api.github.com/repos/{repository}/{path}"


def fetch_reviews(repository: str, pr: int, token: str) -> list[dict[str, Any]]:
    """Fetch both review and issue-comment bodies; callers deduplicate by id."""
    result: list[dict[str, Any]] = []
    for path in (f"pulls/{pr}/reviews?per_page=100", f"issues/{pr}/comments?per_page=100"):
        request = urllib.request.Request(_api_url(repository, path), headers={
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-review-metrics",
        })
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        if isinstance(payload, list):
            result.extend(item for item in payload if isinstance(item, dict))
    return result


def ensure_schema(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS review_runs (
              source_id TEXT PRIMARY KEY, pr_number INTEGER NOT NULL,
              reviewer TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
              outcome TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              validated_chunks INTEGER NOT NULL DEFAULT 0, total_chunks INTEGER NOT NULL DEFAULT 0,
              retries INTEGER NOT NULL DEFAULT 0, fallback_successes INTEGER NOT NULL DEFAULT 0,
              provider_seconds REAL NOT NULL DEFAULT 0, observed_at TEXT NOT NULL,
              head_sha TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT ''
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_review_runs_observed ON review_runs(observed_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_review_runs_route ON review_runs(provider, model)")


def upsert(database: Path, row: dict[str, Any]) -> None:
    columns = ("source_id", "pr_number", "reviewer", "provider", "model", "outcome", "attempts",
               "validated_chunks", "total_chunks", "retries", "fallback_successes", "provider_seconds",
               "observed_at", "head_sha", "run_id")
    # The identifier list is a fixed module constant; never derive it from a
    # review payload. Quoting it also makes that invariant explicit to readers.
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    assignments = ",".join(f'"{column}"=excluded."{column}"' for column in columns[1:])
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO review_runs ({quoted_columns}) VALUES ({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(source_id) DO UPDATE SET {assignments}",
            tuple(row.get(column, "") for column in columns),
        )


def import_pr(repository: str, pr: int, token: str, database: Path) -> int:
    ensure_schema(database)
    imported = 0
    for item in fetch_reviews(repository, pr, token):
        body = item.get("body") or ""
        parsed = parse_review(body)
        if not parsed:
            continue
        parsed.update({
            "source_id": f"{item.get('html_url') or item.get('id')}",
            "pr_number": pr,
            # Pull-request reviews expose submitted_at, issue comments use
            # created_at/updated_at; the fallback chain covers both shapes.
            "observed_at": (item.get("updated_at") or item.get("created_at")
                            or item.get("submitted_at") or ""),
            "head_sha": item.get("commit_id") or "",
            "run_id": "",
        })
        upsert(database, parsed)
        imported += 1
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description="Import GitHub AI review metadata into Hermes metrics")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "YauheniPo/popot-bot-2.0"))
    parser.add_argument("--database", type=Path, default=Path.home() / ".hermes/ops/metrics.db")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        parser.error("GITHUB_TOKEN is required")
    count = import_pr(args.repository, args.pr, token, args.database.expanduser())
    print(f"Imported {count} published review record(s) for PR #{args.pr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
