#!/usr/bin/env python3
"""Export a deliberately small, non-secret Docker configuration for Ansible.

The Docker volume also contains OAuth credentials, sessions, memory, and
runtime state.  None of those are portable configuration, so this exporter
never reads .env, auth.json, databases, or any file other than config.yaml.
It also copies only an explicit safe allowlist from config.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path("/opt/data/config.yaml")

# Keep this list intentionally narrow. Do not add URLs, headers, API keys,
# OAuth fields, custom provider definitions, or arbitrary plugin settings.
# Exported values must always be scalar: a nested mapping can hide credentials
# even when its top-level field name looks safe.
SAFE_FIELDS: dict[str, tuple[str, ...]] = {
    # provider/default stay live so /model_global survives later deployments.
    "model": ("max_tokens",),
    "agent": ("reasoning_effort",),
    "stt": ("enabled", "language"),
    "web": ("backend", "search_backend", "extract_backend", "extract_char_limit"),
    "browser": ("backend", "inactivity_timeout"),
    "compression": (
        "enabled",
        "threshold",
        "target_ratio",
        "protect_last_n",
        "min_tail_user_messages",
        "protect_first_n",
    ),
    "display": ("show_reasoning", "streaming", "skin"),
}


def pick_mapping(config: dict[str, Any], section: str, fields: tuple[str, ...]) -> dict[str, Any]:
    source = config.get(section)
    if not isinstance(source, dict):
        return {}
    return {
        key: source[key]
        for key in fields
        if key in source and isinstance(source[key], (str, int, float, bool))
    }


def main() -> int:
    if not CONFIG_PATH.is_file():
        print(f"error: Hermes configuration not found at {CONFIG_PATH}", file=sys.stderr)
        return 1

    try:
        raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        print(f"error: cannot parse {CONFIG_PATH}: {error}", file=sys.stderr)
        return 1

    if not isinstance(raw_config, dict):
        print(f"error: {CONFIG_PATH} must contain a YAML mapping", file=sys.stderr)
        return 1

    overlay: dict[str, Any] = {}
    for section, fields in SAFE_FIELDS.items():
        selected = pick_mapping(raw_config, section, fields)
        if selected:
            overlay[section] = selected

    # Toolset lists contain no credentials and are useful to preserve enabled
    # voice/STT-related capabilities. Keep only lists of string identifiers.
    toolsets = raw_config.get("platform_toolsets")
    if isinstance(toolsets, dict):
        safe_toolsets = {
            platform: values
            for platform, values in toolsets.items()
            if isinstance(platform, str)
            and isinstance(values, list)
            and all(isinstance(value, str) for value in values)
        }
        if safe_toolsets:
            overlay["platform_toolsets"] = safe_toolsets

    print("# Generated from Docker config.yaml; no .env, OAuth, sessions, memory, or tokens are included.")
    print("# Review before merging managed_overlay into config/vps-defaults.yml.")
    print(
        yaml.safe_dump(
            {"vps_hermes": {"config": {"managed_overlay": overlay}}},
            allow_unicode=True,
            sort_keys=False,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
