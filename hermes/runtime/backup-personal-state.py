#!/usr/bin/env python3
"""Extend scheduled native backups with personal memory and workspace rules."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "workspace_instruction_io", Path(__file__).with_name("manage-workspace-agents.py")
)
instruction_io = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(instruction_io)
MANIFEST = "operator-state/workspace-instructions.json"
INVENTORY = "operator-state/personal-backup-inventory.json"
EXCLUDED_DIRS = {
    ".git", ".cache", "__pycache__", "node_modules", "venv", ".venv",
    "hermes-agent", "state-snapshots", "backups", "checkpoints",
    "browser-profile", "browser-profiles", "workspaces", "attachments",
}
PERSONAL_NAMES = {
    "config.yaml", ".env", "auth.json", "SOUL.md", "USER.md", "MEMORY.md",
    "MEMORIES.md", "external_memory_providers.json", ".managed-swarm",
}


def instruction_name(name: str) -> bool:
    return name == "AGENTS.md" or (name.startswith("AGENTS.") and name.endswith(".md"))


def regular_file(path: Path) -> bool:
    """Check through directory descriptors; do not follow even parent symlinks."""
    try:
        with instruction_io._parent_directory(path) as parent:
            metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            instruction_io._require_regular(metadata)
        return True
    except FileNotFoundError:
        return False


def files_under(root: Path):
    if root.is_symlink():
        raise RuntimeError(f"Backup directory must not be a symlink: {root}")
    def onerror(error):
        raise error
    for directory, children, files in os.walk(root, followlinks=False, onerror=onerror):
        children[:] = sorted(name for name in children if name not in EXCLUDED_DIRS
                             and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            yield Path(directory) / name


def mirror_workspace(home: Path, workspace: Path) -> None:
    files = {}
    if workspace.exists():
        for path in files_under(workspace):
            if instruction_name(path.name):
                content = instruction_io.read_optional(path)
                if content is None:
                    raise RuntimeError("Workspace instructions changed during backup; retry")
                files[path.relative_to(workspace).as_posix()] = content
    payload = json.dumps({"version": 1, "files": files}, ensure_ascii=False, indent=2) + "\n"
    # One atomic manifest replaces the old inventory, including removed entries.
    if instruction_io.read_optional(home / MANIFEST) != payload:
        instruction_io.write_atomic(home / MANIFEST, payload)


def restore_workspace(home: Path, workspace: Path, archive: Path | None = None) -> int | None:
    if archive is None:
        content = instruction_io.read_optional(home / MANIFEST)
    else:
        # An import merges state. Do not restore a stale local manifest when
        # importing a legacy archive that never contained one.
        with zipfile.ZipFile(archive) as backup:
            content = backup.read(MANIFEST).decode("utf-8") if MANIFEST in backup.namelist() else None
    if content is None:
        return None  # Distinguish a legacy archive from an intentionally empty manifest.
    manifest = json.loads(content)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Unsupported workspace instruction backup")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Invalid workspace instruction inventory")
    for name, text in files.items():
        path = Path(name)
        if (not name or path.is_absolute() or ".." in path.parts
                or path.as_posix() != name or not instruction_name(path.name)
                or not isinstance(text, str)):
            raise ValueError("Unsafe workspace instruction entry")
        # Preflight every existing destination before writing any entry.
        instruction_io.read_optional(workspace / path)
    for name, text in files.items():
        instruction_io.write_atomic(workspace / name, text)
    return len(files)


def personal_files(home: Path) -> list[str]:
    roots = [home]
    profiles = home / "profiles"
    if profiles.is_symlink():
        raise RuntimeError("Profile directory must not be a symlink")
    if profiles.is_dir():
        roots.extend(path for path in sorted(profiles.iterdir())
                     if path.is_dir() and not path.is_symlink())
    selected = {home / MANIFEST, home / "operator-state/workspace-AGENTS.md",
                home / "swarm/swarm.yaml"}
    for root in roots:
        selected.update(root / name for name in PERSONAL_NAMES)
        selected.update(path for path in root.iterdir() if instruction_name(path.name))
        if (root / "memories").exists():
            selected.update(files_under(root / "memories"))
    return sorted(path.relative_to(home).as_posix() for path in selected
                  if not shared_profile_secret(path, home) and regular_file(path))


def shared_profile_secret(path: Path, home: Path) -> bool:
    """Swarm intentionally links only these credentials to the global home.

    Full Hermes archives skip symlinks. Save the global regular file once;
    managed Swarm deployment recreates its links on a replacement host.
    """
    relative = path.relative_to(home)
    if (len(relative.parts) != 3 or relative.parts[0] != "profiles"
            or path.name not in (".env", "auth.json") or not path.is_symlink()):
        return False
    target = path.readlink()
    absolute = Path(os.path.abspath(path.parent / target))
    if absolute != home / path.name:
        raise RuntimeError("Profile credential link points outside the shared Hermes credential path")
    return True


def verify_full(archive: Path, expected: list[str]) -> None:
    with zipfile.ZipFile(archive) as backup:
        missing = set(expected) - {item.filename for item in backup.infolist() if not item.is_dir()}
        if missing:
            raise RuntimeError(f"Full backup missing {len(missing)} personal state file(s)")
        if backup.testzip() is not None:
            raise RuntimeError("Full backup failed CRC verification")


def quick_snapshot(home: Path, native) -> Path:
    """Keep native SQLite copy/manifest/restore; publish only verified snapshots.

    This adapter intentionally checks the pinned Hermes private backup contract.
    No installed source is modified. A changed upstream contract fails the job.
    """
    original_files = native._QUICK_STATE_FILES
    original_root = native._QUICK_SNAPSHOTS_DIR
    expected = set(personal_files(home))
    for name in original_files:
        source = home / name
        if source.is_dir():
            expected.update(path.relative_to(home).as_posix()
                            for path in files_under(source) if regular_file(path))
        elif regular_file(source):
            expected.add(name)
    if not expected:
        raise RuntimeError("No Hermes state found to back up")
    snapshot = None
    try:
        native._QUICK_STATE_FILES = tuple(sorted(expected))
        # Native code publishes before returning. Keep incomplete results outside
        # the directory read by backup freshness checks and retention.
        native._QUICK_SNAPSHOTS_DIR = "ops/state-snapshots"
        snapshot_id = native.create_quick_snapshot(label="scheduled", hermes_home=home,
                                                   keep=sys.maxsize)
        if not snapshot_id or Path(snapshot_id).name != snapshot_id:
            raise RuntimeError("Hermes did not create a valid quick snapshot")
        snapshot = home / native._QUICK_SNAPSHOTS_DIR / snapshot_id
        manifest = json.loads((snapshot / "manifest.json").read_text())
        files = manifest.get("files", {})
        if expected - set(files):
            raise RuntimeError("Quick backup missing required state files")
        if manifest.get("failed_dbs") or manifest.get("oversized_skipped"):
            raise RuntimeError("Hermes reported an incomplete quick backup")
        for name in expected:
            path = snapshot / name
            if not regular_file(path) or path.stat().st_size != files[name]:
                raise RuntimeError("Quick backup file verification failed")
        destination = home / original_root / snapshot_id
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise RuntimeError("Quick snapshot destination already exists; retry later")
        snapshot.rename(destination)
        return destination
    finally:
        native._QUICK_STATE_FILES = original_files
        native._QUICK_SNAPSHOTS_DIR = original_root
        if snapshot is not None and snapshot.exists():
            shutil.rmtree(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("mirror", "restore", "inventory", "verify-full", "quick"))
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    home = instruction_io._destination(args.hermes_home)
    if args.command in ("mirror", "restore"):
        if args.workspace is None:
            parser.error("--workspace is required")
        workspace = instruction_io._destination(args.workspace)
        if args.command == "mirror":
            mirror_workspace(home, workspace)
        else:
            count = restore_workspace(home, workspace, args.archive)
            if count is None:
                print("No workspace instruction manifest (legacy backup)")
            else:
                print(f"Restored {count} workspace instruction file(s)")
    elif args.command == "inventory":
        instruction_io.write_atomic(home / INVENTORY, json.dumps(personal_files(home)))
    elif args.command == "verify-full":
        if args.archive is None:
            parser.error("--archive is required")
        verify_full(args.archive, json.loads(instruction_io.read_optional(home / INVENTORY)))
    else:
        sys.path.insert(0, str(home / "hermes-agent"))
        from hermes_cli import backup
        destination = quick_snapshot(home, backup)
        print(f"Verified scheduled quick backup: {destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
