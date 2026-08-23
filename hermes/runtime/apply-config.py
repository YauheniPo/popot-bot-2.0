#!/usr/bin/env python3
"""Apply shared, non-secret Hermes VPS settings through the Hermes CLI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Any, NamedTuple

import yaml


class Operation(NamedTuple):
    action: str
    key: str
    value: Any = None


def load_settings(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read VPS settings from {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return loaded


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read Hermes config from {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return loaded


def nested_value(data: dict[str, Any], dotted_key: str) -> tuple[bool, Any]:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def render_value(value: Any, variables: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    rendered = Template(value).safe_substitute(variables)
    if "${" in rendered:
        raise ValueError(f"unresolved placeholder in VPS setting: {value}")
    return rendered


def _mapping(section: dict[str, Any], key: str) -> dict[str, Any]:
    value = section.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"vps_runtime.{key} must be a mapping")
    return value


def _string_list(section: dict[str, Any], key: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"vps_runtime.{key} must be a list of non-empty strings")
    return value


def build_operations(
    settings: dict[str, Any],
    current_config: dict[str, Any],
    variables: dict[str, str],
    capabilities: set[str],
) -> list[Operation]:
    runtime = settings.get("vps_runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("vps_runtime must be a mapping")

    operations: list[Operation] = []
    for key, raw_value in _mapping(runtime, "set").items():
        if not isinstance(key, str) or not key:
            raise ValueError("vps_runtime.set keys must be non-empty strings")
        value = render_value(raw_value, variables)
        exists, current = nested_value(current_config, key)
        if not exists or current != value:
            operations.append(Operation("set", key, value))

    for key, raw_value in _mapping(runtime, "set_if_missing").items():
        if not isinstance(key, str) or not key:
            raise ValueError("vps_runtime.set_if_missing keys must be non-empty strings")
        exists, _ = nested_value(current_config, key)
        if not exists:
            operations.append(Operation("set", key, render_value(raw_value, variables)))

    for key in _string_list(runtime, "unset"):
        exists, _ = nested_value(current_config, key)
        if exists:
            operations.append(Operation("unset", key))

    capability_settings = runtime.get("capabilities", {})
    if not isinstance(capability_settings, dict):
        raise ValueError("vps_runtime.capabilities must be a mapping")
    for capability, raw_rules in capability_settings.items():
        if not isinstance(capability, str) or not isinstance(raw_rules, dict):
            raise ValueError("each capability must have a name and mapping rules")
        if capability in capabilities:
            for key, raw_value in _mapping(raw_rules, "set").items():
                value = render_value(raw_value, variables)
                exists, current = nested_value(current_config, key)
                if not exists or current != value:
                    operations.append(Operation("set", key, value))
        else:
            for key in _string_list(raw_rules, "unset_when_missing"):
                exists, _ = nested_value(current_config, key)
                if exists:
                    operations.append(Operation("unset", key))

    return operations


def cli_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def run_operation(hermes_bin: Path, operation: Operation) -> None:
    command = [str(hermes_bin), "config", operation.action, operation.key]
    if operation.action == "set":
        command.append(cli_value(operation.value))
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        raise RuntimeError(f"Hermes config {operation.action} failed for {operation.key}: {detail}")


def service_names(settings: dict[str, Any], groups: list[str]) -> list[str]:
    configured = settings.get("vps_services", {})
    if not isinstance(configured, dict):
        raise ValueError("vps_services must be a mapping")
    result: list[str] = []
    for group in groups:
        values = configured.get(group)
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise ValueError(f"vps_services.{group} must be a list of non-empty strings")
        for value in values:
            if value not in result:
                result.append(value)
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Apply shared Hermes runtime settings")
    apply_parser.add_argument("--settings", required=True, type=Path)
    apply_parser.add_argument("--hermes-home", required=True, type=Path)
    apply_parser.add_argument("--hermes-bin", required=True, type=Path)
    apply_parser.add_argument("--workspace", required=True)
    apply_parser.add_argument("--capability", action="append", default=[])

    services_parser = subparsers.add_parser("services", help="Print managed service names")
    services_parser.add_argument("--settings", required=True, type=Path)
    services_parser.add_argument("groups", nargs="+")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        settings = load_settings(args.settings)
        if args.command == "services":
            for service in service_names(settings, args.groups):
                print(service)
            return 0

        current = load_config(args.hermes_home / "config.yaml")
        operations = build_operations(
            settings,
            current,
            {"HERMES_WORKSPACE": args.workspace},
            set(args.capability),
        )
        for operation in operations:
            run_operation(args.hermes_bin, operation)
        print(f"Hermes runtime settings applied: {len(operations)} change(s)")
        return 0
    except (ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
