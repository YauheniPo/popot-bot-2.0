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


MODEL = "moonshotai/kimi-k3"
CHAT_COMPLETIONS_URL = "https://ollama.com/v1/chat/completions"
MESSAGES_URL = "https://ollama.com/v1/messages"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
NVIDIA_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NOUS_CHAT_COMPLETIONS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
PLAIN_JSON_PROVIDERS = frozenset({"ollama-cloud", "nvidia", "nous"})
# Nous publishes this request limit in https://portal.nousresearch.com/api/openapi.
NOUS_MAX_OUTPUT_TOKENS = 32_000
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 2
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


def completion_payload(body: dict[str, object], provider: str = "ollama-cloud") -> dict[str, object]:
    """Request ordinary JSON; schemas remain in the existing prompts.

    Keep the review engine's local shape/anchor validation and retries, but
    omit gateway extensions and schema enforcement for plain-JSON providers.
    """
    payload = {
        key: value for key, value in body.items()
        if key not in {"provider", "plugins", "reasoning", "response_format"}
    }
    max_tokens = payload.get("max_tokens")
    if provider == "nous" and isinstance(max_tokens, int):
        payload["max_tokens"] = min(max_tokens, NOUS_MAX_OUTPUT_TOKENS)
    return payload


def provider_config() -> tuple[str, str, str]:
    """Return provider, API key and chat endpoint from environment.

    DIRECT_REVIEW_* selects the provider and model; REVIEW_PROVIDER remains a
    compatibility alias for the provider. Credentials are selected by provider
    without changing workflow code.
    """
    provider = os.environ.get("DIRECT_REVIEW_PROVIDER", os.environ.get("REVIEW_PROVIDER", "nvidia")).strip().lower()
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


def _probe_request(api_key: str, kind: str, provider: str, model: str) -> urllib.request.Request:
    body: dict[str, object] = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": 'Return only {"status":"ok"}.'}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = {
        "ollama-cloud": CHAT_COMPLETIONS_URL,
        "openrouter": OPENROUTER_CHAT_COMPLETIONS_URL,
        "nvidia": NVIDIA_CHAT_COMPLETIONS_URL,
        "nous": NOUS_CHAT_COMPLETIONS_URL,
    }[provider]
    if kind == "tools":
        # Probe the same Anthropic route used by the Claude action, including
        # an operator-provided base URL. A chat response cannot prove tool support.
        base_url = os.environ.get("CLAUDE_REVIEW_BASE_URL", "").strip().rstrip("/")
        url = f"{base_url}/v1/messages" if base_url else url.removesuffix("chat/completions") + "messages"
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
    try:
        content = result["choices"][0]["message"]["content"]
        if content.strip().startswith("```"):
            content = "\n".join(content.strip().splitlines()[1:-1])
        return json.loads(content) == {"status": "ok"}
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return False


def probe(api_key: str, kind: str, provider: str = "ollama-cloud", model: str = MODEL) -> None:
    request = _probe_request(api_key, kind, provider, model)
    result: object = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"{provider} {kind} probe failed with HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"{provider} {kind} probe failed or timed out") from None
        except ValueError:
            raise RuntimeError(f"{provider} {kind} probe returned invalid JSON") from None
        time.sleep(15 * (attempt + 1))

    if not _probe_response_valid(result, kind):
        raise RuntimeError(f"{provider} {kind} probe did not satisfy the expected response contract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=["json", "tools"], required=True)
    args = parser.parse_args(argv)
    provider, api_key, _ = provider_config()
    if not api_key:
        key_name = {
            "ollama-cloud": "OLLAMA_API_KEY", "openrouter": "OPENROUTER_API_KEY",
            "nvidia": "NVIDIA_API_KEY", "nous": "NOUS_API_KEY",
        }[provider]
        raise RuntimeError(f"{key_name} must be configured as a GitHub Actions secret for {provider}")
    model = configured_model()
    if provider == "ollama-cloud" and model == MODEL:
        probe(api_key, args.probe)
    else:
        probe(api_key, args.probe, provider, model)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        # Keep the workflow output contract and expose configured model IDs.
        # The probe above validates only the primary model, not the fallback.
        values = {
            "provider": provider,
            "primary_model": model, "primary_ready": "true",
            "fallback_model": configured_fallback_model(), "fallback_ready": "true",
            "selected_model": model, "selected_mode": "ordinary",
            "secondary_model": configured_fallback_model(),
        }
        with open(output_path, "a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    print(f"{provider} {model}: live {args.probe} probe passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"AI review preflight failed: {error}", file=sys.stderr)
        sys.exit(1)
