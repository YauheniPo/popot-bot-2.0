"""Hermes hook callbacks that emit metadata-only telemetry."""

from __future__ import annotations

from typing import Any

from .billing import _price, _usage_values
from .privacy import _command_program, _id, _number, _safe_args
from .storage import _audit, _execute, _now

def _pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", turn_id: str = "", **kwargs: Any) -> None:
    _audit("tool.start", tool=_id(tool_name), session_id=_id(task_id), turn_id=_id(turn_id), args=_safe_args(args))


def _post_tool_call(
    tool_name: str = "", args: Any = None, result: Any = None, task_id: str = "",
    turn_id: str = "", duration_ms: Any = 0, status: str = "", **kwargs: Any
) -> None:
    final_status = _id(status or "ok")
    duration = _number(duration_ms or kwargs.get("duration") or kwargs.get("elapsed_ms"))
    _execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), _id(task_id or kwargs.get("session_id")), _id(turn_id), _id(tool_name), final_status, duration),
    )
    _audit("tool.end", tool=_id(tool_name), session_id=_id(task_id), turn_id=_id(turn_id), status=final_status, duration_ms=duration)


def _pre_api_request(**kwargs: Any) -> None:
    _audit(
        "api.start",
        request_id=_id(kwargs.get("api_request_id")),
        session_id=_id(kwargs.get("session_id")),
        turn_id=_id(kwargs.get("turn_id")),
        provider=_id(kwargs.get("provider")),
        model=_id(kwargs.get("model")),
        platform=_id(kwargs.get("platform")),
        approximate_input_tokens=int(_number(kwargs.get("approx_input_tokens"))),
        retry_count=int(_number(kwargs.get("retry_count"))),
    )


def _post_api_request(**kwargs: Any) -> None:
    provider = _id(kwargs.get("provider"))
    model = _id(kwargs.get("response_model") or kwargs.get("model"))
    input_tokens, output_tokens, cache_tokens, total_tokens, cost, source = _usage_values(kwargs.get("usage"))
    if source == "unavailable":
        cost, source = _price(provider, model, input_tokens, output_tokens, cache_tokens)
    duration_ms = _number(kwargs.get("api_duration"))
    # Hermes currently reports API duration in seconds. Accept explicit *_ms if added later.
    if "api_duration_ms" in kwargs:
        duration_ms = _number(kwargs.get("api_duration_ms"))
    elif duration_ms and duration_ms < 10_000:
        duration_ms *= 1000
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), provider, model, _id(kwargs.get("platform")), "ok",
        duration_ms, input_tokens, output_tokens, cache_tokens, total_tokens, cost, source,
        _id(kwargs.get("finish_reason")), 0, 0,
    )
    _execute("INSERT INTO api_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    _audit(
        "api.end", request_id=values[1], session_id=values[2], turn_id=values[3],
        provider=provider, model=model, platform=values[6], status="ok",
        duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_tokens, total_tokens=total_tokens, cost_usd=round(cost, 8),
        cost_source=source,
    )


def _api_request_error(**kwargs: Any) -> None:
    duration_ms = _number(kwargs.get("api_duration"))
    if duration_ms and duration_ms < 10_000:
        duration_ms *= 1000
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), _id(kwargs.get("provider")), _id(kwargs.get("model")),
        _id(kwargs.get("platform")), "error", duration_ms, 0, 0, 0, 0, 0.0,
        "unavailable", _id(kwargs.get("reason")), int(_number(kwargs.get("status_code"))),
        int(_number(kwargs.get("retry_count"))),
    )
    _execute("INSERT INTO api_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
    _audit(
        "api.error", request_id=values[1], session_id=values[2], turn_id=values[3],
        provider=values[4], model=values[5], platform=values[6], status_code=values[16],
        retry_count=values[17], retryable=bool(kwargs.get("retryable")), reason=values[15],
    )


def _on_session_start(session_id: str = "", model: str = "", platform: str = "", **kwargs: Any) -> None:
    _execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'start', 0, 0, 0)",
        (_now(), _id(session_id), _id(model), _id(platform)),
    )
    _audit("session.start", session_id=_id(session_id), model=_id(model), platform=_id(platform))


def _on_session_end(session_id: str = "", model: str = "", platform: str = "", **kwargs: Any) -> None:
    completed = int(bool(kwargs.get("completed")))
    interrupted = int(bool(kwargs.get("interrupted")))
    failed = int(bool(kwargs.get("failed")) or (not completed and not interrupted))
    _execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, 'end', ?, ?, ?)",
        (_now(), _id(session_id), _id(model), _id(platform), completed, failed, interrupted),
    )
    _audit(
        "session.end", session_id=_id(session_id), model=_id(model), platform=_id(platform),
        completed=completed, failed=failed, interrupted=interrupted,
    )


def _pre_approval_request(**kwargs: Any) -> None:
    _execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, 'request', '', ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("turn_id")),
         _id(kwargs.get("surface")), _id(kwargs.get("pattern_key"))),
    )
    _audit(
        "approval.request", session_id=_id(kwargs.get("session_key")),
        turn_id=_id(kwargs.get("turn_id")), surface=_id(kwargs.get("surface")),
        pattern_key=_id(kwargs.get("pattern_key")), command_program=_command_program(kwargs.get("command")),
    )


def _post_approval_response(**kwargs: Any) -> None:
    choice = _id(kwargs.get("choice"))
    _execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, 'response', ?, ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("turn_id")),
         _id(kwargs.get("surface")), choice, _id(kwargs.get("pattern_key"))),
    )
    _audit(
        "approval.response", session_id=_id(kwargs.get("session_key")),
        turn_id=_id(kwargs.get("turn_id")), surface=_id(kwargs.get("surface")),
        pattern_key=_id(kwargs.get("pattern_key")), choice=choice,
        command_program=_command_program(kwargs.get("command")), decided_by=_id(kwargs.get("decided_by")),
    )


def _pre_command(**kwargs: Any) -> None:
    command = _command_program(kwargs.get("command"))
    _execute(
        "INSERT INTO commands VALUES (?, ?, ?, ?, ?)",
        (_now(), _id(kwargs.get("session_key")), _id(kwargs.get("surface")),
         _id(kwargs.get("platform")), command),
    )
    # args_raw is intentionally omitted: slash-command arguments can contain secrets.
    _audit(
        "command", session_id=_id(kwargs.get("session_key")), surface=_id(kwargs.get("surface")),
        platform=_id(kwargs.get("platform")), command=command, alias=_id(kwargs.get("alias_used")),
    )



