"""Bounded Chat Completions SSE reader and POSIX main-thread IO watchdog.

No provider prose, reasoning, credentials or prompts are written to logs.
The alarm bounds even blocked DNS/TLS/reads; the caller owns and closes HTTP IO.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import signal
import time
from typing import TextIO

# Count wire bytes including JSON framing, not just completion text. A 32K-token
# budget can produce tens of thousands of small SSE envelopes (>2 MiB).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class StreamFailure(RuntimeError):
    """Allow-listed machine-readable reason, never raw provider content."""


@dataclass
class Progress:
    log: TextIO
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    state: str = "waiting_for_headers"
    events: int = 0
    content_chars: int = 0
    reasoning_chars: int = 0
    http_status: int | None = None
    response_format: str = "unknown"
    wire_bytes: int = 0
    sse_events: int = 0
    finish_reason: str = "none"
    done_seen: bool = False
    eof_seen: bool = False

    def request_end(self, outcome: str) -> None:
        """One bounded record even when a request fails before its first heartbeat."""
        now = time.monotonic()
        diagnostic = {
            "outcome": outcome, "elapsed_seconds": round(now - self.started, 3),
            "idle_seconds": round(now - self.last_activity, 3), "state": self.state,
            "http_status": self.http_status, "response_format": self.response_format,
            "wire_bytes": self.wire_bytes, "sse_events": self.sse_events,
            "content_chars": self.content_chars, "reasoning_chars": self.reasoning_chars,
            "finish_reason": self.finish_reason, "done_seen": self.done_seen,
            "eof_seen": self.eof_seen,
        }
        print("[direct-review] request_end " + json.dumps(diagnostic), file=self.log, flush=True)

    def record(self, *, content: str = "", reasoning: str = "") -> None:
        if not content and not reasoning:
            return
        self.content_chars += len(content)
        self.reasoning_chars += len(reasoning)
        self.events += 1
        self.last_activity = time.monotonic()
        self.state = "receiving_content" if content else "receiving_reasoning"

    def heartbeat(self) -> None:
        now = time.monotonic()
        print(f"[direct-review] heartbeat elapsed={now-self.started:.1f}s "
              f"idle={now-self.last_activity:.1f}s state={self.state} events={self.events} "
              f"content_chars={self.content_chars} reasoning_chars={self.reasoning_chars} "
              "provider_processing=unknown", file=self.log, flush=True)


@contextmanager
def watchdog(*, total: float, idle: float, heartbeat: float, log: TextIO):
    """CLI runs on Linux/macOS's main thread; no detached IO survives a timeout."""
    if signal.getitimer(signal.ITIMER_REAL) != (0, 0):
        raise StreamFailure("watchdog_already_active")
    progress = Progress(log)
    next_heartbeat = progress.started + heartbeat
    previous = signal.getsignal(signal.SIGALRM)

    def check(*_):
        nonlocal next_heartbeat
        now = time.monotonic()
        if now - progress.started >= total:
            raise StreamFailure("attempt_timeout")
        if now - progress.last_activity >= idle:
            raise StreamFailure("inactivity_timeout")
        if now >= next_heartbeat:
            progress.heartbeat()
            next_heartbeat = now + heartbeat

    signal.signal(signal.SIGALRM, check)
    interval = min(1.0, total, idle, heartbeat)
    outcome = "transport_error"
    try:
        signal.setitimer(signal.ITIMER_REAL, interval, interval)
        yield progress
        outcome = "received"
    except StreamFailure as error:
        outcome = str(error)
        raise
    except ValueError:
        outcome = "invalid_json"
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if progress.http_status is not None and progress.http_status >= 400:
            outcome = f"http_{progress.http_status}"
        progress.request_end(outcome)


def _events(response, progress):
    parts = []
    for raw in iter(lambda: response.readline(MAX_RESPONSE_BYTES + 1), b""):
        progress.wire_bytes += len(raw)
        if progress.wire_bytes > MAX_RESPONSE_BYTES:
            raise StreamFailure("response_limit")
        if raw.startswith(b"data:"):
            parts.append(raw[5:].strip())
        elif not raw.strip() and parts:
            yield b"\n".join(parts)
            parts = []
    progress.eof_seen = True
    if parts:
        yield b"\n".join(parts)


def _apply_choice(choice, content, progress):
    if choice.get("index", 0) != 0:
        return None
    delta = choice.get("delta", {})
    text = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if not isinstance(text, str) or not isinstance(reasoning, str):
        raise StreamFailure("invalid_stream")
    # Some OpenRouter models expose reasoning_details instead of a string.
    details = delta.get("reasoning_details")
    if isinstance(details, list) and details and not reasoning:
        reasoning = json.dumps(details)
    progress.record(content=text, reasoning=reasoning)
    content.append(text)
    return choice.get("finish_reason")


def _apply_event(event, result, content, progress):
    if not isinstance(event, dict):
        raise StreamFailure("invalid_stream")
    if "error" in event:
        raise StreamFailure("provider_stream_error")
    for key in ("id", "model", "usage"):
        if key in event:
            result[key] = event[key]
    finish = None
    for choice in event.get("choices", []):
        finish = _apply_choice(choice, content, progress) or finish
    return finish


def _finish_reason_label(reason: object) -> str:
    """Only known enum values may enter logs; provider strings can echo secrets."""
    if reason is None:
        return "none"
    if reason in ("stop", "length", "tool_calls", "function_call", "content_filter"):
        return reason
    return "other"


def read_completion(response, progress: Progress, *, allow_stop_at_eof: bool = False) -> dict:
    result, content, finish = {}, [], None
    try:
        for raw in _events(response, progress):
            progress.sse_events += 1
            if raw == b"[DONE]":
                progress.done_seen = True
                break
            finish = _apply_event(json.loads(raw), result, content, progress) or finish
            progress.finish_reason = _finish_reason_label(finish)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StreamFailure("invalid_stream") from None
    # Nous closes SSE after finish_reason=stop without a separate [DONE] event.
    # Only a clean EOF qualifies: read errors, timeouts and provider errors still
    # propagate. Callers must still validate the assembled JSON/review contract.
    if finish == "stop" and (progress.done_seen or (allow_stop_at_eof and progress.eof_seen)):
        result["choices"] = [{"message": {"content": "".join(content)}, "finish_reason": finish}]
        return result
    raise StreamFailure("output_limit" if finish == "length" else "stream_incomplete")


def read_response(response, progress: Progress, *, allow_stop_at_eof: bool = False) -> object:
    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    status = getattr(response, "status", None)
    progress.http_status = status if type(status) is int and 100 <= status <= 599 else None
    progress.state = "waiting_for_content"
    if isinstance(content_type, str) and "text/event-stream" in content_type.lower():
        progress.response_format = "sse"
        return read_completion(response, progress, allow_stop_at_eof=allow_stop_at_eof)
    # Keep compatibility with providers that ignore stream=true. Still bounded
    # by the same idle/absolute watchdog; no speculative "thinking" messages.
    progress.response_format = "json"
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    progress.wire_bytes = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StreamFailure("response_limit")
    return json.loads(raw)
