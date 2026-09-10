"""Tests for the read-only Hermes observability report CLI."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ops-report.py")
SPEC = importlib.util.spec_from_file_location("ops_report", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
ops_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops_report)


class OpsReportTests(unittest.TestCase):
    def test_database_paths_are_literal_and_connections_are_read_only(self) -> None:
        for directory in ("backup", "other.db?mode=rwc#", "backup#fragment", "backup%2f with spaces"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                database = root / directory / "metrics.db"
                database.parent.mkdir()
                connection = sqlite3.connect(database)
                try:
                    connection.execute("CREATE TABLE marker (value TEXT)")
                    connection.execute("INSERT INTO marker VALUES ('expected database')")
                    connection.commit()
                finally:
                    connection.close()
                before = set(root.rglob("*"))

                def inspect_connection(connection, since, label):
                    self.assertEqual(
                        connection.execute("SELECT value FROM marker").fetchone()[0],
                        "expected database",
                    )
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        connection.execute("CREATE TABLE unexpected (value TEXT)")
                    return {}

                argv = ["ops-report.py", "--database", str(database), "--format", "json"]
                with mock.patch("sys.argv", argv), \
                        mock.patch.object(ops_report, "report", side_effect=inspect_connection), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ops_report.main(), 0)
                self.assertEqual(set(root.rglob("*")), before)

    def test_main_rejects_a_database_not_named_metrics_db(self) -> None:
        with mock.patch("sys.argv", ["ops-report.py", "--database", "/tmp/not-metrics.db"]):
            exit_code = ops_report.main()

        self.assertEqual(exit_code, 2)

    def test_main_rejects_a_relative_database_path(self) -> None:
        with mock.patch("sys.argv", ["ops-report.py", "--database", "metrics.db"]):
            exit_code = ops_report.main()

        self.assertEqual(exit_code, 2)

    def test_main_rejects_a_database_path_with_a_dotdot_component(self) -> None:
        with mock.patch("sys.argv", ["ops-report.py", "--database", "/tmp/../etc/metrics.db"]):
            exit_code = ops_report.main()

        self.assertEqual(exit_code, 2)

    def test_main_accepts_an_absolute_database_path_elsewhere(self) -> None:
        # A backup copied elsewhere for offline analysis is a real workflow
        # for this report tool -- it is not anchored to HERMES_HOME.
        stderr = io.StringIO()
        with mock.patch("sys.argv", ["ops-report.py", "--database", "/home/admin/backup/metrics.db"]), \
                contextlib.redirect_stderr(stderr):
            exit_code = ops_report.main()

        # Rejected only because the file does not exist, not by the path
        # shape checks above -- confirms the shape checks let it through.
        self.assertEqual(exit_code, 2)
        self.assertIn("No metrics database yet", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
