"""Regression tests for exporter states that are unavailable in Docker sidecars."""

from __future__ import annotations

import importlib.util
import math
import os
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("export-metrics.py")
SPEC = importlib.util.spec_from_file_location("export_metrics", MODULE_PATH)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class ExportMetricsTests(unittest.TestCase):
    def test_unobservable_gateway_is_nan_not_a_false_down_state(self) -> None:
        previous = os.environ.get("HERMES_GATEWAY_STATE")
        os.environ["HERMES_GATEWAY_STATE"] = "unavailable"
        try:
            self.assertTrue(math.isnan(metrics.gateway_up()))
        finally:
            if previous is None:
                os.environ.pop("HERMES_GATEWAY_STATE", None)
            else:
                os.environ["HERMES_GATEWAY_STATE"] = previous

        self.assertEqual(metrics.metric("hermes_gateway_up", float("nan")), "hermes_gateway_up NaN")


if __name__ == "__main__":
    unittest.main()
