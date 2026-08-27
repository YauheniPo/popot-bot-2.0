#!/usr/bin/env python3
"""Prune only Hermes backups created by the scheduled backup service."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path


_DEPLOYMENT_ARCHIVE = re.compile(
    r"^(pre-deploy-[0-9]{8}-[0-9]{6}|pre-config-deploy-[0-9]{8}T[0-9]{6})[.]zip$"
)


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def prune_scheduled_full_backups(backup_dir: Path, keep: int) -> int:
    if not backup_dir.is_dir():
        return 0

    backups = [
        path
        for path in backup_dir.glob("scheduled-full-*.zip")
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".partial.zip")
    ]
    backups.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)

    removed = 0
    for path in backups[keep:]:
        path.unlink()
        removed += 1
    return removed


def prune_deployment_backups(backup_dir: Path, keep: int) -> int:
    """Keep the newest verified source/config deployment backup groups only."""

    if not backup_dir.is_dir():
        return 0

    archives: list[tuple[Path, str]] = []
    for path in backup_dir.iterdir():
        match = _DEPLOYMENT_ARCHIVE.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            archives.append((path, match.group(1)))
    archives.sort(
        key=lambda item: (item[0].stat().st_mtime_ns, item[0].name),
        reverse=True,
    )

    removed = 0
    for archive, prefix in archives[keep:]:
        archive.unlink()
        removed += 1
        suffixes = (
            ("-kanban-before.json", "-kanban-after.json")
            if prefix.startswith("pre-deploy-")
            else ("-state.json",)
        )
        for suffix in suffixes:
            companion = backup_dir / f"{prefix}{suffix}"
            if companion.is_file() and not companion.is_symlink():
                companion.unlink()
    return removed


def is_scheduled_snapshot(path: Path) -> bool:
    manifest = path / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        return False
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("label") == "scheduled"


def prune_scheduled_quick_snapshots(
    snapshots_dir: Path,
    retention_days: int,
    *,
    now: float | None = None,
) -> int:
    if not snapshots_dir.is_dir():
        return 0

    cutoff = (time.time() if now is None else now) - retention_days * 24 * 60 * 60
    removed = 0
    for path in snapshots_dir.iterdir():
        if not path.is_dir() or path.is_symlink() or path.is_mount():
            continue
        if not is_scheduled_snapshot(path) or path.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(path)
        removed += 1
    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--snapshots-dir", required=True, type=Path)
    parser.add_argument("--quick-retention-days", required=True, type=positive_integer)
    parser.add_argument("--full-keep", required=True, type=positive_integer)
    parser.add_argument("--deployment-keep", required=True, type=positive_integer)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    quick_removed = prune_scheduled_quick_snapshots(
        args.snapshots_dir,
        args.quick_retention_days,
    )
    full_removed = prune_scheduled_full_backups(args.backup_dir, args.full_keep)
    deployment_removed = prune_deployment_backups(args.backup_dir, args.deployment_keep)
    print(
        "Pruned Hermes backups: "
        f"quick={quick_removed}, full={full_removed}, deployment={deployment_removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
