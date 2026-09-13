"""Regression tests for standalone Hermes operations scripts."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


OPS_DIR = Path(__file__).parent


class OperationScriptTests(unittest.TestCase):
    def write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def read_nul_arguments(self, path: Path) -> list[str]:
        return [
            value.decode("utf-8")
            for value in path.read_bytes().split(b"\0")
            if value
        ]

    def run_retry_helper(self, temporary: Path, exit_codes: list[int], arguments=(), *, root=False, overrides=None, responses=None):
        executable_dir = temporary / "bin"
        executable_dir.mkdir()
        calls = temporary / "calls.jsonl"
        sleeps = temporary / "sleeps"
        runuser_args = temporary / "runuser.json"
        responses = responses if responses is not None else ["Review response\n"] * len(exit_codes)
        self.assertEqual(len(responses), len(exit_codes))
        hermes = executable_dir / "hermes"
        self.write_executable(hermes, f"""#!{sys.executable}
import json, os, pathlib, sys
path = pathlib.Path({str(calls)!r})
previous = path.read_text().splitlines() if path.exists() else []
with path.open('a') as output:
    output.write(json.dumps({{'args': sys.argv[1:], 'query': sys.stdin.read(),
                             'home': os.environ.get('HOME'), 'hermes_home': os.environ.get('HERMES_HOME'),
                             'inherited_key': 'NVIDIA_API_KEY' in os.environ}}) + '\\n')
index = len(previous)
codes = {exit_codes!r}
if index >= len(codes):
    sys.exit('Unexpected Hermes CLI invocation')
code = codes[index]
if code == 0:
    sys.stdout.write({responses!r}[index])
sys.exit(code)
""")
        # Simulate the VPS utilities without starting Hermes or calling a model.
        self.write_executable(executable_dir / "timeout", '#!/usr/bin/env bash\nshift 2\nexec "$@"\n')
        self.write_executable(executable_dir / "sleep", f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(sleeps))}\n')
        if root:
            self.write_executable(executable_dir / "id", '#!/usr/bin/env bash\nif [[ $# == 1 && "$1" == -u ]]; then echo 0; else echo 1001; fi\n')
            self.write_executable(executable_dir / "runuser", f"""#!{sys.executable}
