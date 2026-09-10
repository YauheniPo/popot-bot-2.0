#!/usr/bin/env python3
"""Enable the superpowers skills plugin without invoking an interactive CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from hermes_config_io import load_config, write_config


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config", required=True, type=Path,
        help="must resolve to $HERMES_HOME/config.yaml (falls back to ~/.hermes); no other location is accepted",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        # Anchor --config to the caller's own HERMES_HOME instead of trusting
        # its basename alone: a basename-only check still lets the directory
        # component point anywhere on the filesystem. Fall back to ~/.hermes,
        # matching hermes_home()/_home() in the other ops scripts, so a
        # manual invocation without HERMES_HOME set still behaves sensibly
        # instead of hard-failing.
        hermes_home = os.environ.get("HERMES_HOME", "").strip()
        expected_config = (
            Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
        ) / "config.yaml"
        expected_config = expected_config.resolve()
        if args.config.resolve() != expected_config:
            raise ValueError(f"--config must be {expected_config}")
        data = load_config(expected_config)
        changed = configure(data)
        if changed:
            write_config(expected_config, data)
        print("changed" if changed else "unchanged")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
