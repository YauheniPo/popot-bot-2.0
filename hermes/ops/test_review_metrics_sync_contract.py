"""Contracts for unattended review imports; publishing belongs to the timer."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock


def load_sync():
    path = Path(__file__).with_name("sync-ai-review-metrics.py")
    spec = importlib.util.spec_from_file_location("sync_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(payload):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = json.dumps(payload).encode()
    return result


class ReviewMetricsSyncContractTests(unittest.TestCase):
    def setUp(self):
        self.module = load_sync()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        environment = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_public_repository_does_not_require_a_token(self):
        self.module.GITHUB_TOKEN = ""
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response([{"number": 7}])) as request:
            self.assertEqual(self.module.get_last_5_prs(), [{"number": 7}])
        self.assertNotIn("Authorization", request.call_args.args[0].headers)

    def test_managed_credentials_stay_in_process_and_database_reaches_extractor(self):
        (self.home / ".env").write_text('GITHUB_TOKEN="fixture-private-token"\n')
        database = self.home / "custom/metrics.db"
        output = io.StringIO()
        with mock.patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Imported 2 records", "")) as run:
            with contextlib.redirect_stdout(output):
                self.assertEqual(self.module.main(["--pr", "7", "--db", str(database)]), 0)
        self.assertEqual(run.call_count, 1, "must not call gh auth token or export metrics")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--database") + 1], str(database))
        self.assertEqual(run.call_args.kwargs["env"]["GITHUB_TOKEN"], "fixture-private-token")
        self.assertNotIn("fixture-private-token", str(argv) + output.getvalue())
        self.assertNotIn("GITHUB_TOKEN", os.environ, "do not mutate the parent environment")

    def test_process_token_precedes_managed_file_and_default_database_uses_active_home(self):
        (self.home / ".env").write_text('GITHUB_TOKEN="file-token"\n')
        with mock.patch.dict(os.environ, {"GH_TOKEN": "process-token"}):
            with mock.patch.object(self.module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                self.assertEqual(self.module.main(["--pr", "7"]), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--database") + 1], str(self.home / "ops/metrics.db"))
        self.assertEqual(run.call_args.kwargs["env"]["GITHUB_TOKEN"], "process-token")

    def test_installed_loader_refreshes_managed_credentials_between_invocations(self):
        source = Path(self.module.__file__)
        shutil.copyfile(source.parents[1] / "runtime/github-cli-wrapper.py", self.home / "github-cli-wrapper.py")
        with mock.patch.object(self.module, "__file__", str(self.home / source.name)):
            for token in ("first-token", "rotated-token"):
                (self.home / ".env").write_text(f'GITHUB_TOKEN="{token}"\n')
                self.assertEqual(self.module.managed_github_token(), token)

    def test_launcher_executes_installed_importer_without_a_shell(self):
        launcher = Path(self.module.__file__).parent / "cron/hermes-ai-review-metrics-sync.py"
        with mock.patch.object(os, "execv") as execute, mock.patch.object(sys, "argv", [str(launcher)]):
            runpy.run_path(str(launcher), run_name="__main__")
        execute.assert_called_once_with(sys.executable,
            [sys.executable, "/usr/local/lib/hermes-ops/sync-ai-review-metrics.py"])

    def test_http_failure_and_invalid_response_never_report_success_or_leak_detail(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://api.github.com", status, "private detail", {}, None)
                self.addCleanup(error.close)
                output = io.StringIO()
                with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=error):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        self.assertEqual(self.module.main([]), 1)
                self.assertIn(f"HTTP {status}", output.getvalue())
                self.assertNotIn("private detail", output.getvalue())
        for payload in ({}, [None], [{"number": "7"}], [{"number": -1}], [{"number": True}]):
            with self.subTest(payload=payload):
                with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response(payload)):
                    self.assertEqual(self.module.main([]), 1)

    def test_empty_public_repository_is_success_and_no_import_or_export_is_run(self):
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response([])):
            with mock.patch.object(self.module.subprocess, "run") as run:
                self.assertEqual(self.module.main([]), 0)
        run.assert_not_called()

    def test_failed_discovery_is_not_reported_as_empty_success(self):
        with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=OSError("network unavailable")):
            with mock.patch.object(self.module.subprocess, "run") as run:
                self.assertEqual(self.module.main([]), 1)
        run.assert_not_called()

    def test_mixed_results_continue_and_return_failure_without_leaking_child_output(self):
        (self.home / ".env").write_text('GITHUB_TOKEN="fixture-private-token"\n')
        results = [subprocess.CompletedProcess([], 1, "", "fixture-private-token"),
                   subprocess.CompletedProcess([], 0, "Imported 1 record", "")]
        output = io.StringIO()
        with mock.patch.object(self.module.urllib.request, "urlopen", return_value=response([{"number": 7}, {"number": 8}])):
            with mock.patch.object(self.module.subprocess, "run", side_effect=results) as run:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(self.module.main([]), 1)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("fixture-private-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
