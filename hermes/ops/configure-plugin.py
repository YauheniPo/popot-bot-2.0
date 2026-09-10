#!/usr/bin/env python3
"""Enable the Hermes observability plugin without invoking an interactive CLI."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from hermes_config_io import load_config, write_config


def configure(
    data: dict[str, Any],
    hermes_home: Path,
    *,
    gateway_service: str = "hermes-gateway.service",
    vscode_compose_file: Path | None = None,
    vscode_env_file: Path = Path("/etc/code-server.env"),
    vscode_project_name: str = "hermes-vscode",
) -> bool:
    # Validate at the command-construction boundary, including direct callers.
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", vscode_project_name) is None:
        raise ValueError("invalid --vscode-project-name")
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*[.]service", gateway_service) is None:
        raise ValueError("invalid --gateway-service")
    vscode_compose_file, vscode_env_file = _resolve_vscode_paths(
        vscode_compose_file, vscode_env_file
    )
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config", required=True, type=Path,
        help="must resolve to <hermes-home>/config.yaml; no other location is accepted",
    )
    result.add_argument("--hermes-home", required=True, type=Path)
    result.add_argument("--gateway-service", default="hermes-gateway.service")
    result.add_argument("--vscode-enabled", action="store_true")
    result.add_argument("--vscode-compose-file", type=Path)
    result.add_argument(
        "--vscode-env-file", type=Path, default=Path("/etc/code-server.env")
    )
    result.add_argument("--vscode-project-name", default="hermes-vscode")
    return result


def _resolve_vscode_path(value: Path, label: str) -> Path:
    # Mirror the safe-path charset install-ops.sh already enforces before
    # this script can be reached in the shipped flow; keep the same
    # defense here so a direct invocation cannot smuggle shell metacharacters
    # into the docker_restart quick command built below. Validate the
    # *resolved* path so a ".."-laden argument cannot slip past the
    # charset check and still land outside the intended directory.
    resolved = value.expanduser().resolve()
    if re.fullmatch(r"/[A-Za-z0-9._/@+-]+", str(resolved)) is None:
        raise ValueError(f"unsafe or unsupported path for {label}: {value}")
    return resolved


def _resolve_vscode_paths(
    compose_file: Path | None, env_file: Path
) -> tuple[Path | None, Path]:
    return (
        _resolve_vscode_path(compose_file, "--vscode-compose-file")
        if compose_file is not None else None,
        _resolve_vscode_path(env_file, "--vscode-env-file"),
    )


def main() -> int:
    args = parser().parse_args()
    try:
        if args.vscode_enabled and args.vscode_compose_file is None:
            raise ValueError("--vscode-enabled requires --vscode-compose-file")
        # Anchor --config to --hermes-home instead of trusting its basename
        # alone: a basename-only check still lets the directory component
        # point anywhere on the filesystem.
        expected_config = (args.hermes_home.expanduser() / "config.yaml").resolve()
        if args.config.resolve() != expected_config:
            raise ValueError(f"--config must be {expected_config}")
        resolved_vscode_compose_file, resolved_vscode_env_file = _resolve_vscode_paths(
            args.vscode_compose_file, args.vscode_env_file
        )
        data = load_config(expected_config)
        changed = configure(
            data,
            args.hermes_home,
            gateway_service=args.gateway_service,
            vscode_compose_file=(
                resolved_vscode_compose_file if args.vscode_enabled else None
            ),
            vscode_env_file=resolved_vscode_env_file,
            vscode_project_name=args.vscode_project_name,
        )
        if changed:
            write_config(expected_config, data)
        print("changed" if changed else "unchanged")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
