from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
