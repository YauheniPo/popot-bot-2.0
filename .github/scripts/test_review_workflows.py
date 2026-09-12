"""Exercise review orchestration and failure paths without external requests."""

from __future__ import annotations

from dataclasses import replace
from email.utils import formatdate
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ai_review_preflight as preflight
import review_execution
import test_ai_pr_review as review_tests


reviewer = review_tests.reviewer
context = review_tests.pr_review_context
HEAD_SHA = "b" * 40
OLD_SHA = "a" * 40


def machine_thread(**changes):
    thread = context.ReviewThread(
        node_id="thread-1", path="app.py", side="RIGHT", line=5,
        original_line=5, outdated=False, viewer_can_reply=True,
        comments=(context.ReviewComment(
            "comment-1", 101, context.AUTOMATED_REVIEW_AUTHOR,
            f"<!-- direct-openrouter-inline:{OLD_SHA}:app.py:RIGHT:5 -->",
        ),),
    )
    return replace(thread, **changes)


def completion(document):
    return {"choices": [{"message": {"content": json.dumps(document)}}]}


class ReviewWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(reviewer.os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(reviewer, "EXECUTION_REPORT", None))
        self.enterContext(mock.patch.object(reviewer, "REVIEW_DEADLINE", reviewer.ReviewDeadline(600)))
        self.enterContext(mock.patch.object(reviewer, "ACTIVE_PROVIDER", "nvidia"))
        self.enterContext(mock.patch.object(reviewer, "OLLAMA_URL", preflight.NVIDIA_CHAT_COMPLETIONS_URL))
        # An accidentally unmocked request must fail the test, never reach GitHub or inference.
        self.enterContext(mock.patch.object(reviewer.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network request")))
        self.output = self.enterContext(mock.patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.enterContext(mock.patch("sys.stderr", new_callable=io.StringIO))
        self.file = reviewer.annotate_diff("app.py", "@@ -5 +5 @@\n-old()\n+new()\n")
        self.plan = reviewer.build_review_plan([self.file])
        self.finding = reviewer.Finding("P1", "app.py", "RIGHT", 5, "Missing guard", "Can crash", "Guard the call")

    def test_retry_headers_accept_http_dates_and_bound_untrusted_delays(self):
        now = 1_700_000_000
        with mock.patch.object(reviewer.time, "time", return_value=now):
            for raw, expected in (("", None), ("not a date", None), (formatdate(now + 20, usegmt=True), 20.0)):
                with self.subTest(value=raw):
                    self.assertEqual(reviewer._seconds_until_reset(raw), expected)
            self.assertEqual(reviewer._retry_after_seconds({"Retry-After": "4"}, ""), 5.0)
            self.assertEqual(reviewer._retry_after_seconds({"Retry-After": formatdate(now + 20, usegmt=True)}, ""), 21.0)
            self.assertEqual(reviewer._retry_after_seconds({"Retry-After": "1000000"}, ""), reviewer.MAX_RETRY_DELAY_SECONDS)
            self.assertIsNone(reviewer._retry_after_seconds({"Retry-After": "bad"}, ""))

    def test_invalid_configuration_fails_before_review(self):
        for name, function in (("OLLAMA_REVIEW_RPM", reviewer.configured_requests_per_minute),
                               ("OLLAMA_MAX_REVIEW_CHUNKS", reviewer.configured_max_review_chunks)):
            with self.subTest(name=name), mock.patch.dict(reviewer.os.environ, {name: "not-a-number"}):
                with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                    function()
        with self.assertRaisesRegex(RuntimeError, "GITHUB_TOKEN is missing"):
            reviewer.required_env("GITHUB_TOKEN")
        with self.assertRaisesRegex(RuntimeError, "API key for nvidia is missing"):
            reviewer.main()
        self.assertEqual(reviewer.review_chunks("key", "model", (), ()), [])

    def test_reads_authoritative_diff_from_a_real_local_git_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "--quiet")
            (root / "app.py").write_text("old()\n")
            git("add", "app.py")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
            (root / "app.py").write_text("new()\n")
            original_run = subprocess.run
            with mock.patch.object(reviewer.subprocess, "run", side_effect=lambda args, **kwargs: original_run(args, cwd=root, **kwargs)):
                self.assertEqual(reviewer.run_git("rev-parse", "--show-toplevel").strip(), str(root.resolve()))
                files = reviewer.read_review_files("HEAD", "--")
            self.assertEqual([file.path for file in files], ["app.py"])
            self.assertIn("LEFT 1|-old()", files[0].rendered_diff)
            self.assertIn("RIGHT 1|+new()", files[0].rendered_diff)

    def test_bounds_long_diff_lines_and_preserves_no_newline_metadata(self):
        line = "x" * (reviewer.MAX_RENDERED_LINE_CHARACTERS + 10)
        file = reviewer.annotate_diff("app.py", f"@@ -1 +1 @@\n+{line}\n\\ No newline at end of file\n")
        self.assertTrue(file.shortened)
        self.assertIn("[line shortened by reviewer]", file.rendered_diff)
        self.assertIn("META|\\ No newline", file.rendered_diff)
        tests = replace(self.file, path="test_app.py")
        docs = replace(self.file, path="README.md")
        plan = reviewer.build_review_plan([docs, tests, self.file])
        self.assertEqual(plan.chunks[0].segment_paths, ("app.py", "test_app.py", "README.md"))

    def test_rules_are_required_and_read_as_plain_text(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules.md"
            with mock.patch.object(reviewer, "REVIEW_RULES_PATH", rules):
                with self.assertRaisesRegex(RuntimeError, "Could not read reviewer rules"):
                    reviewer.read_review_rules()
                rules.write_text("  \n")
                with self.assertRaisesRegex(RuntimeError, "is empty"):
                    reviewer.read_review_rules()
                rules.write_text("  Review only the supplied diff.\n")
                self.assertEqual(reviewer.read_review_rules(), "Review only the supplied diff.")

    def test_response_envelopes_and_malformed_json_are_reported_without_contents(self):
        for invalid in (None, {}, {"choices": []}):
            with self.subTest(response=invalid), self.assertRaises(reviewer.ReviewResponseError):
                reviewer.parse_review_response(invalid)
        response = {"choices": [{"message": {"content": [None, {"text": '{"summary":"ok",'}, {"text": '"findings":[]}'}]}}]}
        self.assertEqual(reviewer.parse_review_response(response)["findings"], [])
        self.assertIn("content_chars=7", reviewer._response_diagnostic({}, "private"))
        report = review_execution.ExecutionReport("nvidia", reviewer.OLLAMA_URL, "model")
        with mock.patch.object(reviewer, "EXECUTION_REPORT", report), mock.patch.object(reviewer, "request_json", side_effect=ValueError("private response")):
            with self.assertRaises(ValueError):
                reviewer._request_review_json({}, {"model": "model"}, 30)
        self.assertEqual(report.attempts[0].outcome, "invalid_json")
        self.assertNotIn("private response", report.details())

    def test_exhausted_primary_without_fallback_propagates_failure(self):
        with mock.patch.object(reviewer, "request_json", side_effect=reviewer.RequestError("no access", status=401)) as request:
            with self.assertRaises(reviewer.RequestError):
                reviewer.review_chunk("key", "model", (), self.plan.chunks[0], 1, 1)
        request.assert_called_once()

    def test_triage_requests_validate_real_envelopes_in_both_formats(self):
        thread = machine_thread()
        document = {"summary": "Rechecked.", "verdicts": [{"thread_id": thread.node_id, "verdict": "fixed", "reason": "Guard added at app.py:5"}]}
        for mode in ("ordinary", "strict"):
            report = review_execution.ExecutionReport("nvidia", reviewer.OLLAMA_URL, "model")
            with self.subTest(mode=mode), mock.patch.dict(reviewer.os.environ, {"DIRECT_REVIEW_MODEL_MODE": mode}), mock.patch.object(reviewer, "EXECUTION_REPORT", report), mock.patch.object(reviewer, "request_json", return_value=completion(document)) as request:
                verdicts = reviewer.request_thread_triage("key", "model", (thread,), [self.file])
            self.assertEqual(verdicts, [reviewer.ThreadVerdict(thread.node_id, "fixed", "Guard added at app.py:5")])
            prompt = request.call_args.args[3]["messages"][1]["content"]
            self.assertIn("RIGHT 5|+new()", prompt)
            self.assertIn(thread.node_id, prompt)
            self.assertEqual(report.attempts[0].outcome, "valid_json")

    def test_triage_evidence_omits_unrelated_and_oversized_files(self):
        oversized = replace(self.file, rendered_diff="x" * (reviewer.MAX_TRIAGE_EVIDENCE_CHARACTERS + 1))
        self.assertIn("no changed lines", reviewer._triage_evidence([self.file], {"other.py"}))
        self.assertNotIn("x" * 20, reviewer._triage_evidence([oversized], {"app.py"}))
        self.assertEqual(reviewer.validate_thread_verdicts({"verdicts": None}, (machine_thread(),)), [])
        self.assertEqual(reviewer.validate_thread_verdicts({"verdicts": [None]}, (machine_thread(),)), [])

    def test_rejected_thread_closes_only_after_evidence_reply_succeeds(self):
        thread = machine_thread()
        report = review_execution.ExecutionReport("nvidia", reviewer.OLLAMA_URL, "model", unit="thread triage")
        report.finish(report.begin({"model": "model"}), "valid_json", 1)
        calls = mock.Mock()
        with mock.patch.object(reviewer, "EXECUTION_REPORT", report), mock.patch.object(reviewer, "reply_to_review_thread", calls.reply), mock.patch.object(reviewer, "resolve_review_thread", calls.resolve):
            outcome = reviewer.apply_thread_verdicts("owner/repo", "31", "token", HEAD_SHA,
                [reviewer.ThreadVerdict(thread.node_id, "rejected", "The guard is already present")], (thread,), {"app.py"})
        self.assertEqual(outcome.rejected, 1)
        self.assertEqual(outcome.closed_thread_ids, frozenset({thread.node_id}))
        self.assertEqual([call[0] for call in calls.mock_calls], ["reply", "resolve"])
        self.assertIn("request #1", calls.reply.call_args.args[-1])
        with mock.patch.object(reviewer, "reply_to_review_thread", side_effect=context.GitHubRequestError("reply failed")), mock.patch.object(reviewer, "resolve_review_thread") as resolve:
            with self.assertRaises(context.GitHubRequestError):
                reviewer.apply_thread_verdicts("owner/repo", "31", "token", HEAD_SHA,
                    [reviewer.ThreadVerdict(thread.node_id, "rejected", "Evidence")], (thread,), {"app.py"})
        resolve.assert_not_called()

    def test_non_replyable_and_already_answered_threads_are_not_replied_to(self):
        marker = reviewer._verdict_marker(HEAD_SHA, "thread-1")
        answered = machine_thread(comments=machine_thread().comments + (context.ReviewComment("answer", 102, context.AUTOMATED_REVIEW_AUTHOR, marker),))
        for thread in (machine_thread(viewer_can_reply=False), machine_thread(comments=()), answered):
            with self.subTest(thread=thread), mock.patch.object(reviewer, "reply_to_review_thread") as reply:
                self.assertFalse(reviewer._reply_if_needed("owner/repo", "31", "token", thread, marker, "Evidence"))
            reply.assert_not_called()

    def test_optional_triage_skips_unavailable_or_failed_reviews(self):
        thread = machine_thread()
        args = ("owner/repo", "31", "token", "key", "model", HEAD_SHA)
        self.assertEqual(reviewer._optional_triage(*args, (), [self.file]), reviewer.TriageOutcome())
        for error in (context.GitHubRequestError("API failed"), reviewer.RequestError("API failed"),
                      reviewer.ReviewResponseError("bad JSON"), reviewer.ReviewBudgetExhausted("no time")):
            with self.subTest(error=type(error)), mock.patch.object(reviewer, "request_thread_triage", side_effect=error):
                self.assertEqual(reviewer._optional_triage(*args, (thread,), [self.file]), reviewer.TriageOutcome())

    def test_partial_review_summary_preserves_followups_duplicates_and_settled_findings(self):
        thread = machine_thread()
        followup = reviewer.FindingFollowUp(self.finding, thread)
        publication = reviewer.PublicationPlan((), (followup,), (self.finding,), (self.finding,))
        partial = replace(self.plan, total_files=2, omitted_files=frozenset({"large.py"}))
        body = reviewer._review_body("model", HEAD_SHA, partial, publication, reviewer.TriageOutcome(fixed=1))
        for phrase in ("partial", "Material additions", "suppressed", "1 new or materially extended issue"):
            self.assertIn(phrase, body)
        empty = reviewer._review_body("model", HEAD_SHA, self.plan, reviewer.PublicationPlan((), (), ()))
        self.assertIn("Findings: No new actionable findings", empty)
        self.assertIn("partial", reviewer._check_run_summary("model", HEAD_SHA, partial, []))
        unavailable = machine_thread(viewer_can_reply=False)
        with mock.patch.object(reviewer, "match_existing_thread", side_effect=[None, context.ThreadMatch(unavailable, "extension", 0.4)]):
            self.assertEqual(reviewer.build_publication_plan([self.finding], (unavailable,)).new_findings, (self.finding,))
        with mock.patch.dict(reviewer.os.environ, {"REVIEW_ORIGIN": "azure-devops"}):
            self.assertIn("azure-devops", reviewer._follow_up_marker(HEAD_SHA, thread.node_id))

    def test_publisher_falls_back_only_when_there_were_inline_findings(self):
        for findings in ((self.finding,), ()):
            with self.subTest(findings=findings), mock.patch.object(reviewer, "request_json", side_effect=[[], RuntimeError("anchor rejected"), {}]) as request:
                publication = reviewer.PublicationPlan(findings, (), ())
                if findings:
                    reviewer.publish_review("owner/repo", "31", "token", "model", HEAD_SHA, self.plan, publication)
                    self.assertNotIn("comments", request.call_args.args[3])
                    self.assertIn(self.finding.impact, request.call_args.args[3]["body"])
                    self.assertEqual(request.call_count, 3)
                else:
                    with self.assertRaisesRegex(RuntimeError, "anchor rejected"):
                        reviewer.publish_review("owner/repo", "31", "token", "model", HEAD_SHA, self.plan, publication)
                    self.assertEqual(request.call_count, 2)
        with mock.patch.object(reviewer, "request_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "invalid pull-request review list"):
                reviewer.publish_review("owner/repo", "31", "token", "model", HEAD_SHA, self.plan, reviewer.PublicationPlan((), (), ()))

    def test_invalid_finding_paths_and_envelopes_cannot_create_comments(self):
        self.assertIsNone(reviewer._parse_finding(None, self.plan.chunks[0], {"app.py": self.file}, "chunk 1/1"))
        self.assertEqual(reviewer.validate_findings([{"findings": None}], self.plan.chunks, [self.file]), [])
        raw = {"severity": "P1", "path": "./app.py", "side": "RIGHT", "line": 5,
               "title": "Crash", "impact": "Runtime failure", "fix": "Add guard"}
        self.assertEqual(reviewer.validate_findings([{"findings": [raw]}], self.plan.chunks, [self.file])[0].path, "app.py")
        self.assertEqual(reviewer.validate_findings([{"findings": [{**raw, "impact": None}]}], self.plan.chunks, [self.file]), [])

    def test_main_selects_configured_provider_and_publishes_pr_or_manual_review(self):
        for pr in ("31", ""):
            with self.subTest(pr=pr), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "github-output"
                environment = {"NVIDIA_API_KEY": "test-model-key", "DIRECT_REVIEW_PROVIDER": "nvidia",
                    "DIRECT_REVIEW_MODEL": "vendor/model", "GITHUB_TOKEN": "test-github-key",
                    "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": pr, "BASE_SHA": OLD_SHA,
                    "HEAD_SHA": HEAD_SHA, "GITHUB_OUTPUT": str(output)}
                requests = []
                def respond(url, method, headers, body=None, **kwargs):
                    requests.append((url, method, headers, body))
                    if url == preflight.NVIDIA_CHAT_COMPLETIONS_URL:
                        self.assertEqual(headers["Authorization"], "Bearer test-model-key")
                        self.assertEqual(body["model"], "vendor/model")
                        return completion({"summary": "No defects", "findings": []})
                    self.assertEqual(headers["Authorization"], "Bearer test-github-key")
                    return [] if method == "GET" else {"html_url": "https://github.com/owner/repo/runs/123"}
                with mock.patch.dict(reviewer.os.environ, environment), mock.patch.object(reviewer, "request_json", side_effect=respond), mock.patch.object(reviewer, "read_review_files", return_value=[self.file]), mock.patch.object(reviewer, "fetch_unresolved_review_threads", return_value=[]) as unresolved, mock.patch.object(reviewer, "fetch_resolved_machine_threads", return_value=[]):
                    reviewer.main()
                self.assertEqual(len([r for r in requests if r[0] == preflight.NVIDIA_CHAT_COMPLETIONS_URL]), 1)
                final = requests[-1]
                self.assertTrue(final[0].endswith("/reviews" if pr else "/check-runs"))
                if pr:
                    unresolved.assert_called_once()
                    self.assertEqual(final[3]["comments"], [])
                else:
                    unresolved.assert_not_called()
                    self.assertEqual(final[3]["conclusion"], "success")
                    self.assertEqual(final[3]["external_id"], HEAD_SHA)
                    self.assertIn("check_run_url=https://github.com/owner/repo/runs/123", output.read_text())

    def test_cli_entrypoints_return_nonzero_without_credentials(self):
        for name, argv in (("ai_pr_review.py", []), ("ai_review_preflight.py", ["--probe", "json"])):
            with self.subTest(name=name), mock.patch.object(sys, "argv", [name, *argv]), self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(Path(__file__).with_name(name)), run_name="__main__")
            self.assertEqual(caught.exception.code, 1)
        self.assertIn("API key for nvidia is missing", self.errors.getvalue())
        self.assertIn("NVIDIA_API_KEY", self.errors.getvalue())


class ProviderMetadataEdgeTest(unittest.TestCase):
    def test_openrouter_alias_selects_only_its_credentials_and_unknown_provider_fails(self):
        with mock.patch.dict(preflight.os.environ, {"DIRECT_REVIEW_PROVIDER": "open-router", "OPENROUTER_API_KEY": "right", "NOUS_API_KEY": "wrong"}, clear=True):
            self.assertEqual(preflight.provider_config(), ("openrouter", "right", preflight.OPENROUTER_CHAT_COMPLETIONS_URL))
        with mock.patch.dict(preflight.os.environ, {"DIRECT_REVIEW_PROVIDER": "typo"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "REVIEW_PROVIDER must be"):
                preflight.provider_config()

    def test_non_string_model_metadata_is_rendered_as_unknown(self):
        report = review_execution.ExecutionReport("nous", "custom", "model")
        attempt = report.begin({"model": {"unexpected": "object"}})
        report.finish(attempt, "received", 1)
        report.validate_last("valid_json")
        self.assertIn("unknown", report.summary())
        self.assertNotIn("unexpected", report.details())


if __name__ == "__main__":
    unittest.main()
