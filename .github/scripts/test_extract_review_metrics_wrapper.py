from __future__ import annotations

import importlib.util
import runpy
from unittest import mock
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


def test_wrapper_entrypoint_runs() -> None:
    response = mock.Mock()
    response.__enter__ = lambda self: self
    response.__exit__ = mock.Mock(return_value=False)
    response.read.return_value = b"[]"
    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}), \
         mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43"]), \
         mock.patch("urllib.request.urlopen", return_value=response):
        with mock.patch("builtins.print"), mock.patch("sys.exit"):
            try:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            except SystemExit as result:
                assert result.code == 0
