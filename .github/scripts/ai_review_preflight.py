#!/usr/bin/env python3
"""Provider-aware review transport and live JSON/tool preflight."""

from __future__ import annotations

import argparse
from http.client import IncompleteRead
import json
import os
import sys
import time
import urllib.error
import urllib.request

from claude_review_runner import _rate_limit_details
from direct_review_stream import StreamFailure, read_response, watchdog
from review_execution import safe_label

MODEL = "moonshotai/kimi-k3"
CHAT_COMPLETIONS_URL = "https://ollama.com/v1/chat/completions"
MESSAGES_URL = "https://ollama.com/v1/messages"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
NVIDIA_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NOUS_CHAT_COMPLETIONS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
# Anthropic routes are independent of Chat Completions support. NVIDIA's
# hosted catalog requires an operator-provided Anthropic-compatible gateway.
MESSAGES_BASE_URLS = {
    "ollama-cloud": "https://ollama.com",
    "openrouter": "https://openrouter.ai/api",
    "nous": "https://inference-api.nousresearch.com",
}
PLAIN_JSON_PROVIDERS = frozenset({"ollama-cloud", "nvidia", "nous"})
# Nous publishes this request limit in https://portal.nousresearch.com/api/openapi.
NOUS_MAX_OUTPUT_TOKENS = 32_000
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 4
# Free OpenRouter routes can queue before producing a response. Keep the
# smoke check bounded, but allow the configured primary model one retry so a
# transient queue or gateway timeout does not immediately block the review.
SMOKE_TIMEOUT_SECONDS = 90
SMOKE_MAX_ATTEMPTS = 2
SMOKE_FALLBACK_MAX_ATTEMPTS = 2
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_WAIT_SECONDS = 120


class ProbeFailure(RuntimeError):
    """Safe probe outcome with allow-listed rate-limit metadata only."""

    def __init__(self, reason: str, details: dict | None = None):
        super().__init__(reason)
        self.details = details or {}


def completion_payload(body: dict[str, object], provider: str = "ollama-cloud") -> dict[str, object]:
    """Request ordinary JSON; schemas remain in the existing prompts.

    Keep the review engine's local shape/anchor validation and retries, but
    omit gateway extensions and schema enforcement for plain-JSON providers.
    """
    payload = {
        key: value for key, value in body.items()
        if key not in {"provider", "plugins", "reasoning", "response_format"}
    }
    # Ollama supports reasoning_effort, not OpenRouter's exclude extension.
    # Dropping effort entirely silently enables long default reasoning on a
    # real diff even when the tiny preflight responds immediately.
    reasoning = body.get("reasoning")
    if provider == "ollama-cloud" and isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort in ("none", "low", "medium", "high", "max"):
            payload["reasoning_effort"] = effort
    max_tokens = payload.get("max_tokens")
    if provider == "nous" and isinstance(max_tokens, int):
        payload["max_tokens"] = min(max_tokens, NOUS_MAX_OUTPUT_TOKENS)
    return payload


def provider_config(provider_name: str | None = None) -> tuple[str, str, str]:
    """Return provider, API key and chat endpoint for a configured route.

    DIRECT_REVIEW_* selects the provider and model; REVIEW_PROVIDER remains a
    compatibility alias for the provider. Credentials are selected by provider
    without changing workflow code.
    """
    provider = (provider_name if provider_name is not None else os.environ.get(
        "DIRECT_REVIEW_PROVIDER", os.environ.get("REVIEW_PROVIDER", "nvidia"),
    )).strip().lower()
    if provider in {"ollama", "ollama_cloud", "ollama-cloud"}:
        return "ollama-cloud", os.environ.get("OLLAMA_API_KEY", "").strip(), CHAT_COMPLETIONS_URL
    if provider in {"openrouter", "open-router"}:
        return "openrouter", os.environ.get("OPENROUTER_API_KEY", "").strip(), OPENROUTER_CHAT_COMPLETIONS_URL
    if provider in {"nvidia", "nvidia-ai", "nvidia-nim"}:
        return "nvidia", os.environ.get("NVIDIA_API_KEY", "").strip(), NVIDIA_CHAT_COMPLETIONS_URL
    if provider in {"nous", "nous-portal", "nous-api"}:
        return "nous", os.environ.get("NOUS_API_KEY", "").strip(), NOUS_CHAT_COMPLETIONS_URL
    raise RuntimeError("REVIEW_PROVIDER must be ollama-cloud, openrouter, nvidia, or nous")


