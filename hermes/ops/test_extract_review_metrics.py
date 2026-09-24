from __future__ import annotations

import importlib.util
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

    def test_main_rejects_missing_token(self):
        with mock.patch.dict(metrics.os.environ, {}, clear=True), \
             mock.patch("sys.argv", ["extract-review-metrics.py", "--pr", "43"]), \
             self.assertRaises(SystemExit) as result:
            metrics.main()
        self.assertEqual(result.exception.code, 2)

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
