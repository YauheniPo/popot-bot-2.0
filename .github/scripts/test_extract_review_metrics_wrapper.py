from __future__ import annotations

import importlib.util
import runpy
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).with_name("extract-review-metrics.py")
SPEC = importlib.util.spec_from_file_location("extract_review_metrics_wrapper", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class ReviewMetricsWrapperTests(unittest.TestCase):
    def test_wrapper_exports_deployable_parser(self) -> None:
        parsed = metrics.parse_review(
            "Technical metadata\nConnection: nous · API: https://example\n"
            "Successful models: model-a\nResult: success\n"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["provider"], "nous")

    def test_wrapper_entrypoint_runs(self) -> None:
        response = mock.Mock()
        response.__enter__ = lambda self: self
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"[]"
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43",
                                    "--database", str(Path(directory) / "metrics.db")]), \
             mock.patch("urllib.request.urlopen", return_value=response), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
