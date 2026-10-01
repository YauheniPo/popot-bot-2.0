from __future__ import annotations

import importlib.util
import json
import runpy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("extract-review-metrics.py")
SPEC = importlib.util.spec_from_file_location("extract_review_metrics", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


class ExtractReviewMetricsTests(unittest.TestCase):
    def test_parse_metadata_and_scope(self):
        body = """ObservableMessagesReview
Technical metadata
Connection: nous · API: https://inference.example/v1
Successful models: inclusionai/ling-3.0-flash-sante:free
Attempts: 3 · Validated: 2 · Retries: 1 · Fallback successes: 1
Provider time: 42.5s
Result: success
CI run · Reviewed revision
"""
        parsed = metrics.parse_review(body, "ObservableMessagesReview")
        self.assertEqual(parsed["reviewer"], "ObservableMessagesReview")
        self.assertEqual(parsed["provider"], "nous")
        self.assertEqual(parsed["model"], "inclusionai/ling-3.0-flash-sante:free")
        self.assertEqual(parsed["validated_chunks"], 2)
        self.assertEqual(parsed["outcome"], "success")
        self.assertEqual(parsed["provider_seconds"], 42.5)

    def test_parse_published_blockquote_format(self):
        # Real bot comments render the metadata as a blockquote with
        # backtick-quoted values and a bolded result.
        body = """## DirectAPI

### Technical metadata
> Connection: `nous` · API: `https://inference-api.nousresearch.com/v1/chat/completions`
> Successful models: `inclusionai/ling-3.0-flash-sante:free`
> Attempts: 1 · Validated: 1 · Retries: 0 · Fallback successes: 0
> Provider time: 60.8s (retry waits, preflight, and GitHub API requests excluded).
> Coverage: complete — 15/15 eligible changed files

Summary: Reviewed the PR in 1 bounded chunk(s); found no new actionable issues.
"""
        parsed = metrics.parse_review(body)
        self.assertEqual(parsed["reviewer"], "DirectAPI")
        self.assertEqual(parsed["provider"], "nous")
        self.assertEqual(parsed["model"], "inclusionai/ling-3.0-flash-sante:free")
        self.assertEqual(parsed["attempts"], 1)
        self.assertEqual(parsed["provider_seconds"], 60.8)
        # Complete coverage supplies the result when Result is absent.
        self.assertEqual(parsed["outcome"], "success")

    def test_parse_bulleted_result_line_strips_markdown(self):
        body = ("ObservableMessagesReview\nTechnical metadata\n"
                "Connection: nous · API: https://inference.example/v1\n"
                "Successful models: model-a\n"
                "Provider time: 10.7s\n\nResult: **success**\n")
        parsed = metrics.parse_review(body)
        self.assertEqual(parsed["outcome"], "success")

    def test_parse_result_line_strips_backticks_like_connection_and_model(self):
        body = ("ObservableMessagesReview\nTechnical metadata\n"
                "Connection: `nous` · API: https://inference.example/v1\n"
                "Successful models: `model-a`\n"
                "Provider time: 10.7s\n\nResult: `success`\n")
        parsed = metrics.parse_review(body)
        self.assertEqual(parsed["provider"], "nous")
        self.assertEqual(parsed["model"], "model-a")
        self.assertEqual(parsed["outcome"], "success")

    def test_parse_plain_metadata_without_result_defaults_to_unknown(self):
        body = ("Technical metadata\nConnection: provider · API: https://example\n"
                "Successful models: model-a\n")
        self.assertEqual(metrics.parse_review(body)["outcome"], "unknown")

    def test_parse_quoted_values_with_surrounding_whitespace(self):
        for value in ("`nous` ", "  `nous`  ", "` nous `", " nous "):
            with self.subTest(value=value):
                body = (f"Technical metadata\n> Connection: {value} · API: https://example\n"
                        f"> Successful models: {value}, `second-model`\n")
                parsed = metrics.parse_review(body)
                self.assertEqual(parsed["provider"], "nous")
                self.assertEqual(parsed["model"], "nous")

    def test_parse_coverage_without_result(self):
        metadata = ("## DirectAPI\n### Technical metadata\n"
                    "> Connection: `provider` · API: `https://example`\n"
                    "> Successful models: `model-a`\n")
        for prefix in ("", "> "):
            for coverage, expected in (("partial", "partial"), ("complete", "success"),
                                       ("unavailable", "unknown")):
                with self.subTest(prefix=prefix, coverage=coverage):
                    body = metadata + f"{prefix}Coverage: {coverage} — 1/2 files complete\n"
                    self.assertEqual(metrics.parse_review(body)["outcome"], expected)

    def test_explicit_result_takes_precedence_over_coverage(self):
        for result in ("failed", "partial", "success"):
            with self.subTest(result=result):
                body = ("ObservableMessagesReview\nTechnical metadata\n"
                        "Connection: provider · API: https://example\n"
                        "Successful models: model-a\n"
                        "> Coverage: partial — 1/2 files complete\n"
                        f"Result: **{result}**\n")
                self.assertEqual(metrics.parse_review(body)["outcome"], result)

    def test_imports_all_three_published_reviewer_summaries(self):
        head = "a" * 40
        items = [
            {"id": 1, "body": (f"<!-- openrouter-pr-review:{head} -->\n## DirectAPI\n"
             "### Technical metadata\n> Connection: `nous` · API: `https://example`\n"
             "> Successful models: `direct-model`\n> Attempts: 2 · Validated: 2 · Retries: 0 · Fallback successes: 0\n"
             "> Provider time: 12.5s\n> Coverage: complete — 2/2 eligible changed files\n"
             "Summary: Reviewed the PR in 2 bounded chunk(s); found no new actionable issues.\n"),
             "submitted_at": "2026-09-23T10:00:00Z", "commit_id": head},
            {"id": 2, "body": ("## ClaudeCodePlugin\n\n### Technical metadata\n"
             "> Connection: `openrouter` · API: `https://example`\n"
             "> Successful models: `claude-model`\n> Attempts: 2 · Validated: 1 · Retries: 1 · Fallback successes: 0\n"
             "> Provider time: 0.0s\n\n### Review scope\nComplete base-to-head diff supplied.\n"
             "Existing DirectAPI findings were checked.\n"
             f"<!-- claude-pr-review:{head}:123 -->\n"),
             "created_at": "2026-09-23T10:01:00Z"},
            {"id": 3, "body": ("## ObservableMessagesReview\n\n### Technical metadata\n"
             "> Connection: `openrouter` · API: `https://example`\n"
             "> Successful models: `observable-model`\n> Attempts: 2 · Validated: 1 · Retries: 1 · Fallback successes: 0\n"
             "> Provider time: 10.0s\n\nResult: **partial**\n"
             "Validated chunks: 1/2; failed: 1; skipped: 0.\n\n"
             f"[CI run](https://github.com/org/repo/actions/runs/123)\n<!-- observable-pr-review:{head}:123:1 -->"),
             "created_at": "2026-09-23T10:02:00Z"},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(metrics, "fetch_reviews", return_value=items):
            database = Path(directory) / "metrics.db"
            self.assertEqual(metrics.import_pr("org/repo", 43, "secret", database), 3)
            self.assertEqual(metrics.import_pr("org/repo", 43, "secret", database), 3)
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT reviewer, model, outcome, validated_chunks, total_chunks, head_sha, run_id "
                    "FROM review_runs ORDER BY source_id"
                ).fetchall()
        self.assertEqual(rows, [
            ("DirectAPI", "direct-model", "success", 2, 2, head, ""),
            ("ClaudeCodePlugin", "claude-model", "success", 1, 1, head, "123"),
            ("ObservableMessagesReview", "observable-model", "partial", 1, 2, head, "123"),
        ])

    def test_imports_azure_direct_review_with_its_run_identity(self):
        head = "b" * 40
        body = (f"<!-- openrouter-pr-review:azure-devops:{head}:nous:model-a:456 -->\n"
                "## Azure DevOps · DirectAPI\n\n### Technical metadata\n"
                "> Connection: `nous` · API: `https://example`\n"
                "> Successful models: `model-a`\n"
                "> Attempts: 1 · Validated: 1 · Retries: 0 · Fallback successes: 0\n"
                "> Provider time: 2.0s\n> Coverage: complete — 1/1 eligible changed files\n"
                "Summary: Reviewed the PR in 1 bounded chunk(s); found no new actionable issues.\n")
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(metrics, "fetch_reviews", return_value=[{
                 "id": 4, "body": body, "submitted_at": "2026-09-23T10:03:00Z"
             }]):
            database = Path(directory) / "metrics.db"
            self.assertEqual(metrics.import_pr("org/repo", 43, "", database), 1)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT reviewer, outcome, head_sha, run_id FROM review_runs"
                ).fetchone()
        self.assertEqual(row, ("DirectAPI", "success", head, "456"))

    def test_fetch_reviews_reads_later_comment_pages(self):
        payloads = [[], [{"id": number} for number in range(100)], [{"id": 101}]]
        responses = []
        for payload in payloads:
            response = mock.Mock()
            response.__enter__ = lambda self: self
            response.__exit__ = mock.Mock(return_value=False)
            response.read.return_value = json.dumps(payload).encode()
            responses.append(response)
        with mock.patch.object(metrics.urllib.request, "urlopen", side_effect=responses) as urlopen:
            items = metrics.fetch_reviews("org/repo", 43, "secret")
        self.assertEqual(len(items), 101)
        self.assertIn("page=2", urlopen.call_args.args[0].full_url)

    def test_reimport_corrects_partial_review_without_duplicate(self):
        body = ("## DirectAPI\n### Technical metadata\n"
                "> Connection: `provider` · API: `https://example`\n"
                "> Successful models: `model-a`\n"
                "> Coverage: partial — 1/2 files complete, 0 partial, 1 omitted by the bounded budget\n")
        item = {"id": 2, "body": body, "submitted_at": "2026-09-23T10:00:00Z"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(metrics, "fetch_reviews", return_value=[item]):
            database = Path(directory) / "metrics.db"
            metrics.import_pr("org/repo", 43, "secret", database)
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE review_runs SET outcome = 'success'")
            metrics.import_pr("org/repo", 43, "secret", database)
            with sqlite3.connect(database) as connection:
                rows = connection.execute("SELECT outcome, observed_at FROM review_runs").fetchall()
            self.assertEqual(rows, [("partial", item["submitted_at"])])

    def test_upsert_is_idempotent_and_creates_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "metrics.db"
            metrics.ensure_schema(database)
            row = {"source_id": "review:1", "pr_number": 43, "reviewer": "DirectAPI",
                   "provider": "nous", "model": "model-a", "outcome": "success",
                   "attempts": 1, "validated_chunks": 1, "total_chunks": 1, "retries": 0,
                   "fallback_successes": 0, "provider_seconds": 2.5, "observed_at": "2026-09-23T10:00:00Z",
                   "head_sha": "abc", "run_id": "10"}
            metrics.upsert(database, row)
            metrics.upsert(database, {**row, "provider_seconds": 3.0})
            with sqlite3.connect(database) as connection:
                count, seconds = connection.execute("SELECT COUNT(*), provider_seconds FROM review_runs").fetchone()
            self.assertEqual((count, seconds), (1, 3.0))

    def test_fetch_reviews_uses_bearer_token(self):
        response = mock.Mock()
        response.__enter__ = lambda self: self
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"[]"
        with mock.patch.object(metrics.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(metrics.fetch_reviews("org/repo", 43, "secret"), [])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_import_pr_skips_non_metadata_and_upserts_valid_records(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(metrics, "fetch_reviews", return_value=[
            {"id": 1, "body": "not a review"},
            {"id": 2, "body": "Technical metadata\nConnection: nous · API: https://example\n"
                                "Successful models: model-a\nResult: success\n", "created_at": "now"},
        ]):
            count = metrics.import_pr("org/repo", 43, "secret", Path(directory) / "metrics.db")
            self.assertEqual(count, 1)

    def test_parse_review_rejects_incomplete_and_supports_unknown_reviewer(self):
        self.assertIsNone(metrics.parse_review("ordinary comment"))
        self.assertIsNone(metrics.parse_review("Technical metadata\nResult: failed"))
        # Explicit failures are retained even with successful model metadata.
        parsed = metrics.parse_review(
            "Technical metadata\nConnection: provider · API: https://example\n"
            "Successful models: model-a\nResult: failed\n"
        )
        self.assertEqual(parsed["reviewer"], "unknown")
        self.assertEqual(parsed["outcome"], "failed")

    def test_main_imports_requested_pr(self):
        with mock.patch.object(metrics, "import_pr", return_value=2) as importer, \
             mock.patch.dict(metrics.os.environ, {"GITHUB_TOKEN": "secret"}), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43"]):
            self.assertEqual(metrics.main(), 0)
        self.assertEqual(importer.call_args.args[1:3], (43, "secret"))

    def test_main_defaults_to_hermes_home_database(self):
        with mock.patch.object(metrics, "import_pr", return_value=0) as importer, \
             mock.patch.dict(metrics.os.environ, {
                 "GITHUB_TOKEN": "secret", "HERMES_HOME": "/srv/hermes-state"
             }), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "63"]):
            self.assertEqual(metrics.main(), 0)
        self.assertEqual(importer.call_args.args[3], Path("/srv/hermes-state/ops/metrics.db"))

    def test_main_allows_public_import_without_token(self):
        with mock.patch.dict(metrics.os.environ, {}, clear=True), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43"]), \
             mock.patch.object(metrics, "import_pr", return_value=1) as importer:
            self.assertEqual(metrics.main(), 0)
        self.assertEqual(importer.call_args.args[2], "")

    def test_fetch_reviews_omits_auth_header_without_token(self):
        response = mock.Mock()
        response.__enter__ = lambda self: self
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"[]"
        with mock.patch.object(metrics.urllib.request, "urlopen", return_value=response) as urlopen:
            metrics.fetch_reviews("org/repo", 43, "")
        self.assertNotIn("Authorization", urlopen.call_args.args[0].headers)

    def test_script_entrypoint_runs(self):
        response = mock.Mock()
        response.__enter__ = lambda self: self
        response.__exit__ = mock.Mock(return_value=False)
        response.read.return_value = b"[]"
        with mock.patch.object(metrics, "import_pr", return_value=0), \
             mock.patch.dict(metrics.os.environ, {"GITHUB_TOKEN": "secret"}), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43"]), \
             mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()


class ExtractReviewMetricsHelperTests(unittest.TestCase):
    """Tests for helper functions in extract-review-metrics.py"""

    def setUp(self):
        import importlib.util
        import sys
        SCRIPT = Path(__file__).parent / "extract-review-metrics.py"
        spec = importlib.util.spec_from_file_location("extract_review_metrics", SCRIPT)
        self.metrics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.metrics)

    def test_review_identity_returns_empty_group_when_lastindex_not_2(self):
        """_review_identity returns empty string for second group when lastindex != 2."""
        # DirectAPI marker has only 1 group (sha), so lastindex == 1
        body = "<!-- openrouter-pr-review:" + "a" * 40 + " -->"
        reviewer, sha, pr = self.metrics._review_identity(body)
        self.assertEqual(reviewer, "DirectAPI")
        self.assertEqual(sha, "a" * 40)
        self.assertEqual(pr, "")  # lastindex == 1, so group(2) returns ""

    def test_apply_claude_validated_marker_false_for_wrong_reviewer(self):
        """_apply_claude_validated_marker returns False for non-Claude reviewers."""
        result = self.metrics._apply_claude_validated_marker(
            "DirectAPI", "unknown", 5, "body"
        )
        self.assertFalse(result)

    def test_apply_claude_validated_marker_false_when_outcome_known(self):
        """_apply_claude_validated_marker returns False when default_outcome != unknown."""
        result = self.metrics._apply_claude_validated_marker(
            "ClaudeCodePlugin", "success", 5, "body"
        )
        self.assertFalse(result)

    def test_apply_claude_validated_marker_false_when_no_validated_chunks(self):
        """_apply_claude_validated_marker returns False when validated_chunks == 0."""
        result = self.metrics._apply_claude_validated_marker(
            "ClaudeCodePlugin", "unknown", 0, "body"
        )
        self.assertFalse(result)

    def test_apply_claude_validated_marker_true_when_marker_found(self):
        """_apply_claude_validated_marker returns True when marker found in body."""
        body = "<!-- claude-pr-review:" + "a" * 40 + ":123 -->"
        result = self.metrics._apply_claude_validated_marker(
            "ClaudeCodePlugin", "unknown", 5, body
        )
        self.assertTrue(result)

    def test_apply_claude_validated_marker_false_when_marker_not_found(self):
        """_apply_claude_validated_marker returns False when marker not in body."""
        body = "no marker here"
        result = self.metrics._apply_claude_validated_marker(
            "ClaudeCodePlugin", "unknown", 5, body
        )
        self.assertFalse(result)

    def test_compute_total_chunks_from_scope(self):
        """_compute_total_chunks uses scope when available."""
        scope = mock.MagicMock()
        scope.group.return_value = "2"
        result = self.metrics._compute_total_chunks(scope, None, "DirectAPI", "unknown")
        self.assertEqual(result, 2)

    def test_compute_total_chunks_from_direct_chunks(self):
        """_compute_total_chunks uses direct_chunks when scope missing."""
        direct_chunks = mock.MagicMock()
        direct_chunks.group.return_value = "3"
        result = self.metrics._compute_total_chunks(None, direct_chunks, "DirectAPI", "unknown")
        self.assertEqual(result, 3)

    def test_compute_total_chunks_claude_success(self):
        """_compute_total_chunks returns 1 for ClaudeCodePlugin with success."""
        result = self.metrics._compute_total_chunks(
            None, None, "ClaudeCodePlugin", "success"
        )
        self.assertEqual(result, 1)

    def test_compute_total_chunks_claude_not_success(self):
        """_compute_total_chunks returns 0 for ClaudeCodePlugin without success."""
        result = self.metrics._compute_total_chunks(
            None, None, "ClaudeCodePlugin", "partial"
        )
        self.assertEqual(result, 0)

    def test_compute_total_chunks_other_reviewer(self):
        """_compute_total_chunks returns 0 for other reviewers."""
        result = self.metrics._compute_total_chunks(
            None, None, "DirectAPI", "unknown"
        )
        self.assertEqual(result, 0)

    def test_parse_review_applies_claude_marker(self):
        """parse_review sets outcome to success when Claude marker applies."""
        # Use proper metadata format with Technical metadata block
        body = (
            "## ClaudeCodePlugin\n\n"
            "### Technical metadata\n"
            "> Connection: `nous` · API: `https://api.example.com`\n"
            "> Successful models: `inclusionai/ling-3.0-flash-sante:free`\n"
            "> Validated: 5\n"
            "<!-- claude-pr-review:" + "a" * 40 + ":1 -->"
        )
        result = self.metrics.parse_review(body, "ClaudeCodePlugin")
        self.assertIsNotNone(result)
        # default_outcome should be "success" due to claude marker
        self.assertEqual(result.get("outcome"), "success")

    def test_fetch_reviews_invalid_payload_raises(self):
        """fetch_reviews raises ValueError for non-list payload."""
        import urllib.request
        original_open = urllib.request.urlopen

        def mock_open(req, timeout):
            response = mock.MagicMock()
            response.__enter__ = lambda s: s
            response.__exit__ = mock.Mock(return_value=False)
            response.read.return_value = b'"not a list"'
            return response

        urllib.request.urlopen = mock_open
        try:
            with self.assertRaises(ValueError) as cm:
                self.metrics.fetch_reviews("YauheniPo/popot-bot-2.0", 1, "token")
            self.assertIn("invalid review/comment list", str(cm.exception))
        finally:
            urllib.request.urlopen = original_open

    def test_fetch_reviews_exceeds_limit_raises(self):
        """fetch_reviews raises RuntimeError when exceeding 50 pages."""
        import urllib.request
        original_open = urllib.request.urlopen

        call_count = [0]

        def mock_open(req, timeout):
            call_count[0] += 1
            response = mock.MagicMock()
            response.__enter__ = lambda s: s
            response.__exit__ = mock.Mock(return_value=False)
            # Return 100 items for all 50 pages to trigger the limit
            response.read.return_value = b'[' + b', '.join([b'{"id": %d}' % i for i in range(100)]) + b']'
            return response

        urllib.request.urlopen = mock_open
        try:
            with self.assertRaises(RuntimeError) as cm:
                self.metrics.fetch_reviews("YauheniPo/popot-bot-2.0", 1, "token")
            self.assertIn("exceeded the 50-page import limit", str(cm.exception))
        finally:
            urllib.request.urlopen = original_open
