"""Third reviewer wiring, failover and independent publication."""
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
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
                prompt, files = observer.prepare_prompt(root, "a"*40, "b"*40)
            self.assertEqual(files, {"app.py", ".ci-observable-review.diff"})
            self.assertIn("thread_verdicts must be []", prompt)
            self.assertIn("unreviewed scope", prompt)
            self.assertIn("Advisory Sonar", prompt)
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

    def attempts(self, outcomes):
        report = {"status": "failed", "attempts": []}
        routes = [{"provider":"openrouter", "role":role, "model":model,
                   "key":"secret-key", "endpoint":"https://example.test/v1/messages"}
                  for role, model in (("primary", "primary"), ("fallback", "backup"))]
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
