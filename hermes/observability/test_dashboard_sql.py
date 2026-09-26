"""Execute every SQLite dashboard query against the real plugin schema."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

OBSERVABILITY = Path(__file__).resolve().parent
DASHBOARD = OBSERVABILITY / "grafana" / "provisioning" / "dashboards" / "hermes" / "hermes-overview.json"
DATASOURCE = OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "sqlite.yml"
PLUGIN = OBSERVABILITY.parent / "ops" / "plugin" / "ops-observability" / "__init__.py"
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def at(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def load_plugin():
    if "ops_observability" not in sys.modules:
        spec = importlib.util.spec_from_file_location("ops_observability", PLUGIN)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["ops_observability"]


def sqlite_targets() -> list[tuple[str, dict]]:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    return [(panel["title"], target) for panel in panels for target in panel.get("targets", [])
            if target.get("datasource", {}).get("uid") == "hermes-sqlite"]


def grafana_sql(text: str) -> str:
    """Substitute the Grafana macros the plugin resolves at query time."""
    return (text.replace("$__from", str(int((T0 - timedelta(hours=1)).timestamp() * 1000)))
                .replace("$__to", str(int((T0 + timedelta(hours=1)).timestamp() * 1000)))
                .replace("$__interval_ms", "60000"))


class DashboardSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(root)}):
            load_plugin()._db().close()
        cls.database = root / "ops" / "metrics.db"
        columns = ("ts", "session_id", "provider", "model", "status", "duration_ms", "input_tokens", "output_tokens",
                   "total_tokens", "cost_usd", "finish_reason", "status_code", "retry_count", "requested_model", "call_index")
        with closing(sqlite3.connect(cls.database)) as connection, connection:
            connection.executemany(
                f"INSERT INTO api_calls ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", [
                    (at(0), "s1", "provider-a", "model-a", "ok", 300, 10, 5, 15, 0.25, "stop", 0, 0, "model-a", 1),
                    (at(10), "s1", "provider-a", "model-a", "error", 100, 0, 0, 0, 0, "rate limit", 429, 1, "model-a", 2),
                    (at(20), "s1", "provider-a", "model-a", "ok", 1500, 20, 10, 30, 0.5, "length", 0, 0, "model-a", 2),
                    (at(30), "s2", "provider-a", "model-a", "error", 0, 0, 0, 0, 0, "upstream error", 503, 2, "model-a", 1),
                    (at(40), "s3", "provider-a", "model-a", "ok", 400, 7, 0, 7, 0, "stop", 0, 0, "model-a", 0),
                    (at(50), "s3", "provider-a", "model-b", "ok", 700, 1, 1, 2, 0, "tool_calls", 0, 0, "model-a", 3),
                    (at(-7200), "s0", "provider-a", "model-a", "ok", 900, 1, 1, 2, 0, "stop", 0, 0, "model-a", 1),
                ])
            connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", [
                (at(-5), "s1", "model-a", "telegram", "start", 0, 0, 0), (at(-5), "s3", "model-b", "telegram", "start", 0, 0, 0)])
            connection.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?,?)", [
                (at(1), "s1", "", "terminal", "ok", 10), (at(3), "s3", "", "browser", "error", 5), (at(4), "s9", "", "browser", "ok", 1)])
            connection.execute("INSERT INTO route_fallbacks VALUES (?,?,?,?,?)",
                               (at(80), "provider-a", "model-a", "provider-b", "model-n"))
            connection.execute("""CREATE TABLE IF NOT EXISTS review_runs (
                source_id TEXT PRIMARY KEY, pr_number INTEGER NOT NULL,
                reviewer TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                outcome TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                validated_chunks INTEGER NOT NULL DEFAULT 0, total_chunks INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0, fallback_successes INTEGER NOT NULL DEFAULT 0,
                provider_seconds REAL NOT NULL DEFAULT 0, observed_at TEXT NOT NULL,
                head_sha TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT ''
            )""")
            connection.execute("INSERT INTO review_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               ("review:1", 43, "DirectAPI", "provider-a", "model-a", "success", 2, 8, 8, 1, 1,
                                12.5, at(60), "abc", "run-1"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def run_query(self, target: dict) -> tuple[list[str], list[tuple]]:
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only = 1")  # the plugin's default path option
            cursor = connection.execute(grafana_sql(target["rawQueryText"]))
            return [column[0] for column in cursor.description], cursor.fetchall()

    def test_datasource_is_provisioned_for_the_snapshot(self) -> None:
        text = DATASOURCE.read_text(encoding="utf-8")
        self.assertIn("uid: hermes-sqlite", text)
        self.assertIn("type: frser-sqlite-datasource", text)
        self.assertIn("path: /var/lib/hermes-observability/metrics.db", text)

    def test_every_sqlite_query_runs_read_only_and_declares_its_time_columns(self) -> None:
        targets = sqlite_targets()
        self.assertGreaterEqual(len(targets), 8)
        for title, target in targets:
            with self.subTest(panel=title):
                self.assertEqual(target["queryText"], target["rawQueryText"])
                self.assertIn(target["queryType"], {"table", "time series"})
                columns, rows = self.run_query(target)
                self.assertTrue(rows, "fixture must produce rows")
                self.assertLessEqual(set(target["timeColumns"]), set(columns))
                if target["queryType"] == "time series":
                    self.assertEqual(columns[0], "time")
                    self.assertTrue(all(isinstance(row[0], int) for row in rows), "unix seconds expected")

    def test_scorecard_values_match_the_fixture(self) -> None:
        target = dict(sqlite_targets())["Route scorecard — selected range"]
        columns, rows = self.run_query(target)
        by_route = {(row[0], row[1]): dict(zip(columns, row)) for row in rows}
        route = by_route[("provider-a", "model-a")]
        self.assertAlmostEqual(route["Availability"], 3 / 5)  # the call two hours ago is outside the range
        self.assertAlmostEqual(route["First-attempt success"], 2 / 3)  # the retried call 2 is excluded
        self.assertEqual(route["p95 response time"], 1500)
        self.assertAlmostEqual(route["Truncated"], 1 / 3)
        self.assertAlmostEqual(route["Empty responses"], 1 / 3)
        self.assertAlmostEqual(route["Cost per success"], 0.25)
        self.assertEqual((route["Successes"], route["Errors"], route["Rate limits"], route["Retries"]), (3, 2, 1, 3))
        self.assertEqual(rows[0][:2], ("provider-a", "model-b"), "sorted by availability")

    def test_events_and_mismatch_tables_use_rfc3339_timestamps(self) -> None:
        targets = dict(sqlite_targets())
        columns, rows = self.run_query(targets["Model/provider — latest availability events"])
        row = dict(zip(columns, next(r for r in rows if r[1] == "model-a")))
        self.assertEqual(row["Last success"], at(40))
        self.assertEqual(row["Last error"], at(30))
        self.assertEqual(row["Last rate limit"], at(10))
        columns, rows = self.run_query(targets["Requested vs served model — selected range"])
        self.assertEqual(rows, [("provider-a", "model-a", "model-b", 1, at(50))])
        columns, rows = self.run_query(targets["Tool call failures by model — selected range"])
        self.assertEqual(rows, [("model-b", 1.0, 1), ("model-a", 0.0, 1), ("unknown", 0.0, 1)])

    def test_summary_charts_group_errors_and_finish_reasons_for_readability(self) -> None:
        targets = dict(sqlite_targets())
        columns, rows = self.run_query(targets["API errors by class"])
        self.assertEqual(columns, ["time", "route", "errors"])
        self.assertEqual({row[1]: row[2] for row in rows}, {"rate_limit": 1, "server": 1})
        columns, rows = self.run_query(targets["Finish reasons"])
        self.assertEqual(columns, ["time", "route", "responses"])
        self.assertEqual({row[1]: row[2] for row in rows},
                         {"stop": 2, "length": 1, "tool_calls": 1})

    def test_summary_charts_handle_empty_errors_and_missing_finish_reason(self) -> None:
        targets = dict(sqlite_targets())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(root)}):
                load_plugin()._db().close()
            database = root / "ops" / "metrics.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "INSERT INTO api_calls (ts, status, finish_reason, status_code) "
                    "VALUES (?, ?, ?, ?)",
                    (at(0), "ok", None, 0),
                )
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
                connection.execute("PRAGMA query_only = 1")
                errors = connection.execute(
                    grafana_sql(targets["API errors by class"]["rawQueryText"])
                ).fetchall()
                finish_reasons = connection.execute(
                    grafana_sql(targets["Finish reasons"]["rawQueryText"])
                ).fetchall()
        self.assertEqual(errors, [])
        self.assertEqual([row[1:] for row in finish_reasons], [("unknown", 1)])


if __name__ == "__main__":
    unittest.main()
