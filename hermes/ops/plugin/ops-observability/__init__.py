"""Privacy-aware local observability for Hermes.

The plugin deliberately stores metadata, not prompts, responses, tool output,
email bodies, browser contents, or provider error bodies.
"""

from __future__ import annotations

from typing import Any

from . import storage
from .billing import _price, _usage_values
from .hooks import (
    _api_request_error,
    _on_session_end,
    _on_session_start,
    _post_api_request,
    _post_approval_response,
    _post_tool_call,
    _pre_api_request,
    _pre_approval_request,
    _pre_command,
    _pre_tool_call,
)
from .privacy import _command_program, _id, _mapping, _nested_number, _number, _safe_args, _short
from .storage import _audit, _db, _enqueue, _execute, _home, _now, _paths, _start_worker


def register(ctx: Any) -> None:
    """Register metadata-only observers for the Prometheus/Grafana pipeline."""
    _db().close()
    _start_worker()
    hooks = {
        "pre_tool_call": _pre_tool_call,
        "post_tool_call": _post_tool_call,
        "pre_api_request": _pre_api_request,
        "post_api_request": _post_api_request,
        "api_request_error": _api_request_error,
        "on_session_start": _on_session_start,
        "on_session_end": _on_session_end,
        "pre_approval_request": _pre_approval_request,
        "post_approval_response": _post_approval_response,
        "pre_command": _pre_command,
    }
    for name, callback in hooks.items():
        ctx.register_hook(name, callback)
