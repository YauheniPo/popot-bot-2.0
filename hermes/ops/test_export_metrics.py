"""Regression tests for exporter states that are unavailable in Docker sidecars."""

from __future__ import annotations

from contextlib import closing
import importlib.util
import json
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
assert SPEC is not None
assert SPEC.loader is not None
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

    def test_long_labels_do_not_cut_an_escape_sequence_in_half(self):
        self.assertEqual(metrics.label("a" * 179 + '"tail'), "a" * 179 + '\\"')

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

    def test_gateway_process_metrics_convert_ticks_and_kib_to_seconds_and_bytes(self) -> None:
        fields = ["321", "(python3)", "S"] + ["0"] * 21
        fields[13:15] = ["125", "75"]  # utime and stime, in clock ticks
        sources = {
            "/proc/321/stat": " ".join(fields),
            "/proc/321/status": "Name:\tpython3\nVmSize:\t8192 kB\nVmRSS:\t4096 kB\n",
        }
        for ticks_per_second, expected_cpu in ((100, 2.0), (250, 0.8)):
            with (
                self.subTest(ticks_per_second=ticks_per_second),
                mock.patch.dict(os.environ, {"HERMES_GATEWAY_SERVICE": "test.service"}),
                mock.patch.object(metrics.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="321\n")) as run,
                mock.patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **_: sources[str(path)]) as read,
                mock.patch.object(metrics.os, "sysconf", return_value=ticks_per_second) as sysconf,
            ):
                self.assertEqual(metrics.gateway_process_metrics(), (expected_cpu, 4194304))
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0],
                                 ["systemctl", "show", "--property=MainPID", "--value", "test.service"])
                self.assertGreater(run.call_args.kwargs["timeout"], 0)
                self.assertEqual(read.call_args_list, [
                    mock.call(Path("/proc/321/stat"), encoding="utf-8"),
                    mock.call(Path("/proc/321/status"), encoding="utf-8"),
                ])
                sysconf.assert_called_once_with("SC_CLK_TCK")

    def test_gateway_process_metrics_are_nan_when_unavailable(self) -> None:
        valid_stat = " ".join(["321", "(python3)", "S"] + ["0"] * 21)
        cases = [
            (0, "0", None), (0, "-1", None), (0, "invalid", None), (1, "321", None),
            (0, "321", OSError("process exited")),
            (0, "321", ["truncated"]),
            (0, "321", [valid_stat, "Name: python3\n"]),
            (0, "321", [valid_stat, "VmRSS: invalid kB\n"]),
            (0, "321", [valid_stat, "VmRSS:\n"]),
        ]
        for index, (returncode, pid, proc_data) in enumerate(cases):
            with (
                self.subTest(index=index),
                mock.patch.object(metrics.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=pid)),
                mock.patch.object(Path, "read_text", side_effect=proc_data) as read,
                mock.patch.object(metrics.os, "sysconf", return_value=100),
            ):
                self.assertTrue(all(math.isnan(value) for value in metrics.gateway_process_metrics()))
                if proc_data is None:
                    read.assert_not_called()
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 5)):
            with self.subTest(error=type(error).__name__), mock.patch.object(metrics.subprocess, "run", side_effect=error):
                self.assertTrue(all(math.isnan(value) for value in metrics.gateway_process_metrics()))

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
            [("model-a", "telegram", "completed", 2), ("model-a", "telegram", "interrupted", 1),
             ("model-a", "telegram", "failed", 1)], [("allow", 1)],
        ]
        disk = SimpleNamespace(total=100, free=25)
        filesystem = SimpleNamespace(f_files=100, f_ffree=75)
        with mock.patch.object(metrics, "rows", side_effect=query_rows), \
                mock.patch.object(metrics.shutil, "disk_usage", return_value=disk), \
                mock.patch.object(metrics.os, "statvfs", return_value=filesystem), \
                mock.patch.object(metrics.os, "getloadavg", return_value=(1, 2, 3)), \
                mock.patch.object(metrics, "memory_ratio", return_value=0.5), \
                mock.patch.object(metrics, "gateway_process_metrics", return_value=(2.0, 4194304)), \
                mock.patch.dict(os.environ, {"HERMES_METRICS_MODE": "0600"}):
            self.assertEqual(metrics.main(), 0)

        target = self.root / "output" / "hermes.prom"
        rendered = target.read_text(encoding="utf-8")
        for line in ('hermes_gateway_up 1.0', 'hermes_host_disk_used_ratio 0.75',
                     'hermes_gateway_process_cpu_seconds_total 2.0',
                     'hermes_gateway_process_resident_memory_bytes 4194304',
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

    def profile_db(self, name, messages):
        root = self.root if name == 'default' else self.root / 'profiles' / name
        root.mkdir(parents=True, exist_ok=True)
        (root / 'config.yaml').write_text('{}')
        with closing(sqlite3.connect(root / 'state.db')) as connection, connection:
            connection.execute('CREATE TABLE messages (role TEXT, timestamp REAL, _compressed_summary INTEGER DEFAULT 0)')
            connection.executemany('INSERT INTO messages VALUES (?, ?, ?)', messages)
        return root

    def test_profile_requests_count_input_records_and_time_windows(self):
        now = 1800000000
        self.profile_db('default', [('user', now - 30, 0)])
        self.profile_db('builder', [('user', now - 10, 0), ('user', now - 4000, 0),
                                   ('user', now - 90000, 0), ('user', now - 700000, 0),
                                   ('assistant', now - 1, 0), ('tool', now - 1, 0),
                                   ('user', now - 1, 1), ('user', now + 100, 0)])
        self.profile_db('reviewer', [])
        lines = metrics.profile_request_metrics(self.root, now)
        for window, count in [('1h', 1), ('24h', 2), ('7d', 3), ('retained', 4)]:
            self.assertIn(f'hermes_profile_user_requests{{profile="builder",window="{window}"}} {count}', lines)
        self.assertIn(f'hermes_profile_last_request_timestamp_seconds{{profile="builder"}} {now - 10}.0', lines)
        self.assertIn('hermes_profile_user_requests{profile="reviewer",window="retained"} 0', lines)
        self.assertIn('hermes_profile_user_requests{profile="default",window="retained"} 1', lines)
        self.assertEqual(lines, metrics.profile_request_metrics(self.root, now))

    def test_profile_collection_failures_are_not_zero_usage_and_symlinks_are_not_followed(self):
        good = self.profile_db('builder', [])
        broken = self.root / 'profiles' / 'broken'
        broken.mkdir()
        (broken / 'config.yaml').write_text('{}')
        (broken / 'state.db').write_text('corrupt')
        (self.root / 'profiles' / 'alias').symlink_to(good, target_is_directory=True)
        lines = metrics.profile_request_metrics(self.root, 1800000000)
        self.assertIn('hermes_profile_history_readable{profile="broken"} 0', lines)
        self.assertFalse(any('user_requests{profile="broken"' in line for line in lines))
        self.assertFalse(any('profile="alias"' in line for line in lines))
        self.assertIn('hermes_profile_history_readable{profile="builder"} 1', lines)
        self.assertFalse((self.root / 'state.db').exists())

    def test_dashboard_exposes_profile_usage_without_counter_rates(self):
        dashboard = MODULE_PATH.parents[1] / 'observability/grafana/provisioning/dashboards/hermes/hermes-overview.json'
        panels = json.loads(dashboard.read_text())['panels']
        expressions = [target['expr'] for panel in panels for target in panel.get('targets', [])]
        self.assertTrue(any('hermes_profile_user_requests' in expr for expr in expressions))
        self.assertTrue(any('hermes_profile_last_request_timestamp_seconds' in expr for expr in expressions))
        self.assertFalse(any('rate(hermes_profile_user_requests' in expr for expr in expressions))

    def test_profile_requests_read_legacy_schema_and_committed_wal_without_mutation(self):
        now = 1800000000
        database = self.root / 'state.db'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE messages (role, timestamp)')
            connection.execute('INSERT INTO messages VALUES (?, ?)', ('user', now - 10))
            connection.commit()
            snapshot = {p.name: p.read_bytes() for p in (database, Path(str(database) + '-wal'))}
            lines = metrics.profile_request_metrics(self.root, now)
            self.assertIn('hermes_profile_user_requests{profile="default",window="1h"} 1', lines)
            self.assertEqual(snapshot, {p.name: p.read_bytes() for p in (database, Path(str(database) + '-wal'))})
            connection.execute('DELETE FROM messages')
            connection.commit()
            lines = metrics.profile_request_metrics(self.root, now)
            self.assertIn('hermes_profile_user_requests{profile="default",window="retained"} 0', lines)

    def test_profile_request_failed_query_closes_connection(self):
        database = self.root / 'state.db'
        database.touch()
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError('interrupted')
        with mock.patch.object(metrics.sqlite3, 'connect', return_value=connection) as connect:
            self.assertIn('hermes_profile_history_readable{profile="default"} 0',
                          metrics.profile_request_metrics(self.root, 1800000000))
        self.assertIn('?mode=ro', connect.call_args.args[0])
        self.assertEqual(connect.call_args.kwargs['timeout'], 1)
        connection.set_progress_handler.assert_called_once()
        connection.close.assert_called_once()

    def test_real_database_aggregates_unique_rendered_label_sets(self) -> None:
        database = self.root / "ops" / "metrics.db"
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript("""
                CREATE TABLE sessions(model,platform,completed,failed,interrupted,event);
                CREATE TABLE commands(command);
                CREATE TABLE approvals(choice,event);
                CREATE TABLE tool_calls(tool_name,status,duration_ms);
                CREATE TABLE api_calls(provider,model,status,input_tokens,output_tokens,
                    cache_read_tokens,total_tokens,cost_usd,duration_ms);
            """)
            for name in (None, "", "unknown"):
                for completed, failed, interrupted in ((1,0,0), (1,0,1), (0,0,1), (0,1,1), (0,1,0), (0,0,0)):
                    connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?,'end')",
                                       (name, name, completed, failed, interrupted))
                connection.execute("INSERT INTO commands VALUES(?)", (name,))
                connection.execute("INSERT INTO approvals VALUES(?,'response')", (name,))
                connection.execute("INSERT INTO tool_calls VALUES(?,?,?)", (name, name, 10))
                connection.execute("INSERT INTO api_calls VALUES(?,?,?,1,2,3,6,0.5,10)", (name,name,name))
        with mock.patch.object(metrics, "gateway_process_metrics", return_value=(0, 0)):
            self.assertEqual(metrics.main(), 0)
        output = (self.root / "output" / "hermes.prom").read_text()
        samples = [line for line in output.splitlines() if not line.startswith("#")]
        keys = [line.rsplit(" ", 1)[0] for line in samples]
        self.assertEqual(len(keys), len(set(keys)), "Duplicate Prometheus label sets")
        for outcome in ("completed", "interrupted", "failed"):
            self.assertIn(f'hermes_turns_total{{model="unknown",outcome="{outcome}",platform="unknown"}} 6\n', output)
        self.assertIn('hermes_commands_total{command="unknown"} 3\n', output)
        self.assertIn('hermes_tool_duration_ms_average{status="unknown",tool="unknown"} 10.0\n', output)
        self.assertIn('hermes_tokens_total{model="unknown",provider="unknown",status="unknown"} 18\n', output)

    def test_newest_helpers_return_zero_for_missing_directory(self) -> None:
        missing = self.root / "does-not-exist"
        self.assertEqual(metrics._newest_archive_time(missing), 0.0)
        self.assertEqual(metrics._newest_scheduled_full_time(missing), 0.0)


if __name__ == "__main__":
    unittest.main()