import json, os, pathlib, sys
pathlib.Path({str(runuser_args)!r}).write_text(json.dumps(sys.argv[1:]))
os.execvp(sys.argv[4], sys.argv[4:])
""")
        settings = {
            "HERMES_RUN_AS_USER": "hermes" if root else getpass.getuser(),
            "HERMES_USER_HOME": str(temporary), "HERMES_HOME": str(temporary / ".hermes"),
            "HERMES_BIN": str(hermes), "HERMES_API_RETRY_PROVIDER": "nvidia",
            "HERMES_API_RETRY_MODEL": "deepseek-ai/deepseek-v4-pro-0813",
            "HERMES_API_RETRY_MESSAGE": "Hello", "HERMES_API_RETRY_MAX_ATTEMPTS": "2",
            "HERMES_API_RETRY_WAIT_SECONDS": "1", "HERMES_API_RETRY_TIMEOUT_SECONDS": "180",
        }
        settings.update(overrides or {})
        config = temporary / "hermes-ops.conf"
        config.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        result = subprocess.run([str(OPS_DIR / "api-retry-loop.sh"), *arguments],
                                env={**os.environ, "PATH": f"{executable_dir}:{os.environ['PATH']}",
                                     "HERMES_OPS_CONFIG": str(config), "NVIDIA_API_KEY": "must-not-be-inherited"},
                                capture_output=True, text=True, timeout=5)
        records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        self.assertEqual(len(records), len(exit_codes), "CLI invocation count must match the planned responses")
        return result, records, sleeps, runuser_args

    def test_api_retry_runs_configured_provider_and_model_without_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            result, calls, sleeps, _ = self.run_retry_helper(temporary, [0])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["args"], ["chat", "--provider", "nvidia", "--model",
                                              "deepseek-ai/deepseek-v4-pro-0813", "--quiet",
                                              "--toolsets", "none", "--max-turns", "1", "--query-file", "-"])
            self.assertEqual(calls[0]["query"], "Hello")
            self.assertEqual(calls[0]["home"], str(temporary))
            self.assertEqual(calls[0]["hermes_home"], str(temporary / ".hermes"))
            self.assertFalse(calls[0]["inherited_key"])
            self.assertFalse(sleeps.exists())

    def test_api_retry_preserves_literal_override_and_drops_root(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            text = 'first line "quoted"\n$(touch ' + str(temporary / "injected") + ')'
            result, calls, _, runuser_args = self.run_retry_helper(temporary, [0], ("vendor/override", text), root=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("vendor/override", calls[0]["args"])
            self.assertEqual(calls[0]["query"], text)
            self.assertEqual(json.loads(runuser_args.read_text())[:3], ["-u", "hermes", "--"])
            self.assertFalse((temporary / "injected").exists())

    def test_api_retry_stops_after_success_or_exhaustion_without_final_sleep(self):
        for exit_codes, status in (([1, 0], 0), ([1, 1], 1), ([124, 124], 1)):
            with self.subTest(exit_codes=exit_codes), tempfile.TemporaryDirectory() as directory:
                result, calls, sleeps, _ = self.run_retry_helper(Path(directory), exit_codes)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(len(calls), 2)
                self.assertEqual(sleeps.read_text().splitlines(), ["1"])

    def test_api_retry_rejects_missing_provider_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, _, _ = self.run_retry_helper(Path(directory), [], overrides={"HERMES_API_RETRY_PROVIDER": ""})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("HERMES_API_RETRY_PROVIDER", result.stderr)
            self.assertFalse(calls)

    def test_retry_fixture_rejects_missing_or_extra_cli_calls(self):
        for exit_codes in ([0, 0], [1]):
            with self.subTest(exit_codes=exit_codes), tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory)
                with self.assertRaisesRegex(AssertionError, "CLI invocation count"):
                    self.run_retry_helper(temporary, exit_codes)

    def test_api_retry_rejects_empty_success_and_reports_the_actual_exit_status(self):
        for empty in ("", " \t\n"):
            for second in ("", "Reply\n"):
                with self.subTest(empty=empty, second=second), tempfile.TemporaryDirectory() as directory:
                    result, calls, sleeps, _ = self.run_retry_helper(Path(directory), [0, 0], responses=[empty, second])
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(sleeps.read_text().splitlines(), ["1"])
                    self.assertEqual(result.returncode, 0 if second else 1)
                    self.assertIn("returned no answer (exit 0)", result.stderr)
                    self.assertNotIn("failed (exit 1)", result.stderr)
                    if second:
                        self.assertIn("Reply", result.stdout)
                    else:
                        self.assertIn("Attempts exhausted", result.stderr)

    def test_notify_doctor_url_encodes_plain_text_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            executable_dir = temporary / "bin"
            executable_dir.mkdir()
            curl_arguments = temporary / "curl-arguments"
            self.write_executable(
                executable_dir / "hermes",
                "#!/usr/bin/env bash\nprintf 'failed: [needs] *attention* & retry\\n'\nexit 42\n",
            )
            self.write_executable(
                executable_dir / "curl",
                "#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" >\"$CURL_ARGUMENTS\"\n",
            )

            environment = {
                **os.environ,
                "CURL_ARGUMENTS": str(curl_arguments),
                "PATH": f"{executable_dir}:{os.environ['PATH']}",
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_CHAT_ID": "chat id",
            }
            completed = subprocess.run(
                [str(OPS_DIR / "notify-doctor.sh")],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = self.read_nul_arguments(curl_arguments)
            self.assertNotIn("parse_mode", arguments)
            self.assertIn("--data-urlencode", arguments)
            self.assertIn("chat_id=chat id", arguments)
            self.assertIn(
                "text=❌ Hermes Doctor failed\nfailed: [needs] *attention* & retry",
                arguments,
            )


if __name__ == "__main__":
    unittest.main()
