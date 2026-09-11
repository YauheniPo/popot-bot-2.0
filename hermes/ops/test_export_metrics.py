"""Regression tests for exporter states that are unavailable in Docker sidecars."""

from __future__ import annotations

import importlib.util
import math
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).with_name("export-metrics.py")
SPEC = importlib.util.spec_from_file_location("export_metrics", MODULE_PATH)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class ExportMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.backups = self.root / "backups"
        self.enterContext(mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.root), "HERMES_BACKUP_DIR": str(self.backups),
            "HERMES_METRICS_FILE": str(self.root / "output" / "hermes.prom"),
            "HERMES_DISK_PATH": str(self.root), "HERMES_GATEWAY_STATE": "up",
        }))

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

    def test_metric_labels_are_escaped_and_sorted(self) -> None:
        self.assertEqual(metrics.metric("sample", 2, {"z": 'a"b\nc\\d', "a": "first"}),
                         'sample{a="first",z="a\\"b\\nc\\\\d"} 2')
        self.assertEqual(metrics.label(None), "unknown")

    def test_gateway_explicit_states_do_not_run_systemctl(self) -> None:
        with mock.patch.object(metrics.subprocess, "run") as run:
            for state, expected in (("up", 1), ("down", 0)):
                with mock.patch.dict(os.environ, {"HERMES_GATEWAY_STATE": state}):
                    self.assertEqual(metrics.gateway_up(), expected)
            run.assert_not_called()

    def test_gateway_systemctl_results_and_failures(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_GATEWAY_STATE": "", "HERMES_GATEWAY_SERVICE": "test.service"}):
            for result, expected in ((0, 1), (3, 0)):
                with mock.patch.object(metrics.subprocess, "run", return_value=SimpleNamespace(returncode=result)) as run:
                    self.assertEqual(metrics.gateway_up(), expected)
                    self.assertEqual(run.call_args.args[0], ["systemctl", "is-active", "--quiet", "test.service"])
            for error in (OSError(), subprocess.TimeoutExpired("systemctl", 5)):
                with mock.patch.object(metrics.subprocess, "run", side_effect=error):
                    self.assertEqual(metrics.gateway_up(), 0)

    def test_memory_ratio_handles_valid_missing_and_malformed_proc_data(self) -> None:
        for text, expected in (("MemTotal: 100 kB\nMemAvailable: 25 kB\n", 0.25), ("", 0), ("invalid", 0)):
            with mock.patch.object(Path, "read_text", return_value=text):
                self.assertEqual(metrics.memory_ratio(), expected)
        with mock.patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual(metrics.memory_ratio(), 0)

    def test_database_rows_are_read_only_and_handle_missing_tables(self) -> None:
        database = self.root / "metrics.db"
        self.assertEqual(metrics.rows(database, "SELECT 1"), [])
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.close()
        self.assertEqual(metrics.rows(database, "SELECT 1"), [(1,)])
        self.assertEqual(metrics.rows(database, "SELECT * FROM missing"), [])
        self.assertEqual(metrics.rows(database, "INSERT INTO marker VALUES ('unexpected')"), [])
        self.assertEqual(metrics.rows(database, "SELECT * FROM marker"), [])

    def test_backup_ages_ignore_partial_archives_and_include_snapshots(self) -> None:
        missing = (0, -1, -1)
        self.assertEqual(metrics.backup_ages(), missing)
        self.backups.mkdir()
        empty = (1, -1, -1)
        self.assertEqual(metrics.backup_ages(), empty)
        for filename, timestamp in (("scheduled-full-example.zip", 100), ("recent.partial.zip", 290)):
            path = self.backups / filename
            path.touch()
            os.utime(path, (timestamp, timestamp))
        snapshot = self.root / "state-snapshots" / "latest"
        snapshot.mkdir(parents=True)
        os.utime(snapshot, (200, 200))
        with mock.patch.object(metrics.time, "time", return_value=300):
            expected = (1, 100, 200)
            self.assertEqual(metrics.backup_ages(), expected)
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError):
            self.assertEqual(metrics.backup_ages(), empty)

    def test_metrics_modes_reject_unsupported_permissions(self) -> None:
        for value, expected in (("0600", 0o600), ("0640", 0o640), ("0644", 0o644), ("0777", 0o600), ("invalid", 0o600)):
            with mock.patch.dict(os.environ, {"HERMES_METRICS_MODE": value}):
                self.assertEqual(metrics.metrics_file_mode(), expected)

    def test_main_exports_all_aggregates_and_private_textfile(self) -> None:
        query_rows = [
            [("provider-a", "model-a", "ok", 2, 10, 5, 1, 15, 0.25, 20)],
            [("terminal", "ok", 2, 10, 20)], [("status", 2)],
            [("model-a", "telegram", 1, 0, 0, 2), ("model-a", "telegram", 0, 0, 1, 1),
             ("model-a", "telegram", 0, 1, 0, 1)], [("allow", 1)],
        ]
        disk = SimpleNamespace(total=100, free=25)
        filesystem = SimpleNamespace(f_files=100, f_ffree=75)
        with mock.patch.object(metrics, "rows", side_effect=query_rows), \
                mock.patch.object(metrics.shutil, "disk_usage", return_value=disk), \
                mock.patch.object(metrics.os, "statvfs", return_value=filesystem), \
                mock.patch.object(metrics.os, "getloadavg", return_value=(1, 2, 3)), \
                mock.patch.object(metrics, "memory_ratio", return_value=0.5), \
                mock.patch.dict(os.environ, {"HERMES_METRICS_MODE": "0600"}):
            self.assertEqual(metrics.main(), 0)

        target = self.root / "output" / "hermes.prom"
        rendered = target.read_text(encoding="utf-8")
        for line in ('hermes_gateway_up 1.0', 'hermes_host_disk_used_ratio 0.75',
                     'hermes_host_inode_used_ratio 0.25', 'hermes_host_memory_available_ratio 0.5',
                     'hermes_commands_total{command="status"} 2',
                     'hermes_cost_usd_total{model="model-a",provider="provider-a",status="ok"} 0.25',
                     'hermes_approval_responses_total{choice="allow"} 1'):
            self.assertIn(line + "\n", rendered)
        for outcome in ("completed", "interrupted", "failed"):
            self.assertIn(f'outcome="{outcome}"', rendered)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(target.parent.iterdir()), [target])

    def test_home_falls_back_to_user_home(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": " "}), \
                mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(metrics.home(), self.root / ".hermes")


if __name__ == "__main__":
    unittest.main()
