"""Contract tests for the mandatory competitor cron publication gate."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).with_name("competitor_digest.py")
NOW = datetime(2026, 10, 10, 19, 25, tzinfo=timezone.utc)


class CompetitorDigestTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("competitor_digest_test", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.watch_path = self.home / "competitor-watches/fitness.json"
        self.watch_path.parent.mkdir()
        self.watch = {
            "watch_slug": "fitness", "name": "Fitness", "language": "en",
            "lookback_days": 60, "materiality_threshold": "medium",
            "companies": [{"name": "BetterMe", "domain": "betterme.world"},
                          {"name": "Simple", "domain": "simple.life"}],
            "event_categories": ["feature_release"], "delivery": "preserve-me",
            "last_cutoff": "2026-09-30T00:00:00Z", "personal_setting": 42,
        }
        self.watch_path.write_text(json.dumps(self.watch))
        self.job = {"id": "job-1", "skills": ["competitor-news-monitor"],
                    "prompt": "Run ~/.hermes/competitor-watches/fitness.json",
                    "deliver": "preserve", "model": "personal-model"}
        self.clock = mock.patch.object(self.module, "utc_now", return_value=NOW)
        self.utc_now = self.module.utc_now
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.guards = []
        self.addCleanup(lambda: [guard.close() for guard in self.guards])

    def begin(self):
        guard = self.module.begin_run(self.job, self.home)
        self.guards.append(guard)
        return guard

    def test_utc_clock_is_timezone_aware(self):
        self.assertEqual(self.utc_now().utcoffset().total_seconds(), 0)

    def test_secure_path_rejects_parent_traversal(self):
        with self.assertRaisesRegex(ValueError, "path outside watch directory"):
            self.module.secure_path(self.home, self.home / ".." / "outside.json")

    def test_read_json_rejects_non_regular_files(self):
        directory = self.home / "not-a-json-file"
        self.module.os.mkfifo(directory)
        with self.assertRaisesRegex(ValueError, "JSON must be a regular file"):
            self.module.read_json(directory)

    def test_write_json_rejects_oversized_data_and_cleans_failed_atomic_write(self):
        target = self.home / "state.json"
        with self.assertRaisesRegex(ValueError, "JSON exceeds size limit"):
            self.module.write_json(target, {"payload": "x" * self.module.MAX_JSON_BYTES})
        with mock.patch.object(self.module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.module.write_json(target, {"safe": True})
        self.assertEqual(list(self.home.glob(".competitor-*")), [])

    def test_begin_run_accepts_a_single_skill_string(self):
        guard = self.module.begin_run(dict(self.job, skills=self.module.SKILL), self.home)
        self.guards.append(guard)
        self.assertIsNotNone(guard)

    def test_pending_window_is_preserved_and_future_cutoff_releases_lock(self):
        state_path = self.home / "competitor-watches/fitness/.publication.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "config": self.module.config(self.watch),
            "last_cutoff": "2026-09-30T00:00:00Z",
            "pending_window_start": "2026-09-20T00:00:00Z",
        }))
        guard = self.begin()
        self.assertEqual(guard.window_start.isoformat(), "2026-09-20T00:00:00+00:00")
        guard.close()

        state_path.write_text(json.dumps({
            "config": self.module.config(self.watch),
            "last_cutoff": "2030-01-01T00:00:00Z",
        }))
        with self.assertRaisesRegex(ValueError, "checkpoint is in the future"):
            self.begin()
        state_path.write_text(json.dumps({
            "config": self.module.config(self.watch),
            "last_cutoff": "2026-09-30T00:00:00Z",
        }))
        self.assertIsNotNone(self.begin())

    def record(self, guard, complete=False, events=True):
        return {
            "run_id": guard.run_id, "watch_slug": "fitness",
            "window_start": guard.window_start.isoformat(),
            "window_end": guard.window_end.isoformat(),
            "coverage_complete": complete,
            "coverage": [{"company": c["name"],
                          "checked_urls": ["https://" + c["domain"] + "/news/launch"],
                          "status": "complete" if complete else "partial",
                          "gaps": [] if complete else ["Release notes unavailable"]}
                         for c in self.watch["companies"]],
            "events": [{"company": "BetterMe", "category": "feature_release",
                        "event_date": "2026-09-12", "primary_evidence_urls": [
                            "https://betterme.world/news/launch"],
                        "what_changed": "Launched a team challenge.",
                        "why_it_matters": "May improve team participation.",
                        "confidence": "high", "confidence_reason": "Dated company announcement"}]
                       if events else [],
            "errors": [], "discarded_candidates": [],
        }

    def save(self, guard, record):
        guard.artifact_path.write_text(json.dumps(record))

    def test_old_betterme_event_is_rejected_and_cutoff_is_not_advanced(self):
        guard = self.begin()
        record = self.record(guard)
        record["events"][0]["event_date"] = "2026-08-04"
        self.save(guard, record)
        mutated = dict(self.watch, last_cutoff="2030-01-01T00:00:00Z")
        self.watch_path.write_text(json.dumps(mutated))
        with self.assertRaisesRegex(ValueError, "outside.*window"):
            guard.finalize()
        guard.close()
        self.assertEqual(json.loads(self.watch_path.read_text()), self.watch)

    def test_runtime_fills_missing_metadata_and_persists_validated_artifact(self):
        guard = self.begin()
        record = self.record(guard, complete=True)
        for field in ("run_id", "watch_slug", "window_start", "window_end"):
            record.pop(field)
        record.update(started_at="2000-01-01", finished_at="2099-01-01")
        self.save(guard, record)
        self.assertIn("1 verified new event", guard.finalize())
        saved = json.loads(guard.artifact_path.read_text())
        self.assertEqual(saved["run_id"], guard.run_id)
        self.assertEqual(saved["watch_slug"], "fitness")
        self.assertEqual(saved["window_start"], guard.window_start.isoformat())
        self.assertEqual(saved["window_end"], NOW.isoformat())
        self.assertEqual(saved["started_at"], NOW.isoformat())
        self.assertEqual(saved["finished_at"], NOW.isoformat())
        self.assertFalse(guard.state_path.exists())  # Still awaiting delivery.

    def test_missing_run_id_does_not_require_model_repair(self):
        guard = self.begin()
        record = self.record(guard)
        record.pop("run_id")  # Reproduce the Oct 11 VPS artifact.
        self.save(guard, record)
        repair = mock.Mock()
        self.assertIn("partial", guard.finalize_with_repair(repair))
        repair.assert_not_called()

    def test_invalid_content_gets_one_repair_with_specific_error(self):
        guard = self.begin()
        invalid = self.record(guard)
        invalid.pop("coverage")
        self.save(guard, invalid)
        repair = mock.Mock(side_effect=lambda prompt: self.save(guard, self.record(guard)))
        self.assertIn("partial", guard.finalize_with_repair(repair))
        repair.assert_called_once()
        prompt = repair.call_args.args[0]
        self.assertIn("missing company coverage", prompt)
        self.assertIn(str(guard.artifact_path), prompt)
        self.assertIn("Do not restart research", prompt)
        self.assertFalse(guard.state_path.exists())

    def test_unsuccessful_repair_stops_without_checkpoint(self):
        guard = self.begin()
        self.save(guard, {"events": []})
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "missing company coverage"):
            guard.finalize_with_repair(repair)
        repair.assert_called_once()
        self.assertIsNone(guard.record)
        self.assertFalse(guard.state_path.exists())

    def test_unreadable_json_can_be_repaired_but_symlink_cannot(self):
        guard = self.begin()
        guard.artifact_path.write_text('{"events": [')
        repair = mock.Mock(side_effect=lambda prompt: self.save(guard, self.record(guard)))
        guard.finalize_with_repair(repair)
        self.assertIn("JSON missing or unreadable", repair.call_args.args[0])
        guard.artifact_path.unlink()
        guard.artifact_path.symlink_to(self.watch_path)
        repair.reset_mock()
        with self.assertRaisesRegex(ValueError, "symlink"):
            guard.finalize_with_repair(repair)
        repair.assert_not_called()
        self.assertIsNone(guard.record)

    def test_service_metadata_does_not_hide_invalid_event_dates(self):
        guard = self.begin()
        record = self.record(guard)
        for field in ("run_id", "watch_slug", "window_start", "window_end"):
            record.pop(field)
        record["events"][0]["event_date"] = "2026-08-04"
        self.save(guard, record)
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "outside.*window"):
            guard.finalize_with_repair(repair)
        repair.assert_called_once()
        self.assertFalse(guard.state_path.exists())

    def test_repair_preserves_cancellation_and_provider_failure(self):
        for error in (KeyboardInterrupt(), TimeoutError("provider timed out")):
            with self.subTest(error=type(error).__name__):
                guard = self.begin()
                self.save(guard, {"events": []})
                repair = mock.Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    guard.finalize_with_repair(repair)
                repair.assert_called_once()
                self.assertIsNone(guard.record)
                guard.close()

    def test_conflicting_metadata_and_watch_changes_never_trigger_repair(self):
        for field, value in (("run_id", "old-run"), ("watch_slug", "other"),
                             ("window_start", "2000-01-01T00:00:00Z"),
                             ("window_end", None)):
            with self.subTest(field=field):
                guard = self.begin()
                record = self.record(guard)
                record[field] = value
                self.save(guard, record)
                repair = mock.Mock()
                with self.assertRaisesRegex(ValueError, field):
                    guard.finalize_with_repair(repair)
                repair.assert_not_called()
                guard.close()
        guard = self.begin()
        self.save(guard, {"events": []})
        self.watch_path.write_text(json.dumps(dict(self.watch, language="changed")))
        repair = mock.Mock()
        with self.assertRaisesRegex(ValueError, "watch changed"):
            guard.finalize_with_repair(repair)
        repair.assert_not_called()

    def test_partial_report_is_rendered_from_record_and_never_advances_cutoff(self):
        guard = self.begin()
        self.save(guard, self.record(guard))
        report = guard.finalize()
        self.assertIn("1 verified new event", report)
        self.assertIn("partial", report.lower())
        self.assertIn("Simple", report)
        self.assertIn("https://betterme.world/news/launch", report)
        self.assertIn("Interpretation", report)
        self.assertNotIn("no verified new events", report.lower())
        guard.commit()
        saved = json.loads(self.watch_path.read_text())
        self.assertEqual(saved["last_cutoff"], self.watch["last_cutoff"])
        self.assertEqual(saved["last_run_status"], "coverage_incomplete")
        self.assertEqual(saved["personal_setting"], 42)
        self.assertEqual(saved["delivery"], "preserve-me")

    def test_delivery_failure_leaves_retry_eligible_and_ignores_agent_cutoff(self):
        guard = self.begin()
        self.save(guard, self.record(guard, complete=True))
        guard.finalize()
        self.watch_path.write_text(json.dumps(dict(self.watch, last_cutoff="2030-01-01")))
        guard.close()  # No commit: delivery failed, was queued, cancelled, or claim lost.
        self.assertEqual(json.loads(self.watch_path.read_text()), self.watch)
        again = self.begin()
        self.save(again, self.record(again, complete=True))
        self.assertIn("1 verified new event", again.finalize())

    def test_complete_delivery_commits_once_and_deduplicates_repeated_run(self):
        guard = self.begin()
        record = self.record(guard, complete=True)
        record["events"][0]["event_date"] = NOW.date().isoformat()
        self.save(guard, record)
        guard.finalize()
        guard.commit()
        after = self.watch_path.read_bytes()
        guard.commit()
        self.assertEqual(self.watch_path.read_bytes(), after)
        guard.close()
        again = self.begin()
        self.assertEqual((again.window_end - again.window_start).days, 1)
        record = self.record(again, complete=True)
        record["events"][0]["event_date"] = NOW.date().isoformat()
        record["events"][0]["primary_evidence_urls"][0] += "?utm_source=duplicate"
        self.save(again, record)
        self.assertEqual(again.finalize(), "[SILENT]")

    def test_only_complete_valid_zero_event_run_can_be_silent(self):
        guard = self.begin()
        self.save(guard, self.record(guard, events=False))
        self.assertIn("No verified new events", guard.finalize())
        self.save(guard, self.record(guard, complete=True, events=False))
        self.assertEqual(guard.finalize(), "[SILENT]")

    def test_rejects_invalid_coverage_events_and_urls(self):
        mutations = [
            lambda r: r["coverage"].pop(),
            lambda r: r["coverage"].append(copy.deepcopy(r["coverage"][0])),
            lambda r: r.update(coverage_complete=True),
            lambda r: r["coverage"][0].update(status="complete", gaps=[] ,checked_urls=[]),
            lambda r: r["events"][0].update(primary_evidence_urls=[]),
            lambda r: r["events"][0].update(primary_evidence_urls=["javascript:alert(1)"]),
            lambda r: r["events"][0].update(primary_evidence_urls=["https://token@example.com/"]),
            lambda r: r["events"][0].update(primary_evidence_urls=["https://other.example/news"]),
            lambda r: r["events"][0].update(company="Unwatched"),
            lambda r: r["events"][0].update(category="invented"),
            lambda r: r["events"][0].update(event_date="2027-01-01"),
            lambda r: r["events"][0].update(event_date="2026-09-12T10:00:00"),
            lambda r: r["events"].append(copy.deepcopy(r["events"][0])),
            lambda r: r.update(run_id="old-run"),
            lambda r: r.update(window_start="2000-01-01T00:00:00Z"),
            lambda r: r.update(coverage_complete="true"),
            lambda r: r.update(errors=[{"error": "not text"}]),
            lambda r: r.update(greeting="g" * 161),
            lambda r: r.update(events=None),
        ]
        guard = self.begin()
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                record = self.record(guard)
                mutate(record)
                self.save(guard, record)
                with self.assertRaises(ValueError):
                    guard.finalize()

    def test_coverage_rejects_unknown_or_duplicate_company_rows(self):
        coverage = self.record(self.begin())["coverage"]
        coverage[0]["company"] = "Unwatched"
        with self.assertRaisesRegex(ValueError, "unknown or duplicate coverage company"):
            self.module.validate_coverage(coverage, {"BetterMe", "Simple"})

    def test_follow_up_is_validated_and_rendered(self):
        guard = self.begin()
        record = self.record(guard)
        record["events"][0]["follow_up"] = "Check next quarter"
        self.save(guard, record)
        self.assertIn("Follow-up: Check next quarter", guard.finalize())

    def test_nonhashable_json_fields_become_validation_errors(self):
        guard = self.begin()
        record = self.record(guard)
        record["coverage"][0]["status"] = []
        self.save(guard, record)
        with self.assertRaisesRegex(self.module.ReportValidationError, "invalid JSON field type"):
            guard.finalize()

    def test_same_watch_concurrent_run_is_rejected_and_lock_released(self):
        guard = self.begin()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.begin()
        guard.close()
        self.assertIsNotNone(self.begin())

    def test_watch_snapshot_is_read_after_acquiring_publication_lock(self):
        updated = dict(self.watch, last_cutoff="2026-10-09T00:00:00Z")
        flock = self.module.fcntl.flock

        def previous_run_finishes_before_lock(fd, operation):
            self.watch_path.write_text(json.dumps(updated))
            flock(fd, operation)

        with mock.patch.object(self.module.fcntl, "flock", side_effect=previous_run_finishes_before_lock):
            guard = self.begin()
        guard.close()
        self.assertEqual(json.loads(self.watch_path.read_text()), updated)

    def test_unrelated_jobs_are_unchanged(self):
        for job in ({"id": "other"}, {"skills": ["ai_digest"]},
                    dict(self.job, no_agent=True)):
            before = copy.deepcopy(job)
            self.assertIsNone(self.module.begin_run(job, self.home))
            self.assertEqual(job, before)

    def test_invalid_watch_releases_lock_and_leaves_configuration_intact(self):
        changes = ({"language": "unknown"}, {"companies": []},
                   {"companies": [{"name": "Same"}, {"name": "Same"}]},
                   {"event_categories": "not a list"}, {"lookback_days": True})
        for change in changes:
            with self.subTest(change=change):
                invalid = dict(self.watch, **change)
                self.watch_path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    self.begin()
                self.assertEqual(json.loads(self.watch_path.read_text()), invalid)
        self.watch_path.write_text(json.dumps(self.watch))
        self.assertIsNotNone(self.begin())

    def test_missing_or_ambiguous_watch_and_symlinks_fail_closed(self):
        for prompt in ("No watch", "../fitness.json", self.job["prompt"] +
                       " ~/.hermes/competitor-watches/other.json"):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ValueError):
                    self.module.begin_run(dict(self.job, prompt=prompt), self.home)
        self.watch_path.unlink()
        outside = self.home / "outside.json"
        outside.write_text(json.dumps(self.watch))
        self.watch_path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.begin()

    def test_missing_oversized_symlink_and_nonobject_artifact_fail_closed(self):
        guard = self.begin()
        with self.assertRaises(ValueError):
            guard.finalize()
        for content in ('[]', '{bad json', ' ' * (self.module.MAX_JSON_BYTES + 1)):
            guard.artifact_path.write_text(content)
            with self.assertRaises(ValueError):
                guard.finalize()
        guard.artifact_path.unlink()
        guard.artifact_path.symlink_to(self.watch_path)
        with self.assertRaises(ValueError):
            guard.finalize()

    def test_commit_requires_validation_and_preserves_concurrent_personal_edits(self):
        guard = self.begin()
        with self.assertRaises(ValueError):
            guard.commit()
        updated = dict(self.watch, personal_setting=43)
        self.watch_path.write_text(json.dumps(updated))
        self.save(guard, self.record(guard))
        with self.assertRaisesRegex(ValueError, "changed"):
            guard.finalize()
        guard.close()
        self.assertEqual(json.loads(self.watch_path.read_text())["personal_setting"], 43)

    def test_instructions_override_model_watch_writes_without_changing_job(self):
        before = copy.deepcopy(self.job)
        guard = self.begin()
        text = guard.instructions("Extra user context")
        self.assertIn(str(guard.artifact_path), text)
        self.assertIn(guard.run_id, text)
        self.assertIn("Do not modify", text)
        self.assertEqual(self.job, before)

    def test_config_change_starts_new_coverage_window_and_keeps_delivery_history(self):
        guard = self.begin()
        record = self.record(guard, complete=True)
        record["events"][0]["event_date"] = NOW.date().isoformat()
        self.save(guard, record)
        guard.finalize()
        guard.commit()
        guard.close()
        watch = json.loads(self.watch_path.read_text())
        watch["lookback_days"] = 90
        self.watch_path.write_text(json.dumps(watch))
        again = self.begin()
        self.assertEqual((again.window_end - again.window_start).days, 90)
        record = self.record(again, complete=True)
        record["events"][0]["event_date"] = NOW.date().isoformat()
        self.save(again, record)
        self.assertEqual(again.finalize(), "[SILENT]")

    def test_checkpoint_write_failure_restores_public_state_and_releases_lock(self):
        guard = self.begin()
        self.save(guard, self.record(guard, complete=True))
        guard.finalize()
        write = self.module.write_json

        def fail_checkpoint(path, value):
            if path == guard.state_path:
                raise OSError("disk full")
            write(path, value)

        with mock.patch.object(self.module, "write_json", side_effect=fail_checkpoint):
            with self.assertRaises(OSError):
                guard.commit()
        guard.close()
        self.assertEqual(json.loads(self.watch_path.read_text()), self.watch)
        self.assertIsNotNone(self.begin())

    def test_personal_greeting_and_partial_errors_are_rendered_as_plain_text(self):
        guard = self.begin()
        record = self.record(guard, events=False)
        record.update(greeting="Elusenka, your fitness & wellness competitor brief",
                      errors=["Search service unavailable"])
        record["coverage"][0]["gaps"] = ["MEDIA:/private/file.txt [hidden](https://example.com)"]
        self.save(guard, record)
        report = guard.finalize()
        self.assertIn("Elusenka", report)
        self.assertIn("Search service unavailable", report)
        self.assertNotIn("MEDIA:", report)
        self.assertNotIn("[hidden]", report)

    def test_rejects_evidence_url_with_delivery_directives(self):
        for value in ("https://example.com/NEWS_CARDS:/tmp/cards.json",
                      "https://example.com/MEDIA:/tmp/file.txt"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.module.url(value)


if __name__ == "__main__":
    unittest.main()
