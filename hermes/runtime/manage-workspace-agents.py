#!/usr/bin/env python3
"""Reconcile shared and environment instructions without replacing personal notes."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


SPEC = importlib.util.spec_from_file_location("safe_files", Path(__file__).with_name("safe_files.py"))
safe_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(safe_files)


BEGIN_MARKER = "<!-- BEGIN ANSIBLE MANAGED HOST ADMINISTRATION -->"
END_MARKER = "<!-- END ANSIBLE MANAGED HOST ADMINISTRATION -->"
LEGACY_VAULT_BEGIN_MARKER = "<!-- BEGIN MANAGED VAULT ENVIRONMENT NAMES -->"


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
            remainder = personal[len(legacy):]
            # A prefix match is a legacy full-file layout only when the next
            # block is the known Vault block that older deploys appended. Do
            # not remove personal notes that merely begin like old policy.
            if remainder.lstrip().startswith(LEGACY_VAULT_BEGIN_MARKER):
                personal = remainder.strip()

    parts: list[str] = []
    if present:
        parts.append(_managed_block(managed_source))
    if personal:
        parts.append(personal)
    return "\n\n".join(parts).rstrip() + "\n" if parts else ""


def _remove_layer(existing: str, name: str) -> str:
    begin, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
    if existing.count(begin) != existing.count(end) or existing.count(begin) > 1:
        raise ManagedBlockError(f"AGENTS.md has malformed or duplicate {name} markers")
    if begin not in existing:
        return existing
    start = existing.index(begin)
    finish = existing.index(end)
    if finish < start:
        raise ManagedBlockError(f"AGENTS.md has reversed {name} markers")
    return existing[:start] + existing[finish + len(end):]


def reconcile_layers(existing: str, common: str, environment: str, *, legacy_sources=()) -> str:
    """Replace owned layers and exact legacy prefixes, never infer ownership by heading."""
    names = ("HERMES MANAGED COMMON", "HERMES MANAGED ENVIRONMENT")
    for source in (common, environment):
        if not source.strip() or "<!-- BEGIN " in source or "<!-- END " in source:
            raise ManagedBlockError("instruction sources must be non-empty and contain no managed markers")
    # The previous VPS version owned this entire marked section.
    personal = reconcile(existing, "", present=False)
    for name in names:
        personal = _remove_layer(personal, name)
    personal = personal.strip()
    for legacy in legacy_sources:
        legacy = legacy.strip()
        if legacy and (personal == legacy or personal.startswith(legacy + "\n")):
            personal = personal[len(legacy):].strip()
            break
    blocks = [f"<!-- BEGIN {name} -->\n{source.strip()}\n<!-- END {name} -->"
              for name, source in zip(names, (common, environment))]
    if personal:
        blocks.append(personal)
    return "\n\n".join(blocks) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True,
                        help="destination with no symlinks in the file or parent directories")
    parser.add_argument("--managed-source", type=Path, required=True)
    parser.add_argument("--common-source", type=Path,
                        help="shared instructions; managed-source supplies the environment layer")
    parser.add_argument("--legacy-source", type=Path, action="append", default=[],
                        help="known unmarked legacy text eligible for exact-prefix migration")
    parser.add_argument("--backup-copy", type=Path,
                        help="backup destination with the same no-symlink restriction")
    parser.add_argument("--state", choices=("present", "absent"), default="present",
                        help="legacy single-block mode only; shared layers are always installed")
    return parser


def _destination_pair(target: Path, backup_copy: Path | None) -> tuple[Path, Path | None]:
    """Validate both destinations, replacing None when no backup copy is wanted."""
    return safe_files.absolute_path(target), safe_files.absolute_path(backup_copy) if backup_copy is not None else None


def _reconcile_instructions(args: argparse.Namespace, existing: str, managed_source: str) -> str:
    """Return the reconciled content for the requested instruction mode."""
    if args.common_source is None:
        if args.legacy_source:
            raise ManagedBlockError("--legacy-source requires --common-source")
        return reconcile(existing, managed_source, present=args.state == "present")
    if args.state != "present":
        raise ManagedBlockError("shared instructions cannot be disabled via --state")
    return reconcile_layers(
        existing, args.common_source.read_text(encoding="utf-8"), managed_source,
        legacy_sources=[path.read_text(encoding="utf-8") for path in args.legacy_source],
    )


def update_instructions(args: argparse.Namespace) -> int:
    args.target, args.backup_copy = _destination_pair(args.target, args.backup_copy)
    # Validate and read BOTH destinations before changing either one.
    target_content = safe_files.read_optional(args.target)
    backup_content = safe_files.read_optional(args.backup_copy) if args.backup_copy is not None else None
    managed_source = args.managed_source.read_text(encoding="utf-8")
    existing = target_content if target_content is not None else (backup_content or "")
    updated = _reconcile_instructions(args, existing, managed_source)
    changed = False
    if updated != existing or (updated and target_content is None):
        safe_files.write_atomic(args.target, updated)
        changed = True
    if args.backup_copy is not None:
        safe_files.ensure_private_parent(args.backup_copy)
        if backup_content != updated:
            safe_files.write_atomic(args.backup_copy, updated, mode=0o600)
            changed = True
    print(
        "workspace AGENTS.md managed block updated"
        if changed
        else "workspace AGENTS.md managed block unchanged"
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return update_instructions(args)
    except (OSError, ValueError, ManagedBlockError, safe_files.UnsafeFileError) as error:
        print(f"workspace instructions refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
