"""Third reviewer wiring, failover and independent publication."""
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import yaml
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).parent))
import observable_review as observer
import pr_review_context as context


class ObservableReviewTests(unittest.TestCase):
    def test_settled_observable_thread_is_suppressed_without_affecting_thread_ownership(self):
        finding = context.ReviewFinding("P2", "app.py", "RIGHT", 12, "Missing checkout validation",
                                        "Invalid checkout permits arbitrary writes", "Validate checkout before writing")
        thread = context.ReviewThread("settled", "app.py", "RIGHT", 99, 12, True, True,
            (context.ReviewComment("comment", 7, "github-actions[bot]",
             "<!-- observable-inline:" + "a"*40 + ":123:1:id -->\n" + context._finding_text(finding)),), resolved=True)
        with mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[]), mock.patch.object(context, "_fetch_review_threads", return_value=[thread]):
            new, notes = observer._new_publication_findings([finding], "owner/repo", "1", "key")
        self.assertEqual(new, [])
        self.assertIn("Previously settled: 1", "\n".join(notes))
        self.assertFalse(context.is_machine_thread(thread))

    def test_new_finding_is_not_suppressed_and_extensions_stay_in_summary(self):
        finding = context.ReviewFinding("P2", "app.py", "RIGHT", 12, "Missing checkout validation",
                                        "Invalid checkout permits arbitrary writes", "Validate checkout before writing")
        unrelated = context.ReviewThread("existing", "app.py", "RIGHT", 99, 99, False, True,
            (context.ReviewComment("comment", 7, "human", "Arithmetic overflow in addition"),))
        with mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[unrelated]), mock.patch.object(context, "_fetch_review_threads", return_value=[]):
            new, notes = observer._new_publication_findings([finding], "owner/repo", "1", "key")
        self.assertEqual(new, [finding])
        self.assertEqual(notes, [])
        with mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[replace(unrelated, line=12)]), mock.patch.object(context, "_fetch_review_threads", return_value=[]):
            new, notes = observer._new_publication_findings([finding], "owner/repo", "1", "key")
        self.assertEqual(new, [])
        self.assertIn("#discussion_r7", "\n".join(notes))
        self.assertIn(finding.impact, "\n".join(notes))

    def test_repeated_finding_has_one_thread_but_fresh_run_summaries(self):
        finding = {"severity":"P2", "path":"app.py", "side":"RIGHT", "line":12,
                   "title":"Missing checkout validation", "impact":"Invalid checkout permits arbitrary writes",
                   "fix":"Validate checkout before writing"}
        threads = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.json"
            path.write_text(json.dumps({"status":"success", "attempts":[], "result":{
                "summary":"Checked", "findings":[finding], "thread_verdicts":[]}}))

            def create(*args):
                threads.append(context.ReviewThread("thread", args[4], args[5], args[6], args[6], False, True,
                    (context.ReviewComment("comment", 7, "github-actions[bot]", args[7]),)))

            with (
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY":"owner/repo", "PR_NUMBER":"1",
                    "BASE_SHA":"a"*40, "HEAD_SHA":"b"*40, "GITHUB_RUN_ID":"123",
                    "GITHUB_RUN_ATTEMPT":"1", "GITHUB_TOKEN":"test-key"}, clear=True),
                mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
                mock.patch.object(context, "changed_diff_lines", return_value={"LEFT":set(), "RIGHT":{12}}),
                mock.patch.object(context, "_review_already_posted", return_value=False),
                mock.patch.object(context, "fetch_unresolved_review_threads", side_effect=lambda *_: list(threads)),
                mock.patch.object(context, "_fetch_review_threads", return_value=[]),
                mock.patch.object(context, "create_inline_comment", side_effect=create) as inline,
                mock.patch.object(context, "_request_json", return_value={}) as post,
            ):
                observer.publish(path)
                os.environ["GITHUB_RUN_ATTEMPT"] = "2"
                observer.publish(path)
                os.environ.update(HEAD_SHA="c"*40, GITHUB_RUN_ID="124", GITHUB_RUN_ATTEMPT="1")
                threads[0] = replace(threads[0], line=99, outdated=True)
                observer.publish(path)
            self.assertEqual(inline.call_count, 1)
            self.assertEqual(post.call_count, 3)
            self.assertIn("Already reported", post.call_args.args[3]["body"])

    def test_prompt_uses_numbered_diff_and_read_only_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/".github").mkdir()
            (root/".github/REVIEWER.md").write_text("Review proven regressions only.")
            (root/"sonar-review-context.json").write_text('{"issues":[]}')
            with mock.patch.object(observer, "_git", side_effect=["@@ -1 +1 @@\n-old\n+new\n", "app.py\n"]), mock.patch.object(observer.runner, "tracked_files", return_value={"app.py"}):
                chunks, files = observer.prepare_prompt(root, "a"*40, "b"*40)
            self.assertEqual(len(chunks), 1)
            self.assertEqual(files, {"app.py", ".ci-observable-review.diff"})
            prompt = chunks[0]["prompt"]
            self.assertIn("thread_verdicts must be []", prompt)
            self.assertIn("unreviewed scope", prompt)
            self.assertIn("Advisory Sonar", prompt)
            self.assertIn("chunk 1 of 1", prompt)
            self.assertIn("+new", (root/".ci-observable-review.diff").read_text())
            with mock.patch.object(observer, "_git", return_value="some diff"), self.assertRaisesRegex(observer.runner.ReviewFailure, "reserved_diff_path_exists"):
                observer.prepare_prompt(root, "a"*40, "b"*40)

    def test_invalid_limits_are_not_silently_accepted(self):
        for value in ("0", "-1", "NaN", "99999"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CLAUDE_REVIEW_MAX_TURNS":value}):
                with self.assertRaisesRegex(observer.runner.ReviewFailure, "invalid_review_limit"):
                    observer._limit("CLAUDE_REVIEW_MAX_TURNS", 3, 10)

    def test_identical_model_on_different_provider_is_a_distinct_route(self):
        with mock.patch.dict(os.environ, {
            "CLAUDE_REVIEW_PROVIDER": "openrouter", "CLAUDE_REVIEW_MODEL": "same-model",
            "CLAUDE_REVIEW_FALLBACK_PROVIDER": "nous", "CLAUDE_REVIEW_FALLBACK_MODEL": "same-model",
        }, clear=True):
            self.assertEqual(len(observer.routes()), 2)
        with mock.patch.dict(os.environ, {
            "CLAUDE_REVIEW_PROVIDER": "openrouter", "CLAUDE_REVIEW_MODEL": "same-model",
            "CLAUDE_REVIEW_FALLBACK_MODEL": "same-model",
        }, clear=True):
            self.assertEqual(len(observer.routes()), 1)

    def test_transient_failures_retry_then_start_fresh_fallback(self):
        report, calls, pauses, code, output = self.attempts([
            observer.runner.ReviewTimeout("inactivity_timeout: details"),
            observer.runner.ReviewFailure("invalid_result"), {},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "success")
        self.assertEqual([c.kwargs["model"] for c in calls], ["primary", "primary", "backup"])
        self.assertEqual(len(pauses), 1)
        self.assertEqual([a["outcome"] for a in report["attempts"]],
                         ["inactivity_timeout", "invalid_result", "valid_json"])
        self.assertNotIn("secret-key", output)

    def test_auth_errors_skip_useless_retry_and_report_failure(self):
        report, calls, pauses, code, output = self.attempts([
            observer.runner.ReviewFailure("http_401"), observer.runner.ReviewFailure("http_403"),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(report["reason"], "all_attempts_failed")
        self.assertEqual(len(calls), 2)
        self.assertFalse(pauses)
        self.assertIn("::error::", output)

    def test_rate_limits_back_off_and_do_not_expose_provider_body(self):
        report, calls, pauses, code, output = self.attempts([
            observer.runner.RateLimitFailure({"scope":"provider", "quota":"unknown", "retry_after_seconds":45}),
            observer.runner.ReviewFailure("http_429"),
            observer.runner.ReviewFailure("http_429"), {},
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 4)
        self.assertGreaterEqual(sum(c.args[0] for c in pauses), 45 + 60)
        self.assertEqual(report["attempts"][0]["retry_wait_seconds"], 45)
        self.assertIn("retry_wait", output)
        self.assertIn("scope=provider", output)
        self.assertNotIn("secret-key", output)
        self.assertIn("Retry wait", observer.diagnostics(report))

    def test_long_retry_after_is_not_shortened_to_budget(self):
        report, calls, pauses, code, _ = self.attempts([
            observer.runner.RateLimitFailure({"scope":"provider", "quota":"unknown", "retry_after_seconds":3600}),
            observer.runner.ReviewFailure("http_429"), observer.runner.ReviewFailure("http_429"),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(report["reason"], "rate_limited")
        self.assertEqual([c.kwargs["model"] for c in calls], ["primary", "backup", "backup"])
        self.assertEqual(report["attempts"][0]["retry_decision"], "wait_exceeds_budget")
        self.assertLessEqual(sum(c.args[0] for c in pauses), observer.RATE_LIMIT_WAIT_BUDGET)

    def test_daily_free_quota_skips_same_provider_free_fallback(self):
        report, calls, pauses, code, output = self.attempts([
            observer.runner.RateLimitFailure({"scope":"platform", "quota":"free_daily"}),
        ], models=("primary:free", "backup:free"))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["reason"], "rate_limited")
        self.assertFalse(pauses)
        self.assertEqual(report["skipped_routes"][0]["reason"], "free_daily_quota")
        self.assertIn("route_skipped", output)
        self.assertIn("Skipped route: fallback; reason: free_daily_quota.", observer.diagnostics(report))

    def test_daily_free_quota_allows_independent_provider_fallback(self):
        report, calls, pauses, code, _ = self.attempts([
            observer.runner.RateLimitFailure({"scope":"platform", "quota":"free_daily"}), {},
        ], models=("primary:free", "backup:free"), fallback_provider="nous")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertFalse(pauses)

    def test_known_free_quota_skips_fallback_retry_after_incomplete_result(self):
        report, calls, pauses, code, output = self.attempts([
            observer.runner.RateLimitFailure({"scope": "platform", "quota": "free_daily"}),
            observer.runner.ReviewFailure("provider_incomplete_result"),
        ], models=("primary:free", "backup"), fallback_provider="ollama-cloud")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(pauses)
        self.assertEqual(report["attempts"][-1]["retry_decision"], "quota_known_incomplete_skip")
        self.assertIn("quota_known_incomplete_skip", output)

    def test_incomplete_retry_uses_short_prompt(self):
        prompts = []
        state = {"free_daily": False, "blocked_providers": set()}

        def attempt(*args, **kwargs):
            prompts.append(args[2])
            report["attempts"].append({
                "outcome": "provider_incomplete_result" if len(prompts) == 1 else "all_attempts_failed"
            })
            return False

        report = {"status": "failed", "attempts": []}
        routes = [{"provider": "ollama-cloud", "role": "fallback", "model": "backup",
                   "key": "k", "endpoint": "https://example.test"}]
        with mock.patch.object(observer, "routes", return_value=routes), \
                mock.patch.object(observer, "_single_attempt", side_effect=attempt), \
                mock.patch.object(observer, "_retry_after_attempt", return_value=True):
            observer.review_attempts(Path("."), "full review prompt", set(), report,
                                     Path("report.json"), "a" * 40, "b" * 40, state=state)
        self.assertEqual(len(prompts), 2)
        self.assertIn("reread the full diff", prompts[1])

    def test_free_daily_quota_state_is_shared_between_chunks(self):
        report = {"status": "failed", "attempts": []}
        chunks = [
            {"index": 1, "prompt": "p", "diff": "+a\n"},
            {"index": 2, "prompt": "p", "diff": "+b\n"},
        ]
        observed_states = []

        def fake_attempt(workspace, prompt, files, chunk_report, report_path, base, head, index, state):
            observed_states.append(state)
            if index == 1:
                state["free_daily"] = True
            chunk_report.update(status="success", result={"summary": "checked", "findings": []})
            return 0

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(observer, "review_attempts", side_effect=fake_attempt):
            root = Path(directory)
            self.assertEqual(
                observer.review_chunks(root, chunks, set(), report, root / "report.json", "a" * 40, "b" * 40),
                0,
            )

        self.assertEqual(len(observed_states), 2)
        self.assertTrue(observed_states[1]["free_daily"])
        self.assertEqual(
            observer._route_skip_reason(
                {"provider": "openrouter", "model": "primary:free"}, observed_states[1]
            ),
            "free_daily_quota",
        )

    def test_rate_limit_wait_can_be_cancelled(self):
        with mock.patch.object(observer.time, "sleep", side_effect=KeyboardInterrupt), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                observer._retry_wait(60)

    def test_platform_retry_after_blocks_same_provider_fallback(self):
        report, calls, pauses, code, _ = self.attempts([
            observer.runner.RateLimitFailure({"scope":"platform", "quota":"unknown", "retry_after_seconds":3600}),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(pauses)
        self.assertEqual(report["skipped_routes"][0]["reason"], "platform_rate_limit")
        self.assertIn("Skipped route: fallback; reason: platform_rate_limit.", observer.diagnostics(report))

    def test_route_skip_reason_respects_provider_and_quota_scope(self):
        cases = (
            ("openrouter", "model:free", False, set(), None),
            ("openrouter", "model:free", True, set(), "free_daily_quota"),
            ("openrouter", "paid-model", True, set(), None),
            ("nous", "model:free", True, {"openrouter"}, None),
            ("openrouter", "paid-model", False, {"openrouter"}, "platform_rate_limit"),
            ("openrouter", "model:free", True, {"openrouter"}, "free_daily_quota"),
        )
        for provider, model, free_daily, blocked, expected in cases:
            with self.subTest(provider=provider, model=model, free_daily=free_daily, blocked=blocked):
                state = {"free_daily": free_daily, "blocked_providers": blocked}
                route = {"provider": provider, "model": model}
                self.assertEqual(observer._route_skip_reason(route, state), expected)

    def test_retry_wait_budget_is_shared_and_cannot_be_exceeded(self):
        report, calls, pauses, code, _ = self.attempts([
            observer.runner.RateLimitFailure({"scope":"provider", "quota":"unknown", "retry_after_seconds":100}),
            observer.runner.ReviewFailure("http_429"),
            observer.runner.RateLimitFailure({"scope":"provider", "quota":"unknown", "retry_after_seconds":30}),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum(c.args[0] for c in pauses), 100)
        self.assertEqual(report["attempts"][-1]["retry_decision"], "wait_exceeds_budget")

    def test_rate_limit_terminal_route_outcomes_override_earlier_transient_failure(self):
        report, _, _, code, _ = self.attempts([
            observer.runner.ReviewFailure("invalid_result"), observer.runner.ReviewFailure("http_429"),
            observer.runner.ReviewFailure("http_401"),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(report["reason"], "rate_limited")

    def test_all_429_stops_real_attempt_loop_after_first_chunk(self):
        report = {"status":"failed", "attempts":[]}
        chunks = [{"index": i, "prompt":"p", "diff":"+a\n"} for i in range(1, 5)]
        routes = [{"provider":"openrouter", "role": role, "model": role + ":free", "key":"k", "endpoint":"https://example.test"}
                  for role in ("primary", "fallback")]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(observer, "routes", return_value=routes), \
                mock.patch.object(observer.runner, "run_review", side_effect=observer.runner.ReviewFailure("http_429")) as run, \
                mock.patch.object(observer.time, "sleep") as sleep, redirect_stdout(io.StringIO()):
            root = Path(directory)
            self.assertEqual(observer.review_chunks(root, chunks, set(), report, root/"report.json", "a"*40, "b"*40), 1)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(report["skipped_chunks"], 3)
        self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), 90)

    def test_rate_limit_stops_later_chunks_and_reports_partial_coverage(self):
        for first_success in (False, True):
            with self.subTest(first_success=first_success), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report = {"status":"failed", "attempts":[]}
                chunks = [{"index": i, "prompt":"p", "diff":"+a\n"} for i in range(1, 5)]
                def attempt(workspace, prompt, files, chunk_report, report_path, base, head, index, state):
                    if first_success and index == 1:
                        chunk_report.update(status="success", result={"summary":"Checked first chunk", "findings":[]})
                        return 0
                    chunk_report.update(status="failed", reason="rate_limited")
                    return 1
                with mock.patch.object(observer, "review_attempts", side_effect=attempt) as run, redirect_stdout(io.StringIO()):
                    code = observer.review_chunks(root, chunks, set(), report, root/"report.json", "a"*40, "b"*40)
                self.assertEqual(code, 1)
                self.assertEqual(run.call_count, 1 + first_success)
                self.assertEqual(report["completed_chunks"], int(first_success))
                self.assertEqual(report["skipped_chunks"], 3 - first_success)
                self.assertEqual(report["reason"], "rate_limited")
                self.assertNotIn("result", report)
                details = observer.diagnostics(report)
                self.assertIn("not a clean review", details)
                self.assertIn(f"Validated chunks: {int(first_success)}/4", details)

    def attempts(self, outcomes, models=("primary", "backup"), fallback_provider="openrouter"):
        report = {"status": "failed", "attempts": []}
        routes = [{"provider":"openrouter", "role":role, "model":model,
                   "key":"secret-key", "endpoint":"https://example.test/v1/messages"}
                  for role, model in zip(("primary", "fallback"), models)]
        routes[1]["provider"] = fallback_provider
        result = json.dumps({"summary":"Reviewed", "findings":[], "thread_verdicts":[]})
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(observer, "routes", return_value=routes),
            mock.patch.object(observer.runner, "run_review", side_effect=outcomes) as run,
            mock.patch.object(observer.publisher, "_claude_final_response", return_value=result),
            mock.patch.object(observer.publisher, "_normalized_claude_result", return_value=result),
            mock.patch.object(observer.time, "sleep") as sleep,
            redirect_stdout(output),
        ):
            root = Path(directory)
            code = observer.review_attempts(root, "prompt", set(), report, root/"report.json", "a"*40, "b"*40)
        return report, run.call_args_list, sleep.call_args_list, code, output.getvalue()

    def test_setup_failure_still_writes_a_report_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory)/"report.json"
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(observer.run(report_path), 1)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "failed")
            self.assertIn("not a clean review", observer.diagnostics(report))

    def test_parallel_jobs_have_same_predecessor_and_owner_guards(self):
        workflow = yaml.safe_load((Path(__file__).parents[1]/"workflows/pr-ai-review.yml").read_text())
        base = workflow["jobs"]["claude-code-plugin-review"]
        third = workflow["jobs"]["observable-claude-review"]
        self.assertEqual(third["needs"], base["needs"])
        self.assertEqual(third["if"], base["if"])
        for step in third["steps"]:
            if "observable_review.py run" in step.get("run", ""):
                self.assertNotIn("GITHUB_TOKEN", step.get("env", {}))
        self.assertTrue(any("observable_review.py publish" in s.get("run", "") for s in third["steps"]))

    def test_routes_ignore_direct_configuration(self):
        with mock.patch.dict(os.environ, {
            "CLAUDE_REVIEW_PROVIDER": "openrouter", "CLAUDE_REVIEW_MODEL": "primary:free",
            "CLAUDE_REVIEW_FALLBACK_MODEL": "backup:free",
            "DIRECT_REVIEW_PROVIDER": "nvidia", "DIRECT_REVIEW_FALLBACK_PROVIDER": "nvidia",
            "OPENROUTER_API_KEY": "test-key",
        }, clear=True):
            routes = observer.routes()
        self.assertEqual([r["provider"] for r in routes], ["openrouter", "openrouter"])
        self.assertEqual([r["model"] for r in routes], ["primary:free", "backup:free"])

    def test_routes_rejects_missing_model_and_bad_endpoint(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_PROVIDER": "openrouter"}, clear=True):
            with self.assertRaisesRegex(observer.runner.ReviewFailure, "missing_primary_model"):
                observer.routes()
        with mock.patch.dict(os.environ, {
            "CLAUDE_REVIEW_PROVIDER": "openrouter", "CLAUDE_REVIEW_MODEL": "m",
            "OPENROUTER_API_KEY": "k",
        }, clear=True), mock.patch.object(observer.transport, "messages_url", return_value="http://insecure/v1/messages"):
            routes = observer.routes()
        self.assertEqual(routes[0]["error"], "unsupported_provider_route")

    def test_routes_marks_missing_key(self):
        with mock.patch.dict(os.environ, {
            "CLAUDE_REVIEW_PROVIDER": "openrouter", "CLAUDE_REVIEW_MODEL": "m",
        }, clear=True), mock.patch.object(observer.transport, "provider_config", return_value=("openrouter", "", "x")):
            routes = observer.routes()
        self.assertEqual(routes[0]["error"], "missing_provider_key")

    def test_prepare_prompt_rejects_empty_and_oversized_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/".github").mkdir()
            (root/".github/REVIEWER.md").write_text("policy")
            with mock.patch.object(observer, "_git", side_effect=["   \n", ""]), \
                    self.assertRaisesRegex(observer.runner.ReviewFailure, "empty_or_oversized_diff"):
                observer.prepare_prompt(root, "a"*40, "b"*40)
            with mock.patch.object(observer, "_git", side_effect=["x" * (2 * 1024 * 1024 + 1), ""]), \
                    self.assertRaisesRegex(observer.runner.ReviewFailure, "empty_or_oversized_diff"):
                observer.prepare_prompt(root, "a"*40, "b"*40)

    def test_prepare_prompt_skips_oversized_sonar_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/".github").mkdir()
            (root/".github/REVIEWER.md").write_text("policy")
            (root/"sonar-review-context.json").write_text("x" * 25000)
            with mock.patch.object(observer, "_git", side_effect=["+new\n", "app.py\n"]), \
                    mock.patch.object(observer.runner, "tracked_files", return_value={"app.py"}):
                chunks, files = observer.prepare_prompt(root, "a"*40, "b"*40)
            self.assertNotIn("Advisory Sonar", chunks[0]["prompt"])

    def test_run_success_and_review_failure_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            with mock.patch.dict(os.environ, {"BASE_SHA": "a"*40, "HEAD_SHA": "b"*40}, clear=True), \
                    mock.patch.object(observer, "_confine_report_path", return_value=report_path), \
                    mock.patch.object(observer, "prepare_prompt", side_effect=observer.runner.ReviewFailure("empty_or_oversized_diff")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(observer.run(report_path), 1)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["reason"], "empty_or_oversized_diff")

    def test_run_setup_failure_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            with mock.patch.dict(os.environ, {"BASE_SHA": "a"*40, "HEAD_SHA": "b"*40}, clear=True), \
                    mock.patch.object(observer, "_confine_report_path", return_value=report_path), \
                    mock.patch.object(observer, "prepare_prompt", side_effect=RuntimeError("boom")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(observer.run(report_path), 1)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["reason"], "review_setup_failed")

    def test_run_writes_step_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            summary_path = root / "summary.md"
            with mock.patch.dict(os.environ, {"BASE_SHA": "a"*40, "HEAD_SHA": "b"*40, "GITHUB_STEP_SUMMARY": str(summary_path)}, clear=True), \
                    mock.patch.object(observer, "_confine_report_path", return_value=report_path), \
                    mock.patch.object(observer, "prepare_prompt", side_effect=observer.runner.ReviewFailure("empty_or_oversized_diff")), \
                    redirect_stdout(io.StringIO()):
                observer.run(report_path)
            self.assertIn("ObservableMessagesReview", summary_path.read_text())
            self.assertIn("### Technical metadata", summary_path.read_text())
            self.assertIn("<details>\n<summary>Execution history</summary>\n\n", summary_path.read_text())

    def test_confine_report_path_rejects_escape(self):
        with mock.patch.object(observer.os, "environ", {}, create=True):
            outside = Path("/definitely-outside-workspace/report.json")
            with self.assertRaisesRegex(RuntimeError, "scratch"):
                observer._confine_report_path(outside)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(observer.os, "environ", {"RUNNER_TEMP": str(root)}, create=True), \
                    mock.patch.object(observer, "Path", wraps=Path):
                inside = root / "inside.json"
                self.assertEqual(observer._confine_report_path(inside), inside)

    def test_confine_report_path_resolves_symlink(self):
        # realpath must resolve a symlink to its target before the base check,
        # so a symlink cannot smuggle a path past confinement.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.json").write_text("{}")
            link = root / "link.json"
            link.symlink_to(root / "real.json")
            with mock.patch.object(observer.os, "environ", {"RUNNER_TEMP": str(root)}, create=True):
                resolved = observer._confine_report_path(link)
            self.assertEqual(resolved, (root / "real.json").resolve())

    def test_load_report_rejects_missing_and_oversized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                observer._load_report(root / "missing.json")
            big = root / "big.json"
            big.write_text("x" * (observer.MAX_REPORT_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "oversized"):
                observer._load_report(big)

    def test_publish_context_rejects_bad_run_identity(self):
        with mock.patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "1",
            "BASE_SHA": "a"*40, "HEAD_SHA": "b"*40, "GITHUB_RUN_ID": "not-a-number",
        }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "run identity"):
                observer._publish_context()

    def test_publish_one_finding_unresolvable_anchor(self):
        finding = context.ReviewFinding("P2", "app.py", "RIGHT", 1, "t", "i", "f")
        with mock.patch.object(context, "_review_already_posted", return_value=False), \
                mock.patch.object(context, "create_inline_comment", side_effect=context.GitHubRequestError("unresolvable")), \
                mock.patch.object(context, "_is_unresolvable_inline_anchor", return_value=True):
            detail = observer._publish_one_finding(finding, "owner/repo", "1", "tok", "h"*40, "1", "1")
        self.assertIn("rejected the inline anchor", detail)

    def test_success_lines_rejects_verdicts(self):
        report = {"status": "success", "result": {"summary": "s", "findings": [], "thread_verdicts": [{"x": 1}]}}
        with mock.patch.object(context, "_validated_claude_result", return_value=("summary", [], [{"thread_id": "t"}])):
            with self.assertRaisesRegex(RuntimeError, "cannot publish thread verdicts"):
                observer._success_lines(report, "owner/repo", "1", "tok", "a"*40, "b"*40, "1", "1")

    def test_main_dispatch(self):
        with mock.patch.object(sys, "argv", ["observable_review.py", "run", "--report", "/tmp/x.json"]), \
                mock.patch.object(observer, "run", return_value=0) as run:
            self.assertEqual(observer.main(), 0)
            run.assert_called_once()
        with mock.patch.object(sys, "argv", ["observable_review.py", "publish", "--report", "/tmp/x.json"]), \
                mock.patch.object(observer, "publish") as pub:
            self.assertEqual(observer.main(), 0)
            pub.assert_called_once()

    def test_main_propagates_publish_failure(self):
        with mock.patch.object(sys, "argv", ["observable_review.py", "publish", "--report", "/tmp/x.json"]), \
                mock.patch.object(observer, "publish", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                observer.main()

    def test_module_main_guards_publish_failure(self):
        # The __main__ guard catches any publish failure and exits non-zero.
        script = observer.__file__
        proc = subprocess.run(
            [sys.executable, script, "publish", "--report", "/nonexistent/report.json"],
            capture_output=True, text=True, cwd=Path(script).parent,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Observable review publication failed", proc.stderr)

    def test_main_guard_exits_zero_and_one(self):
        with mock.patch.object(observer, "main", return_value=0):
            with self.assertRaises(SystemExit) as ctx:
                observer._main_guard()
            self.assertEqual(ctx.exception.code, 0)
        with mock.patch.object(observer, "main", side_effect=RuntimeError("boom")), \
                redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                observer._main_guard()
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("publication failed", err.getvalue())

    def test_diagnostics_includes_attempt_rows(self):
        report = {"status": "failed", "attempts": [
            {"role": "primary", "number": 1, "chunk": 2, "provider": "p", "model": "m",
             "outcome": "http_401", "seconds": 1.5}],
            "reason": "all_attempts_failed"}
        text = observer.diagnostics(report)
        self.assertIn("| Chunk |", text)
        self.assertIn("| primary 1 | 2 |", text)
        self.assertIn("http_401", text)
        self.assertIn("not a clean review", text)

    def test_diagnostics_collapses_only_attempt_table(self):
        attempt = {"role": "primary", "number": 1, "chunk": 2, "provider": "p",
                   "model": "m", "outcome": "valid_json", "seconds": 1.5}
        for status in ("success", "failed"):
            for attempts in ([], [attempt]):
                with self.subTest(status=status, attempts=len(attempts)):
                    text = observer.diagnostics({"status": status, "attempts": attempts,
                                                 "reason": "all_attempts_failed"})
                    before, details = text.split("<details>\n", 1)
                    table, after = details.split("\n</details>", 1)
                    self.assertIn(f"Result: **{status}**", before)
                    self.assertIn("Execution: independent Messages API tool loop.", before)
                    self.assertTrue(table.startswith("<summary>Execution history</summary>\n\n"))
                    self.assertIn("| Attempt | Chunk | Provider | Model | Outcome | Seconds |", table)
                    self.assertNotIn("<details open", text)
                    self.assertEqual(text.count("<details>"), 1)
                    self.assertEqual(text.count("</details>"), 1)
                    if attempts:
                        self.assertIn("| primary 1 | 2 | p | m | valid_json | 1.5 |", table)
                    if status == "failed":
                        self.assertIn("not a clean review", after)
                        self.assertIn("Reason: all_attempts_failed", after)

    def test_git_runs_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertEqual(observer._git(root, "rev-parse", "--is-inside-work-tree").strip(), "true")

    def test_single_attempt_handles_route_error_and_filters_invalid_anchor(self):
        report = {"attempts": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = root / "exec.json"
            limits = {"max_turns": 2, "attempt_timeout_seconds": 60,
                      "inactivity_timeout_seconds": 30, "heartbeat_seconds": 30}
            route = {"provider": "p", "model": "m", "role": "primary", "error": "missing_provider_key",
                     "key": "", "endpoint": ""}
            with mock.patch.object(observer.time, "monotonic", side_effect=[0.0, 0.1]):
                ok = observer._single_attempt(route, 1, "prompt", root, set(), execution, report, "a"*40, "b"*40, limits)
            self.assertFalse(ok)
            self.assertEqual(report["attempts"][0]["outcome"], "missing_provider_key")

        # Invalid anchors are filtered by the shared normalizer.  A readable
        # review still succeeds, while diagnostics record what was discarded.
        report = {"attempts": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = root / "exec.json"
            route = {"provider": "p", "model": "m", "role": "primary", "key": "k", "endpoint": "https://e.test"}
            with mock.patch.object(observer.runner, "run_review", return_value={"turns": 1, "events": 1, "seconds": 1}), \
                    mock.patch.object(observer.publisher, "_claude_final_response", return_value='{"summary":"s","findings":[{"severity":"P2","path":"a.py","side":"RIGHT","line":1,"title":"t","impact":"i","fix":"f"}],"thread_verdicts":[]}'), \
                    mock.patch.object(observer.publisher, "_normalized_claude_result", return_value='{"summary":"s","findings":[],"thread_verdicts":[]}'), \
                    mock.patch.object(observer.time, "monotonic", side_effect=[0.0, 0.1]):
                ok = observer._single_attempt(route, 1, "prompt", root, set(), execution, report, "a"*40, "b"*40, limits)
            self.assertTrue(ok)
            self.assertEqual(report["attempts"][0]["outcome"], "valid_json_filtered")
            self.assertEqual(report["attempts"][0]["filtered_findings"], 1)
            self.assertEqual(report["result"]["findings"], [])

    def test_single_attempt_generic_exception(self):
        report = {"attempts": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = root / "exec.json"
            route = {"provider": "p", "model": "m", "role": "primary", "key": "k", "endpoint": "https://e.test"}
            limits = {"max_turns": 2, "attempt_timeout_seconds": 60,
                      "inactivity_timeout_seconds": 30, "heartbeat_seconds": 30}
            with mock.patch.object(observer.runner, "run_review", side_effect=ValueError("boom")), \
                    mock.patch.object(observer.time, "monotonic", side_effect=[0.0, 0.1]):
                ok = observer._single_attempt(route, 1, "prompt", root, set(), execution, report, "a"*40, "b"*40, limits)
            self.assertFalse(ok)
            self.assertEqual(report["attempts"][0]["outcome"], "validation_or_transport_error")

    def test_run_success_path_returns_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"

            def mark_success(workspace, chunks, files, report, report_path, base, head):
                # Mirror review_chunks: on success it marks the shared report dict.
                report.update(status="success", result={"summary": "done", "findings": [], "thread_verdicts": []})
                return 0

            with mock.patch.dict(os.environ, {"BASE_SHA": "a"*40, "HEAD_SHA": "b"*40}, clear=True), \
                    mock.patch.object(observer, "_confine_report_path", return_value=report_path), \
                    mock.patch.object(observer, "prepare_prompt", return_value=([{"index": 1, "total": 1, "prompt": "p", "diff": "+new\n"}], {"app.py"})), \
                    mock.patch.object(observer, "review_chunks", side_effect=mark_success), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(observer.run(report_path), 0)
            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "success")

    def test_new_publication_findings_empty(self):
        new, notes = observer._new_publication_findings([], "owner/repo", "1", "tok")
        self.assertEqual((new, notes), ([], []))

    def test_split_diff_chunks_bounds_each_chunk(self):
        # A diff with several file headers splits on boundaries; each chunk
        # stays under MAX_CHUNK_CHARS so a small model can finish it.
        diff = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n" + ("x" * observer.MAX_CHUNK_CHARS) + "\ndiff --git a/b.py b/b.py\n@@ -1 +1 @@\n-old\n+new\n"
        chunks = observer._split_diff_chunks(diff)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk) + 1, observer.MAX_CHUNK_CHARS + 2000)

    def test_split_diff_chunks_single_small_diff(self):
        diff = "@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(observer._split_diff_chunks(diff), ["@@ -1 +1 @@\n-old\n+new"])

    def test_review_chunks_aggregates_and_caps_findings(self):
        findings = [{"severity": "P2", "path": f"f{i}.py", "side": "RIGHT", "line": 1,
                     "title": "t", "impact": "i", "fix": "f"} for i in range(observer.MAX_FINDINGS + 3)]
        report = {"status": "failed", "attempts": []}
        chunks = [
            {"index": 1, "total": 2, "prompt": "p1", "diff": "+a\n"},
            {"index": 2, "total": 2, "prompt": "p2", "diff": "+b\n"},
        ]
        def fake_attempts(workspace, prompt, files, chunk_report, report_path, base, head, chunk_index, state):
            chunk_report.update(status="success",
                result={"summary": "ok", "findings": findings, "thread_verdicts": []})
            return 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / observer.runner.REVIEW_DIFF_PATH).write_text("placeholder\n")
            with mock.patch.object(observer, "review_attempts", side_effect=fake_attempts):
                code = observer.review_chunks(root, chunks, {"a.py"}, report,
                                              root / "report.json", "a"*40, "b"*40)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "success")
        self.assertEqual(len(report["result"]["findings"]), observer.MAX_FINDINGS)
        self.assertEqual(report["result"]["thread_verdicts"], [])
        self.assertEqual(len(report["attempts"]), 0)

    def test_review_chunks_fails_when_a_chunk_fails(self):
        report = {"status": "failed", "attempts": []}
        chunks = [{"index": 1, "total": 1, "prompt": "p", "diff": "+a\n"}]
        def fake_attempts(workspace, prompt, files, chunk_report, report_path, base, head, chunk_index, state):
            chunk_report.update(status="failed", reason="all_attempts_failed")
            return 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / observer.runner.REVIEW_DIFF_PATH).write_text("placeholder\n")
            with mock.patch.object(observer, "review_attempts", side_effect=fake_attempts), \
                    redirect_stdout(io.StringIO()):
                code = observer.review_chunks(root, chunks, {"a.py"}, report,
                                              root / "report.json", "a"*40, "b"*40)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "all_attempts_failed")

    def test_publish_one_finding_keeps_metadata_in_summary_only(self):
        finding = context.ReviewFinding("P2", "app.py", "RIGHT", 1, "t", "i", "f")
        with mock.patch.object(context, "_review_already_posted", return_value=True):
            detail = observer._publish_one_finding(finding, "owner/repo", "1", "tok", "h"*40, "1", "1")
        self.assertNotIn("Provider:", detail)
        self.assertNotIn("Model:", detail)

    def test_publish_one_finding_reraises_unresolvable(self):
        finding = context.ReviewFinding("P2", "app.py", "RIGHT", 1, "t", "i", "f")
        with mock.patch.object(context, "_review_already_posted", return_value=False), \
                mock.patch.object(context, "create_inline_comment", side_effect=context.GitHubRequestError("boom")), \
                mock.patch.object(context, "_is_unresolvable_inline_anchor", return_value=False):
            with self.assertRaises(context.GitHubRequestError):
                observer._publish_one_finding(finding, "owner/repo", "1", "tok", "h"*40, "1", "1")

    def test_observable_publication_cannot_close_claude_threads(self):
        report = {"status": "success", "attempts": [], "result": {
            "summary": "Independent review completed", "findings": [{
                "severity":"P2", "path":"app.py", "side":"RIGHT", "line":12,
                "title":"Bug", "impact":"Wrong result", "fix":"Correct value",
            }], "thread_verdicts": [],
        }}
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory)/"report.json"
            report_path.write_text(json.dumps(report))
            with (
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY":"owner/repo", "PR_NUMBER":"1",
                    "HEAD_SHA":"b"*40, "BASE_SHA":"a"*40, "GITHUB_RUN_ID":"123",
                    "GITHUB_TOKEN":"test-key"}, clear=True),
                mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
                mock.patch.object(context, "changed_diff_lines", return_value={"LEFT":set(), "RIGHT":{12}}),
                mock.patch.object(context, "_review_already_posted", return_value=False),
                mock.patch.object(context, "create_inline_comment") as inline,
                mock.patch.object(context, "_request_json", return_value={}) as post,
                mock.patch.object(context, "_process_thread_verdicts") as verdicts,
                mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[]),
                mock.patch.object(context, "_fetch_review_threads", return_value=[]),
            ):
                observer.publish(report_path)
            verdicts.assert_not_called()
            self.assertIn("observable-inline:", inline.call_args.args[7])
            self.assertIn("ObservableMessagesReview", inline.call_args.args[7])
            body = post.call_args.args[3]["body"]
            self.assertIn("<details>\n<summary>Execution history</summary>\n\n", body)
            self.assertLess(body.index("</details>"), body.index("Independent review completed"))
            self.assertIn("observable-pr-review:", body)
            self.assertNotIn("claude-pr-review:", body)

    def test_failed_review_is_published_and_republication_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.json"
            path.write_text(json.dumps({"status":"failed", "attempts":[], "reason":"all_attempts_failed"}))
            with (
                mock.patch.dict(os.environ, {"GITHUB_REPOSITORY":"owner/repo", "PR_NUMBER":"1",
                    "HEAD_SHA":"b"*40, "BASE_SHA":"a"*40, "GITHUB_RUN_ID":"123",
                    "GITHUB_RUN_ATTEMPT":"2", "GITHUB_TOKEN":"test-key"}, clear=True),
                mock.patch.object(context, "_review_already_posted", side_effect=[False, True]),
                mock.patch.object(context, "_request_json") as post,
                mock.patch.object(context, "create_inline_comment") as inline,
            ):
                observer.publish(path)
                observer.publish(path)
            inline.assert_not_called()
            post.assert_called_once()
            body = post.call_args.args[3]["body"]
            self.assertIn("not a clean review", body)
            self.assertIn(":123:2 -->", body)
            self.assertIn("actions/runs/123", body)


if __name__ == "__main__":
    unittest.main()
