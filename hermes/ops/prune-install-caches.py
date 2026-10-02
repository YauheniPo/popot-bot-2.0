#!/usr/bin/env python3
"""Bound disk use after a verified Hermes browser installation."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path


CHROME_BUILD = re.compile(r"chrome-(\d+)\.(\d+)\.(\d+)\.(\d+)\Z")
DOT_CACHE = ".cache"
DOT_AGENT_BROWSER = ".agent-browser"
MAX_CACHE_BYTES = 512 * 1024 * 1024

CACHE_CHILDREN = {
    ".npm": ("_cacache", "_npx"),
    DOT_CACHE: ("uv", "ms-playwright", "electron", "node-gyp", "pip", "pnpm"),
    ".sonar": ("cache", "js", "_tmp"),
}


def checked_directory(path: Path) -> bool:
    if path.is_symlink():
        raise ValueError(f"refusing symlinked directory: {path}")
    return path.is_dir()


def prune_browser_builds(root: Path, active_builds: set[str]) -> int:
    if not checked_directory(root):
        return 0
    builds = []
    for path in root.iterdir():
        match = CHROME_BUILD.fullmatch(path.name)
        if match and checked_directory(path):
            builds.append((tuple(map(int, match.groups())), path))
    builds.sort(reverse=True)
    keep = {path.name for _, path in builds[:2]} | active_builds
    removed = 0
    for _, path in builds:
        if path.name not in keep:
            shutil.rmtree(path)
            removed += 1
    return removed


def directory_size(path: Path) -> int:
    total = 0
    for current, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            entry = Path(current) / name
            if entry.is_symlink():
                # Skip symlinked entries gracefully instead of raising
                continue
            total += entry.stat().st_size if entry.is_file() else 0
    return total


def prune_large_caches(home: Path, max_bytes: int, busy: bool) -> int:
    if busy:
        return 0
    removed = 0
    for root_name, children in CACHE_CHILDREN.items():
        root = home / root_name
        if not checked_directory(root):
            continue
        targets = [root / name for name in children if checked_directory(root / name)]
        total_size = 0
        for path in targets:
            total_size += directory_size(path)
        if total_size == 0:
            # Nothing to prune if there are no regular files (symlinks are skipped)
            continue
        if total_size < max_bytes:
            continue
        for path in targets:
            shutil.rmtree(path)
            removed += 1
    return removed
def _check_command_for_cache_busy(command: str, process: Path) -> bool:
    """Check if command indicates cache activity."""
    if command in {"uv", "pip", "pip3", "pnpm", "sonar-scanner"}:
        return True
    if command in {"node", "java"} or command.startswith("python"):
        return _check_argv_for_cache_markers(process)
    return False


def active_processes(home: Path, proc: Path = Path("/proc")) -> tuple[set[str], bool]:
    """Keep Chrome builds in use and defer cache cleanup while installers run."""
    if not proc.is_dir():
        return set(), True
    browser_root = home / DOT_AGENT_BROWSER / "browsers"
    active_builds: set[str] = set()
    cache_busy = False
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            executable = os.readlink(process / "exe")
            command = (process / "comm").read_text().strip().lower()
        except (OSError, UnicodeError):
            continue
        executable_path = Path(executable)
        if browser_root in executable_path.parents:
            active_builds.update(_extract_chrome_build(executable_path, browser_root))
        if _is_cache_root(executable_path, home / DOT_CACHE):
            cache_busy = True
        if _check_command_for_cache_busy(command, process):
            cache_busy = True
    return active_builds, cache_busy


def _extract_chrome_build(executable_path: Path, browser_root: Path) -> set[str]:
    """Extract Chrome build names from executable path."""
    builds: set[str] = set()
    for part in executable_path.relative_to(browser_root).parts:
        if CHROME_BUILD.fullmatch(part):
            builds.add(part)
            break
    return builds


def _is_cache_root(executable_path: Path, cache_root: Path) -> bool:
    """Check if executable is under a cache root directory."""
    return cache_root in executable_path.parents


def _check_argv_for_cache_markers(process: Path) -> bool:
    """Check process cmdline for cache-related markers."""
    try:
        argv = (process / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    markers = (
        b"npm-cli.js", b"npx-cli.js", b"pnpm.cjs", b"playwright",
        b"node-gyp", b"sonar-scanner", b"pip/_internal",
    )
    if any(marker in argument for argument in argv for marker in markers):
        return True
    return any(argv[index:index + 2] == [b"-m", b"pip"] for index in range(len(argv) - 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-home", type=Path, required=True)
    args = parser.parse_args()
    home = args.user_home
    if not home.is_absolute() or not checked_directory(home):
        parser.error("--user-home must be an existing, non-symlinked absolute directory")
    browser_root = home / DOT_AGENT_BROWSER / "browsers"
    checked_directory(home / DOT_AGENT_BROWSER)
    active_builds, cache_busy = active_processes(home)
    browser_removed = prune_browser_builds(browser_root, active_builds)
    cache_removed = prune_large_caches(home, MAX_CACHE_BYTES, cache_busy)
    print(f"browser_removed={browser_removed} cache_entries_removed={cache_removed}")


if __name__ == "__main__":
    main()
