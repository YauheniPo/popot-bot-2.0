#!/usr/bin/env python3
"""Manage only the repository-owned block in workspace/AGENTS.md."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


BEGIN_MARKER = "<!-- BEGIN ANSIBLE MANAGED HOST ADMINISTRATION -->"
END_MARKER = "<!-- END ANSIBLE MANAGED HOST ADMINISTRATION -->"


class ManagedBlockError(RuntimeError):
    """Raised when an existing managed block cannot be updated safely."""


def _managed_block(source: str) -> str:
    return f"{BEGIN_MARKER}\n{source.strip()}\n{END_MARKER}"


def reconcile(existing: str, managed_source: str, *, present: bool) -> str:
    """Return AGENTS.md with only the managed host-administration block changed."""
    begin_count = existing.count(BEGIN_MARKER)
    end_count = existing.count(END_MARKER)
    if begin_count != end_count or begin_count > 1:
        raise ManagedBlockError("AGENTS.md has malformed or duplicate managed markers")

    personal = existing
    if begin_count == 1:
        start = existing.index(BEGIN_MARKER)
        end = existing.index(END_MARKER, start) + len(END_MARKER)
        personal = (existing[:start] + existing[end:]).strip()
    else:
        legacy = managed_source.strip()
        if personal.strip() == legacy:
            personal = ""
        elif personal.startswith(legacy):
            personal = personal[len(legacy):].strip()

    parts: list[str] = []
    if present:
        parts.append(_managed_block(managed_source))
    if personal:
        parts.append(personal)
    return "\n\n".join(parts).rstrip() + "\n" if parts else ""


def write_atomic(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".partial",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--managed-source", type=Path, required=True)
    parser.add_argument("--backup-copy", type=Path)
    parser.add_argument("--state", choices=("present", "absent"), required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    managed_source = args.managed_source.read_text(encoding="utf-8")
    if args.target.exists():
        existing = args.target.read_text(encoding="utf-8")
    elif args.backup_copy is not None and args.backup_copy.exists():
        existing = args.backup_copy.read_text(encoding="utf-8")
    else:
        existing = ""
    updated = reconcile(existing, managed_source, present=args.state == "present")
    changed = False
    if updated != existing:
        write_atomic(args.target, updated)
        changed = True
    elif updated and not args.target.exists():
        write_atomic(args.target, updated)
        changed = True
    if args.backup_copy is not None:
        args.backup_copy.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(args.backup_copy.parent, 0o700)
        backup_content = (
            args.backup_copy.read_text(encoding="utf-8")
            if args.backup_copy.exists()
            else None
        )
        if backup_content != updated:
            write_atomic(args.backup_copy, updated, mode=0o600)
            changed = True
    print(
        "workspace AGENTS.md managed block updated"
        if changed
        else "workspace AGENTS.md managed block unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
