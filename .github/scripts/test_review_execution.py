from __future__ import annotations

import unittest

from review_execution import ExecutionReport, claude_execution_report


class ExecutionReportTest(unittest.TestCase):
    def test_five_chunks_count_only_additional_requests_to_the_same_chunk_as_retries(self):
        report = ExecutionReport("nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", "model")
        for index in range(5):
            report.unit = f"chunk {index + 1}/5"
            attempt = report.begin({"model": "model"})
            report.finish(attempt, "received", 1)
            report.validate_last("valid_json")
        self.assertEqual([a.number for a in report.attempts], [1, 2, 3, 4, 5])
        self.assertIn("Requests: 5 · Validated: 5 · Retries: 0", report.summary())
        for chunk in ("chunk 2/5", "chunk 5/5"):
            report.unit = chunk
            report.begin({"model": "model"})
        self.assertIn("Requests: 7 · Validated: 5 · Retries: 2", report.summary())

    def test_nous_reports_canonical_endpoints_for_both_reviewers(self):
        for endpoint in ("https://inference-api.nousresearch.com", "https://inference-api.nousresearch.com/v1/chat/completions"):
            with self.subTest(endpoint=endpoint):
                report = ExecutionReport("nous", endpoint, "vendor/model")
                self.assertIn(endpoint, report.connection())
                self.assertIn("`nous`", report.connection())

    def test_reports_real_fallback_and_validation_not_just_http_success(self):
        report = ExecutionReport("nvidia", "https://integrate.api.nvidia.com/v1/chat/completions", "primary")
        report.unit = "chunk 1/2"
        first = report.begin({"model": "primary"})
        report.finish(first, "http_429", 1.5)
        second = report.begin({"model": "backup"})
        report.finish(second, "received", 2.5, {"id": "chatcmpl-123", "model": "backup-served"})
        self.assertNotIn("Successful models:", report.summary())
        report.validate_last("valid_json")
        report.unit = "chunk 2/2"
        third = report.begin({"model": "primary", "response_format": {"type": "json_schema"}})
        report.finish(third, "received", 3)
        report.validate_last("valid_json")
        rendered = report.summary() + report.details()
        self.assertIn("nvidia", rendered)
        self.assertIn("backup", rendered)
        self.assertIn("http_429", rendered)
        self.assertIn("Requests: 3", rendered)
        self.assertIn("Retries: 1", rendered)
        self.assertIn("Fallback successes: 1", rendered)
        self.assertIn("chatcmpl-123", rendered)
        self.assertIn("backup-served", rendered)
        self.assertIn("request #2", report.footer("chunk 1/2"))
        self.assertIn("fallback", report.footer("chunk 1/2"))
        self.assertIn("request #3", report.footer("chunk 2/2"))

    def test_redacts_custom_endpoint_and_bounds_untrusted_metadata(self):
        report = ExecutionReport("openrouter", "https://user:password@private.example/v1?api_key=secret", "model")
        attempt = report.begin({"model": "model", "messages": ["private prompt"]})
        report.finish(attempt, "received", 1, {
            "id": "bad\n<script>@user</script>", "model": "bad|table\nmodel",
            "error": "secret", "choices": ["private response"],
        })
        report.validate_last("valid_json")
        rendered = report.summary() + report.details()
        for forbidden in ("password", "private.example", "secret", "private prompt", "private response", "<script>", "@user"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("custom endpoint", rendered)

    def test_claude_reports_ci_attempts_and_actual_fallback_model(self):
        report = claude_execution_report({
            "CLAUDE_REVIEW_PROVIDER": "openrouter",
            "CLAUDE_REVIEW_ENDPOINT": "https://openrouter.ai/api",
            "CLAUDE_REVIEW_PRIMARY_MODEL": "primary",
            "CLAUDE_REVIEW_FALLBACK_MODEL": "backup",
            "CLAUDE_REVIEW_PRIMARY_OUTCOME": "failure",
            "CLAUDE_REVIEW_PRIMARY_VALIDATION": "skipped",
            "CLAUDE_REVIEW_RETRY_OUTCOME": "success",
            "CLAUDE_REVIEW_RETRY_VALIDATION": "failure",
            "CLAUDE_REVIEW_FALLBACK_OUTCOME": "success",
            "CLAUDE_REVIEW_FALLBACK_VALIDATION": "success",
        })
        rendered = report.summary() + report.details() + report.footer("review")
        self.assertIn("CI attempts: 3", rendered)
        self.assertIn("backup", rendered)
        self.assertIn("execution_failed", rendered)
        self.assertIn("validation_failed", rendered)
        self.assertIn("CI attempt #3", rendered)
        self.assertIn("SDK HTTP retries are not recorded", rendered)

    def test_skipped_claude_steps_are_not_counted(self):
        report = claude_execution_report({
            "CLAUDE_REVIEW_PRIMARY_MODEL": "primary",
            "CLAUDE_REVIEW_PRIMARY_OUTCOME": "success",
            "CLAUDE_REVIEW_PRIMARY_VALIDATION": "success",
            "CLAUDE_REVIEW_RETRY_OUTCOME": "skipped",
            "CLAUDE_REVIEW_FALLBACK_OUTCOME": "skipped",
        })
        self.assertEqual(len(report.attempts), 1)
        self.assertIn("Retries: 0", report.summary())

    def test_long_history_is_bounded_while_totals_remain_exact(self):
        report = ExecutionReport("nvidia", "https://integrate.api.nvidia.com", "primary")
        for index in range(100):
            report.unit = f"chunk {index + 1}/100"
            attempt = report.begin({"model": "primary"})
            report.finish(attempt, "received", 1)
            report.validate_last("valid_json")
        self.assertIn("Requests: 100", report.summary())
        self.assertIn("all 100", report.details())
        self.assertLess(len(report.details()), 10000)


if __name__ == "__main__":
    unittest.main()
