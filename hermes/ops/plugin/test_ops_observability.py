"""Regression tests for observability metadata redaction."""

from __future__ import annotations

import contextvars
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ops-observability") / "__init__.py"
SPEC = importlib.util.spec_from_file_location("ops_observability", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
observability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observability
SPEC.loader.exec_module(observability)


class ObservabilityRedactionTests(unittest.TestCase):
    def test_oversized_metadata_is_redacted_without_exposing_truncated_secrets(self) -> None:
        # Oversized inputs with secrets must have those secrets redacted even after truncation
        text_with_password = "password=" + "s" * 600
        result = observability._short(text_with_password)
        # The secret value should be redacted
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("s" * 10, result)  # original secret not exposed

        text_with_url = "https://user:" + "s" * 600 + "@example.invalid"
        result = observability._short(text_with_url)
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("s" * 10, result)

        text_with_curl = 'curl -u "user:' + "s" * 600 + '"'
        result = observability._short(text_with_curl)
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("s" * 10, result)

    def test_metadata_length_guard_respects_each_field_limit(self) -> None:
        for limit in (5, 120, 240, 500):
            with self.subTest(limit=limit):
                # Oversized values are replaced whole before running regexes.
                self.assertEqual(observability._short("x" * limit, limit), "x" * limit)
                self.assertEqual(observability._short("x" * (limit + 1), limit), "[REDACTED]"[:limit])
                # Secret text is redacted then truncated
                secret_text = "password=" + "s" * (limit + 10)
                result = observability._short(secret_text, limit)
                self.assertLessEqual(len(result), limit)
                # For limits that can hold the redacted form, verify it appears
                if limit >= 20:
                    self.assertIn("[REDACTED]", result)

    def test_api_error_redacts_multiline_private_keys_and_normalized_passwords(self) -> None:
        fixtures = (
            ("-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC_KEY_BODY\n-----END RSA PRIVATE KEY-----", "SYNTHETIC_KEY_BODY"),
            ("-----BEGIN PRIVATE KEY-----\r\nSYNTHETIC_PKCS8_BODY\r\n-----END PRIVATE KEY-----", "SYNTHETIC_PKCS8_BODY"),
            ("passw\x00ord=SYNTHETIC_PASSWORD", "SYNTHETIC_PASSWORD"),
        )
        for reason, secret in fixtures:
            with self.subTest(reason=reason), mock.patch.object(observability.hooks, "_execute") as execute, \
                    mock.patch.object(observability.storage, "_enqueue") as enqueue:
                observability.hooks._api_request_error(reason=reason, provider="fixture", model="fixture")
                self.assertNotIn(secret, str(execute.call_args))
                enqueue.assert_called_once()
                record = json.loads(enqueue.call_args.args[1])
                self.assertNotIn(secret, json.dumps(record))
                self.assertIn("REDACTED", record["reason"])

    def test_command_program_uses_bounded_placeholder_for_invalid_input(self) -> None:
        self.assertEqual(observability._command_program(""), "[command]")
        self.assertEqual(observability._command_program("bad 'quote"), "[command]")
        self.assertEqual(observability._command_program("/bad/cmd"), "[command]")

    def test_register_keeps_only_metric_collection_hooks(self) -> None:
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
                "on_skill_lifecycle",
                "on_model_fallback",
            },
        )
        self.assertEqual(context.commands, [])
        self.assertEqual(context.tools, [])

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

    def test_safe_arg_value_rejects_non_scalars(self) -> None:
        self.assertIsNone(observability.privacy._safe_arg_value("path", ["a", "b"]))

    def test_safe_arg_value_normalizes_and_invalidates_urls(self) -> None:
        self.assertEqual(
            observability.privacy._safe_arg_value("url", "https://example.com/a/b"),
            ("url", "https://example.com"),
        )
        self.assertEqual(
            observability.privacy._safe_arg_value("url", "http://["),
            ("url", "[invalid-url]"),
        )

    def test_safe_arg_value_passes_through_other_scalars(self) -> None:
        self.assertEqual(
            observability.privacy._safe_arg_value("path", "/tmp/file"),
            ("path", "/tmp/file"),
        )

    def test_safe_args_skips_sensitive_keys(self) -> None:
        self.assertEqual(
            observability._safe_args({"api_key": "secret-value"}),
            {"arg_keys": ["api_key"]},
        )

    def test_rotate_audit_files_skips_missing_and_replaces_existing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ops-audit.jsonl"
            # No files present: every source is missing -> continue branch.
            observability.storage._rotate_audit_files(path, 2)

            path.write_bytes(b"current")
            path.with_name("ops-audit.jsonl.1").write_bytes(b"one")
            path.with_name("ops-audit.jsonl.2").write_bytes(b"two")
            observability.storage._rotate_audit_files(path, 2)
            # Shift up: current -> .1, .1 -> .2, old .2 discarded.
            self.assertFalse(path.exists())
            self.assertEqual(path.with_name("ops-audit.jsonl.1").read_bytes(), b"current")
            self.assertEqual(path.with_name("ops-audit.jsonl.2").read_bytes(), b"one")

    def test_journald_copy_skips_when_dev_log_missing(self) -> None:
        with mock.patch.object(observability.storage.Path, "exists", return_value=False):
            observability.storage._journald_copy(b"payload")

    def test_journald_copy_swallows_syslog_errors(self) -> None:
        fake_syslog = mock.MagicMock()
        fake_syslog.syslog.side_effect = Exception("journal unavailable")
        with mock.patch.dict(sys.modules, {"syslog": fake_syslog}):
            observability.storage._journald_copy(b"payload")

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
    def test_attempt_cache_is_bounded_and_legacy_durations_remain_supported(self):
        with mock.patch.object(self.hooks, '_MAX_ATTEMPTS', 2):
            for request in ('old', 'new', 'newest'):
                self.hooks._pre_api_request(api_request_id=request)
        self.assertEqual(len(self.hooks._ATTEMPTS), 2)
        self.assertEqual(self.hooks._finish_attempt(
            dict(api_request_id='old', api_duration_ms=17, retry_count=4)), (17, 4))
        self.assertEqual(self.hooks._finish_attempt(dict(api_duration=12_000)), (12_000_000, 0))

    def test_parallel_requests_with_shared_ids_keep_session_timing_separate(self):
        barrier = threading.Barrier(4)
        def attempt(index):
            identity = dict(api_request_id='shared', session_id=f'session-{index}')
            self.hooks._pre_api_request(**identity, retry_count=index)
            barrier.wait(timeout=5)
            return self.hooks._finish_attempt(identity)
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(attempt, range(4)))
        self.assertEqual([retry for _, retry in results], [0, 1, 2, 3])
        self.assertTrue(all(duration >= 0 for duration, _ in results))
        self.assertEqual(len(self.hooks._ATTEMPTS), 0)

    def test_tool_identity_prefers_real_session_to_terminal_task(self):
        self.hooks._post_tool_call(tool_name='terminal', session_id='session', task_id='terminal-task')
        self.assertEqual(self.connection.execute('SELECT session_id FROM tool_calls').fetchone(), ('session',))

    def test_attempt_duration_and_successful_retry_use_matching_pre_hook(self):
        identity = dict(api_request_id='request', session_id='session', turn_id='turn',
                        provider='fixture', model='model', api_call_count=1)
        with mock.patch.object(self.hooks.time, 'monotonic', side_effect=[10, 11, 20, 22]):
            self.hooks._pre_api_request(**identity, retry_count=0)
            self.hooks._api_request_error(**identity, api_duration=1, retry_count=0)
            self.hooks._pre_api_request(**identity, retry_count=1)
            self.hooks._post_api_request(**identity, api_duration=12)
        self.assertEqual(self.connection.execute(
            'SELECT duration_ms,retry_count FROM api_calls ORDER BY rowid').fetchall(), [(1000, 0), (2000, 1)])

    def test_native_fallback_records_only_route_metadata(self):
        self.hooks._on_model_fallback(from_provider='a', from_model='first', to_provider='b', to_model='second',
                                      api_key='not-for-metrics')
        self.assertEqual(self.connection.execute(
            'SELECT from_provider,from_model,to_provider,to_model FROM route_fallbacks').fetchall(),
            [('a', 'first', 'b', 'second')])
        self.assertNotIn('not-for-metrics', str(self.audit.call_args))

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}))
        self.connection = observability._db()
        self.addCleanup(self.connection.close)
        self.hooks = sys.modules["ops_observability.hooks"]
        self.hooks._ATTEMPTS.clear()
        self.audit = self.enterContext(mock.patch.object(self.hooks, "_audit"))
        self.enterContext(mock.patch.object(self.hooks, "_execute", side_effect=self.execute))

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

    def skill_context(self, agent=None, origin="foreground"):
        return mock.patch.dict(sys.modules, {
            "agent.subagent_lifecycle": SimpleNamespace(get_active_subagent_parent=lambda: agent),
            "tools.skill_provenance": SimpleNamespace(get_current_write_origin=lambda: origin),
        })

    def test_skill_events_distinguish_main_subagent_and_self_improvement(self):
        cases = (
            (SimpleNamespace(session_id="main-session"), "foreground", "main"),
            (SimpleNamespace(session_id="child-session", _delegate_depth=2), "foreground", "subagent"),
            (SimpleNamespace(session_id="main-session"), "background_review", "self_improvement"),
            (None, "foreground", "unknown"),  # cron preload / CLI outside an agent turn
        )
        for agent, origin, expected in cases:
            with self.subTest(actor=expected), self.skill_context(agent, origin):
                self.hooks._on_skill_lifecycle(
                    skill_name="engineering/test-skill", action="loaded", provenance="installed",
                    task_id="task", content="private instructions", goal="private task")
        rows = self.connection.execute(
            "SELECT skill_name, action, agent_type, provenance, session_id, task_id FROM skill_events ORDER BY rowid"
        ).fetchall()
        self.assertEqual(rows, [
            ("engineering/test-skill", "loaded", actor, "installed", getattr(agent, "session_id", ""), "task")
            for agent, _, actor in cases])
        self.assertNotIn("private", str(self.audit.call_args_list))

    def test_skill_changes_keep_timestamp_and_only_allowlisted_metadata(self):
        with self.skill_context(), mock.patch.object(self.hooks, "_now", return_value="2026-10-14T10:00:00.000+00:00"):
            for action in ("created", "patched", "edited", "installed", "archived", "restored", "stale"):
                self.hooks._on_skill_lifecycle(
                    skill_name="plugin:task-verification", action=action, provenance="agent_created",
                    session_id="session", task_id="task", content="secret-content", file_path="/private/file")
        rows = self.connection.execute("SELECT ts, action, session_id FROM skill_events").fetchall()
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(ts == "2026-10-14T10:00:00.000+00:00" and sid == "session" for ts, _, sid in rows))
        self.assertNotIn("secret-content", str(self.audit.call_args_list))
        self.assertNotIn("/private/file", str(self.audit.call_args_list))

    def test_skill_observation_does_not_infer_updates_from_tool_attempts(self):
        self.hooks._post_tool_call(tool_name="skill_manage", status="error", args={"name": "demo", "action": "patch"})
        self.hooks._post_tool_call(tool_name="skill_manage", status="ok", result={"pending_approval": True})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM skill_events").fetchone(), (0,))

    def test_skill_event_rejects_untrusted_names_and_unknown_actions(self):
        for name in ("", None, [], "../private", "/private/skill", "password=secret", "skill\nsecret", "x" * 161):
            self.hooks._on_skill_lifecycle(skill_name=name, action="loaded")
        self.hooks._on_skill_lifecycle(skill_name="valid", action="secret-content")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM skill_events").fetchone(), (0,))
        self.audit.assert_not_called()

    def test_skill_unknown_runtime_and_provenance_are_not_fabricated(self):
        with mock.patch.dict(sys.modules, {"agent.subagent_lifecycle": None, "tools.skill_provenance": None}):
            self.hooks._on_skill_lifecycle(skill_name="demo", action="loaded", provenance="private-url")
        self.assertEqual(self.connection.execute("SELECT agent_type, provenance FROM skill_events").fetchone(),
                         ("unknown", "unknown"))

    def test_concurrent_skill_hooks_keep_each_agents_context(self):
        active = contextvars.ContextVar("skill-test-agent", default=None)
        origin = contextvars.ContextVar("skill-test-origin", default="foreground")
        barrier = threading.Barrier(2, timeout=5)
        recorded = []
        lock = threading.Lock()

        def collect(sql, params):
            with lock:
                recorded.append((sql, params))

        def observe():
            barrier.wait()
            self.hooks._on_skill_lifecycle(skill_name="demo", action="patched")

        def submit(pool, session, depth, write_origin):
            context = contextvars.Context()
            context.run(active.set, SimpleNamespace(session_id=session, _delegate_depth=depth))
            context.run(origin.set, write_origin)
            return pool.submit(context.run, observe)

        with mock.patch.dict(sys.modules, {
            "agent.subagent_lifecycle": SimpleNamespace(get_active_subagent_parent=active.get),
            "tools.skill_provenance": SimpleNamespace(get_current_write_origin=origin.get),
        }), mock.patch.object(self.hooks, "_execute", side_effect=collect), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [submit(pool, "child", 1, "foreground"), submit(pool, "main", 0, "background_review")]
            for future in futures:
                future.result(timeout=10)
        for sql, params in recorded:
            self.execute(sql, params)
        self.assertEqual(self.connection.execute(
            "SELECT agent_type, session_id FROM skill_events ORDER BY session_id").fetchall(),
            [("subagent", "child"), ("self_improvement", "main")])

    def test_hooks_persist_usage_durations_and_metadata_without_raw_payloads(self) -> None:
        self.seed_activity()
        self.hooks._pre_tool_call(tool_name="terminal", args={"command": "echo private-input"})
        self.hooks._pre_api_request(provider="test-provider", model="test-model", approx_input_tokens=12)
        self.hooks._post_api_request(
            provider="test-provider", model="test-model", response_model="served-model", api_duration_ms=17,
            api_call_count=3, finish_reason="length", usage={"prompt_tokens": 2, "completion_tokens": 1},
        )
        self.hooks._api_request_error(
            provider="test-provider", model="test-model", api_duration=0.1, status_code=429,
            retry_count=2, retryable=True, api_call_count=3,
        )

        rows = self.connection.execute(
            "SELECT status, duration_ms, input_tokens, output_tokens, total_tokens, cost_usd, "
            "cost_source, status_code, retry_count, model, requested_model, call_index, finish_reason "
            "FROM api_calls ORDER BY rowid"
        ).fetchall()
        self.assertEqual(rows, [
            ("ok", 500, 10, 5, 15, 0.25, "provider", 0, 0, "test-model", "test-model", 0, ""),
            ("ok", 17, 2, 1, 3, 0, "unavailable", 0, 0, "served-model", "test-model", 3, "length"),
            ("error", 100, 0, 0, 0, 0, "unavailable", 429, 2, "test-model", "test-model", 3, ""),
        ])
        self.assertEqual(self.connection.execute("SELECT tool_name, status, duration_ms FROM tool_calls").fetchall(),
                         [("terminal", "error", 20)])
        self.assertNotIn("private-input", str(self.audit.call_args_list))

    def test_existing_database_gains_new_api_call_columns_in_place(self) -> None:
        self.connection.close()
        database = self.root / "ops" / "metrics.db"
        database.unlink()
        with sqlite3.connect(database) as legacy:
            legacy.execute(
                "CREATE TABLE api_calls (ts TEXT NOT NULL, request_id TEXT, session_id TEXT, turn_id TEXT,"
                " provider TEXT, model TEXT, platform TEXT, status TEXT, duration_ms REAL DEFAULT 0,"
                " input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,"
                " cache_read_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,"
                " cost_usd REAL DEFAULT 0, cost_source TEXT DEFAULT 'unavailable', finish_reason TEXT,"
                " status_code INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0)"
            )
            legacy.execute("INSERT INTO api_calls VALUES ('2026-09-01T00:00:00Z','','','','p','m','',"
                           "'ok',1,0,0,0,0,0,'unavailable','',0,0)")
        self.connection = observability._db()
        observability._db().close()  # repeated registration must stay idempotent
        columns = [row[1] for row in self.connection.execute("PRAGMA table_info(api_calls)")]
        self.assertEqual(columns[-2:], ["requested_model", "call_index"])
        self.assertEqual(self.connection.execute("SELECT requested_model, call_index FROM api_calls").fetchall(),
                         [(None, 0)])
        self.hooks._post_api_request(provider="p", model="m", api_call_count=1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM api_calls").fetchone(), (2,))
        self.assertIn("route_fallbacks", {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")})

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


if __name__ == "__main__":
    unittest.main()
