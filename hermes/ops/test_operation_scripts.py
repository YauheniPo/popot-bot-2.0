"""Regression tests for standalone Hermes operations scripts."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from pathlib import Path


OPS_DIR = Path(__file__).parent


class OperationScriptTests(unittest.TestCase):
    def test_grafana_plugin_installer_prefers_non_deprecated_cli(self):
        packages = (OPS_DIR / 'install' / 'packages.sh').read_text(encoding='utf-8')
        self.assertIn('grafana cli plugins --help', packages)
        self.assertIn('grafana_plugins install frser-sqlite-datasource', packages)
        self.assertIn('grafana cli plugins "$@"', packages)
        self.assertIn('grafana-cli plugins "$@"', packages)

    def test_grafana_snapshot_directory_preserves_read_access_after_atomic_replace(self):
        assets = (OPS_DIR / 'install' / 'assets.sh').read_text(encoding='utf-8')
        self.assertIn('install -d -o "${HERMES_USER}" -g grafana -m 2750 /var/lib/hermes-observability', assets)
        self.assertIn('chown "${HERMES_USER}:grafana" /var/lib/hermes-observability/metrics.db', assets)
        self.assertIn('chmod 0640 /var/lib/hermes-observability/metrics.db', assets)

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
        mock_source = """#!%s
import json, os, pathlib, sys
path = pathlib.Path(%r)
previous = path.read_text().splitlines() if path.exists() else []
with path.open('a') as output:
    output.write(json.dumps({'args': sys.argv[1:], 'query': sys.stdin.read(),
                             'home': os.environ.get('HOME'), 'hermes_home': os.environ.get('HERMES_HOME'),
                             'inherited_key': 'NVIDIA_API_KEY' in os.environ}) + '\\n')
index = len(previous)
codes = %r
if index >= len(codes):
    sys.exit('Unexpected Hermes CLI invocation')
code = codes[index]
if code == 0:
    sys.stdout.write(%r[index])
sys.exit(code)
""" % (sys.executable, str(calls), exit_codes, responses)
        self.write_executable(hermes, mock_source)
        # Simulate the VPS utilities without starting Hermes or calling a model.
        self.write_executable(executable_dir / "timeout", '#!/usr/bin/env bash\nshift 2\nexec "$@"\n')
        self.write_executable(executable_dir / "sleep", f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(sleeps))}\n')
        if root:
            self.write_executable(executable_dir / "id", '''#!/usr/bin/env bash
if [[ $# == 1 && "$1" == "-un" ]]; then
  echo root
  exit 0
fi
case "$*" in
  "-u") echo 0 ;;
  "-u hermes") echo 1001 ;;
  *) exit 1 ;;
esac
''')
            self.write_executable(executable_dir / "runuser", f"""#!{sys.executable}
