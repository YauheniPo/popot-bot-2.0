"""Regression tests for observability metadata redaction."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ops-observability") / "__init__.py"
SPEC = importlib.util.spec_from_file_location("ops_observability", MODULE_PATH)
assert SPEC and SPEC.loader
observability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observability
SPEC.loader.exec_module(observability)


class ObservabilityRedactionTests(unittest.TestCase):
    def test_command_program_uses_bounded_placeholder_for_invalid_input(self) -> None:
        self.assertEqual(observability._command_program(""), "[command]")
        self.assertEqual(observability._command_program("bad 'quote"), "[command]")
        self.assertEqual(observability._command_program("/bad/cmd"), "[command]")

    def test_register_keeps_all_hooks_command_and_metrics_tool(self) -> None:
        class Connection:
            def close(self) -> None:
                return None

        class Context:
            def __init__(self) -> None:
                self.hooks: dict[str, object] = {}
                self.commands: list[tuple[tuple[object, ...], dict[str, object]]] = []
                self.tools: list[dict[str, object]] = []

            def register_hook(self, name: str, callback: object) -> None:
                self.hooks[name] = callback

            def register_command(self, *args: object, **kwargs: object) -> None:
                self.commands.append((args, kwargs))

            def register_tool(self, **kwargs: object) -> None:
                self.tools.append(kwargs)

        original_db = observability._db
        original_start_worker = observability._start_worker
        observability._db = lambda: Connection()
        observability._start_worker = lambda: None
        try:
            context = Context()
            observability.register(context)
        finally:
            observability._db = original_db
            observability._start_worker = original_start_worker

        self.assertEqual(
            set(context.hooks),
            {
                "pre_tool_call",
                "post_tool_call",
                "pre_api_request",
                "post_api_request",
                "api_request_error",
                "on_session_start",
                "on_session_end",
                "pre_approval_request",
                "post_approval_response",
                "pre_command",
            },
        )
        self.assertEqual(context.commands[0][0], ("ops",))
        self.assertEqual(context.tools[0]["name"], "ops_metrics")

    def test_commands_store_only_program_name_and_never_credentials(self) -> None:
        credential = "sensitive" + "-value"
        command = " ".join(
            ("curl", "-u", f"test-user:{credential}", "https://example.invalid")
        )
        self.assertEqual(observability._command_program(command), "curl")
        self.assertNotIn(credential, observability._short(command))
        self.assertEqual(
            observability._safe_args({"command": command}),
            {"arg_keys": ["command"], "command_program": "curl"},
        )

    def test_audit_record_omits_passwords_from_commands_and_nested_metadata(self) -> None:
        events: list[tuple[str, object]] = []
        ssh_password = "ssh-sensitive" + "-value"
        uri_password = "uri-sensitive" + "-value"
        curl_password = "curl-sensitive" + "-value"
        ssh_uri = "".join(
            ("ssh://", "test-user", ":", uri_password, "@example.invalid")
        )
        ssh_command = " ".join(("sshpass", "-p", ssh_password, ssh_uri))
        curl_command = " ".join(
            ("curl", "-u", f"test-user:{curl_password}", "https://example.invalid")
        )
        original_enqueue = observability.storage._enqueue
        observability.storage._enqueue = lambda kind, payload: events.append((kind, payload))
        try:
            observability._audit(
                "approval.request",
                command=ssh_command,
                args={"command": curl_command},
            )
        finally:
            observability.storage._enqueue = original_enqueue

        self.assertEqual(len(events), 1)
        payload = events[0][1]
        self.assertIsInstance(payload, bytes)
        record = json.loads(payload.decode())
        rendered = json.dumps(record)
        self.assertEqual(record["command_program"], "sshpass")
        self.assertEqual(record["args"]["command_program"], "curl")
        self.assertNotIn(ssh_password, rendered)
        self.assertNotIn(uri_password, rendered)
        self.assertNotIn(curl_password, rendered)

    def test_audit_rotation_uses_configured_number_of_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audit = root / "ops-audit.jsonl"
            audit.write_bytes(b"current" * 10000)
            audit.with_name(audit.name + ".1").write_bytes(b"previous")
            original_paths = observability.storage._paths
            original_max = os.environ.get("HERMES_OBSERVABILITY_AUDIT_MAX_BYTES")
            original_keep = os.environ.get("HERMES_OBSERVABILITY_AUDIT_ROTATED_FILES")
            observability.storage._paths = lambda: (root, root / "metrics.db", audit)
            os.environ["HERMES_OBSERVABILITY_AUDIT_MAX_BYTES"] = "65536"
            os.environ["HERMES_OBSERVABILITY_AUDIT_ROTATED_FILES"] = "2"
            try:
                observability.storage._write_audit(b"new\n")
            finally:
                observability.storage._paths = original_paths
                if original_max is None:
                    os.environ.pop("HERMES_OBSERVABILITY_AUDIT_MAX_BYTES", None)
                else:
                    os.environ["HERMES_OBSERVABILITY_AUDIT_MAX_BYTES"] = original_max
                if original_keep is None:
                    os.environ.pop("HERMES_OBSERVABILITY_AUDIT_ROTATED_FILES", None)
                else:
                    os.environ["HERMES_OBSERVABILITY_AUDIT_ROTATED_FILES"] = original_keep

            self.assertEqual(audit.read_bytes(), b"new\n")
            self.assertTrue(audit.with_name(audit.name + ".1").read_bytes().startswith(b"current"))
            self.assertEqual(audit.with_name(audit.name + ".2").read_bytes(), b"previous")


class ObservabilityReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}))
        self.connection = observability._db()
        self.addCleanup(self.connection.close)
        self.hooks = sys.modules["ops_observability.hooks"]
        self.metrics = sys.modules["ops_observability.metrics"]
        self.commands = sys.modules["ops_observability.commands"]
        self.audit = self.enterContext(mock.patch.object(self.hooks, "_audit"))
        self.enterContext(mock.patch.object(self.hooks, "_execute", side_effect=self.execute))
        # A reporting test must never contact the developer's Prometheus instance.
        self.request = self.enterContext(mock.patch.object(self.metrics.urllib.request, "urlopen", side_effect=TimeoutError))

    def execute(self, sql, params) -> None:
        with self.connection:
            self.connection.execute(sql, params)

    def seed_activity(self) -> None:
        self.hooks._post_api_request(
            provider="test-provider", model="test-model", session_id="session-a", api_duration=0.5,
            usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25},
        )
        self.hooks._post_tool_call(tool_name="terminal", duration_ms=20, status="error")
        self.hooks._pre_command(command="status", args_raw="private-input")

    def test_hooks_persist_usage_durations_and_metadata_without_raw_payloads(self) -> None:
        self.seed_activity()
        self.hooks._pre_tool_call(tool_name="terminal", args={"command": "echo private-input"})
        self.hooks._pre_api_request(provider="test-provider", model="test-model", approx_input_tokens=12)
        self.hooks._post_api_request(
            provider="test-provider", model="test-model", api_duration_ms=17,
            usage={"prompt_tokens": 2, "completion_tokens": 1},
        )
        self.hooks._api_request_error(api_duration=0.1, status_code=429, retry_count=2, retryable=True)

        rows = self.connection.execute(
            "SELECT status, duration_ms, input_tokens, output_tokens, total_tokens, cost_usd, "
            "cost_source, status_code, retry_count FROM api_calls ORDER BY rowid"
        ).fetchall()
        self.assertEqual(rows, [
            ("ok", 500, 10, 5, 15, 0.25, "provider", 0, 0),
            ("ok", 17, 2, 1, 3, 0, "unavailable", 0, 0),
            ("error", 100, 0, 0, 0, 0, "unavailable", 429, 2),
        ])
        self.assertEqual(self.connection.execute("SELECT tool_name, status, duration_ms FROM tool_calls").fetchall(),
                         [("terminal", "error", 20)])
        self.assertNotIn("private-input", str(self.audit.call_args_list))

    def test_session_and_approval_hooks_preserve_outcomes(self) -> None:
        self.hooks._on_session_start(session_id="session-a")
        for outcome in ({"completed": True}, {"interrupted": True}, {}):
            self.hooks._on_session_end(session_id="session-a", **outcome)
        self.hooks._pre_approval_request(session_key="session-a", command="echo private-input")
        self.hooks._post_approval_response(session_key="session-a", choice="allow", command="echo private-input")

        self.assertEqual(self.connection.execute(
            "SELECT completed, failed, interrupted FROM sessions WHERE event='end' ORDER BY rowid"
        ).fetchall(), [(1, 0, 0), (0, 0, 1), (0, 1, 0)])
        self.assertEqual(self.connection.execute("SELECT event, choice FROM approvals ORDER BY rowid").fetchall(),
                         [("request", ""), ("response", "allow")])
        self.assertNotIn("private-input", str(self.audit.call_args_list))

    def test_price_file_and_usage_fallbacks(self) -> None:
        prices = self.root / "ops" / "model-prices.json"
        expected_unavailable = (0.0, "unavailable")
        self.assertEqual(observability._price("p", "m", 10, 5, 2), expected_unavailable)
        prices.write_text(json.dumps({"p/m": {
            "input_per_million": 2, "output_per_million": 4, "cache_read_per_million": 1,
        }}), encoding="utf-8")
        cost, source = observability._price("p", "m", 10, 5, 2)
        self.assertAlmostEqual(cost, 38 / 1_000_000)
        self.assertEqual(source, "price-file")
        self.assertEqual(observability._price("p", "missing", 10, 5, 2), expected_unavailable)
        prices.write_text("not json", encoding="utf-8")
        self.assertEqual(observability._price("p", "m", 10, 5, 2), expected_unavailable)
        usage = {"details": {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 2, "total_cost": 0.25}}
        expected = (10, 5, 2, 15, 0.25, "provider")
        self.assertEqual(observability._usage_values(usage), expected)

    def test_commands_report_populated_and_empty_data(self) -> None:
        for topic in ("models", "costs", "tools", "commands"):
            with self.subTest(topic=topic):
                self.assertTrue(observability._ops_command(topic).startswith("No "))
        self.assertIn("API calls: 0", observability._ops_command(""))
        self.seed_activity()
        for topic, expected in (("summary", "API calls: 1"), ("models", "test-provider/test-model"),
                                ("costs", "$0.2500"), ("tools", "terminal — 1 | 1 | 20"),
                                ("commands", "/status — 1"), ("system", "Tokens: 15")):
            with self.subTest(topic=topic):
                self.assertIn(expected, observability._ops_command(f"{topic} 1h"))
        with mock.patch.object(self.commands, "_query", return_value=[]):
            self.assertIn("API calls: 0", observability._ops_command("summary"))

    def test_commands_validate_arguments_and_report_health(self) -> None:
        self.assertIn("Invalid arguments", observability._ops_command('"'))
        self.assertIn("period must", observability._ops_command("summary invalid"))
        self.assertIn("maximum period", observability._ops_command("summary 367d"))
        for topic in ("help", "unknown"):
            self.assertTrue(observability._ops_command(topic).startswith("Usage:"))
        self.assertEqual(observability._ops_command("health"), "VPS health: OK")
        (self.root / "ops" / "health-state").write_text("disk-low\n", encoding="utf-8")
        self.assertIn("disk-low", observability._ops_command("health"))

    def test_metrics_views_and_fallbacks(self) -> None:
        self.seed_activity()
        overview = observability._metrics_snapshot("unknown", "invalid")
        self.assertEqual(overview["activity"]["tokens"], 15)
        self.assertEqual(overview["models"][0]["calls"], 1)
        self.assertEqual(overview["tools"][0]["errors"], 1)
        self.assertIsNone(overview["resources"]["gateway_up"])
        for view in ("resources", "activity", "models", "tools"):
            with self.subTest(view=view):
                result = json.loads(observability._ops_metrics_tool({"view": view, "period": "1h"}))
                self.assertEqual(set(result), {"source", "period", "generated_at", view})
        self.assertIn("resources", json.loads(observability._ops_metrics_tool(None)))
        self.assertEqual(self.metrics._database_breakdown("invalid", "models")[0]["tokens"], 15)
        with mock.patch.object(self.metrics, "_query", return_value=[]):
            self.assertEqual(self.metrics._activity_snapshot("invalid")["api_calls"], 0)
        self.assertEqual(observability._query("SELECT * FROM nonexistent", ()), [])

    def test_prometheus_rejects_non_private_endpoints(self) -> None:
        for url in ("https://example.invalid", "http://127.0.0.1/private", "http://localhost?x=1",
                    "http://localhost#fragment", "http://user@localhost", "http://[invalid"):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"HERMES_PROMETHEUS_URL": url}):
                self.assertIsNone(self.metrics._prometheus_vector("up"))
        self.request.assert_not_called()

    def test_prometheus_vector_parses_values_and_bounds_response(self) -> None:
        self.request.side_effect = None
        response = self.request.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"status": "success", "data": {"result": [
            {"metric": {"__name__": "hermes_gateway_up"}, "value": [0, "1"]}, "invalid", {},
        ]}}).encode()
        with mock.patch.dict(os.environ, {"HERMES_PROMETHEUS_URL": "http://localhost:9090/"}):
            self.assertEqual(self.metrics._prometheus_vector("up"), [({"__name__": "hermes_gateway_up"}, 1.0)])
        response.read.assert_called_once_with(256 * 1024)
        request = self.request.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:9090/api/v1/query?query=up")
        self.assertEqual(self.request.call_args.kwargs["timeout"], 1.0)

    def test_prometheus_malformed_responses_are_unavailable(self) -> None:
        self.request.side_effect = None
        response = self.request.return_value.__enter__.return_value
        with mock.patch.dict(os.environ, {"HERMES_PROMETHEUS_URL": "http://localhost:9090"}):
            for payload in (b"invalid", b'{"status":"error"}',
                            b'{"status":"success","data":{"result":{}}}',
                            b'{"status":"success","data":{"result":[{"metric":{},"value":[0,"bad"]}]}}'):
                with self.subTest(payload=payload):
                    response.read.return_value = payload
                    self.assertIsNone(self.metrics._prometheus_vector("up"))

    def test_resource_metrics_map_only_known_names_and_windows(self) -> None:
        rows = [({"__name__": name}, value) for name, value in (
            ("hermes_gateway_up", 1), ("hermes_host_disk_used_ratio", 0.25),
            ("hermes_host_memory_available_ratio", 0.75), ("unknown", 99),
        )]
        rows.extend(({"__name__": "hermes_host_load", "window": window}, value)
                    for window, value in (("1m", 1), ("5m", 2), ("15m", 3), ("bad", 99)))
        with mock.patch.object(self.metrics, "_prometheus_vector", return_value=rows):
            self.assertEqual(self.metrics._resources_snapshot(), {
                "gateway_up": 1, "disk_used_ratio": 0.25, "memory_available_ratio": 0.75,
                "load_1m": 1, "load_5m": 2, "load_15m": 3,
            })


if __name__ == "__main__":
    unittest.main()
