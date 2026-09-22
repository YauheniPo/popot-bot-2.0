"""Tests for local Hermes observability retention."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("prune-observability.py")
SPEC = importlib.util.spec_from_file_location("prune_observability", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
prune_observability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prune_observability)


def create_schema(root: Path) -> Path:
    """Create the plugin's real schema under root/ops/metrics.db."""
    if "ops_observability" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "ops_observability", Path(__file__).with_name("plugin") / "ops-observability" / "__init__.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    with mock.patch.dict(os.environ, {"HERMES_HOME": str(root)}):
        sys.modules["ops_observability"]._db().close()
    return root / "ops" / "metrics.db"


class PruneObservabilityTests(unittest.TestCase):
    def test_upgrade_api_schema_adds_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "metrics.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE api_calls (ts TEXT)")
            prune_observability._upgrade_api_schema(connection, {"api_calls"})
            columns = {row[1] for row in connection.execute("PRAGMA table_info(api_calls)")}
            connection.close()
            self.assertEqual(columns, {"ts", "requested_model", "call_index"})

    def test_main_passes_only_the_canonical_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            alias = root / "alias"
            alias.symlink_to(home, target_is_directory=True)
            database = alias / "ops" / "metrics.db"
            argv = ["prune-observability.py", "--database", str(database), "--retention-days", "90"]
            with mock.patch.dict("os.environ", {"HERMES_HOME": str(home)}), \
                    mock.patch("sys.argv", argv), \
                    mock.patch.object(prune_observability, "prune_database", return_value=0) as prune:
                self.assertEqual(prune_observability.main(), 0)
            prune.assert_called_once_with((home / "ops").resolve() / "metrics.db", 90)

    def test_database_disappearing_before_connect_is_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "metrics.db"
            sqlite3.connect(database).close()
            original_connect = sqlite3.connect

            def remove_before_connect(*args, **kwargs):
                database.unlink()
                return original_connect(*args, **kwargs)

            with mock.patch.object(sqlite3, "connect", side_effect=remove_before_connect):
                with self.assertRaises(sqlite3.OperationalError):
                    prune_observability.prune_database(database, 90)
            self.assertFalse(database.exists())

    def test_uri_characters_cannot_redirect_database_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = root / "other.db?mode=rwc#%" / "metrics.db"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE commands (ts TEXT, command TEXT)")
            connection.execute("INSERT INTO commands VALUES ('2000-01-01', 'status')")
            connection.commit()
            connection.close()
            before = set(root.rglob("*"))
            self.assertEqual(prune_observability.prune_database(database, 90), 1)
            self.assertEqual(set(root.rglob("*")), before)

    def test_prune_removes_only_expired_rows_from_known_tables(self) -> None:
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = create_schema(Path(temporary_directory))
            connection = sqlite3.connect(database)
            try:
                for table in prune_observability.TABLES:
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
                    self.assertEqual(connection.execute(f"SELECT SUM(weight) FROM {table}_rollup").fetchone(), (1,))
            finally:
                connection.close()

    def test_rollups_accumulate_across_runs_and_keep_pre_bucketed_latency(self) -> None:
        now = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = create_schema(Path(temporary_directory))
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "INSERT INTO api_calls (ts, session_id, provider, model, status, duration_ms, output_tokens,"
                    " input_tokens, total_tokens, retry_count, call_index) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [((now - timedelta(days=age)).isoformat(timespec="milliseconds"), "s", "p", "m", status, ms,
                      out, 3, 3 + out, retries, index)
                     for age, status, ms, out, retries, index in (
                         (95, "ok", 300, 5, 0, 1), (94, "error", 100, 0, 1, 2), (94, "ok", 1500, 0, 0, 2),
                         (92, "ok", 700, 2, 0, 3), (10, "ok", 50, 1, 0, 4))],
                )
            prune_observability.prune_database(database, 93, now=now)
            prune_observability.prune_database(database, 90, now=now)
            with sqlite3.connect(database) as connection:
                rollup = connection.execute(
                    "SELECT status, duration_bucket, weight, output_tokens, retry_count, first_attempt, empty_success "
                    "FROM api_calls_rollup ORDER BY status, duration_bucket"
                ).fetchall()
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_calls").fetchone(), (1,))
            self.assertEqual(rollup, [
                ("error", 250, 1, 0, 1, 0, 0),
                ("ok", 500, 1, 5, 0, 1, 0),    # first run
                ("ok", 1000, 1, 2, 0, 1, 0),   # second run, new bucket
                ("ok", 2000, 1, 0, 0, 0, 1),   # retried after the error of call 2; empty output
            ])

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

    def test_hermes_home_falls_back_to_home_hermes_when_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("pathlib.Path.home", return_value=Path("/fake/home")):
            self.assertEqual(prune_observability.hermes_home(), Path("/fake/home") / ".hermes")

    def test_main_rejects_a_database_outside_the_hermes_home_fallback(self) -> None:
        argv = ["prune-observability.py", "--database", "/tmp/metrics.db", "--retention-days", "90"]
        with mock.patch("sys.argv", argv), \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("pathlib.Path.home", return_value=Path("/fake/home")):
            exit_code = prune_observability.main()

        # /tmp/metrics.db does not match the ~/.hermes fallback, but main()
        # must reach that comparison instead of hard-failing over the
        # missing HERMES_HOME env var.
        self.assertEqual(exit_code, 2)

    def test_main_rejects_a_database_outside_hermes_home(self) -> None:
        argv = ["prune-observability.py", "--database", "/tmp/metrics.db", "--retention-days", "90"]
        with mock.patch("sys.argv", argv), mock.patch.dict("os.environ", {"HERMES_HOME": "/home/hermes/.hermes"}):
            exit_code = prune_observability.main()

        self.assertEqual(exit_code, 2)
