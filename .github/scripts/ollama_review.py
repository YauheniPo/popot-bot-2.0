#!/usr/bin/env python3
"""Pinned Ollama Cloud review transport and live JSON/tool preflight."""

from __future__ import annotations

import argparse
from http.client import IncompleteRead
import json
import os
import sys
import time
import urllib.error
import urllib.request


MODEL = "kimi-k3"
CHAT_COMPLETIONS_URL = "https://ollama.com/v1/chat/completions"
MESSAGES_URL = "https://ollama.com/v1/messages"
REQUEST_TIMEOUT_SECONDS = 90
MAX_ATTEMPTS = 3
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


def completion_payload(body: dict[str, object]) -> dict[str, object]:
    """Cloud returns ordinary JSON; schemas remain in the existing prompts.

    Keep the review engine's local shape/anchor validation and retries, but
    never send OpenRouter routing/plugins or unsupported Cloud JSON schemas.
    """
    return {
        key: value for key, value in body.items()
        if key not in {"provider", "plugins", "reasoning", "response_format"}
    }


def probe(api_key: str, kind: str) -> None:
    body: dict[str, object] = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": 'Return only {"status":"ok"}.'}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = CHAT_COMPLETIONS_URL
    if kind == "tools":
        url = MESSAGES_URL
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
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Ollama {kind} probe failed with HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            if attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Ollama {kind} probe failed or timed out") from None
        except ValueError:
            raise RuntimeError(f"Ollama {kind} probe returned invalid JSON") from None
        time.sleep(15 * (attempt + 1))

    valid = False
    if isinstance(result, dict):
        if kind == "tools":
            blocks = result.get("content", [])
            valid = isinstance(blocks, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                and block.get("name") == "review_model_preflight"
                and block.get("input") == {"status": "ok"}
                for block in blocks
            )
        else:
            try:
                content = result["choices"][0]["message"]["content"]
                # Match the review engine's support for fenced ordinary JSON.
                if content.strip().startswith("```"):
                    content = "\n".join(content.strip().splitlines()[1:-1])
                valid = json.loads(content) == {"status": "ok"}
            except (KeyError, IndexError, TypeError, AttributeError, ValueError):
                pass
    if not valid:
        raise RuntimeError(f"Ollama {kind} probe did not satisfy the expected response contract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", choices=["json", "tools"], required=True)
    args = parser.parse_args(argv)
    api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY must be configured as a GitHub Actions secret")
    probe(api_key, args.probe)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        # Keep the existing workflow output contract. Claude's last attempt is
        # a retry of Kimi, never a fallback to another provider or model.
        values = {
            "primary_model": MODEL, "primary_ready": "true",
            "fallback_model": MODEL, "fallback_ready": "true",
            "selected_model": MODEL, "selected_mode": "ordinary",
            "secondary_model": "", "secondary_mode": "ordinary",
        }
        with open(output_path, "a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    print(f"Ollama Cloud {MODEL}: live {args.probe} probe passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Ollama review preflight failed: {error}", file=sys.stderr)
        sys.exit(1)