def configured_model() -> str:
    return os.environ.get("DIRECT_REVIEW_MODEL", MODEL).strip() or MODEL


def configured_fallback_model() -> str:
    return os.environ.get("DIRECT_REVIEW_FALLBACK_MODEL", "").strip()


def configured_fallback_provider(primary_provider: str) -> str:
    """Use the primary route unless an independent fallback was configured."""
    return os.environ.get("DIRECT_REVIEW_FALLBACK_PROVIDER", "").strip() or primary_provider


def anthropic_base_url(provider: str) -> str:
    """Normalize operator input for both the probe and Claude's SDK routes."""
    primary = os.environ.get("CLAUDE_REVIEW_PROVIDER") or os.environ.get("DIRECT_REVIEW_PROVIDER") or provider
    primary = provider_config(primary)[0]
    # A primary gateway override must never receive a different provider's key.
    base_url = os.environ.get("CLAUDE_REVIEW_BASE_URL", "").strip().rstrip("/") if provider == primary else ""
    base_url = base_url or MESSAGES_BASE_URLS.get(provider, "")
    if not base_url:
        raise RuntimeError(
            f"{provider} has no configured Anthropic Messages route; set "
            "CLAUDE_REVIEW_BASE_URL to an Anthropic-compatible gateway for Claude Code, "
            "or use the direct API reviewer"
        )
    for suffix in ("/v1/messages", "/v1"):
        if base_url.endswith(suffix):
            return base_url.removesuffix(suffix)
    return base_url


def messages_url(provider: str) -> str:
    return f"{anthropic_base_url(provider)}/v1/messages"


