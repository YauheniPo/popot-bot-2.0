#!/usr/bin/env python3
"""Run the system GitHub CLI with a managed Hermes token, without printing it."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


REAL_GH = Path("/usr/bin/gh")
TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")


def _clean_token(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return ""
    return value


def managed_token(path: Path) -> str:
    """Read only supported token keys from an Ansible-generated dotenv file."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in TOKEN_NAMES:
            continue
        raw_value = raw_value.strip()
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        token = _clean_token(value)
        if token:
            values[name] = token

    return next((values[name] for name in TOKEN_NAMES if name in values), "")


def github_environment(current: dict[str, str], hermes_home: Path) -> dict[str, str]:
    environment = dict(current)
    token = ""
    for name in TOKEN_NAMES:
        token = _clean_token(environment.get(name))
        if token:
            break
    if not token:
        token = managed_token(hermes_home / ".env")
    if token:
        # GH_TOKEN has the highest documented precedence in GitHub CLI.
        environment["GH_TOKEN"] = token
    return environment


def main() -> int:
    if not REAL_GH.is_file() or not os.access(REAL_GH, os.X_OK):
        print("managed gh wrapper: /usr/bin/gh is not installed", file=sys.stderr)
        return 127
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    environment = github_environment(dict(os.environ), hermes_home)
    os.execve(REAL_GH, [str(REAL_GH), *sys.argv[1:]], environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
