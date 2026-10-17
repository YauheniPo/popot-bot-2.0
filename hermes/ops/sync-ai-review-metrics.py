#!/usr/bin/env python3
"""Sync AI review metrics from last 5 PRs into review_runs.

Every 12 hours, finds the 5 most recently updated PRs in
YauheniPo/popot-bot-2.0 and imports their published reviews and comments
using extract-review-metrics.py. Handles all reviewer types:
DirectAPI, Azure DirectAPI, ClaudeCodePlugin, ObservableMessagesReview.

Idempotent: duplicate runs don't create duplicates.
Resilient: one PR failure doesn't stop the rest.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GITHUB_REPO = "YauheniPo/popot-bot-2.0"
EXTRACTOR = "/usr/local/lib/hermes-ops/extract-review-metrics.py"
_DEFAULT_HERMES_DIR = ".hermes"
DB_PATH = Path(os.environ.get("HERMES_OPS_DB", str(Path.home() / _DEFAULT_HERMES_DIR / "ops" / "metrics.db")))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_PRIS = 5


def managed_github_token() -> str:
    """Reuse the managed gh loader without executing gh or exposing its token."""
    loader = Path(__file__).with_name("github-cli-wrapper.py")
    if not loader.is_file():
        loader = Path(__file__).resolve().parents[1] / "runtime/github-cli-wrapper.py"
    github_environment = runpy.run_path(str(loader))["github_environment"]
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / _DEFAULT_HERMES_DIR)
    return github_environment(dict(os.environ), home).get("GH_TOKEN", "")


def get_last_5_prs() -> list[dict[str, Any]]:
    """Fetch the 5 most recently updated PRs from the repository."""
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
        "?state=all&sort=updated&direction=desc&per_page=5"
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sync-ai-review-metrics"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        prs = json.loads(resp.read())
    if not isinstance(prs, list) or any(
        not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] <= 0
        for pr in prs
    ):
        raise ValueError("GitHub returned an invalid PR list")
    return prs[:MAX_PRIS]


def import_pr_reviews(pr_number: int) -> bool:
    """Import review metrics for a single PR. Returns True on success."""
    try:
        result = subprocess.run(  # noqa: S8705
            [sys.executable, EXTRACTOR, "--pr", str(pr_number),
             "--repository", GITHUB_REPO, "--database", str(DB_PATH)],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GITHUB_TOKEN": GITHUB_TOKEN},
        )
        if result.returncode == 0:
            print(f"  PR #{pr_number}: OK")
            return True
        else:
            print(f"  PR #{pr_number}: FAILED — extractor exit {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  PR #{pr_number}: TIMEOUT (120s)")
        return False
    except Exception as e:
        print(f"  PR #{pr_number}: ERROR — {type(e).__name__}")
        return False


def _run_single_pr_mode(parsed) -> int:
    """Run single PR import mode."""
    success = import_pr_reviews(parsed.pr)
    return 0 if success else 1


def _run_list_mode() -> int:
    """Run list mode: fetch PRs and import review metrics."""
    prs = get_last_5_prs()
    if not prs:
        print("No PRs found. Nothing to import.")
        return 0

    print(f"\nFound {len(prs)} PRs to process:")
    for pr in prs:
        print(f"  PR #{pr['number']}")

    print("\nImporting review metrics...")
    ok = 0
    fail = 0
    for pr in prs:
        success = import_pr_reviews(pr["number"])
        if success:
            ok += 1
        else:
            fail += 1

    print(f"\n{'=' * 60}")
    print(f"Done: {ok} succeeded, {fail} failed out of {len(prs)} PRs")
    print(f"{'=' * 60}")

    return 0 if fail == 0 else 1


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, help="Import a single PR (overrides list mode)")
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / _DEFAULT_HERMES_DIR)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("HERMES_OPS_DB") or home / "ops/metrics.db"), help="Path to metrics DB")
    parsed = parser.parse_args(args)
    if parsed.pr is not None and parsed.pr <= 0:
        parser.error("--pr must be a positive integer")

    global GITHUB_TOKEN, DB_PATH
    DB_PATH = parsed.db.expanduser()

    print("=" * 60)
    print("AI Review Metrics Sync")
    print(f"DB: {DB_PATH}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    try:
        GITHUB_TOKEN = managed_github_token()
        if parsed.pr is not None:
            return _run_single_pr_mode(parsed)
        return _run_list_mode()
    except urllib.error.HTTPError as error:
        print(f"GitHub PR discovery failed: HTTP {error.code}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"Review sync failed: {type(error).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
