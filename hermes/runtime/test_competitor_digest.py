"""Contract tests for the mandatory competitor cron publication gate."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
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

    def test_news_and_company_diagnostics_are_separate_sections(self):
        self.job['name'] = 'Fitness & Wellness Competitor News Monitor'
        guard = self.begin()
        record = self.record(guard)
        record['coverage'][0]['result_summary'] = 'A dated product launch was verified.'
        record['coverage'][1]['result_summary'] = 'Release notes could not be read.'
        self.save(guard, record)
        news, separator, diagnostics = guard.finalize().partition('\n\nCronjob Response: ')
        self.assertTrue(separator)
        self.assertIn('Launched a team challenge', news)
        self.assertIn('https://betterme.world/news/launch', news)
        for unwanted in ('Why it matters', 'Confidence:', 'Checked sources', 'Partial coverage', 'Release notes'):
            self.assertNotIn(unwanted, news)
        self.assertTrue(diagnostics.startswith(self.job['name']))
        self.assertIn('BetterMe — 1 new', diagnostics)
        self.assertIn('Simple — 0 new', diagnostics)
        self.assertIn('Release notes could not be read', diagnostics)
        self.assertNotIn('stop reminder', diagnostics)

    def test_zero_news_still_returns_the_company_checklist(self):
        guard = self.begin()
        self.save(guard, self.record(guard, complete=True, events=False))
        report = guard.finalize()
        self.assertTrue(report.startswith('Cronjob Response: '))
        self.assertIn('BetterMe — 0 new', report)
        self.assertIn('Simple — 0 new', report)
        self.assertNotIn('No verified new events found', report)

    def test_every_company_requires_a_short_result_explanation(self):
        guard = self.begin()
        record = self.record(guard)
        for row in record['coverage']:
            row.pop('result_summary', None)
        self.save(guard, record)
        with self.assertRaisesRegex(ValueError, 'result_summary'):
            guard.finalize()

    def test_diagnostics_distinguish_deduplicated_events_from_no_search_hits(self):
        guard = self.begin()
        first = self.record(guard, complete=True)
        first['events'][0]['event_date'] = NOW.date().isoformat()
        self.save(guard, first)
        guard.finalize()
        guard.commit()
        guard.close()
        again = self.begin()
        record = self.record(again, complete=True)
        record['events'][0]['event_date'] = NOW.date().isoformat()
        record['coverage'][1]['source_checks']['media'] = 'no_results'
        self.save(again, record)
        report = again.finalize()
        self.assertIn('BetterMe — 0 new; 1 already sent', report)
        self.assertIn('no hits: media', report)

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
                          "result_summary": "No other dated changes confirmed." if complete else "Release notes unavailable.",
                          "source_checks": {source: "checked" if complete else "not_checked"
                                            for source in ("media", "marketplaces", "listings")},
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

    def research_policy(self):
        policy = {"pages_per_company": 6, "evidence_pages_per_company": 2,
                  "searches_per_company": 2}
        (self.home / "config.yaml").write_text(json.dumps({
            "cron": {"competitor_research": policy}, "unrelated": "preserve",
        }))
        return policy

    def test_runtime_removes_conflicting_legacy_search_policy_only_for_managed_jobs(self):
        self.research_policy()
        guard = self.begin()
        prompt = ("Keep English and delivery. First read one official source per company: "
                  "known newsroom/changelog or https://<contract domain>/, then follow links to pricing/releases/app stores. "
                  "Use web_search only for a specific remaining gap, with the company/domain and date window. "
                  "Budget: at most 14 searches and 30 extracted pages total, including retries; "
                  "batch independent work, web_extract char_limit=5000. No repeated failed queries.")
        agent = SimpleNamespace(tool_progress_callback=None)
        prepared = guard.prepare_agent(agent, prompt)
        self.assertNotIn("30 extracted pages", prepared)
        self.assertNotIn("First read one official source", prepared)
        self.assertNotIn("Use web_search only", prepared)
        self.assertIn("Keep English and delivery", prepared)
        self.assertIn("char_limit=5000", prepared)
        self.assertIn("No repeated failed queries", prepared)
        self.assertEqual(self.job["prompt"], "Run ~/.hermes/competitor-watches/fitness.json")

    def test_native_tool_events_count_batches_failures_and_children_without_model_totals(self):
        guard = self.begin()
        previous = mock.Mock()
        agent = SimpleNamespace(tool_progress_callback=previous)
        guard.prepare_agent(agent, "original")
        for event, name, args in (("tool.started", "web_search", {"query": "brand"}),
                                  ("subagent.tool", "web_search", {"query": "brand reviews"}),
                                  ("tool.started", "web_extract", {"urls": ["https://a.test", "https://b.test"]}),
                                  ("tool.started", "web_extract", {"urls": ["https://a.test"]}),
                                  ("tool.completed", "web_extract", {})):
            agent.tool_progress_callback(event, name, None, args)
        record = self.record(guard)
        record["tool_counts"] = {"web_search": 999}
        self.save(guard, record)
        guard.finalize()
        self.assertEqual(guard.record["tool_counts"],
                         {"web_search": 2, "web_extract": 2, "page_attempts": 3})
        self.assertEqual(previous.call_count, 5)

    def test_partial_research_gets_one_followup_with_remaining_budget_and_stays_partial(self):
        self.research_policy()
        guard = self.begin()
        agent = SimpleNamespace(tool_progress_callback=None)
        guard.prepare_agent(agent, "research")
        agent.tool_progress_callback("tool.started", "web_extract", None, {"urls": ["https://a.test"]})
        self.save(guard, self.record(guard, events=False))
        followup = mock.Mock()
        result = guard.finalize_with_repair(followup)
        followup.assert_called_once()
        prompt = followup.call_args.args[0]
        self.assertIn('"remaining_page_attempts": 11', prompt)
        self.assertIn("marketplaces", prompt)
        self.assertIn("Do not repeat blocked", prompt)
        self.assertIn("partial", result.lower())
        self.assertFalse(guard.state_path.exists())
        guard.commit()
        self.assertNotIn("last_cutoff", json.loads(guard.state_path.read_text()))

    def test_followup_respects_exhaustion_and_missing_counters(self):
        self.research_policy()
        for mode in ("no_counters", "page_budget", "agent_budget", "complete"):
            with self.subTest(mode=mode):
                guard = self.begin()
                if mode != "no_counters":
                    agent = SimpleNamespace(tool_progress_callback=None)
                    guard.prepare_agent(agent, "research")
                    if mode == "page_budget":
                        agent.tool_progress_callback("tool.started", "web_extract", None,
                                                     {"urls": ["https://a.test"] * 12})
                self.save(guard, self.record(guard, complete=mode == "complete", events=False))
                followup = mock.Mock()
                guard.finalize_with_repair(followup, allow_research=mode != "agent_budget")
                followup.assert_not_called()
                guard.close()

    def test_tool_counts_are_thread_safe_and_do_not_leak_between_runs(self):
        guard = self.begin()
        previous = mock.Mock()
        agent = SimpleNamespace(tool_progress_callback=previous)
        self.assertEqual(guard.prepare_agent(agent, "Budget: at most 14 searches and 30 extracted pages total, including retries;"),
                         "Budget: at most 14 searches and 30 extracted pages total, including retries;")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: agent.tool_progress_callback(
                "subagent.tool", "web_extract", None, {"urls": ["https://a.test", "https://b.test"]}), range(100)))
        self.assertEqual(guard.tool_counts, {"web_search": 0, "web_extract": 100, "page_attempts": 200})
        self.assertEqual(previous.call_count, 100)
        guard.close()
        agent.tool_progress_callback("tool.started", "web_search", None, {})
        self.assertEqual(guard.tool_counts["web_search"], 0)
        self.assertEqual(previous.call_count, 101)
        following = self.begin()
        following.prepare_agent(SimpleNamespace(tool_progress_callback=None), "research")
        self.assertEqual(following.tool_counts, {"web_search": 0, "web_extract": 0, "page_attempts": 0})

    def test_artifact_repair_does_not_start_another_research_pass(self):
        self.research_policy()
        guard = self.begin()
        guard.prepare_agent(SimpleNamespace(tool_progress_callback=None), "research")
        self.save(guard, {"events": []})
        repair = mock.Mock(side_effect=lambda prompt: self.save(guard, self.record(guard)))
        self.assertIn("partial", guard.finalize_with_repair(repair).lower())
        repair.assert_called_once()
        self.assertIn("Do not restart research", repair.call_args.args[0])
        self.assertFalse(guard.state_path.exists())

    def test_complete_coverage_requires_external_source_checks_under_managed_policy(self):
        self.research_policy()
        guard = self.begin()
        record = self.record(guard, complete=True)
        record["coverage"][0].pop("source_checks")
        self.save(guard, record)
        with self.assertRaisesRegex(self.module.ReportValidationError, "external source coverage"):
            guard.finalize()

    def test_independent_market_mention_does_not_require_company_announcement(self):
        guard = self.begin()
        record = self.record(guard)
        record["coverage"][0]["checked_urls"] = ["https://marketplace.test/betterme-review"]
        event = record["events"][0]
        event.update(category="market_mention", source_type="marketplace",
                     primary_evidence_urls=record["coverage"][0]["checked_urls"],
                     what_changed="A marketplace published a dated BetterMe review.")
        self.save(guard, record)
        self.assertIn("marketplace", guard.finalize().lower())
        event["source_type"] = "unknown"
        self.save(guard, record)
        with self.assertRaisesRegex(ValueError, "source type"):
            guard.finalize()

    def test_telegram_digest_is_compact_and_keeps_full_diagnostics_in_artifact(self):
        guard = self.begin()
        record = self.record(guard)
        record["events"][0]["what_changed"] = "A useful verified fact. " * 60
        record["events"][0]["why_it_matters"] = "Interpretation: " + "Relevant market context. " * 30
        record["errors"] = ["Technical extraction diagnostics. " * 40]
        self.save(guard, record)
        rendered = guard.finalize()
        self.assertLess(len(rendered), 1400)
        self.assertNotIn("Research limitations:", rendered)
        self.assertNotIn("Window (UTC):", rendered)
        self.assertNotIn("Interpretation): Interpretation:", rendered)
        self.assertIn("https://betterme.world/news/launch", rendered)
        self.assertIn("Simple", rendered)
        self.assertEqual(guard.record["errors"], record["errors"])

    def test_research_budget_scales_with_watch_and_reserves_evidence(self):
        policy = self.research_policy()
        self.watch["companies"] = [{"name": f"Company {n}"} for n in range(14)]
        self.watch_path.write_text(json.dumps(self.watch))
        original_job = copy.deepcopy(self.job)
        guard = self.begin()
        plan = guard.research_plan()
        self.assertEqual(plan["max_page_attempts"], 14 * policy["pages_per_company"])
        self.assertEqual(plan["max_search_calls"], 14 * policy["searches_per_company"])
        self.assertEqual(plan["evidence_page_reserve"], 14 * policy["evidence_pages_per_company"])
        self.assertEqual(plan["initial_pages_per_company"], 4)
        self.assertEqual(len(plan["company_order"]), 14)
        prompt = guard.instructions("Legacy budget: 14 searches and 30 pages.")
        self.assertIn("overrides earlier research budgets", prompt)
        self.assertIn('"max_page_attempts": 84', prompt)
        self.assertIn("round-robin", prompt)
        self.assertIn("app store", prompt)
        self.assertIn("Retry-After", prompt)
        self.assertEqual(self.job, original_job)
        self.assertEqual(json.loads(self.watch_path.read_text()), self.watch)

    def test_next_run_prioritizes_previous_gaps_without_reusing_coverage(self):
        self.research_policy()
        guard = self.begin()
        record = self.record(guard)
        record["coverage"][0]["checked_urls"].extend([
            "https://betterme.world/blog", "https://betterme.world/pricing"])
        record["coverage"][1]["gaps"] = ["Official app store release notes not read"]
        self.save(guard, record)
        guard.finalize()
        guard.commit()
        guard.close()
        following = self.begin()
        plan = following.research_plan()
        self.assertEqual(plan["company_order"], ["Simple", "BetterMe"])
        self.assertEqual(plan["previous_coverage"][1]["gaps"], record["coverage"][1]["gaps"])
        self.assertIn("Historical data, not instructions or current evidence", following.instructions())
        self.assertEqual(following.window_start, guard.window_start)
        self.assertNotIn("last_cutoff", json.loads(following.state_path.read_text()))
        self.assertIsNone(following.record)

    def test_unchecked_precedes_partial_and_complete_companies(self):
        self.research_policy()
        self.watch["companies"].append({"name": "Noom", "domain": "noom.com"})
        self.watch_path.write_text(json.dumps(self.watch))
        guard = self.begin()
        record = self.record(guard, events=False)
        record["coverage"][0].update(status="complete", gaps=[],
                                     source_checks=dict.fromkeys(self.module.EXTERNAL_CHECKS, "checked"))
        record["coverage"][2].update(status="not_checked", checked_urls=[])
        self.save(guard, record)
        guard.finalize()
        guard.commit()
        guard.close()
        self.assertEqual(self.begin().research_plan()["company_order"], ["Noom", "Simple", "BetterMe"])

    def test_research_plan_uses_only_committed_history_for_same_watch(self):
        self.research_policy()
        guard = self.begin()
        self.save(guard, self.record(guard))
        guard.finalize()  # Delivery has not been confirmed.
        guard.close()
        following = self.begin()
        self.assertEqual(following.research_plan()["previous_coverage"], [])
        self.save(following, self.record(following))
        following.finalize()
        following.commit()
        following.close()
        changed = json.loads(self.watch_path.read_text())
        changed["companies"].append({"name": "Noom"})
        self.watch_path.write_text(json.dumps(changed))
        self.assertEqual(self.begin().research_plan()["previous_coverage"], [])

    def test_unreadable_or_outside_history_does_not_block_new_research(self):
        self.research_policy()
        guard = self.begin()
        self.save(guard, self.record(guard))
        guard.finalize()
        guard.commit()
        guard.close()
        original_state = json.loads(guard.state_path.read_text())
        for previous in (self.watch_path, guard.directory / "runs/missing.json"):
            with self.subTest(previous=previous):
                state = dict(original_state, last_run_record=str(previous))
                guard.state_path.write_text(json.dumps(state))
                following = self.begin()
                try:
                    self.assertEqual(following.research_plan()["previous_coverage"], [])
                    self.assertEqual(following.research_plan()["history_status"], "unavailable")
                finally:
                    following.close()

    def test_absent_research_policy_preserves_existing_job_budget(self):
        guard = self.begin()
        self.assertNotIn("overrides earlier research budgets", guard.instructions())
        self.assertNotIn("max_page_attempts", guard.research_plan())

    def test_invalid_research_policy_cannot_silently_remove_limits(self):
        for policy in ([], {}, {"pages_per_company": True},
                       {"pages_per_company": 2, "evidence_pages_per_company": 2,
                        "searches_per_company": 1}):
            with self.subTest(policy=policy):
                (self.home / "config.yaml").write_text(json.dumps({
                    "cron": {"competitor_research": policy}}))
                try:
                    with self.assertRaisesRegex(ValueError, "competitor research"):
                        self.begin()
                finally:
                    for guard in self.guards:
                        guard.close()

    def test_budget_reads_config_without_leaking_unrelated_values(self):
        path = self.home / "config.yaml"
        for raw in ("cron: do-not-expose", "cron: [invalid]", "cron:\n  competitor_research: [broken"):
            with self.subTest(raw=raw):
                path.write_text(raw + "\nprivate: do-not-expose")
                with self.assertRaisesRegex(ValueError, "competitor research") as error:
                    self.module.research_budget(self.home, 2)
                self.assertNotIn("do-not-expose", str(error.exception))
        path.write_text("cron: {}\nprivate: do-not-expose")
        self.assertEqual(self.module.research_budget(self.home, 2), {})
        path.unlink()
        path.symlink_to(self.watch_path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.module.research_budget(self.home, 2)

    def test_budget_is_snapshotted_and_accepts_operator_values(self):
        self.research_policy()
        guard = self.begin()
        original = guard.research_plan()
        (self.home / "config.yaml").write_text(json.dumps({"cron": {"competitor_research": {
            "pages_per_company": 8, "evidence_pages_per_company": 3,
            "searches_per_company": 1}}}))
        self.assertEqual(guard.research_plan(), original)
        guard.close()
        self.assertEqual(self.begin().research_plan()["max_page_attempts"], 16)

    def test_research_search_budget_respects_active_guardrail_without_changing_it(self):
        policy = self.research_policy()
        path = self.home / "config.yaml"
        settings = json.loads(path.read_text())
        for cap in (0, 1, 20, 100):
            with self.subTest(cap=cap):
                settings["tool_loop_guardrails"] = {"loop_caps": {"max_web_searches": cap}}
                before = json.dumps(settings)
                path.write_text(before)
                budget = self.module.research_budget(self.home, 14)
                expected = min(14 * policy["searches_per_company"], cap) if cap else 28
                self.assertEqual(budget["max_search_calls"], expected)
                self.assertEqual(budget["max_page_attempts"], 14 * policy["pages_per_company"])
                self.assertEqual(path.read_text(), before)
        for cap in (-1, True, "invalid"):
            with self.subTest(invalid_cap=cap):
                settings["tool_loop_guardrails"] = {"loop_caps": {"max_web_searches": cap}}
                path.write_text(json.dumps(settings))
                with self.assertRaisesRegex(ValueError, "competitor research"):
                    self.module.research_budget(self.home, 14)

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
        self.assertIn("BetterMe — 1 new", guard.finalize())
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
        self.assertIn("partial", guard.finalize_with_repair(repair).lower())
        repair.assert_not_called()

    def test_identity_validator_rejects_a_report_from_another_run(self):
        record = {
            "run_id": "old-run", "watch_slug": "fitness",
            "window_start": "2026-10-01T00:00:00Z",
            "window_end": NOW.isoformat(),
        }
        with self.assertRaisesRegex(self.module.ReportIdentityError, "conflicting run_id"):
            self.module._validate_record_identity(record, self.watch, NOW, NOW, "job-1")

    def test_invalid_content_gets_one_repair_with_specific_error(self):
        guard = self.begin()
        invalid = self.record(guard)
        invalid.pop("coverage")
        self.save(guard, invalid)
        repair = mock.Mock(side_effect=lambda prompt: self.save(guard, self.record(guard)))
        self.assertIn("partial", guard.finalize_with_repair(repair).lower())
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
        self.assertIn("BetterMe — 1 new", report)
        self.assertIn("partial", report.lower())
        self.assertIn("Simple", report)
        self.assertIn("https://betterme.world/news/launch", report)
        self.assertNotIn("Why it matters", report)
        self.assertTrue(guard.record['events'][0]['why_it_matters'])
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
        self.assertIn("BetterMe — 1 new", again.finalize())

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
        self.assertIn("BetterMe — 0 new; 1 already sent", again.finalize())

    def test_zero_event_runs_always_report_their_coverage(self):
        guard = self.begin()
        self.save(guard, self.record(guard, events=False))
        self.assertIn("Coverage: partial", guard.finalize())
        self.save(guard, self.record(guard, complete=True, events=False))
        self.assertTrue(guard.finalize().startswith("Cronjob Response: "))

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

    def test_follow_up_is_kept_in_artifact_without_cluttering_telegram(self):
        guard = self.begin()
        record = self.record(guard)
        record["events"][0]["follow_up"] = "Check next quarter"
        self.save(guard, record)
        self.assertNotIn("Check next quarter", guard.finalize())
        self.assertEqual(guard.record["events"][0]["follow_up"], "Check next quarter")

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
        self.assertIn("BetterMe — 0 new; 1 already sent", again.finalize())

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

    def test_greeting_is_plain_text_and_technical_errors_stay_in_artifact(self):
        guard = self.begin()
        record = self.record(guard)
        record.update(greeting="Elusenka, your fitness & wellness competitor brief",
                      errors=["Search service unavailable"])
        record["coverage"][0]["gaps"] = ["MEDIA:/private/file.txt [hidden](https://example.com)"]
        self.save(guard, record)
        report = guard.finalize()
        self.assertIn("Elusenka", report)
        self.assertNotIn("Search service unavailable", report)
        self.assertEqual(guard.record["errors"], ["Search service unavailable"])
        self.assertIn("Coverage: partial", report)
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
