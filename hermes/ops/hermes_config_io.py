"""Shared YAML I/O for the install-time Hermes config tools.

Callers anchor paths with validated_config_path. The Hermes home directory is
trusted deployment input; the config leaf must never be a symlink. This module
ships alongside the configurators in the operations bundle.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml


def validated_config_path(path: Path, home: Path) -> Path:
    """Normalize parent directories, never resolve the config leaf itself."""
    candidate = path.expanduser()
    expected = home.expanduser().resolve() / "config.yaml"
    if candidate.is_symlink() or expected.is_symlink():
        raise ValueError("Hermes config.yaml and --config must not be symlinks")
    if candidate.parent.resolve() / candidate.name != expected:
        raise ValueError(f"--config must be {expected}")
    return expected


def load_config(path: Path) -> dict[str, Any]:
    # The open-time check also rejects a symlink swapped in after validation.
    # NONBLOCK prevents a substituted FIFO from hanging the deployment.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Hermes config.yaml must be a regular file without hardlinks")
        loaded = yaml.safe_load(stream)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Hermes config.yaml must contain a YAML mapping")
    return loaded


def write_config(path: Path, data: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("Hermes config.yaml must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Create exclusively with private permissions before writing any data.
        # Stay in the destination directory so replacement remains atomic.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
        # Atomic replacement does not follow a leaf symlink even if one
        # appears after the initial check. Parent directories must be trusted.
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
