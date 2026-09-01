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
MODEL_ID = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$")
REQUEST_TIMEOUT_SECONDS = 15
MAX_METADATA_ATTEMPTS = 3
MAX_FREE_CANDIDATES = 12
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


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


def discover_free_fallback(
    required_capabilities: frozenset[str],
    excluded_models: frozenset[str],
    fetch_models: Callable[[frozenset[str]], object] = _fetch_free_models,
    fetch_model: Callable[[str], object] = _fetch_model,
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
        candidate = check_model(model, required_capabilities, fetch_model)
        if candidate.ready:
            return candidate
        if checked >= MAX_FREE_CANDIDATES:
            break
    return None


def check_model(
    model: str,
    required_capabilities: frozenset[str],
    fetch_model: Callable[[str], object] = _fetch_model,
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
            return ModelCheck(
                model,
                True,
                len(matching_endpoints),
                f"has {len(matching_endpoints)} compatible active endpoint(s)",
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
) -> ModelSelection:
    primary = check_model(primary_model, required_capabilities, fetch_model)
    if fallback_model == primary_model:
        fallback = ModelCheck(
            fallback_model,
            False,
            0,
            "duplicates the primary model and cannot provide fallback isolation",
        )
    else:
        fallback = check_model(fallback_model, required_capabilities, fetch_model)

    if not fallback.ready and discover_free:
        discovered = discover_free_fallback(
            required_capabilities,
            frozenset({primary_model, fallback_model}),
            fetch_models,
            fetch_model,
        )
        if discovered is not None:
            fallback = discovered

    if primary.ready:
        selected_model = primary.model
        secondary_model = fallback.model if fallback.ready else ""
    elif fallback.ready:
        selected_model = fallback.model
        secondary_model = ""
    else:
        raise RuntimeError(
            "no usable OpenRouter review model: "
            f"primary {primary.model} {primary.reason}; "
            f"fallback {fallback.model} {fallback.reason}"
        )

    return ModelSelection(primary, fallback, selected_model, secondary_model)


def _write_github_outputs(selection: ModelSelection, output_path: str) -> None:
    values = {
        "primary_model": selection.primary.model,
        "primary_ready": str(selection.primary.ready).lower(),
        "fallback_model": selection.fallback.model,
        "fallback_ready": str(selection.fallback.ready).lower(),
        "selected_model": selection.selected_model,
        "secondary_model": selection.secondary_model,
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
    args = parser.parse_args(argv)

    required_capabilities = frozenset(args.required_capabilities)
    if not required_capabilities:
        raise RuntimeError("at least one --require capability is required")
    if any(not re.fullmatch(r"[a-z0-9_]+", item) for item in required_capabilities):
        raise RuntimeError("required capabilities must use lowercase API parameter names")

    selection = select_models(
        args.primary.strip(),
        args.fallback.strip(),
        required_capabilities,
        discover_free=args.discover_free_fallback,
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
