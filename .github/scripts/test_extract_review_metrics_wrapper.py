from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("extract-review-metrics.py")
SPEC = importlib.util.spec_from_file_location("extract_review_metrics_wrapper", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def test_wrapper_exports_deployable_parser() -> None:
    parsed = metrics.parse_review(
        "Technical metadata\nConnection: nous · API: https://example\n"
        "Successful models: model-a\nResult: success\n"
    )
    assert parsed is not None
    assert parsed["provider"] == "nous"
