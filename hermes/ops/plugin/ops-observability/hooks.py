"""Hermes hook callbacks that emit metadata-only telemetry."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any

from .billing import _price, _usage_values
from .privacy import _command_program, _id, _number, _safe_args
from .storage import _audit, _execute, _now

_API_CALL_COLUMNS = (
    "ts", "request_id", "session_id", "turn_id", "provider", "model", "platform", "status",
    "duration_ms", "input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "cost_usd",
    "cost_source", "finish_reason", "status_code", "retry_count", "requested_model", "call_index",
)
_INSERT_API_CALL = (
    f"INSERT INTO api_calls ({', '.join(_API_CALL_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _API_CALL_COLUMNS)})"
)

_SKILL_ACTIONS = frozenset({"loaded", "created", "patched", "edited", "installed", "archived", "restored", "stale"})
_SKILL_PROVENANCE = frozenset({"installed", "agent_created", "external", "local"})

# A logical request ID is reused by retries. Measure each actual attempt from
# its pre hook, excluding retry sleeps. Bound abandoned/cancelled entries.
_ATTEMPTS: OrderedDict[tuple[str, ...], tuple[float, int]] = OrderedDict()
_ATTEMPTS_LOCK = threading.Lock()
_MAX_ATTEMPTS = 512


def _attempt_key(kwargs: dict) -> tuple[str, ...]:
    return tuple(_id(kwargs.get(key)) for key in
                 ('api_request_id', 'session_id', 'turn_id', 'provider', 'model'))


def _finish_attempt(kwargs: dict) -> tuple[float, int]:
    with _ATTEMPTS_LOCK:
        attempt = _ATTEMPTS.pop(_attempt_key(kwargs), None)
    if attempt is not None:
        started, retry = attempt
        return max(0, time.monotonic() - started) * 1000, retry
    # Compatibility with observers without pre hooks. Historical cumulative
    # durations cannot be reconstructed into provider processing time.
    duration = (_number(kwargs['api_duration_ms']) if 'api_duration_ms' in kwargs
                else _number(kwargs.get('api_duration')) * 1000)
    return duration, int(_number(kwargs.get('retry_count')))


def _on_model_fallback(from_provider: str = '', from_model: str = '',
                       to_provider: str = '', to_model: str = '', **kwargs: Any) -> None:
    route = tuple(_id(value) for value in (from_provider, from_model, to_provider, to_model))
    _execute('INSERT INTO route_fallbacks VALUES (?, ?, ?, ?, ?)', (_now(), *route))
    _audit('model.fallback', from_provider=route[0], from_model=route[1],
           to_provider=route[2], to_model=route[3])


def _skill_actor() -> tuple[str, str]:
    """Read the turn's context, never a process-global 'current agent' cache.

    Hermes binds each child to its own turn and propagates ContextVars into
    tool/hook workers. Self-improvement forks can share their parent's session
    ID, so session IDs alone cannot identify the actor.
    """
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
        from tools.skill_provenance import get_current_write_origin
        agent = get_active_subagent_parent()
        origin = get_current_write_origin()
    except ImportError:
        return "unknown", ""
    session_id = _id(getattr(agent, "session_id", ""))
    if origin == "background_review":
        return "self_improvement", session_id
    if agent is None:
        return "unknown", session_id
    return ("subagent" if _number(getattr(agent, "_delegate_depth", 0)) > 0 else "main"), session_id


def _on_skill_lifecycle(
    skill_name: str = "", action: str = "", provenance: str = "",
    task_id: str = "", session_id: str = "", **kwargs: Any,
) -> None:
    """Persist only confirmed native lifecycle facts, not tool attempts/content."""
    if (not isinstance(skill_name, str) or not re.fullmatch(r"\w[\w.:/-]{0,159}", skill_name, re.ASCII)
            or any(part in {"", ".", ".."} for part in re.split(r"[/:]", skill_name))
            or not isinstance(action, str) or action not in _SKILL_ACTIONS):
        return
    actor, active_session = _skill_actor()
    source = provenance if isinstance(provenance, str) and provenance in _SKILL_PROVENANCE else "unknown"
    timestamp = _now()
    session = _id(session_id) or active_session
    task = _id(task_id)
    _execute(
        "INSERT INTO skill_events (ts, skill_name, action, agent_type, provenance, session_id, task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (timestamp, skill_name, action, actor, source, session, task),
    )
    _audit("skill.lifecycle", skill=skill_name, action=action, agent_type=actor,
           provenance=source, session_id=session, task_id=task)


def _pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", turn_id: str = "", **kwargs: Any) -> None:
    _audit("tool.start", tool=_id(tool_name), session_id=_id(kwargs.get('session_id') or task_id),
           turn_id=_id(turn_id), args=_safe_args(args))


def _post_tool_call(
    tool_name: str = "", task_id: str = "",
    turn_id: str = "", duration_ms: Any = 0, status: str = "", **kwargs: Any
) -> None:
    final_status = _id(status or "ok")
    duration = _number(duration_ms or kwargs.get("duration") or kwargs.get("elapsed_ms"))
    session = _id(kwargs.get('session_id') or task_id)
    _execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), session, _id(turn_id), _id(tool_name), final_status, duration),
    )
    _audit("tool.end", tool=_id(tool_name), session_id=session, turn_id=_id(turn_id), status=final_status, duration_ms=duration)


def _pre_api_request(**kwargs: Any) -> None:
    key = _attempt_key(kwargs)
    if key[0]:
        with _ATTEMPTS_LOCK:
            _ATTEMPTS[key] = (time.monotonic(), int(_number(kwargs.get('retry_count'))))
            _ATTEMPTS.move_to_end(key)
            while len(_ATTEMPTS) > _MAX_ATTEMPTS:
                _ATTEMPTS.popitem(last=False)
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
    duration_ms, retry_count = _finish_attempt(kwargs)
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), provider, model, _id(kwargs.get("platform")), "ok",
        duration_ms, input_tokens, output_tokens, cache_tokens, total_tokens, cost, source,
        _id(kwargs.get("finish_reason")), 0, retry_count,
        _id(kwargs.get("model")), int(_number(kwargs.get("api_call_count"))),
    )
    _execute(_INSERT_API_CALL, values)
    _audit(
        "api.end", request_id=values[1], session_id=values[2], turn_id=values[3],
        provider=provider, model=model, requested_model=values[18], platform=values[6], status="ok",
        duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_tokens, total_tokens=total_tokens, cost_usd=round(cost, 8),
        cost_source=source, finish_reason=values[15],
    )


def _api_request_error(**kwargs: Any) -> None:
    duration_ms, retry_count = _finish_attempt(kwargs)
    values = (
        _now(), _id(kwargs.get("api_request_id")), _id(kwargs.get("session_id")),
        _id(kwargs.get("turn_id")), _id(kwargs.get("provider")), _id(kwargs.get("model")),
        _id(kwargs.get("platform")), "error", duration_ms, 0, 0, 0, 0, 0.0,
        "unavailable", _id(kwargs.get("reason")), int(_number(kwargs.get("status_code"))),
        retry_count,
        _id(kwargs.get("model")), int(_number(kwargs.get("api_call_count"))),
    )
    _execute(_INSERT_API_CALL, values)
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



