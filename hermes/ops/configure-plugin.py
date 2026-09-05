#!/usr/bin/env python3
"""Enable the Hermes observability plugin without invoking an interactive CLI."""

from __future__ import annotations

import argparse
import os
import re
import shlex
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


def configure(
    data: dict[str, Any],
    hermes_home: Path,
    *,
    gateway_service: str = "hermes-gateway.service",
    vscode_compose_file: Path | None = None,
    vscode_env_file: Path = Path("/etc/code-server.env"),
    vscode_project_name: str = "hermes-vscode",
) -> bool:
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
        "command": (
            f"HERMES_HOME={shlex.quote(str(hermes_home))} "
            f"HERMES_GATEWAY_SERVICE={shlex.quote(gateway_service)} "
            "/usr/local/lib/hermes-ops/status-report.py"
        ),
    }
    if quick_commands.get("status") != status_command:
        quick_commands["status"] = status_command
        changed = True

    if vscode_compose_file is not None:
        docker_restart_command = {
            "type": "exec",
            "command": shlex.join(
                [
                    "sudo",
                    "docker",
                    "compose",
                    "--project-name",
                    vscode_project_name,
                    "--env-file",
                    str(vscode_env_file),
                    "-f",
                    str(vscode_compose_file),
                    "restart",
                    "code-server",
                ]
            ),
        }
        if quick_commands.get("docker_restart") != docker_restart_command:
            quick_commands["docker_restart"] = docker_restart_command
            changed = True
    elif "docker_restart" in quick_commands:
        del quick_commands["docker_restart"]
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
    result.add_argument("--gateway-service", default="hermes-gateway.service")
    result.add_argument("--vscode-enabled", action="store_true")
    result.add_argument("--vscode-compose-file", type=Path)
    result.add_argument(
        "--vscode-env-file", type=Path, default=Path("/etc/code-server.env")
    )
    result.add_argument("--vscode-project-name", default="hermes-vscode")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.vscode_enabled and args.vscode_compose_file is None:
            raise ValueError("--vscode-enabled requires --vscode-compose-file")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.vscode_project_name) is None:
            raise ValueError("invalid --vscode-project-name")
        if re.fullmatch(r"[A-Za-z0-9_.@:-]+[.]service", args.gateway_service) is None:
            raise ValueError("invalid --gateway-service")
        data = load_config(args.config)
        changed = configure(
            data,
            args.hermes_home,
            gateway_service=args.gateway_service,
            vscode_compose_file=(
                args.vscode_compose_file if args.vscode_enabled else None
            ),
            vscode_env_file=args.vscode_env_file,
            vscode_project_name=args.vscode_project_name,
        )
        if changed:
            write_config(args.config, data)
        print("changed" if changed else "unchanged")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
