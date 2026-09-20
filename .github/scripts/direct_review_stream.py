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
    try:
        signal.setitimer(signal.ITIMER_REAL, interval, interval)
        yield progress
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _events(response):
    size, parts = 0, []
    for raw in iter(lambda: response.readline(MAX_RESPONSE_BYTES + 1), b""):
        size += len(raw)
        if size > MAX_RESPONSE_BYTES:
            raise StreamFailure("response_limit")
        if raw.startswith(b"data:"):
            parts.append(raw[5:].strip())
        elif not raw.strip() and parts:
            yield b"\n".join(parts)
            parts = []
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


def read_completion(response, progress: Progress) -> dict:
    result, content, finish = {}, [], None
    try:
        for raw in _events(response):
            if raw == b"[DONE]":
                if finish != "stop":
                    raise StreamFailure("output_limit" if finish == "length" else "stream_incomplete")
                result["choices"] = [{"message": {"content": "".join(content)}, "finish_reason": finish}]
                return result
            finish = _apply_event(json.loads(raw), result, content, progress) or finish
    except (ValueError, KeyError, TypeError, AttributeError):
        raise StreamFailure("invalid_stream") from None
    raise StreamFailure("stream_incomplete")


def read_response(response, progress: Progress) -> object:
    content_type = getattr(response, "headers", {}).get("Content-Type", "")
    progress.state = "waiting_for_content"
    if isinstance(content_type, str) and "text/event-stream" in content_type.lower():
        return read_completion(response, progress)
    # Keep compatibility with providers that ignore stream=true. Still bounded
    # by the same idle/absolute watchdog; no speculative "thinking" messages.
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StreamFailure("response_limit")
    return json.loads(raw)
