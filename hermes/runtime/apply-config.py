#!/usr/bin/env python3
"""Apply shared, non-secret Hermes VPS settings through the Hermes CLI."""

from __future__ import annotations

import argparse
import re
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


_UNRESOLVED_PLACEHOLDER = re.compile(r"\$\{|\$[A-Za-z_][A-Za-z0-9_]*")


def render_value(value: Any, variables: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    rendered = Template(value).safe_substitute(variables)
    # safe_substitute() leaves an unresolved reference untouched instead of
    # raising, in both the ${NAME} and the bare $NAME form. Catch both, not
    # just braces, or a typo'd placeholder is silently written out as a
    # literal "$..." string instead of failing loudly.
    if _UNRESOLVED_PLACEHOLDER.search(rendered):
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
            if re.fullmatch(r"[A-Za-z0-9_.@:-]+[.](?:service|timer)", value) is None:
                raise ValueError(f"vps_services.{group} contains an unsafe unit name")
            if value not in result:
                result.append(value)
    return result


def setting_value(settings: dict[str, Any], dotted_key: str) -> Any:
    exists, value = nested_value(settings, dotted_key)
    if not exists:
        raise ValueError(f"required VPS setting is missing: {dotted_key}")
    return value


def _string_setting(
    settings: dict[str, Any],
    dotted_key: str,
    pattern: str,
) -> str:
    value = setting_value(settings, dotted_key)
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid VPS setting: {dotted_key}")
    return value


def _integer_setting(
    settings: dict[str, Any],
    dotted_key: str,
    minimum: int,
    maximum: int,
) -> int:
    value = setting_value(settings, dotted_key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid VPS setting: {dotted_key}")
    return value


def _boolean_setting(settings: dict[str, Any], dotted_key: str) -> bool:
    value = setting_value(settings, dotted_key)
    if not isinstance(value, bool):
        raise ValueError(f"invalid VPS setting: {dotted_key}")
    return value


def build_asset_values(
    settings: dict[str, Any],
    *,
    hermes_user: str,
    hermes_group: str,
    user_home: str,
    hermes_home: str,
    hermes_bin: str,
    workspace: str,
    backup_dir: str,
) -> dict[str, str]:
    duration_pattern = r"[1-9][0-9]*(?:ms|s|min|h|d|w)"
    bind_address_pattern = r"127[.]0[.]0[.]1"
    alert_target = _string_setting(settings, "vps_ops.alert_target", r"[A-Za-z0-9._:-]+")
    service_restart_sec = _string_setting(
        settings,
        "vps_ops.service_restart_sec",
        duration_pattern,
    )
    gateway_services = service_names(settings, ["gateway"])
    if len(gateway_services) != 1:
        raise ValueError("vps_services.gateway must contain exactly one service")

    api_retry_endpoint = _string_setting(
        settings,
        "vps_ops.api_retry.endpoint",
        r"http://127[.]0[.]0[.]1:[1-9][0-9]{0,4}/[A-Za-z0-9._~/%:-]*",
    )
    api_retry_model = _string_setting(
        settings,
        "vps_ops.api_retry.model",
        r"[A-Za-z0-9._:/-]+",
    )
    api_retry_message = _string_setting(
        settings,
        "vps_ops.api_retry.message",
        r"[^\r\n]+",
    )
    api_retry_max_attempts = _integer_setting(
        settings,
        "vps_ops.api_retry.max_attempts",
        1,
        99,
    )
    api_retry_wait_seconds = _integer_setting(
        settings,
        "vps_ops.api_retry.wait_seconds",
        1,
        3600,
    )

    backup_required = _boolean_setting(settings, "vps_ops.backup.required")
    backup_retention = _integer_setting(settings, "vps_ops.backup.retention_days", 1, 3650)
    backup_full_day = _integer_setting(settings, "vps_ops.backup.full_day", 1, 7)
    backup_full_keep = _integer_setting(settings, "vps_ops.backup.full_keep", 1, 1000)
    deployment_backup_keep = _integer_setting(
        settings,
        "vps_ops.backup.deployment_keep",
        1,
        1000,
    )
    backup_max_age = _integer_setting(settings, "vps_ops.backup.max_age_hours", 1, 87600)
    backup_full_max_age = _integer_setting(
        settings,
        "vps_ops.backup.full_max_age_hours",
        1,
        87600,
    )
    disk_warn = _integer_setting(settings, "vps_ops.health.disk_warn_percent", 1, 100)
    inode_warn = _integer_setting(settings, "vps_ops.health.inode_warn_percent", 1, 100)
    memory_warn = _integer_setting(
        settings,
        "vps_ops.health.memory_available_warn_percent",
        1,
        100,
    )
    load_warn = _integer_setting(settings, "vps_ops.health.load_warn_per_cpu", 1, 100)
    metrics_max_age = _integer_setting(
        settings,
        "vps_ops.health.metrics_max_age_minutes",
        1,
        1440,
    )

    timer_values: dict[str, str] = {}
    timer_names = {
        "backup": "BACKUP",
        "health": "HEALTH",
        "metrics": "METRICS",
        "observability_cleanup": "OBSERVABILITY_PRUNE",
    }
    for timer, rendered_prefix in timer_names.items():
        prefix = f"vps_ops.timers.{timer}"
        timer_values[f"{rendered_prefix}_ON_BOOT_SEC"] = _string_setting(
            settings,
            f"{prefix}.on_boot_sec",
            duration_pattern,
        )
        timer_values[f"{rendered_prefix}_INTERVAL"] = _string_setting(
            settings,
            f"{prefix}.interval",
            duration_pattern,
        )
        timer_values[f"{rendered_prefix}_ACCURACY_SEC"] = _string_setting(
            settings,
            f"{prefix}.accuracy_sec",
            duration_pattern,
        )
        timer_values[f"{rendered_prefix}_RANDOMIZED_DELAY_SEC"] = _string_setting(
            settings,
            f"{prefix}.randomized_delay_sec",
            duration_pattern,
        )

    dashboard_address = _string_setting(
        settings,
        "vps_observability.dashboard.bind_address",
        bind_address_pattern,
    )
    grafana_address = _string_setting(
        settings,
        "vps_observability.grafana.bind_address",
        bind_address_pattern,
    )
    prometheus_address = _string_setting(
        settings,
        "vps_observability.prometheus.bind_address",
        bind_address_pattern,
    )
    node_exporter_address = _string_setting(
        settings,
        "vps_observability.node_exporter.bind_address",
        bind_address_pattern,
    )
    dashboard_port = _integer_setting(settings, "vps_observability.dashboard.port", 1, 65535)
    grafana_port = _integer_setting(settings, "vps_observability.grafana.port", 1, 65535)
    prometheus_port = _integer_setting(settings, "vps_observability.prometheus.port", 1, 65535)
    node_exporter_port = _integer_setting(
        settings,
        "vps_observability.node_exporter.port",
        1,
        65535,
    )
    if len({dashboard_port, grafana_port, prometheus_port, node_exporter_port}) != 4:
        raise ValueError("vps_observability ports must be unique")

    prometheus_retention = _string_setting(
        settings,
        "vps_observability.prometheus.storage_retention",
        duration_pattern,
    )
    scrape_interval = _string_setting(
        settings,
        "vps_observability.prometheus.scrape_interval",
        duration_pattern,
    )
    evaluation_interval = _string_setting(
        settings,
        "vps_observability.prometheus.evaluation_interval",
        duration_pattern,
    )
    database_retention_days = _integer_setting(
        settings,
        "vps_observability.local_storage.database_retention_days",
        1,
        3650,
    )
    audit_max_bytes = _integer_setting(
        settings,
        "vps_observability.local_storage.audit_max_bytes",
        65536,
        104857600,
    )
    audit_rotated_files = _integer_setting(
        settings,
        "vps_observability.local_storage.audit_rotated_files",
        1,
        10,
    )

    values = {
        "HERMES_USER": hermes_user,
        "HERMES_GROUP": hermes_group,
        "USER_HOME": user_home,
        "HERMES_HOME": hermes_home,
        "HERMES_BIN": hermes_bin,
        "WORKSPACE": workspace,
        "OPS_ALERT_TARGET": alert_target,
        "SERVICE_RESTART_SEC": service_restart_sec,
        "GATEWAY_SERVICE": gateway_services[0],
        "API_RETRY_ENDPOINT": api_retry_endpoint,
        "API_RETRY_MODEL": api_retry_model,
        "API_RETRY_MESSAGE": api_retry_message,
        "API_RETRY_MAX_ATTEMPTS": str(api_retry_max_attempts),
        "API_RETRY_WAIT_SECONDS": str(api_retry_wait_seconds),
        "BACKUP_REQUIRED": cli_value(backup_required),
        "BACKUP_DIR": backup_dir,
        "BACKUP_MAX_AGE_HOURS": str(backup_max_age),
        "BACKUP_RETENTION_DAYS": str(backup_retention),
        "FULL_BACKUP_DAY": str(backup_full_day),
        "FULL_BACKUP_MAX_AGE_HOURS": str(backup_full_max_age),
        "FULL_BACKUP_KEEP": str(backup_full_keep),
        "DEPLOYMENT_BACKUP_KEEP": str(deployment_backup_keep),
        "DISK_WARN_PERCENT": str(disk_warn),
        "INODE_WARN_PERCENT": str(inode_warn),
        "MEMORY_AVAILABLE_WARN_PERCENT": str(memory_warn),
        "LOAD_WARN_PER_CPU": str(load_warn),
        "METRICS_FILE": f"{hermes_home}/ops/metrics/hermes.prom",
        "METRICS_MAX_AGE_MINUTES": str(metrics_max_age),
        "OBSERVABILITY_DATABASE_RETENTION_DAYS": str(database_retention_days),
        "OBSERVABILITY_AUDIT_MAX_BYTES": str(audit_max_bytes),
        "OBSERVABILITY_AUDIT_ROTATED_FILES": str(audit_rotated_files),
        "DASHBOARD_BIND_ADDRESS": dashboard_address,
        "DASHBOARD_PORT": str(dashboard_port),
        "GRAFANA_BIND_ADDRESS": grafana_address,
        "GRAFANA_PORT": str(grafana_port),
        "PROMETHEUS_BIND_ADDRESS": prometheus_address,
        "PROMETHEUS_PORT": str(prometheus_port),
        "PROMETHEUS_RETENTION": prometheus_retention,
        "PROMETHEUS_SCRAPE_INTERVAL": scrape_interval,
        "PROMETHEUS_EVALUATION_INTERVAL": evaluation_interval,
        "NODE_EXPORTER_BIND_ADDRESS": node_exporter_address,
        "NODE_EXPORTER_PORT": str(node_exporter_port),
    }
    values.update(timer_values)
    for key, value in values.items():
        if not value or "\n" in value or "\r" in value:
            raise ValueError(f"unsafe rendered VPS setting: {key}")
    return values


def render_asset(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"@{key}@", value)
    unresolved = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", rendered)))
    if unresolved:
        raise ValueError(f"unresolved template placeholders: {', '.join(unresolved)}")
    return rendered


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

    value_parser = subparsers.add_parser("value", help="Print one scalar VPS setting")
    value_parser.add_argument("--settings", required=True, type=Path)
    value_parser.add_argument("key")

    render_parser = subparsers.add_parser("render", help="Render one managed VPS asset")
    render_parser.add_argument("--settings", required=True, type=Path)
    render_parser.add_argument("--template", required=True, type=Path)
    render_parser.add_argument("--hermes-user", required=True)
    render_parser.add_argument("--hermes-group", required=True)
    render_parser.add_argument("--user-home", required=True)
    render_parser.add_argument("--hermes-home", required=True)
    render_parser.add_argument("--hermes-bin", required=True)
    render_parser.add_argument("--workspace", required=True)
    render_parser.add_argument("--backup-dir", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        settings = load_settings(args.settings)
        if args.command == "services":
            for service in service_names(settings, args.groups):
                print(service)
            return 0
        if args.command == "value":
            value = setting_value(settings, args.key)
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"VPS setting is not scalar: {args.key}")
            print(cli_value(value))
            return 0
        if args.command == "render":
            values = build_asset_values(
                settings,
                hermes_user=args.hermes_user,
                hermes_group=args.hermes_group,
                user_home=args.user_home,
                hermes_home=args.hermes_home,
                hermes_bin=args.hermes_bin,
                workspace=args.workspace,
                backup_dir=args.backup_dir,
            )
            template = args.template.read_text(encoding="utf-8")
            print(render_asset(template, values), end="")
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