import json, os, pathlib, sys
pathlib.Path({str(runuser_args)!r}).write_text(json.dumps(sys.argv[1:]))
os.execvp(sys.argv[4], sys.argv[4:])
""")
        settings = {
            "HERMES_RUN_AS_USER": "hermes" if root else getpass.getuser(),
            "HERMES_USER_HOME": str(temporary), "HERMES_HOME": str(temporary / ".hermes"),
            "HERMES_BIN": str(hermes), "HERMES_API_RETRY_PROVIDER": "test-provider",
            "HERMES_API_RETRY_MODEL": "vendor/test-model",
            "HERMES_API_RETRY_MESSAGE": "Hello", "HERMES_API_RETRY_MAX_ATTEMPTS": "2",
            "HERMES_API_RETRY_WAIT_SECONDS": "1", "HERMES_API_RETRY_TIMEOUT_SECONDS": "180",
            "HERMES_API_RETRY_FALLBACKS": "",
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
            self.assertEqual(calls[0]["args"], ["chat", "--provider", "test-provider", "--model",
                                              "vendor/test-model", "--quiet",
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

    def test_api_retry_accepts_absolute_runtime_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="hermes home ") as directory:
            result, calls, _, _ = self.run_retry_helper(Path(directory), [0])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 1)

    def test_api_retry_reports_a_missing_hermes_home_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            missing_home = temporary / "missing home"
            result, calls, _, _ = self.run_retry_helper(
                temporary, [], overrides={"HERMES_USER_HOME": str(missing_home)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"Hermes user home is not a directory: {missing_home}", result.stderr)
            self.assertFalse(calls)

    def test_api_retry_stops_after_success_or_exhaustion_without_final_sleep(self):
        for exit_codes, status in (([1, 0], 0), ([1, 1], 1), ([124], 124)):
            with self.subTest(exit_codes=exit_codes), tempfile.TemporaryDirectory() as directory:
                result, calls, sleeps, _ = self.run_retry_helper(Path(directory), exit_codes)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(len(calls), len(exit_codes))
                if status == 124:
                    self.assertFalse(sleeps.exists())
                    self.assertIn("timed out", result.stderr)
                else:
                    self.assertEqual(sleeps.read_text().splitlines(), ["1"])

    def test_api_retry_switches_to_fallback_for_the_rest_of_the_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".hermes" / "ops").mkdir(parents=True)
            result, calls, sleeps, _ = self.run_retry_helper(
                Path(directory), [1, 1, 0],
                overrides={"HERMES_API_RETRY_FALLBACKS": "backup-provider:vendor/backup-model",
                           "HERMES_API_RETRY_MAX_ATTEMPTS": "3"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            database = Path(directory) / ".hermes" / "ops" / "metrics.db"
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT from_provider, from_model, to_provider, to_model FROM route_fallbacks").fetchall(),
                    [("test-provider", "vendor/test-model", "backup-provider", "vendor/backup-model")])
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                [(call["args"][2], call["args"][4]) for call in calls],
                [("test-provider", "vendor/test-model"),
                 ("backup-provider", "vendor/backup-model"),
                 ("backup-provider", "vendor/backup-model")],
            )
            self.assertEqual(sleeps.read_text().splitlines(), ["1", "1"])

    def test_api_retry_uses_fallback_after_a_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, _, _ = self.run_retry_helper(
                Path(directory), [124, 0],
                overrides={"HERMES_API_RETRY_FALLBACKS": "backup-provider:vendor/backup-model",
                           "HERMES_API_RETRY_MAX_ATTEMPTS": "2"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls[0]["args"][2], "test-provider")
            self.assertEqual(calls[1]["args"][2], "backup-provider")

    def test_api_retry_stops_on_fixed_cli_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, sleeps, _ = self.run_retry_helper(Path(directory), [2])
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(len(calls), 1)
            self.assertFalse(sleeps.exists())
            self.assertIn("failed (exit 2)", result.stderr)

    def test_api_retry_preserves_trailing_response_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls, _, _ = self.run_retry_helper(
                Path(directory), [0], responses=["Reply\n\n"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 1)
            self.assertTrue(result.stdout.endswith("Reply\n\n"), repr(result.stdout))

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
        with tempfile.TemporaryDirectory() as directory:
            result, calls, sleeps, _ = self.run_retry_helper(Path(directory), [0], responses=[""])
            self.assertEqual(len(calls), 1)
            self.assertFalse(sleeps.exists())
            self.assertEqual(result.returncode, 1)
            self.assertIn("returned an empty response (exit 0)", result.stderr)

        with tempfile.TemporaryDirectory() as directory:
            result, calls, sleeps, _ = self.run_retry_helper(Path(directory), [0], responses=[" \t\n"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 1)
            self.assertFalse(sleeps.exists())
            self.assertTrue(result.stdout.endswith(" \t\n"), repr(result.stdout))

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


class SyncAiReviewMetricsTests(unittest.TestCase):
    """Tests for sync-ai-review-metrics.py"""

    def setUp(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "sync_ai_review_metrics",
            OPS_DIR / "sync-ai-review-metrics.py"
        )
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules["sync_ai_review_metrics"] = self.mod
        spec.loader.exec_module(self.mod)
        self.original_subprocess_run = self.mod.subprocess.run

    def tearDown(self):
        import sys
        self.mod.subprocess.run = self.original_subprocess_run
        sys.modules.pop("sync_ai_review_metrics", None)

    def test_import_pr_reviews_calls_extractor_with_correct_args(self):
        """import_pr_reviews calls extractor with PR number."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            mock = unittest.mock.MagicMock()
            mock.returncode = 0
            mock.stdout = "OK"
            mock.stderr = ""
            return mock

        self.mod.subprocess.run = mock_run
        try:
            result = self.mod.import_pr_reviews(42)
            self.assertTrue(result)
            # Verify extractor was called with correct args (second call after gh auth)
            extractor_calls = [c for c in calls if c[0][0] == "python3"]
            self.assertEqual(len(extractor_calls), 1)
            self.assertEqual(extractor_calls[0][0], ["python3", self.mod.EXTRACTOR, "--pr", "42"])
            self.assertEqual(extractor_calls[0][1].get("timeout"), 120)
            self.assertIn("GITHUB_TOKEN", extractor_calls[0][1].get("env", {}))
        finally:
            self.mod.subprocess.run = self.original_subprocess_run

    def test_import_pr_reviews_handles_failure(self):
        """import_pr_reviews returns False on extractor failure."""
        def mock_run(cmd, **kwargs):
            mock = unittest.mock.MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "extractor failed"
            return mock

        self.mod.subprocess.run = mock_run
        try:
            result = self.mod.import_pr_reviews(42)
            self.assertFalse(result)
        finally:
            self.mod.subprocess.run = self.original_subprocess_run

    def test_import_pr_reviews_handles_timeout(self):
        """import_pr_reviews returns False on timeout."""
        import subprocess
        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 120)

        self.mod.subprocess.run = mock_run
        try:
            result = self.mod.import_pr_reviews(42)
            self.assertFalse(result)
        finally:
            self.mod.subprocess.run = self.original_subprocess_run

    def test_get_last_5_prs_requires_token(self):
        """get_last_5_prs returns empty list without token."""
        original_token = self.mod.GITHUB_TOKEN
        self.mod.GITHUB_TOKEN = ""
        try:
            prs = self.mod.get_last_5_prs()
            self.assertEqual(prs, [])
        finally:
            self.mod.GITHUB_TOKEN = original_token

    def test_main_single_pr_mode(self):
        """main with --pr imports single PR."""
        original_import = self.mod.import_pr_reviews

        def mock_import(pr_num):
            self.assertEqual(pr_num, 123)
            return True

        self.mod.import_pr_reviews = mock_import
        try:
            import sys
            original_argv = sys.argv
            sys.argv = ["sync-ai-review-metrics.py", "--pr", "123"]
            try:
                exit_code = self.mod.main()
                self.assertEqual(exit_code, 0)
            finally:
                sys.argv = original_argv
        finally:
            self.mod.import_pr_reviews = original_import

    def test_main_single_pr_mode_failure(self):
        """main with --pr returns 1 on failure."""
        original_import = self.mod.import_pr_reviews

        def mock_import(pr_num):
            return False

        self.mod.import_pr_reviews = mock_import
        try:
            import sys
            original_argv = sys.argv
            sys.argv = ["sync-ai-review-metrics.py", "--pr", "123"]
            try:
                exit_code = self.mod.main()
                self.assertEqual(exit_code, 1)
            finally:
                sys.argv = original_argv
        finally:
            self.mod.import_pr_reviews = original_import

    def test_main_list_mode_no_token(self):
        """main list mode returns 1 when no token available."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run

        def mock_gh(cmd, **kwargs):
            mock = unittest.mock.MagicMock()
            mock.returncode = 1
            return mock

        self.mod.GITHUB_TOKEN = ""
        self.mod.subprocess.run = mock_gh
        try:
            import sys
            original_argv = sys.argv
            sys.argv = ["sync-ai-review-metrics.py"]
            try:
                exit_code = self.mod.main()
                self.assertEqual(exit_code, 1)
            finally:
                sys.argv = original_argv
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh

    def test_main_uses_gh_cli_for_token(self):
        """main falls back to gh auth token."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            mock = unittest.mock.MagicMock()
            if cmd[0] == "gh":
                mock.returncode = 0
                mock.stdout = "gh-token-123"
            else:
                mock.returncode = 0
                mock.stdout = "OK"
            mock.stderr = ""
            return mock

        self.mod.GITHUB_TOKEN = ""
        self.mod.subprocess.run = mock_run
        try:
            exit_code = self.mod.main(["--pr", "1"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(self.mod.GITHUB_TOKEN, "gh-token-123")
            # Verify gh auth token was called first
            self.assertEqual(calls[0], ["gh", "auth", "token"])
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh

    def test_get_last_5_prs_success(self):
        """get_last_5_prs returns PRs on successful API call."""
        import urllib.request
        original_open = urllib.request.urlopen

        def mock_open(req, timeout):
            self.assertIn("Authorization", req.headers)
            response = unittest.mock.MagicMock()
            response.__enter__ = lambda s: s
            response.__exit__ = unittest.mock.Mock(return_value=False)
            response.read.return_value = json.dumps([
                {"number": 1, "title": "PR 1"},
                {"number": 2, "title": "PR 2"},
            ]).encode()
            return response

        urllib.request.urlopen = mock_open
        try:
            original_token = self.mod.GITHUB_TOKEN
            self.mod.GITHUB_TOKEN = "test-token"
            try:
                prs = self.mod.get_last_5_prs()
                self.assertEqual(len(prs), 2)
                self.assertEqual(prs[0]["number"], 1)
                self.assertEqual(prs[1]["number"], 2)
            finally:
                self.mod.GITHUB_TOKEN = original_token
        finally:
            urllib.request.urlopen = original_open

    def test_get_last_5_prs_exception(self):
        """get_last_5_prs returns empty list on exception."""
        import urllib.request
        original_open = urllib.request.urlopen

        def mock_open(req, timeout):
            raise Exception("network error")

        urllib.request.urlopen = mock_open
        try:
            original_token = self.mod.GITHUB_TOKEN
            self.mod.GITHUB_TOKEN = "test-token"
            try:
                prs = self.mod.get_last_5_prs()
                self.assertEqual(prs, [])
            finally:
                self.mod.GITHUB_TOKEN = original_token
        finally:
            urllib.request.urlopen = original_open

    def test_import_pr_reviews_timeout(self):
        """import_pr_reviews returns False on timeout."""
        import subprocess
        original_run = self.mod.subprocess.run

        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 120)

        self.mod.subprocess.run = mock_run
        try:
            result = self.mod.import_pr_reviews(42)
            self.assertFalse(result)
        finally:
            self.mod.subprocess.run = original_run

    def test_import_pr_reviews_exception(self):
        """import_pr_reviews returns False on exception."""
        original_run = self.mod.subprocess.run

        def mock_run(cmd, **kwargs):
            raise Exception("some error")

        self.mod.subprocess.run = mock_run
        try:
            result = self.mod.import_pr_reviews(42)
            self.assertFalse(result)
        finally:
            self.mod.subprocess.run = original_run

    def test_main_list_mode_with_prs(self):
        """main list mode processes multiple PRs."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run
        original_get = self.mod.get_last_5_prs
        original_import = self.mod.import_pr_reviews

        self.mod.GITHUB_TOKEN = "test-token"

        def mock_get_last_5_prs():
            return [
                {"number": 1, "title": "First PR"},
                {"number": 2, "title": "Second PR"},
                {"number": 3, "title": "Third PR"},
            ]

        def mock_import(pr_num):
            return pr_num != 2  # Fail on PR #2

        self.mod.get_last_5_prs = mock_get_last_5_prs
        self.mod.import_pr_reviews = mock_import

        try:
            exit_code = self.mod.main([])
            # Should fail because PR #2 failed
            self.assertEqual(exit_code, 1)
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh
            self.mod.get_last_5_prs = original_get
            self.mod.import_pr_reviews = original_import

    def test_main_list_mode_all_succeed(self):
        """main list mode returns 0 when all PRs succeed."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run
        original_get = self.mod.get_last_5_prs
        original_import = self.mod.import_pr_reviews

        self.mod.GITHUB_TOKEN = "test-token"

        def mock_get_last_5_prs():
            return [
                {"number": 1, "title": "First PR"},
                {"number": 2, "title": "Second PR"},
            ]

        def mock_import(pr_num):
            return True

        self.mod.get_last_5_prs = mock_get_last_5_prs
        self.mod.import_pr_reviews = mock_import

        try:
            exit_code = self.mod.main([])
            self.assertEqual(exit_code, 0)
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh
            self.mod.get_last_5_prs = original_get
            self.mod.import_pr_reviews = original_import

    def test_main_list_mode_no_prs(self):
        """main list mode returns 1 when no PRs found."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run
        original_get = self.mod.get_last_5_prs

        self.mod.GITHUB_TOKEN = "test-token"
        self.mod.get_last_5_prs = lambda: []

        try:
            exit_code = self.mod.main([])
            self.assertEqual(exit_code, 1)
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh
            self.mod.get_last_5_prs = original_get

    def test_main_list_mode_gh_fallback_success(self):
        """main list mode uses gh CLI for token when env var not set."""
        original_token = self.mod.GITHUB_TOKEN
        original_gh = self.mod.subprocess.run
        original_get = self.mod.get_last_5_prs
        original_import = self.mod.import_pr_reviews

        self.mod.GITHUB_TOKEN = ""

        def mock_run(cmd, **kwargs):
            if cmd[0] == "gh":
                mock = unittest.mock.MagicMock()
                mock.returncode = 0
                mock.stdout = "gh-token-456"
                return mock
            return original_run(cmd, **kwargs)

        def mock_get_last_5_prs():
            return [{"number": 1, "title": "PR 1"}]

        def mock_import(pr_num):
            return True

        self.mod.subprocess.run = mock_run
        self.mod.get_last_5_prs = mock_get_last_5_prs
        self.mod.import_pr_reviews = mock_import

        try:
            exit_code = self.mod.main([])
            self.assertEqual(exit_code, 0)
            self.assertEqual(self.mod.GITHUB_TOKEN, "gh-token-456")
        finally:
            self.mod.GITHUB_TOKEN = original_token
            self.mod.subprocess.run = original_gh
            self.mod.get_last_5_prs = original_get
            self.mod.import_pr_reviews = original_import
