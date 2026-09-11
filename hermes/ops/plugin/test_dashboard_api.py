"""Dashboard aggregation tests using real SQLite and FastAPI, without a server."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException


PLUGIN_DIR = Path(__file__).with_name("ops-observability")
SPEC = importlib.util.spec_from_file_location("dashboard_observability", PLUGIN_DIR / "__init__.py")
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)

API_SPEC = importlib.util.spec_from_file_location("ops_dashboard_api", PLUGIN_DIR / "dashboard" / "plugin_api.py")
assert API_SPEC and API_SPEC.loader
api = importlib.util.module_from_spec(API_SPEC)
API_SPEC.loader.exec_module(api)


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}))
        self.now = datetime(2026, 1, 20, 12, tzinfo=timezone.utc)
        self.enterContext(mock.patch.object(api.time, "time", return_value=self.now.timestamp()))
        self.database = self.root / "ops" / "metrics.db"

    def create_database(self) -> None:
        connection = plugin.storage._db()
        connection.close()

    def test_home_uses_configured_path_or_user_default(self) -> None:
        self.assertEqual(api._home(), self.root)
        with mock.patch.dict(os.environ, {"HERMES_HOME": " "}), \
                mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(api._home(), self.root / ".hermes")

    def test_period_normalizes_units_and_accepts_limit(self) -> None:
        for value, label, seconds in ((" 24H ", "24h", 86400), ("7d", "7d", 7 * 86400)):
            with self.subTest(value=value):
                expected = (label, seconds)
                self.assertEqual(api._period(value), expected)
        max_days = api._MAX_PERIOD_SECONDS // 86400
        self.assertEqual(api._period(f"{max_days}d")[1], api._MAX_PERIOD_SECONDS)

    def test_period_rejects_invalid_and_excessive_values(self) -> None:
        excessive = api._MAX_PERIOD_SECONDS // 3600 + 1
        for value in ("", "0h", "-1d", "1.5h", "01h", "24h;DROP TABLE api_calls", f"{excessive}h"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                api._period(value)
            self.assertEqual(raised.exception.status_code, 422)

    def test_missing_database_returns_empty_payload_without_creating_files(self) -> None:
        result = api.summary("24h")

        self.assertFalse(result["available"])
        self.assertEqual(result["period"], "24h")
        self.assertEqual(result["summary"], {})
        for field in ("models", "tools", "timeline"):
            self.assertEqual(result[field], [])
        self.assertEqual(result["health"], {"issues": [], "database_bytes": 0})
        self.assertIsNotNone(datetime.fromisoformat(result["generated_at"]).tzinfo)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_health_issues_are_bounded_and_blank_lines_ignored(self) -> None:
        health = self.database.with_name("health-state")
        health.parent.mkdir()
        health.write_text(" \n" + "\n".join("x" * 100 for _ in range(25)), encoding="utf-8")

        self.assertEqual(api.summary("24h")["health"]["issues"], ["x" * 80] * 20)

    def test_unreadable_health_state_is_reported(self) -> None:
        health = self.database.with_name("health-state")
        health.parent.mkdir()
        health.touch()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertEqual(api.summary("24h")["health"]["issues"], ["health-state-unreadable"])

    def test_database_stat_failure_returns_service_unavailable(self) -> None:
        with mock.patch.object(Path, "is_file", side_effect=PermissionError), \
                self.assertRaises(HTTPException) as raised:
            api.summary("24h")

        self.assertEqual(raised.exception.status_code, 503)

    def test_empty_database_has_zero_totals(self) -> None:
        self.create_database()

        result = api.summary("24h")

        self.assertTrue(result["available"])
        self.assertTrue(all(value == 0 for value in result["summary"].values()))
        self.assertEqual(result["models"], [])
        self.assertEqual(result["tools"], [])
        self.assertEqual(result["timeline"], [])
        self.assertGreater(result["health"]["database_bytes"], 0)

    def test_summary_aggregates_calls_and_excludes_rows_before_cutoff(self) -> None:
        self.create_database()
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.executemany(
                    "INSERT INTO api_calls (ts, provider, model, status, input_tokens, output_tokens, "
                    "total_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("2026-01-20T10:00:00+00:00", "provider-a", "model-a", "ok", 10, 5, 15, 0.25),
                        ("2026-01-20T10:30:00+00:00", None, None, "error", 2, 1, 3, 0.5),
                        ("1900-01-01T00:00:00+00:00", "old", "old", "ok", 100, 100, 200, 100),
                    ],
                )
                connection.executemany(
                    "INSERT INTO tool_calls (ts, tool_name, status, duration_ms) VALUES (?, ?, ?, ?)",
                    [
                        ("2026-01-20T10:00:00+00:00", "terminal", "ok", 10),
                        ("2026-01-20T10:30:00+00:00", "terminal", "error", 30),
                        ("1900-01-01T00:00:00+00:00", "old", "ok", 100),
                    ],
                )
        finally:
            connection.close()

        for period, bucket in (("48h", "2026-01-20T10:00:00Z"), ("49h", "2026-01-20")):
            with self.subTest(period=period):
                result = api.summary(period)
                self.assertEqual(result["summary"], {
                    "api_calls": 2, "tokens": 18, "input_tokens": 12, "output_tokens": 6,
                    "cost_usd": 0.75, "api_errors": 1, "tool_calls": 2, "tool_errors": 1,
                })
                self.assertEqual(result["models"][0]["model"], "?")
                self.assertEqual(result["models"][0]["provider"], "?")
                self.assertEqual(len(result["models"]), 2)
                self.assertEqual(result["tools"], [
                    {"name": "terminal", "calls": 2, "errors": 1, "avg_duration_ms": 20.0},
                ])
                self.assertEqual(result["timeline"], [
                    {"bucket": bucket, "calls": 2, "tokens": 18, "cost_usd": 0.75},
                ])

    def test_cutoff_is_utc_and_numeric_values_are_not_coerced(self) -> None:
        self.assertEqual(api._cutoff(3600), "2026-01-20T11:00:00.000+00:00")
        for value, expected in ((None, 0), ("9", 0), (7, 7), (1.25, 1.25)):
            with self.subTest(value=value):
                self.assertEqual(api._number(value), expected)

    def test_database_paths_are_literal_and_connection_is_read_only(self) -> None:
        for directory in ("plain", "metrics?mode=rwc#", "hash#", "percent%2f space"):
            with self.subTest(directory=directory):
                database = self.root / directory / "metrics.db"
                database.parent.mkdir()
                writer = sqlite3.connect(database)
                writer.execute("CREATE TABLE marker (value TEXT)")
                writer.close()
                connection = api._connect(database)
                try:
                    self.assertEqual(connection.execute("SELECT * FROM marker").fetchall(), [])
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        connection.execute("INSERT INTO marker VALUES ('unexpected')")
                finally:
                    connection.close()
        with self.assertRaises(sqlite3.OperationalError):
            api._connect(self.root / "missing.db")
        self.assertFalse((self.root / "missing.db").exists())

    def test_database_connection_failure_returns_service_unavailable(self) -> None:
        self.create_database()
        with mock.patch.object(api, "_connect", side_effect=sqlite3.OperationalError("private details")), \
                self.assertRaises(HTTPException) as raised:
            api.summary("24h")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("private details", raised.exception.detail)

    def test_query_failure_closes_connection_and_returns_service_unavailable(self) -> None:
        self.create_database()
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError("no such table")
        with mock.patch.object(api, "_connect", return_value=connection), \
                self.assertRaises(HTTPException) as raised:
            api.summary("24h")

        connection.close.assert_called_once_with()
        self.assertEqual(raised.exception.status_code, 503)

    def test_size_stat_failure_does_not_discard_query_results(self) -> None:
        self.create_database()
        with mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=""), \
                mock.patch.object(api, "_connect", return_value=sqlite3.connect(self.database)) as connect, \
                mock.patch.object(Path, "stat", side_effect=OSError):
            connect.return_value.row_factory = sqlite3.Row
            result = api.summary("24h")

        self.assertTrue(result["available"])
        self.assertEqual(result["health"]["database_bytes"], 0)

    def test_summary_route_is_registered(self) -> None:
        routes = [route for route in api.router.routes if route.path == "/summary"]
        self.assertEqual(len(routes), 1)
        self.assertIn("GET", routes[0].methods)
        self.assertIs(routes[0].endpoint, api.summary)