def _probe_request(api_key: str, kind: str, provider: str, model: str) -> urllib.request.Request:
    body: dict[str, object] = {
        "model": model,
        # The preflight response is deliberately tiny; a large completion
        # budget would make this early availability check slower and costlier.
        "max_tokens": 512,
        "messages": [{"role": "user", "content": 'Return only {"status":"ok"}.'}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = {
        "ollama-cloud": CHAT_COMPLETIONS_URL,
        "openrouter": OPENROUTER_CHAT_COMPLETIONS_URL,
        "nvidia": NVIDIA_CHAT_COMPLETIONS_URL,
        "nous": NOUS_CHAT_COMPLETIONS_URL,
    }[provider]
    if kind == "json":
        body["stream"] = True
    if kind in {"tools", "claude"}:
        # Probe the same Anthropic route used by the Claude action, including
        # an operator-provided base URL. A chat response cannot prove tool support.
        url = messages_url(provider)
        headers["anthropic-version"] = "2023-06-01"
        body.update({
            "tools": [{
                "name": "review_model_preflight", "description": "Confirm tool calling.",
                "input_schema": {
                    "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"],
                },
            }],
            "tool_choice": {"type": "tool", "name": "review_model_preflight"},
            "messages": [{"role": "user", "content": "Call review_model_preflight with status ok."}],
        })
        if kind == "claude":
            body["tools"] = []
            body.pop("tool_choice", None)
            body["messages"] = [{
                "role": "user",
                "content": (
                    "Return exactly one JSON object with string summary, an array findings, "
                    "and an array thread_verdicts. Use empty arrays. No markdown."
                ),
            }]
    return urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")


def _probe_response_valid(result: object, kind: str) -> bool:
    if not isinstance(result, dict):
        return False
    if kind == "tools":
        blocks = result.get("content", [])
        return isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            and block.get("name") == "review_model_preflight"
            and block.get("input") == {"status": "ok"}
            for block in blocks
        )
    if kind == "claude":
        try:
            content = "".join(
                block.get("text", "") for block in result.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            parsed = json.loads(content)
            return (
                isinstance(parsed, dict)
                and isinstance(parsed.get("summary"), str)
                and isinstance(parsed.get("findings"), list)
                and isinstance(parsed.get("thread_verdicts"), list)
            )
        except (TypeError, AttributeError, ValueError):
            return False
    try:
        content = result["choices"][0]["message"]["content"]
        if content.strip().startswith("```"):
            content = "\n".join(content.strip().splitlines()[1:-1])
        return json.loads(content) == {"status": "ok"}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return False


def _http_retry_delay(error, provider: str, kind: str, attempt: int, attempts: int, remaining: float) -> float:
    with error:
        details = _rate_limit_details(error) if error.code == 429 else {}
    failure = ProbeFailure(f"{provider} {kind} probe failed with HTTP {error.code}", details)
    delay = max(15 * (attempt + 1), details.get("retry_after_seconds", 0))
    if details:
        print(f"[preflight] rate_limit {json.dumps(details, sort_keys=True)}", file=sys.stderr)
    if (error.code not in RETRYABLE_STATUSES or attempt == attempts - 1
            or details.get("quota") == "free_daily" or delay > remaining):
        raise failure from None
    return delay


def _stream_retry_delay(error: StreamFailure, provider: str, kind: str, attempt: int, attempts: int) -> float:
    """Only watchdog timeouts can retry; malformed or incomplete streams fail this route."""
    if str(error) not in {"inactivity_timeout", "attempt_timeout"} or attempt == attempts - 1:
        raise RuntimeError(f"{provider} {kind} probe failed: {error}") from None
    return 15 * (attempt + 1)


def _request_probe_response(request: urllib.request.Request, attempts: int, timeout: int,
                            provider: str, model: str, kind: str) -> object:
    result: object = None
    remaining = MAX_RETRY_WAIT_SECONDS
    for attempt in range(attempts):
        if remaining <= 0:
            raise RuntimeError(f"{provider} {kind} probe retry wait budget exhausted")
        delay = 15 * (attempt + 1)
        print(
            f"{provider} {model}: {kind} probe attempt {attempt + 1}/{attempts} "
            f"(timeout {timeout}s)", file=sys.stderr,
        )
        try:
            result = _read_probe_response(request, timeout, kind, provider)
            break
        except StreamFailure as error:
            delay = _stream_retry_delay(error, provider, kind, attempt, attempts)
        except urllib.error.HTTPError as error:
            delay = _http_retry_delay(error, provider, kind, attempt, attempts, remaining)
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            if attempt == attempts - 1:
                raise RuntimeError(f"{provider} {kind} probe failed or timed out") from None
        except ValueError:
            raise RuntimeError(f"{provider} {kind} probe returned invalid JSON") from None
        # Pace only the next request; the final retryable response raises above
        # without an unnecessary sleep because there is no next attempt.
        delay = min(delay, remaining)
        # Reserve the delay before sleeping so every retryable path, including
        # HTTP 429/5xx responses, consumes the same bounded wait budget.
        remaining -= delay
        print(f"[preflight] retry_wait={delay}s; no provider request in flight", file=sys.stderr)
        time.sleep(delay)
    return result


def _read_probe_response(request: urllib.request.Request, timeout: int, kind: str, provider: str) -> object:
    if kind != "json":
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    # Exercise the exact review reader, including idle/absolute bounds and diagnostics.
    with watchdog(total=timeout, idle=timeout, heartbeat=15, log=sys.stderr) as progress:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return read_response(response, progress, allow_stop_at_eof=provider == "nous")
        except urllib.error.HTTPError as error:
            progress.http_status = error.code
            raise


def probe(api_key: str, kind: str, provider: str = "ollama-cloud", model: str = MODEL,
          *, attempts_override: int | None = None, timeout_override: int | None = None) -> None:
    if attempts_override is not None and (
        not isinstance(attempts_override, int) or isinstance(attempts_override, bool) or attempts_override < 1
    ):
        raise ValueError("attempts_override must be positive integer")
    if timeout_override is not None and (
        not isinstance(timeout_override, int) or isinstance(timeout_override, bool) or timeout_override < 1
    ):
        raise ValueError("timeout_override must be positive integer")
    if attempts_override is not None:
        attempts = attempts_override
    elif kind == "claude":
        attempts = SMOKE_MAX_ATTEMPTS
    else:
        attempts = MAX_ATTEMPTS
    if timeout_override is not None:
        timeout = timeout_override
    elif kind == "claude":
        timeout = SMOKE_TIMEOUT_SECONDS
    else:
        timeout = REQUEST_TIMEOUT_SECONDS
    if kind == "claude":
        # Run both checks against the same Anthropic route. This catches the
        # common case where tool use works but Claude cannot emit our JSON contract.
        probe(api_key, "tools", provider, model,
              attempts_override=attempts, timeout_override=SMOKE_TIMEOUT_SECONDS)
    request = _probe_request(api_key, kind, provider, model)
    result = _request_probe_response(request, attempts, timeout, provider, model, kind)

    if not _probe_response_valid(result, kind):
        raise RuntimeError(f"{provider} {kind} probe did not satisfy the expected response contract")


def probe_ready(api_key: str, kind: str, provider: str, model: str, role: str,
                attempts_override: int | None = None, diagnostics: dict | None = None) -> bool:
    try:
        if attempts_override is None:
            probe(api_key, kind, provider, model)
        else:
            probe(api_key, kind, provider, model, attempts_override=attempts_override)
    except RuntimeError as error:
        if diagnostics is not None:
            diagnostics[role] = {"reason": safe_label(str(error)), "details": getattr(error, "details", {})}
        print(f"::warning::{provider} {model}: {role} probe unavailable. {error}", file=sys.stderr)
        return False
    print(f"{provider} {model}: live {kind} probe passed ({role})")
    return True


def probe_models(
    api_key: str, kind: str, provider: str, primary: str, fallback: str,
    *, fallback_provider: str | None = None, fallback_api_key: str | None = None,
    diagnostics: dict | None = None,
) -> tuple[bool, bool]:
    diagnostics = diagnostics if diagnostics is not None else {}
    primary_ready = probe_ready(api_key, kind, provider, primary, "primary", diagnostics=diagnostics)
    fallback_provider = fallback_provider or provider
    fallback_api_key = fallback_api_key if fallback_api_key is not None else api_key
    if not fallback:
        fallback_ready = False
    elif fallback == primary and fallback_provider == provider:
        # Reuse failures as well as successes; the same model gets no extra tries.
        fallback_ready = primary_ready
        diagnostics["fallback"] = diagnostics.get("primary", {})
    elif _blocked_fallback(provider, fallback_provider, fallback, diagnostics):
        fallback_ready = False
        diagnostics["fallback"] = {"reason": "shared_platform_limit", "details": diagnostics["primary"]["details"]}
    else:
        fallback_ready = probe_ready(
            fallback_api_key, kind, fallback_provider, fallback, "fallback",
            attempts_override=SMOKE_FALLBACK_MAX_ATTEMPTS if kind == "claude" else None,
            diagnostics=diagnostics,
        )
    return primary_ready, fallback_ready


def _blocked_fallback(provider: str, fallback_provider: str, fallback: str, diagnostics: dict) -> bool:
    details = diagnostics.get("primary", {}).get("details", {})
    if provider != fallback_provider or details.get("scope") != "platform":
        return False
    if details.get("quota") == "free_daily":
        return fallback.endswith(":free")
    return details.get("retry_after_seconds", 0) > MAX_RETRY_WAIT_SECONDS


def _export_outputs(
    output_path: str,
    provider: str,
    selected_provider: str,
    base_url: str,
    model: str,
    fallback: str,
    primary_ready: bool,
    fallback_ready: bool,
    selected_model: str,
    normalized_fallback_provider: str,
    secondary_base_url: str,
) -> None:
    # A configured fallback is selectable only after the matching API probe.
    values = {
        "provider": provider,
        "selected_provider": selected_provider,
        "anthropic_base_url": base_url,
        "primary_model": model, "primary_ready": str(primary_ready).lower(),
        "fallback_model": fallback, "fallback_ready": str(fallback_ready).lower(),
        "selected_model": selected_model if primary_ready or fallback_ready else "", "selected_mode": "ordinary",
        "secondary_model": fallback if primary_ready and fallback_ready else "",
        "secondary_provider": normalized_fallback_provider if primary_ready and fallback_ready else "",
        "fallback_provider": normalized_fallback_provider if fallback else "",
        "secondary_anthropic_base_url": secondary_base_url,
    }
    with open(output_path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _export_probe_diagnostics(output_path: str, diagnostics: dict, primary_ready: bool, fallback_ready: bool) -> None:
    readiness = {"primary": primary_ready, "fallback": fallback_ready}
    with open(output_path, "a", encoding="utf-8") as output:
        for role, ready in readiness.items():
            value = diagnostics.get(role, {})
            reason = value.get("reason", "ready" if ready else "not_configured")
            details = " ".join(f"{safe_label(key)}: {safe_label(str(item))}"
                               for key, item in value.get("details", {}).items())
            output.write(f"{role}_reason={safe_label(reason)} {details}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=["json", "tools", "claude"], required=True)
    args = parser.parse_args(argv)
    provider, api_key, _ = provider_config()
    if not api_key:
        key_name = {
            "ollama-cloud": "OLLAMA_API_KEY", "openrouter": "OPENROUTER_API_KEY",
            "nvidia": "NVIDIA_API_KEY", "nous": "NOUS_API_KEY",
        }[provider]
        raise RuntimeError(f"{key_name} must be configured as a GitHub Actions secret for {provider}")
    # Reject incompatible transport before probing and export the same normalized
    # base for every Claude stage, including token-counting and Messages requests.
    base_url = anthropic_base_url(provider) if args.probe in {"tools", "claude"} else ""
    model = configured_model()
    fallback = configured_fallback_model()
    fallback_provider = configured_fallback_provider(provider)
    normalized_fallback_provider, fallback_api_key, _ = provider_config(fallback_provider)
    diagnostics: dict = {}
    primary_ready, fallback_ready = probe_models(
        api_key, args.probe, provider, model, fallback,
        fallback_provider=normalized_fallback_provider,
        fallback_api_key=fallback_api_key,
        diagnostics=diagnostics,
    )
    selected_model = model if primary_ready else fallback
    selected_provider = provider if primary_ready else normalized_fallback_provider
    secondary_base_url = ""
    if args.probe in {"tools", "claude"} and fallback and fallback_ready:
        secondary_base_url = anthropic_base_url(normalized_fallback_provider)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        _export_outputs(
            output_path, provider, selected_provider, base_url, model, fallback,
            primary_ready, fallback_ready, selected_model,
            normalized_fallback_provider, secondary_base_url,
        )
        _export_probe_diagnostics(output_path, diagnostics, primary_ready, fallback_ready)
    if not primary_ready and not fallback_ready:
        raise RuntimeError("No configured review model passed preflight; see the probe failures above")
    print(f"{selected_provider}: selected {selected_model} for review")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"AI review preflight failed: {error}", file=sys.stderr)
        sys.exit(1)
