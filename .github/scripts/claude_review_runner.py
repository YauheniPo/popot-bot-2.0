"""Observable Messages API tool loop. Linux/macOS CI; no model-executed shell."""
from __future__ import annotations

import fnmatch
from email.utils import parsedate_to_datetime
import json
import math
import multiprocessing
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, TextIO
import urllib.error
import urllib.request

from pr_review_context import _json_response_text
from review_execution import safe_label

MAX_TOOL_BYTES = 48_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FILES = 20_000
REVIEW_DIFF_PATH = ".ci-observable-review.diff"
TOOLS = [
    {"name": "Read", "description": "Read a tracked text file, with line numbers. Use offset/limit to page through large files.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"},
        "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1}}, "required": ["path"]}},
    {"name": "Glob", "description": "List tracked files matching a shell glob (at most 100 results).",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search tracked files for a literal string, NOT a regex; optionally restrict paths with glob. At most 100 matches.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"},
        "glob": {"type": "string"}}, "required": ["pattern"]}},
]


class ReviewTimeout(RuntimeError):
    """A wall-clock or inactivity deadline expired."""


class ReviewFailure(RuntimeError):
    """Safe machine-readable failure reason; never carries provider body text."""


class RateLimitFailure(ReviewFailure):
    """Only allow-listed rate-limit diagnostics may cross the worker boundary."""

    def __init__(self, details: dict):
        super().__init__("http_429")
        self.details = details


