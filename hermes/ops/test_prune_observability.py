"""Tests for local Hermes observability retention."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("prune-observability.py")
SPEC = importlib.util.spec_from_file_location("prune_observability", MODULE_PATH)
assert SPEC and SPEC.loader
prune_observability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune_observability)


class PruneObservabilityTests(unittest.TestCase):
    def test_prune_removes_only_expired_rows_from_known_tables(self) -> None:
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "metrics.db"
            connection = sqlite3.connect(database)
            try:
                for table in prune_observability.TABLES:
                    connection.execute(f"CREATE TABLE {table} (ts TEXT NOT NULL)")
                    connection.execute(
                        f"INSERT INTO {table} (ts) VALUES (?)",
                        ((now - timedelta(days=91)).isoformat(timespec="milliseconds"),),
                    )
                    connection.execute(
                        f"INSERT INTO {table} (ts) VALUES (?)",
                        ((now - timedelta(days=89)).isoformat(timespec="milliseconds"),),
                    )
                connection.commit()
            finally:
                connection.close()

            removed = prune_observability.prune_database(database, 90, now=now)

            self.assertEqual(removed, len(prune_observability.TABLES))
            connection = sqlite3.connect(database)
            try:
                for table in prune_observability.TABLES:
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone(), (1,))
            finally:
                connection.close()

    def test_prune_rejects_symlink_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target.db"
            sqlite3.connect(target).close()
            symlink = root / "metrics.db"
            symlink.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "regular file"):
                prune_observability.prune_database(symlink, 90)

            self.assertTrue(target.exists())
            self.assertTrue(symlink.is_symlink())

    def test_prune_rejects_a_database_not_named_metrics_db(self) -> None:
        with self.assertRaisesRegex(ValueError, "metrics.db"):
            prune_observability.prune_database(Path("/tmp/not-metrics.db"), 90)

    def test_main_requires_hermes_home(self) -> None:
        argv = ["prune-observability.py", "--database", "/tmp/metrics.db", "--retention-days", "90"]
        with mock.patch("sys.argv", argv), mock.patch.dict("os.environ", {}, clear=True):
            exit_code = prune_observability.main()

        self.assertEqual(exit_code, 2)

    def test_main_rejects_a_database_outside_hermes_home(self) -> None:
        argv = ["prune-observability.py", "--database", "/tmp/metrics.db", "--retention-days", "90"]
        with mock.patch("sys.argv", argv), mock.patch.dict("os.environ", {"HERMES_HOME": "/home/hermes/.hermes"}):
            exit_code = prune_observability.main()

        self.assertEqual(exit_code, 2)
