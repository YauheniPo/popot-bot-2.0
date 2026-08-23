"""Bounded metadata sanitization for observability events."""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import urlsplit

_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s&;]+"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--(?:password|passwd|secret|token|api[-_]?key|authorization|auth)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
_BASIC_AUTH_FLAG = re.compile(r"(?i)(\b(?:curl|wget)\b.*?\s-u\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")
_SSHPASS_FLAG = re.compile(r"(?i)(\bsshpass\s+-p\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_SAFE_ARG_KEYS = {
    "command", "path", "file_path", "directory", "url", "repo", "branch", "operation", "action"
}
_COMMAND_VALUE_KEYS = {"command", "cmd", "shell_command", "args_raw"}

def _short(value: Any, limit: int = 500) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    text = _SECRET_TEXT.sub(lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]", text)
    text = _SECRET_FLAG.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _BASIC_AUTH_FLAG.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _SSHPASS_FLAG.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = _URL_USERINFO.sub(r"\1[REDACTED]@", text)
    text = re.sub(r"-----BEGIN [^-]+ PRIVATE KEY-----.*", "[REDACTED PRIVATE KEY]", text, flags=re.IGNORECASE)
    return text[:limit]


def _command_program(value: Any) -> str:
    """Return only a safe executable/command identifier, never its arguments."""

    try:
        parts = shlex.split(str(value or ""))
    except ValueError:
        return "[command]"
    if not parts:
        return "[command]"
    candidate = parts[0].lstrip("/")
    if re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", candidate):
        return candidate
    return "[command]"


def _safe_audit_value(value: Any, limit: int = 240) -> Any | None:
    """Keep bounded metadata while rejecting opaque values and secret-bearing keys."""

    if isinstance(value, (str, int, float, bool)):
        return _short(value, limit)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, nested_value in value.items():
            key_text = str(key)[:80]
            if _SENSITIVE_KEY.search(key_text):
                continue
            if key_text.lower() in _COMMAND_VALUE_KEYS:
                safe["command_program"] = _command_program(nested_value)
                continue
            sanitized = _safe_audit_value(nested_value, limit)
            if sanitized is not None:
                safe[key_text] = sanitized
        return safe
    if isinstance(value, (list, tuple)):
        return [_short(item, 120) for item in value[:40] if isinstance(item, (str, int, float, bool))]
    return None


def _id(value: Any) -> str:
    return _short(value, 160)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            result = value.model_dump()
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        try:
            return dict(vars(value))
        except Exception:
            return {}
    return {}


def _nested_number(data: dict[str, Any], *names: str) -> float | None:
    for name in names:
        if name in data:
            return _number(data.get(name))
    for nested_name in ("usage", "token_usage", "details", "input_tokens_details", "output_tokens_details"):
        nested = _mapping(data.get(nested_name))
        if nested:
            result = _nested_number(nested, *names)
            if result is not None:
                return result
    return None


def _safe_args(args: Any) -> dict[str, Any]:
    data = _mapping(args)
    summary: dict[str, Any] = {"arg_keys": sorted(str(key)[:80] for key in data.keys())[:40]}
    for key, value in data.items():
        key_text = str(key)
        if _SENSITIVE_KEY.search(key_text) or key_text.lower() not in _SAFE_ARG_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)):
            if key_text.lower() == "url":
                try:
                    parsed = urlsplit(str(value))
                    value = f"{parsed.scheme}://{parsed.hostname or ''}"
                except ValueError:
                    value = "[invalid-url]"
            if key_text.lower() == "command":
                summary["command_program"] = _command_program(value)
            else:
                summary[key_text[:80]] = _short(value, 240)
    return summary



