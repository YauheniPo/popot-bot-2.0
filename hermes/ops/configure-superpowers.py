#!/usr/bin/env python3
"""Enable the superpowers skills plugin without invoking an interactive CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("Hermes config.yaml must contain a YAML mapping")
    return loaded


def configure(data: dict[str, Any]) -> bool:
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Hermes config.yaml plugins must be a YAML mapping")

    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
        raise ValueError("Hermes config.yaml plugins.enabled must be a list of strings")

    changed = False
    if "superpowers" not in enabled:
        enabled.append("superpowers")
        changed = True

    # superpowers ships skills and a bootstrap hook only; it must never
    # override built-in tools.
    entries = plugins.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("Hermes config.yaml plugins.entries must be a YAML mapping")
    plugins["entries"] = entries
    entry = entries.setdefault("superpowers", {})
    if not isinstance(entry, dict):
        raise ValueError("Hermes config.yaml plugins.entries.superpowers must be a mapping")
    if entry.get("allow_tool_override") is not False:
        entry["allow_tool_override"] = False
        changed = True

    return changed


def write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.sp.tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        data = load_config(args.config)
        changed = configure(data)
        if changed:
            write_config(args.config, data)
        print("changed" if changed else "unchanged")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
