"""Tests for the read-only Hermes observability report CLI."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ops-report.py")
SPEC = importlib.util.spec_from_file_location("ops_report", MODULE_PATH)
assert SPEC and SPEC.loader
ops_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops_report)


class OpsReportTests(unittest.TestCase):
    def test_main_rejects_a_database_not_named_metrics_db(self) -> None:
        with mock.patch("sys.argv", ["ops-report.py", "--database", "/tmp/not-metrics.db"]):
            exit_code = ops_report.main()

        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
