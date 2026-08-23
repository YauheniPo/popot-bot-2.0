#!/usr/bin/env python3
"""Enable the Hermes observability plugin without invoking an interactive CLI."""

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


def configure(data: dict[str, Any], hermes_home: Path) -> bool:
    plugins = data.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Hermes config.yaml plugins must be a YAML mapping")

    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
        raise ValueError("Hermes config.yaml plugins.enabled must be a list of strings")

    changed = False
    if "ops-observability" not in enabled:
        enabled.append("ops-observability")
        changed = True

    quick_commands = data.setdefault("quick_commands", {})
    if not isinstance(quick_commands, dict):
        raise ValueError("Hermes config.yaml quick_commands must be a YAML mapping")

    status_command = {
        "type": "exec",
        "command": f"HERMES_HOME={hermes_home} /usr/local/lib/hermes-ops/status-report.py",
    }
    if quick_commands.get("status") != status_command:
        quick_commands["status"] = status_command
        changed = True

    docker_restart_command = {
        "type": "exec",
        "command": (
            f"sudo docker compose -f {hermes_home.parent}/"
            "workspace/repositories/YauheniPo/popot-bot-2.0/hermes/vscode-server/"
            "docker-compose.yml up -d --force-recreate"
        ),
    }
    if quick_commands.get("docker_restart") != docker_restart_command:
        quick_commands["docker_restart"] = docker_restart_command
        changed = True
    return changed


def write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.ops.tmp")
    temporary.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    result.add_argument("--hermes-home", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        data = load_config(args.config)
        changed = configure(data, args.hermes_home)
        if changed:
            write_config(args.config, data)
        print("changed" if changed else "unchanged")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
