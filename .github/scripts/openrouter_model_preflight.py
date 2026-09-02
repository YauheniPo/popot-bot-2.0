#!/usr/bin/env python3
"""Select OpenRouter review models with active, compatible endpoints."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_ANTHROPIC_MESSAGES_URL = "https://openrouter.ai/api/v1/messages"
MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$")
REQUEST_TIMEOUT_SECONDS = 15
SCHEMA_PROBE_MAX_TOKENS = 1024
MAX_METADATA_ATTEMPTS = 3
MAX_FREE_CANDIDATES = 12
MAX_FREE_SCHEMA_PROBES = 4
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MANDATORY_REASONING_ERROR = "reasoning is mandatory"
NO_STRUCTURED_CONTENT_REASON = "live JSON-schema probe returned no structured content"
REQUIRES_REASONING_REASON = "live JSON-schema probe requires reasoning"
ORDINARY_JSON_OMITTED_CAPABILITIES = frozenset(
    {"response_format", "structured_outputs"}
)
SCHEMA_PROBE = {
    "name": "review_model_preflight",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status"],
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
    },
}
TOOL_PROBE = {
    "name": "review_model_preflight",
    "description": "Confirm that the review model can call tools.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["status"],
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
    },
}


@dataclass(frozen=True)
class ModelCheck:
    model: str
    ready: bool
    matching_endpoints: int
    reason: str


@dataclass(frozen=True)
class ModelSelection:
    primary: ModelCheck
    fallback: ModelCheck
    selected_model: str
    secondary_model: str
    selected_mode: str = "strict"
    secondary_mode: str = ""


def _safe_message(value: object) -> str:
    """Keep untrusted provider text on one non-command output line."""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:500]


def _model_url(model: str) -> str:
    if not MODEL_ID.fullmatch(model):
        raise RuntimeError(
            "model id must use the provider/model form and contain only safe slug characters"
        )
    return f"{OPENROUTER_MODELS_URL}/{model}/endpoints"


def _fetch_model(model: str) -> object:
    request = urllib.request.Request(
        _model_url(model),
        headers={
            "Accept": "application/json",
            "User-Agent": "popot-bot-pr-review-preflight/1",
        },
    )
    for attempt in range(1, MAX_METADATA_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            retryable = error.code in RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == MAX_METADATA_ATTEMPTS:
                raise RuntimeError(
                    f"OpenRouter endpoint metadata returned HTTP {error.code}"
                ) from error
        except urllib.error.URLError as error:
            if attempt == MAX_METADATA_ATTEMPTS:
                raise RuntimeError(
                    "OpenRouter endpoint metadata is temporarily unavailable"
                ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("OpenRouter endpoint metadata was not valid JSON") from error
        time.sleep(float(2 ** (attempt - 1)))
    raise AssertionError("unreachable")


def _fetch_free_models(required_capabilities: frozenset[str]) -> object:
    query = urllib.parse.urlencode(
        {
            "supported_parameters": ",".join(sorted(required_capabilities)),
            "sort": "most-popular",
        }
    )
    request = urllib.request.Request(
        f"{OPENROUTER_MODELS_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "popot-bot-pr-review-preflight/1",
        },
    )
    for attempt in range(1, MAX_METADATA_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            retryable = error.code in RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == MAX_METADATA_ATTEMPTS:
                raise RuntimeError(
                    f"OpenRouter model catalog returned HTTP {error.code}"
                ) from error
        except urllib.error.URLError as error:
            if attempt == MAX_METADATA_ATTEMPTS:
                raise RuntimeError(
                    "OpenRouter model catalog is temporarily unavailable"
                ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError("OpenRouter model catalog was not valid JSON") from error
        time.sleep(float(2 ** (attempt - 1)))
    raise AssertionError("unreachable")


def _request_probe(
    model: str,
    api_key: str,
    body: dict[str, object],
    label: str,
    url: str = OPENROUTER_CHAT_COMPLETIONS_URL,
) -> tuple[object | None, tuple[bool | None, str] | None]:
    _model_url(model)
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com",
            "User-Agent": f"popot-bot-pr-review-{label}-probe/1",
            "X-Title": "popot-bot-2.0 PR review model preflight",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in RETRYABLE_HTTP_STATUSES:
            return None, (
                None,
                f"live {label} probe was inconclusive after HTTP {error.code}",
            )
        if error.code == 400:
            provider_message = ""
            try:
                error_payload = json.loads(error.read(8192).decode("utf-8"))
                if isinstance(error_payload, dict):
                    error_details = error_payload.get("error")
                    if isinstance(error_details, dict):
                        raw_message = error_details.get("message")
                        if isinstance(raw_message, str):
                            provider_message = raw_message.lower()
            except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
                pass
            if MANDATORY_REASONING_ERROR in provider_message:
                return None, (False, REQUIRES_REASONING_REASON)
        return None, (False, f"live {label} probe returned HTTP {error.code}")
    except urllib.error.URLError:
        return None, (
            None,
            f"live {label} probe was inconclusive because OpenRouter was unreachable",
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, (False, f"live {label} probe returned invalid response JSON")

    return payload, None


def _completion_message(payload: object, label: str) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, f"live {label} probe returned an invalid response shape"
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, f"live {label} probe returned no completion"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None, f"live {label} probe returned no completion message"
    return message, None


def _validate_schema_probe_payload(payload: object) -> tuple[bool, str]:
    message, message_error = _completion_message(payload, "JSON-schema")
    if message_error is not None or message is None:
        return False, message_error or "live JSON-schema probe returned no message"
    content = message.get("content")
    if isinstance(content, dict):
        result = content
    else:
        if isinstance(content, list):
            content = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        if isinstance(content, str) and content.strip():
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                return False, "live JSON-schema probe did not return valid JSON"
        else:
            result = message.get("parsed")
            if not isinstance(result, dict):
                return False, NO_STRUCTURED_CONTENT_REASON
    if result != {"status": "ok"}:
        return False, "live JSON-schema probe did not satisfy the requested schema"
    return True, "passed the live JSON-schema probe"


def _probe_json_schema(model: str, api_key: str) -> tuple[bool | None, str]:
    """Make a live structured-output request and validate its exact result."""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": SCHEMA_PROBE_MAX_TOKENS,
        "reasoning": {"effort": "none", "exclude": True},
        "provider": {"require_parameters": True},
        "response_format": {"type": "json_schema", "json_schema": SCHEMA_PROBE},
        "messages": [
            {
                "role": "system",
                "content": "Return only the JSON object required by the response schema.",
            },
            {"role": "user", "content": "Report status ok."},
        ],
    }
    payload, failure = _request_probe(model, api_key, body, "JSON-schema")
    retry_with_reasoning = failure == (False, REQUIRES_REASONING_REASON)
    if failure is not None and not retry_with_reasoning:
        return failure
    if failure is None:
        ready, reason = _validate_schema_probe_payload(payload)
        retry_with_reasoning = not ready and reason == NO_STRUCTURED_CONTENT_REASON
        if not retry_with_reasoning:
            return ready, reason

    reasoning_body = {
        **body,
        "reasoning": {"effort": "low", "exclude": True},
    }
    payload, failure = _request_probe(
        model,
        api_key,
        reasoning_body,
        "JSON-schema reasoning-compatible",
    )
    if failure is not None:
        return failure
    ready, reason = _validate_schema_probe_payload(payload)
    if ready:
        return True, "passed the live JSON-schema probe with low reasoning"
    return ready, reason


def _probe_tool_call(model: str, api_key: str) -> tuple[bool | None, str]:
    """Make a live forced tool call and validate its name and arguments."""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 128,
        "provider": {"require_parameters": True},
        "tools": [TOOL_PROBE],
        "tool_choice": {
            "type": "tool",
            "name": "review_model_preflight",
        },
        "messages": [
            {
                "role": "user",
                "content": "Call review_model_preflight with status ok.",
            }
        ],
    }
    payload, failure = _request_probe(
        model,
        api_key,
        body,
        "Claude-tool-call",
        OPENROUTER_ANTHROPIC_MESSAGES_URL,
    )
    if failure is not None:
        return failure
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return False, "live Claude tool-call probe returned no content blocks"
    tool_uses = [
        block
        for block in payload["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_uses:
        return False, "live Claude tool-call probe returned no tool call"
    tool_use = tool_uses[0]
    if tool_use.get("name") != "review_model_preflight":
        return False, "live tool-call probe invoked the wrong tool"
    arguments = tool_use.get("input")
    if arguments != {"status": "ok"}:
        return False, "live tool-call probe returned incorrect tool arguments"
    return True, "passed the live tool-call probe"


def discover_free_fallback(
    required_capabilities: frozenset[str],
    excluded_models: frozenset[str],
    fetch_models: Callable[[frozenset[str]], object] = _fetch_free_models,
    fetch_model: Callable[[str], object] = _fetch_model,
    probe_model: Callable[[str], tuple[bool | None, str]] | None = None,
) -> ModelCheck | None:
    payload = fetch_models(required_capabilities)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("OpenRouter model catalog has an invalid shape")

    checked = 0
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model = entry.get("id")
        supported = entry.get("supported_parameters", [])
        if (
            not isinstance(model, str)
            or not model.endswith(":free")
            or model in excluded_models
            or not isinstance(supported, list)
            or not required_capabilities <= {item for item in supported if isinstance(item, str)}
        ):
            continue
        checked += 1
        candidate = check_model(model, required_capabilities, fetch_model, probe_model)
        if candidate.ready:
            return candidate
        candidate_limit = (
            MAX_FREE_SCHEMA_PROBES if probe_model is not None else MAX_FREE_CANDIDATES
        )
        if checked >= candidate_limit:
            break
    return None


def check_model(
    model: str,
    required_capabilities: frozenset[str],
    fetch_model: Callable[[str], object] = _fetch_model,
    probe_model: Callable[[str], tuple[bool | None, str]] | None = None,
) -> ModelCheck:
    try:
        _model_url(model)
        payload = fetch_model(model)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise RuntimeError("OpenRouter endpoint metadata has an invalid top-level shape")
        endpoints = payload["data"].get("endpoints")
        if not isinstance(endpoints, list):
            raise RuntimeError("OpenRouter endpoint metadata has an invalid endpoints field")

        active_endpoints = [
            endpoint
            for endpoint in endpoints
            if isinstance(endpoint, dict) and endpoint.get("status", 0) == 0
        ]
        if not active_endpoints:
            return ModelCheck(model, False, 0, "has no active endpoints")

        matching_endpoints = []
        available_capabilities: set[str] = set()
        for endpoint in active_endpoints:
            supported = endpoint.get("supported_parameters", [])
            if not isinstance(supported, list):
                continue
            endpoint_capabilities = {
                capability for capability in supported if isinstance(capability, str)
            }
            available_capabilities.update(endpoint_capabilities)
            if required_capabilities <= endpoint_capabilities:
                matching_endpoints.append(endpoint)

        if matching_endpoints:
            probe_reason = ""
            if probe_model is not None:
                probe_ready, probe_reason = probe_model(model)
                if probe_ready is False:
                    return ModelCheck(model, False, 0, _safe_message(probe_reason))
            return ModelCheck(
                model,
                True,
                len(matching_endpoints),
                (
                    f"has {len(matching_endpoints)} compatible active endpoint(s)"
                    + (f" and {probe_reason}" if probe_reason else "")
                ),
            )

        missing = sorted(required_capabilities - available_capabilities)
        missing_text = ", ".join(missing) if missing else "the complete capability set"
        return ModelCheck(
            model,
            False,
            0,
            f"has no active endpoint supporting {missing_text}",
        )
    except RuntimeError as error:
        return ModelCheck(model, False, 0, _safe_message(error))


def select_models(
    primary_model: str,
    fallback_model: str,
    required_capabilities: frozenset[str],
    fetch_model: Callable[[str], object] = _fetch_model,
    discover_free: bool = False,
    fetch_models: Callable[[frozenset[str]], object] = _fetch_free_models,
    probe_model: Callable[[str], tuple[bool | None, str]] | None = None,
    fallback_required_capabilities: frozenset[str] | None = None,
    fallback_probe_model: Callable[[str], tuple[bool | None, str]] | None = None,
) -> ModelSelection:
    primary = check_model(primary_model, required_capabilities, fetch_model, probe_model)
    fallback_capabilities = (
        required_capabilities
        if fallback_required_capabilities is None
        else fallback_required_capabilities
    )
    fallback_mode = "strict"
    if fallback_model == primary_model:
        fallback = ModelCheck(
            fallback_model,
            False,
            0,
            "duplicates the primary model and cannot provide fallback isolation",
        )
    else:
        fallback = check_model(
            fallback_model,
            required_capabilities,
            fetch_model,
            probe_model,
        )
        if not fallback.ready and fallback_required_capabilities is not None:
            ordinary_fallback = check_model(
                fallback_model,
                fallback_capabilities,
                fetch_model,
                fallback_probe_model,
            )
            fallback = ordinary_fallback
            if ordinary_fallback.ready:
                fallback_mode = "ordinary"

    if not fallback.ready and discover_free:
        discovered = discover_free_fallback(
            required_capabilities,
            frozenset({primary_model, fallback_model}),
            fetch_models,
            fetch_model,
            probe_model,
        )
        if discovered is not None:
            fallback = discovered
            fallback_mode = "strict"
        elif fallback_required_capabilities is not None:
            ordinary_discovered = discover_free_fallback(
                fallback_capabilities,
                frozenset({primary_model, fallback_model}),
                fetch_models,
                fetch_model,
                fallback_probe_model,
            )
            if ordinary_discovered is not None:
                fallback = ordinary_discovered
                fallback_mode = "ordinary"

    if primary.ready:
        selected_model = primary.model
        secondary_model = fallback.model if fallback.ready else ""
        selected_mode = "strict"
        secondary_mode = fallback_mode if fallback.ready else ""
    elif fallback.ready:
        selected_model = fallback.model
        secondary_model = ""
        selected_mode = fallback_mode
        secondary_mode = ""
    else:
        raise RuntimeError(
            "no usable OpenRouter review model: "
            f"primary {primary.model} {primary.reason}; "
            f"fallback {fallback.model} {fallback.reason}"
        )

    return ModelSelection(
        primary,
        fallback,
        selected_model,
        secondary_model,
        selected_mode,
        secondary_mode,
    )


def _write_github_outputs(selection: ModelSelection, output_path: str) -> None:
    values = {
        "primary_model": selection.primary.model,
        "primary_ready": str(selection.primary.ready).lower(),
        "fallback_model": selection.fallback.model,
        "fallback_ready": str(selection.fallback.ready).lower(),
        "selected_model": selection.selected_model,
        "secondary_model": selection.secondary_model,
        "selected_mode": selection.selected_mode,
        "secondary_mode": selection.secondary_mode,
    }
    with open(output_path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", required=True)
    parser.add_argument("--fallback", required=True)
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        dest="required_capabilities",
        help="Endpoint capability that must be present; may be repeated.",
    )
    parser.add_argument(
        "--discover-free-fallback",
        action="store_true",
        help="Select another compatible :free model when the configured fallback is unavailable.",
    )
    probe_group = parser.add_mutually_exclusive_group()
    probe_group.add_argument(
        "--probe-schema",
        action="store_true",
        help="Require a live OpenRouter completion that satisfies a strict JSON schema.",
    )
    probe_group.add_argument(
        "--probe-tools",
        action="store_true",
        help="Require a live OpenRouter completion that performs a forced tool call.",
    )
    parser.add_argument(
        "--allow-ordinary-json-fallback",
        action="store_true",
        help=(
            "Allow the configured/discovered fallback to omit response_format and "
            "structured_outputs; its ordinary response is validated locally."
        ),
    )
    args = parser.parse_args(argv)

    required_capabilities = frozenset(args.required_capabilities)
    if not required_capabilities:
        raise RuntimeError("at least one --require capability is required")
    if any(not re.fullmatch(r"[a-z0-9_]+", item) for item in required_capabilities):
        raise RuntimeError("required capabilities must use lowercase API parameter names")

    probe_model: Callable[[str], tuple[bool | None, str]] | None = None
    if args.allow_ordinary_json_fallback and not args.probe_schema:
        raise RuntimeError(
            "--allow-ordinary-json-fallback requires --probe-schema for the primary"
        )
    if args.probe_schema or args.probe_tools:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for live model probes")

        def live_probe(model: str) -> tuple[bool | None, str]:
            if args.probe_tools:
                return _probe_tool_call(model, api_key)
            return _probe_json_schema(model, api_key)

        probe_model = live_probe

    fallback_required_capabilities = None
    if args.allow_ordinary_json_fallback:
        fallback_required_capabilities = (
            required_capabilities - ORDINARY_JSON_OMITTED_CAPABILITIES
        )

    selection = select_models(
        args.primary.strip(),
        args.fallback.strip(),
        required_capabilities,
        discover_free=args.discover_free_fallback,
        probe_model=probe_model,
        fallback_required_capabilities=fallback_required_capabilities,
    )
    for label, check in (("primary", selection.primary), ("fallback", selection.fallback)):
        state = "ready" if check.ready else "unavailable"
        print(
            f"OpenRouter {label} model {_safe_message(check.model)}: "
            f"{state} ({_safe_message(check.reason)})."
        )
    print(f"Selected OpenRouter review model: {_safe_message(selection.selected_model)}")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        _write_github_outputs(selection, output_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"OpenRouter model preflight failed: {_safe_message(error)}", file=sys.stderr)
        sys.exit(1)
