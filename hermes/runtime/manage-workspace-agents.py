#!/usr/bin/env python3
"""Manage only the repository-owned block in workspace/AGENTS.md."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat
import sys


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


def _destination(path: Path) -> Path:
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise ManagedBlockError("instruction paths must not contain '..'")
    return path


@contextmanager
def _parent_directory(path: Path, *, create: bool = False):
    # Walk from the root using directory descriptors. Do not resolve symlinks:
    # even a parent swapped after validation must not redirect reads or writes.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _require_regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ManagedBlockError("instruction files must be regular files without links")


def read_optional(path: Path) -> str | None:
    try:
        with _parent_directory(path) as directory:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                _require_regular(os.fstat(handle.fileno()))
                return handle.read()
    except FileNotFoundError:
        return None


def write_atomic(path: Path, content: str, *, mode: int = 0o600) -> None:
    path = _destination(path)
    with _parent_directory(path, create=True) as directory:
        try:
            _require_regular(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        temporary = f".{path.name}.{secrets.token_hex(16)}.partial"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(content)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True,
                        help="destination with no symlinks in the file or parent directories")
    parser.add_argument("--managed-source", type=Path, required=True)
    parser.add_argument("--backup-copy", type=Path,
                        help="backup destination with the same no-symlink restriction")
    parser.add_argument("--state", choices=("present", "absent"), required=True)
    return parser


def update_instructions(args: argparse.Namespace) -> int:
    args.target = _destination(args.target)
    if args.backup_copy is not None:
        args.backup_copy = _destination(args.backup_copy)
    # Validate and read BOTH destinations before changing either one.
    target_content = read_optional(args.target)
    backup_content = read_optional(args.backup_copy) if args.backup_copy is not None else None
    managed_source = args.managed_source.read_text(encoding="utf-8")
    existing = target_content if target_content is not None else (backup_content or "")
    updated = reconcile(existing, managed_source, present=args.state == "present")
    changed = False
    if updated != existing or (updated and target_content is None):
        write_atomic(args.target, updated)
        changed = True
    if args.backup_copy is not None:
        with _parent_directory(args.backup_copy, create=True) as directory:
            os.fchmod(directory, 0o700)
        if backup_content != updated:
            write_atomic(args.backup_copy, updated, mode=0o600)
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
    except (OSError, ValueError, ManagedBlockError) as error:
        print(f"workspace instructions refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
