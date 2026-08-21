"""Token normalization and optional local model pricing."""

from __future__ import annotations

import json
from typing import Any

from .privacy import _mapping, _nested_number, _number
from .storage import _home

def _price(provider: str, model: str, input_tokens: int, output_tokens: int, cache_tokens: int) -> tuple[float, str]:
    price_file = _home() / "ops" / "model-prices.json"
    try:
        prices = json.loads(price_file.read_text(encoding="utf-8"))
        entry = prices.get(f"{provider}/{model}") or prices.get(model)
        if not isinstance(entry, dict):
            return 0.0, "unavailable"
        uncached_input = max(input_tokens - cache_tokens, 0)
        cost = (
            uncached_input * _number(entry.get("input_per_million"))
            + output_tokens * _number(entry.get("output_per_million"))
            + cache_tokens * _number(entry.get("cache_read_per_million"))
        ) / 1_000_000
        return cost, "price-file"
    except Exception:
        return 0.0, "unavailable"


def _usage_values(usage: Any) -> tuple[int, int, int, int, float, str]:
    data = _mapping(usage)
    input_tokens = int(_nested_number(data, "input_tokens", "prompt_tokens") or 0)
    output_tokens = int(_nested_number(data, "output_tokens", "completion_tokens") or 0)
    cache_tokens = int(_nested_number(data, "cache_read_tokens", "cached_tokens", "cache_tokens") or 0)
    total_tokens = int(_nested_number(data, "total_tokens") or 0) or input_tokens + output_tokens
    provider_cost = _nested_number(data, "cost_usd", "total_cost", "cost")
    if provider_cost is not None:
        return input_tokens, output_tokens, cache_tokens, total_tokens, provider_cost, "provider"
    return input_tokens, output_tokens, cache_tokens, total_tokens, 0.0, "unavailable"



