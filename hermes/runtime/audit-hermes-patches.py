#!/usr/bin/env python3
"""Audit the local Hermes patch layer against upstream Hermes.

`apply-hermes-patches.py` carries one (path, marker, old, new) block per local
customization and fails a deploy when a block no longer matches its target. That
safety check only fires at deploy time, on the pinned Hermes version, so drift
against upstream `main` is invisible until an upgrade breaks the deploy.

This tool answers, for every block, one of three questions:

* ``APPLICABLE`` - upstream still holds the unpatched text; the block is needed.
* ``OBSOLETE``   - upstream already contains the replacement; retire the block at
                   the next version bump instead of re-anchoring it.
* ``DRIFT``      - neither text is present: the surrounding code moved, so the
                   block must be re-anchored before the next bump.
* ``NO_FILE``    - the target file does not exist upstream any more.

Read-only: it fetches upstream sources with `gh` and never writes to the repo.

Usage::

    python3 hermes/runtime/audit-hermes-patches.py
    python3 hermes/runtime/audit-hermes-patches.py --json /tmp/audit.json --fail-on-drift
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

APPLIER_NAME = "apply-hermes-patches.py"
DEFAULT_UPSTREAM = "NousResearch/hermes-agent"
VERDICTS = ("APPLICABLE", "OBSOLETE", "DRIFT", "NO_FILE")


def classify_block(old: str, new: str, source: str) -> str:
    """Classify one patch block against one upstream source text."""
    if not source:
        return "NO_FILE"
    if new and new.strip() and new in source:
        return "OBSOLETE"
    if old in source:
        return "APPLICABLE"
    return "DRIFT"


def load_blocks(applier_path: Path) -> list[Sequence[Any]]:
    """Import the applier module and return its patch block list."""
    spec = importlib.util.spec_from_file_location("apply_hermes_patches_audit", applier_path)
    if spec is None or spec.loader is None:  # pragma: no cover - malformed install
        raise RuntimeError(f"cannot import {applier_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return list(module._PATCHES)


def fetch_upstream(relative_path: str, upstream: str, cache_dir: Path | None) -> str:
    """Return the upstream file text, or an empty string when it is unavailable."""
    cache_path = None
    if cache_dir is not None:
        cache_path = cache_dir / relative_path.replace("/", "_")
        if cache_path.is_file():
            return cache_path.read_text(encoding="utf-8")
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{upstream}/contents/{relative_path}",
            "-H",
            "Accept: application/vnd.github.raw",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def audit(applier_path: Path, upstream: str, cache_dir: Path | None) -> list[dict[str, Any]]:
    """Classify every block of the local patch layer."""
    rows: list[dict[str, Any]] = []
    for index, block in enumerate(load_blocks(applier_path), 1):
        relative_path, label, old, new = block[0], block[1], block[2], block[3]
        source = fetch_upstream(relative_path, upstream, cache_dir)
        rows.append(
            {
                "n": index,
                "path": relative_path,
                "label": label,
                "verdict": classify_block(old, new, source),
            }
        )
    return rows


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Count rows per verdict, including zero entries."""
    return {verdict: sum(1 for row in rows if row["verdict"] == verdict) for verdict in VERDICTS}


def render_markdown(rows: Sequence[dict[str, Any]], upstream: str) -> str:
    """Render the audit as a Markdown table grouped by verdict."""
    lines = [f"# Hermes patch-layer audit against `{upstream}` `main`", ""]
    summary = summarize(rows)
    lines.append(
        "Blocks: {total} - applicable {APPLICABLE}, obsolete {OBSOLETE}, "
        "drift {DRIFT}, missing file {NO_FILE}".format(total=len(rows), **summary)
    )
    for verdict in VERDICTS:
        selected = [row for row in rows if row["verdict"] == verdict]
        if not selected:
            continue
        lines.extend(["", f"## {verdict}", ""])
        lines.extend(f"- `{row['path']}` :: {row['label']}" for row in selected)
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--applier", type=Path, default=Path(__file__).with_name(APPLIER_NAME))
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args(argv)

    rows = audit(args.applier, args.upstream, args.cache_dir)
    for row in rows:
        print(f"{row['n']:>2} {row['verdict']:<10} {row['path']} :: {row['label']}")
    summary = summarize(rows)
    print(f"\nblocks: {len(rows)} {summary}")

    if args.json is not None:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    if args.markdown is not None:
        args.markdown.write_text(render_markdown(rows, args.upstream), encoding="utf-8")
    if args.fail_on_drift and summary["DRIFT"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
