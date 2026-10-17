"""Managed migration must preserve the existing cron job's lifecycle."""

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("apply-config.py")
SPEC = importlib.util.spec_from_file_location("metrics_job_config", SOURCE)
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


class ReviewMetricsJobTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        (self.home / "cron").mkdir()
        (self.home / "cron/jobs.json").write_text('{"jobs": []}')
        (self.home / "scripts").mkdir()
        self.script = self.home / "scripts/hermes-ai-review-metrics-sync.py"
        self.script.write_text("# managed launcher\n")
        self.settings = {"vps_ops": {"review_metrics_sync_job_id": "fixture"}}
        self.records = [{"id": "fixture", "prompt": "old shell commands", "no_agent": False,
                         "script": None, "schedule": {"kind": "interval", "minutes": 720},
                         "enabled": False, "state": "paused", "deliver": "telegram:fixture",
                         "next_run_at": None, "last_status": "ok", "last_run_at": "fixture-time",
                         "fire_claim": {"token": "fixture-claim"}},
                        {"id": "other", "prompt": "unrelated task"}]
        self.jobs = mock.Mock()
        self.jobs.use_cron_store.side_effect = lambda _: contextlib.nullcontext()
        self.jobs._jobs_lock.side_effect = contextlib.nullcontext
        self.jobs.load_jobs.side_effect = lambda: copy.deepcopy(self.records)

        def update(job_id, updates):
            record = next(row for row in self.records if row["id"] == job_id)
            record.update(updates)
            return copy.deepcopy(record)

        self.jobs.update_job.side_effect = update
        loader = mock.patch.object(config, "_load_cron_store", return_value=self.jobs)
        self.loader = loader.start()
        self.addCleanup(loader.stop)

    def test_migration_changes_only_execution_fields_and_is_idempotent(self):
        expected = copy.deepcopy(self.records)
        expected[0].update(script=str(self.script), no_agent=True, prompt="")
        self.assertEqual(config.sync_review_metrics_job(self.home, self.settings), 1)
        self.assertEqual(self.records, expected)
        self.assertEqual(config.sync_review_metrics_job(self.home, self.settings), 0)
        self.jobs.update_job.assert_called_once()
        self.jobs.use_cron_store.assert_called_with(self.home)
        self.assertEqual(self.jobs._jobs_lock.call_count, 2)

    def test_missing_or_disabled_target_does_not_create_jobs(self):
        for job_id in ("", "absent", "fixt"):
            with self.subTest(job_id=job_id):
                self.settings["vps_ops"]["review_metrics_sync_job_id"] = job_id
                self.assertEqual(config.sync_review_metrics_job(self.home, self.settings), 0)
        self.jobs.update_job.assert_not_called()
        (self.home / "cron/jobs.json").unlink()
        self.settings["vps_ops"]["review_metrics_sync_job_id"] = "fixture"
        self.loader.reset_mock()
        self.assertEqual(config.sync_review_metrics_job(self.home, self.settings), 0)
        self.loader.assert_not_called()

    def test_missing_launcher_does_not_disable_agent_execution(self):
        self.script.unlink()
        with self.assertRaisesRegex(ValueError, "launcher"):
            config.sync_review_metrics_job(self.home, self.settings)
        self.jobs.update_job.assert_not_called()

    def test_symlinked_script_or_store_is_rejected_without_writes(self):
        for target in (self.script, self.home / "cron/jobs.json", self.home / "cron/.jobs.lock"):
            with self.subTest(target=target):
                if target.exists():
                    target.unlink()
                target.symlink_to(self.home / "outside")
                with self.assertRaises(ValueError):
                    config.sync_review_metrics_job(self.home, self.settings)
                target.unlink()
                target.write_text("fixture")
        self.jobs.update_job.assert_not_called()

    def test_native_store_failure_is_not_reported_as_success(self):
        self.jobs.update_job.side_effect = None
        self.jobs.update_job.return_value = None
        with self.assertRaises(RuntimeError):
            config.sync_review_metrics_job(self.home, self.settings)

    def test_cli_reports_change_marker(self):
        settings = self.home / "vps-defaults.yml"
        settings.write_text(json.dumps(self.settings))
        args = [str(SOURCE), "review-metrics-job", "--settings", str(settings),
                "--hermes-home", str(self.home)]
        for expected in ("true", "false"):
            output = io.StringIO()
            with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(output):
                self.assertEqual(config.main(), 0)
            self.assertIn("review-metrics-job-changed=" + expected, output.getvalue())

    @unittest.skipUnless(os.environ.get("HERMES_UPSTREAM_DIR"), "pinned Hermes source not configured")
    def test_real_native_storage_preserves_job_record_and_repeated_deploy(self):
        script = '''
import copy, importlib.util, os, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location('config', sys.argv[1])
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    os.environ['HERMES_HOME'] = directory
    (home / 'hermes-agent').symlink_to(sys.argv[2])
    (home / 'scripts').mkdir()
    launcher = home / 'scripts/hermes-ai-review-metrics-sync.py'
    launcher.write_text('# managed launcher')
    jobs = config._load_cron_store(home)
    with jobs.use_cron_store(home):
        for paused in (False, True):
            job = jobs.create_job(prompt='legacy shell commands', schedule='every 12h',
                                  deliver='telegram:123', paused=paused)
            before = copy.deepcopy(jobs.get_job(job['id']))
            settings = {'vps_ops': {'review_metrics_sync_job_id': job['id']}}
            assert config.sync_review_metrics_job(home, settings) == 1
            expected = {**before, 'no_agent': True, 'script': str(launcher), 'prompt': ''}
            assert jobs.get_job(job['id']) == expected
            stamp = (home / 'cron/jobs.json').stat().st_mtime_ns
            assert config.sync_review_metrics_job(home, settings) == 0
            assert (home / 'cron/jobs.json').stat().st_mtime_ns == stamp
'''
        result = subprocess.run([sys.executable, "-c", script, str(SOURCE),
                                 str(Path(os.environ["HERMES_UPSTREAM_DIR"]).resolve())],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
