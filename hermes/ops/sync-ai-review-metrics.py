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
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import urllib.request
except ImportError:
    pass

GITHUB_REPO = "YauheniPo/popot-bot-2.0"
EXTRACTOR = "/usr/local/lib/hermes-ops/extract-review-metrics.py"
DB_PATH = Path(os.environ.get("HERMES_OPS_DB", str(Path.home() / ".hermes" / "ops" / "metrics.db")))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
MAX_PRIS = 5


def get_last_5_prs() -> list[dict[str, Any]]:
    """Fetch the 5 most recently updated PRs from the repository."""
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        return []

    url = (
        f"https://api.github.com/repos/{GITHUB_REPO}/pulls"
        "?state=all&sort=updated&direction=desc&per_page=5"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "sync-ai-review-metrics",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            prs = json.loads(resp.read())
        return prs[:MAX_PRIS]
    except Exception as e:
        print(f"ERROR: Failed to fetch PRs: {e}", file=sys.stderr)
        return []


def import_pr_reviews(pr_number: int) -> bool:
    """Import review metrics for a single PR. Returns True on success."""
    try:
        result = subprocess.run(  # noqa: S8705
            ["python3", EXTRACTOR, "--pr", str(pr_number)],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GITHUB_TOKEN": GITHUB_TOKEN},
        )
        if result.returncode == 0:
            print(f"  PR #{pr_number}: OK — {result.stdout.strip()}")
            return True
        else:
            print(f"  PR #{pr_number}: FAILED — {result.stderr.strip()[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"  PR #{pr_number}: TIMEOUT (120s)")
        return False
    except Exception as e:
        print(f"  PR #{pr_number}: ERROR — {e}")
        return False


def _run_single_pr_mode(parsed) -> int:
    """Run single PR import mode."""
    success = import_pr_reviews(parsed.pr)
    return 0 if success else 1


def _run_list_mode() -> int:
    """Run list mode: fetch PRs and import review metrics."""
    global GITHUB_TOKEN

    if not GITHUB_TOKEN:
        print("ERROR: No GITHUB_TOKEN available", file=sys.stderr)
        return 1

    prs = get_last_5_prs()
    if not prs:
        print("No PRs found or failed to fetch.")
        return 1

    print(f"\nFound {len(prs)} PRs to process:")
    for pr in prs:
        print(f"  PR #{pr['number']}: {pr['title'][:60]}...")

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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, help="Import a single PR (overrides list mode)")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Path to metrics DB")
    parsed = parser.parse_args(args)

    global GITHUB_TOKEN

    print("=" * 60)
    print("AI Review Metrics Sync")
    print(f"DB: {DB_PATH}")
    print(f"Timestamp: {__import__('datetime').datetime.utcnow().isoformat()}")
    print("=" * 60)

    if not GITHUB_TOKEN:
        # Try to get token via gh CLI
        try:
            gh_result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10
            )
            if gh_result.returncode == 0:
                GITHUB_TOKEN = gh_result.stdout.strip()
                os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN
        except Exception:
            pass

    if parsed.pr:
        return _run_single_pr_mode(parsed)

    return _run_list_mode()


if __name__ == "__main__":
    sys.exit(main())
