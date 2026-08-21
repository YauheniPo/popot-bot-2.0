"""Regression tests for observability metadata redaction."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("ops-observability") / "__init__.py"
SPEC = importlib.util.spec_from_file_location("ops_observability", MODULE_PATH)
assert SPEC and SPEC.loader
observability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observability
SPEC.loader.exec_module(observability)


class ObservabilityRedactionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