def _nonnegative_number(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _retry_hint(headers: dict) -> int | None:
    now = time.time()
    raw = headers.get("retry-after", "")
    delay = _nonnegative_number(raw)
    if delay is None and raw:
        try:
            delay = max(0, parsedate_to_datetime(raw).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    reset = _nonnegative_number(headers.get("x-ratelimit-reset"))
    if reset is not None:
        # Accept Unix reset timestamps expressed in seconds or milliseconds.
        reset = reset / 1000 if reset > 100_000_000_000 else reset
        delay = max(delay or 0, reset - now, 0)
    return math.ceil(delay) if delay is not None else None


def _rate_limit_details(error) -> dict:
    headers = {key.lower(): value for key, value in (error.headers or {}).items()}
    details = {"scope": "unknown", "quota": "unknown"}
    # Never retain arbitrary messages, provider metadata, request IDs or URLs:
    # providers can echo credentials and prompt content in an error body.
    try:
        raw = error.read(16_385)
        body = json.loads(raw) if len(raw) <= 16_384 else {}
    except (OSError, ValueError, TypeError):
        body = {}
    body = body.get("error", {}) if isinstance(body, dict) else {}
    body = body if isinstance(body, dict) else {}
    metadata = body.get("metadata", {})
    if isinstance(metadata, dict) and (metadata.get("provider_code") or metadata.get("provider_name")):
        details["scope"] = "provider"
    if any(key.startswith("x-ratelimit-") for key in headers):
        details["scope"] = "platform"
    message = body.get("message", "")
    if isinstance(message, str) and re.search(r"\bfree[- ]models?[- ]per[- ]day\b", message, re.IGNORECASE):
        details.update(scope="platform", quota="free_daily")
    delay = _retry_hint(headers)
    if delay is not None:
        details["retry_after_seconds"] = delay
    for name in ("limit", "remaining"):
        value = _nonnegative_number(headers.get(f"x-ratelimit-{name}"))
        if value is not None:
            details[name] = value
    return details


def tracked_files(workspace: Path) -> set[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=workspace, check=True,
                            capture_output=True, timeout=15)
    paths = set(result.stdout.decode("utf-8").split("\0")) - {""}
    if len(paths) > MAX_FILES:
        raise ReviewFailure("repository_file_limit")
    return paths


def _workspace_path(workspace: Path, raw: object, allowed: set[str]) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty string")
    path = workspace / raw
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError("path is outside the review workspace")
    relative = path.relative_to(workspace).as_posix()
    # Reject symlinks in any component, even when they point to another tracked file.
    if relative not in allowed or path.absolute() != resolved or ".git" in Path(relative).parts:
        raise ValueError("only tracked regular files may be read")
    if not path.is_file():
        raise ValueError("path is not a regular file")
    return path


def _bounded_line(source) -> str | None:
    """Consume one logical line with bounded memory; None marks an omitted line."""
    first = chunk = source.readline(MAX_TOOL_BYTES + 1)
    oversized = len(first.encode()) > MAX_TOOL_BYTES
    while chunk and not chunk.endswith("\n"):
        chunk = source.readline(MAX_TOOL_BYTES + 1)
        oversized = oversized or bool(chunk)
    return None if oversized else first


def _read_lines(path: Path, offset: int, limit: int) -> str:
    lines, size = [], 0
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for number in range(1, offset + limit):
            line = _bounded_line(source)
            if line == "":
                break
            if number < offset:
                continue
            rendered = (f"[line {number} exceeds the read limit; line omitted]\n"
                        if line is None else f"{number}: {line}")
            size += len(rendered.encode())
            if size > MAX_TOOL_BYTES:
                lines.append(f"\n[output limit; continue at line {number}]")
                break
            lines.append(rendered)
    return "".join(lines) or "No lines in requested range."


def _matching_files(workspace: Path, allowed: set[str], glob: str):
    for relative in sorted(allowed):
        if fnmatch.fnmatchcase(relative, glob):
            try:
                yield relative, _workspace_path(workspace, relative, allowed)
            except (ValueError, OSError):
                continue


def _grep_file(path: Path, relative: str, pattern: str) -> list[str]:
    with path.open("rb") as source:
        text = source.read(MAX_TOOL_BYTES).decode("utf-8", errors="replace")
    return [f"{relative}:{number}:{line[:300]}" for number, line in enumerate(text.splitlines(), 1)
            if pattern in line][:100]


def _search(name: str, arguments: dict, workspace: Path, allowed: set[str]) -> str:
    pattern, glob = arguments.get("pattern"), arguments.get("glob", "*")
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 500 or not isinstance(glob, str):
        raise ValueError("invalid search pattern")
    matches = []
    for relative, path in _matching_files(workspace, allowed, pattern if name == "Glob" else glob):
        matches.extend([relative] if name == "Glob" else _grep_file(path, relative, pattern))
        if len(matches) >= 100:
            break
    return ("\n".join(matches[:100])[:MAX_TOOL_BYTES] or "No matches.") + "\n[bounded search: at most 100 results, first 48000 bytes per file]"


def execute_tool(name: object, arguments: object, workspace: Path, allowed_files: set[str] | None = None) -> str:
    allowed = allowed_files if allowed_files is not None else set()
    try:
        if not isinstance(arguments, dict):
            raise ValueError("invalid tool arguments")
        if name in ("Glob", "Grep"):
            return _search(name, arguments, workspace, allowed)
        if name != "Read":
            raise ValueError("tool is not allowed")
        path = _workspace_path(workspace, arguments.get("path"), allowed)
        offset, limit = arguments.get("offset", 1), arguments.get("limit", 200)
        if type(offset) is not int or type(limit) is not int or not 1 <= offset <= 100_000 or not 1 <= limit <= 1000:
            raise ValueError("invalid line range")
        return _read_lines(path, offset, limit)
    except (ValueError, OSError, RuntimeError):
        return "Tool error: invalid arguments, path outside the review workspace, or unavailable tracked regular file."


def event_label(response: dict[str, Any]) -> str:
    for block in response.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            return f"tool_call={name if name in {'Read', 'Glob', 'Grep'} else 'denied'}"
    return "provider_response"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReviewFailure("provider_redirect_rejected")


def _stream_events(response):
    size = 0
    for raw in iter(lambda: response.readline(MAX_RESPONSE_BYTES + 1), b""):
        size += len(raw)
        if size > MAX_RESPONSE_BYTES:
            raise ReviewFailure("provider_response_limit")
        if not raw.startswith(b"data:"):
            continue
        yield json.loads(raw[5:].strip())


def _apply_delta(event, blocks, tool_json):
    index, delta = event["index"], event["delta"]
    if delta.get("type") == "input_json_delta":
        tool_json[index] = tool_json.get(index, "") + delta["partial_json"]
    else:
        for key in ("text", "thinking", "signature"):
            if key in delta:
                blocks[index][key] = blocks[index].get(key, "") + delta[key]
    return "provider_reasoning_delta" if delta.get("type") == "thinking_delta" else "provider_content_delta"


def _consume_event(event, result, blocks, tool_json, emit):
    kind = event.get("type")
    if kind == "error":
        raise ReviewFailure("provider_stream_error")
    if kind == "message_start":
        result.update(event["message"])
        emit("provider_message_start")
    elif kind == "content_block_start":
        blocks[event["index"]] = event["content_block"]
        emit("provider_content_start")
    elif kind == "content_block_delta":
        emit(_apply_delta(event, blocks, tool_json))
    elif kind == "message_delta":
        result.update(event.get("delta", {}))
        emit("provider_message_delta")
    # SSE pings indicate a connection, not model progress; do not reset idle timer.


def _stream_response(response, emit) -> dict:
    result: dict = {"content": []}
    blocks: dict[int, dict] = {}
    tool_json: dict[int, str] = {}
    for event in _stream_events(response):
        if event.get("type") == "message_stop":
            for index, encoded in tool_json.items():
                blocks[index]["input"] = json.loads(encoded)
            result["content"] = [blocks[i] for i in sorted(blocks)]
            return result
        _consume_event(event, result, blocks, tool_json, emit)
    raise ReviewFailure("provider_stream_incomplete")


def request_message(endpoint: str, api_key: str, payload: dict, timeout: float, emit=lambda _: None) -> dict:
    request = urllib.request.Request(endpoint, data=json.dumps({**payload, "stream": True}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                result = _stream_response(response, emit)
            else:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ReviewFailure("provider_response_limit")
                result = json.loads(raw)
                emit("provider_response")
    except urllib.error.HTTPError as error:
        with error:
            failure = RateLimitFailure(_rate_limit_details(error)) if error.code == 429 else ReviewFailure(f"http_{error.code}")
        raise failure from None
    except OSError:
        raise ReviewFailure("provider_connection_error") from None
    except (ValueError, KeyError, TypeError):
        raise ReviewFailure("provider_invalid_response") from None
    if not isinstance(result, dict):
        raise ReviewFailure("provider_invalid_response")
    return result


def _final_result(response, content):
    if response.get("stop_reason") != "end_turn":
        raise ReviewFailure("provider_incomplete_result")
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    try:
        result = _json_response_text(text)
        if json.loads(result)["thread_verdicts"]:
            raise RuntimeError("thread verdicts are not supported by this reviewer")
    except RuntimeError:
        raise ReviewFailure("invalid_result") from None
    return result


def _tool_results(blocks, workspace, allowed, emit, read_files):
    results = []
    for block in blocks:
        if not isinstance(block.get("id"), str):
            raise ReviewFailure("invalid_tool_response")
        emit("tool_started")
        content = execute_tool(block.get("name"), block.get("input"), workspace, allowed)
        # Errors, empty pages and omitted oversized lines are not evidence of reading.
        if block.get("name") == "Read" and re.search(r"^[1-9]\d*: ", content, re.MULTILINE):
            path = _workspace_path(workspace, block["input"]["path"], allowed)
            read_files.add(path.relative_to(workspace).as_posix())
        results.append({"type": "tool_result", "tool_use_id": block["id"], "content": content})
        emit("tool_completed")
    return results


def _final_response_or_none(pipe, response, content, blocks, read_files, turn):
    """Return the validated final result when the model finished, else None."""
    if blocks:
        return None
    result = _final_result(response, content)
    if REVIEW_DIFF_PATH not in read_files:
        raise ReviewFailure("diff_not_read")
    pipe.send(("result", result, turn))
    return result


def _worker(pipe, endpoint, api_key, model, prompt, workspace, allowed, max_turns, timeout, max_tokens=8192):
    # Only this process owns network IO and tools. Parent can terminate it even
    # during DNS/TLS, a slow file read, a streaming response, or final teardown.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    messages = [{"role": "user", "content": prompt}]
    read_files: set[str] = set()
    turn = 0

    def emit(label):
        pipe.send(("event", label, turn))

    try:
        for turn in range(1, max_turns + 1):
            emit("request_dispatched")
            response = request_message(endpoint, api_key, {
                "model": model, "max_tokens": max_tokens, "temperature": 0,
                # This is the Anthropic Messages API (/v1/messages). A reasoning
                # model such as nex-agi/nex-n2.5-pro:free burns its output budget
                # and wall-clock on extended-thinking tokens every turn, so the
                # loop never reaches a stop_reason=end_turn final JSON and the
                # attempt dies with provider_incomplete_result or attempt_timeout.
                # Disable extended thinking for a deterministic, non-reasoning
                # completion the runner can validate. If a model mandates
                # thinking it will reject this with HTTP 400 (visible in the
                # attempt table), which is the signal to swap in a non-reasoning
                # model instead.
                "thinking": {"type": "disabled"},
                "tools": TOOLS, "messages": messages,
            }, timeout, emit)
            emit(event_label(response))
            content = response.get("content")
            if not isinstance(content, list):
                raise ReviewFailure("provider_invalid_response")
            blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            if _final_response_or_none(pipe, response, content, blocks, read_files, turn) is not None:
                return
            if len(blocks) > 16 or response.get("stop_reason") != "tool_use":
                raise ReviewFailure("invalid_tool_response")
            messages.append({"role": "assistant", "content": content})
            results = _tool_results(blocks, workspace, allowed, emit, read_files)
            messages.append({"role": "user", "content": results})
            if turn >= max_turns - 2:
                messages.append({"role": "user", "content": "Budget nearly exhausted. Return final JSON now. State any unreviewed scope in summary; do not claim complete coverage."})
        raise ReviewFailure("turn_limit")
    except RateLimitFailure as error:
        pipe.send(("rate_limit", error.details, turn))
    except ReviewFailure as error:
        pipe.send(("failure", str(error), turn))
    except Exception:
        pipe.send(("failure", "worker_error", turn))
    finally:
        pipe.close()


def _validate_limits(max_turns, attempt_timeout_seconds, inactivity_timeout_seconds, heartbeat_seconds, max_tokens) -> None:
    if min(max_turns, attempt_timeout_seconds, inactivity_timeout_seconds, heartbeat_seconds, max_tokens) <= 0:
        raise ReviewFailure("invalid_limits")


def _resolve_allowed(workspace: Path, allowed_files: set[str] | None) -> set[str]:
    return tracked_files(workspace) if allowed_files is None else allowed_files


def _terminate_worker(worker) -> None:
    if worker.pid is None:
        return
    if worker.is_alive():
        worker.terminate()
    worker.join(0.5)
    if worker.is_alive():
        worker.kill()
        worker.join(0.5)


def _check_deadlines(now, started, last_activity, attempt_timeout_seconds, inactivity_timeout_seconds) -> None:
    if now - started >= attempt_timeout_seconds:
        raise ReviewTimeout("attempt_timeout: no validated result before attempt deadline")
    if now - last_activity >= inactivity_timeout_seconds:
        raise ReviewTimeout("inactivity_timeout: no substantive provider/tool event before idle deadline")


def _maybe_heartbeat(now, heartbeat, heartbeat_seconds, started, last_activity, turns, events, state, worker, log) -> float:
    if now < heartbeat:
        return heartbeat
    print(f"[review] heartbeat elapsed={now-started:.1f}s last_activity={now-last_activity:.1f}s "
          f"turn={turns} events={events} state={state} process_alive={worker.is_alive()} "
          "provider_processing=unknown", file=log, flush=True)
    return now + heartbeat_seconds


def _handle_message(kind, value, turns, output, started, events, log):
    if kind == "rate_limit":
        raise RateLimitFailure(value)
    if kind == "failure":
        raise ReviewFailure(value)
    if kind == "result":
        output.write_text(json.dumps([{"type": "result", "subtype": "success",
            "is_error": False, "result": value}]), encoding="utf-8")
        output.chmod(0o600)
        print(f"[review] valid_json_received turns={turns} elapsed={time.monotonic()-started:.1f}s", file=log, flush=True)
        return {"turns": turns, "events": events, "seconds": round(time.monotonic()-started, 2)}
    return None


def _cancel_signal(*_):
    raise KeyboardInterrupt


def _recv_message(receive):
    try:
        return receive.recv()
    except EOFError:
        raise ReviewFailure("worker_exited_without_result") from None


def run_review(*, endpoint: str, api_key: str, model: str, prompt: str, workspace: Path,
               output: Path, max_turns: int, attempt_timeout_seconds: float,
               inactivity_timeout_seconds: float, heartbeat_seconds: float, log: TextIO,
               allowed_files: set[str] | None = None, max_tokens: int = 8192) -> dict:
    _validate_limits(max_turns, attempt_timeout_seconds, inactivity_timeout_seconds, heartbeat_seconds, max_tokens)
    workspace = workspace.resolve()
    allowed = _resolve_allowed(workspace, allowed_files)
    ctx = multiprocessing.get_context("fork")
    receive, send = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_worker, args=(send, endpoint, api_key, model, prompt, workspace,
        allowed, max_turns, inactivity_timeout_seconds, max_tokens))
    started = last_activity = time.monotonic()
    heartbeat = started + heartbeat_seconds
    state, turns, events = "starting", 0, 0
    old_handler = signal.getsignal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, _cancel_signal)
    try:
        worker.start()
        send.close()
        print(f"[review] started model={safe_label(model)} attempt_limit={attempt_timeout_seconds}s idle_limit={inactivity_timeout_seconds}s", file=log, flush=True)
        while True:
            now = time.monotonic()
            _check_deadlines(now, started, last_activity, attempt_timeout_seconds, inactivity_timeout_seconds)
            heartbeat = _maybe_heartbeat(now, heartbeat, heartbeat_seconds, started, last_activity,
                                        turns, events, state, worker, log)
            wait = max(0, min(0.2, heartbeat-now, attempt_timeout_seconds-(now-started),
                              inactivity_timeout_seconds-(now-last_activity)))
            if not receive.poll(wait):
                continue
            kind, value, turns = _recv_message(receive)
            result = _handle_message(kind, value, turns, output, started, events, log)
            if result is not None:
                return result
            events += 1
            state, last_activity = value, time.monotonic()
            if not value.endswith("_delta"):
                print(f"[review] {value} turn={turns}", file=log, flush=True)
    finally:
        send.close()
        receive.close()
        _terminate_worker(worker)
        worker.close()
        signal.signal(signal.SIGTERM, old_handler)
