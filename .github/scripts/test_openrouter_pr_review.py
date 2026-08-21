from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("openrouter_pr_review.py")
SPEC = importlib.util.spec_from_file_location("openrouter_pr_review", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
reviewer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reviewer
SPEC.loader.exec_module(reviewer)


class AnnotatedDiffTest(unittest.TestCase):
    def test_tracks_only_changed_lines_on_the_correct_side(self) -> None:
        review_file = reviewer.annotate_diff(
            "app.py",
            """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@
 context
-old_value
+new_value
+second_value
 trailing
""",
        )

        self.assertEqual(review_file.left_lines, frozenset({11}))
        self.assertEqual(review_file.right_lines, frozenset({11, 12}))
        self.assertIn("LEFT 11|-old_value", review_file.rendered_diff)
        self.assertIn("RIGHT 12|+second_value", review_file.rendered_diff)
        self.assertIn("CONTEXT L12/R13| trailing", review_file.rendered_diff)


class ReviewPlanTest(unittest.TestCase):
    def test_prioritizes_source_files_before_large_documentation(self) -> None:
        code = reviewer.ReviewFile(
            "src/app.py",
            "\n".join(f"RIGHT {line}|+code" for line in range(1, 6)),
            frozenset(range(1, 6)),
            frozenset(),
        )
        docs = reviewer.ReviewFile(
            "docs/guide.md",
            "\n".join(f"RIGHT {line}|+documentation" for line in range(1, 80)),
            frozenset(range(1, 80)),
            frozenset(),
        )

        with (
            mock.patch.object(reviewer, "MAX_CHUNK_CHARACTERS", 300),
            mock.patch.object(reviewer, "MAX_REVIEW_CHUNKS", 1),
        ):
            plan = reviewer.build_review_plan([docs, code])

        self.assertIn("src/app.py", plan.chunks[0].paths)
        self.assertIn("src/app.py", plan.complete_files)
        self.assertNotIn("docs/guide.md", plan.complete_files)
        self.assertFalse(plan.complete)


class FindingValidationTest(unittest.TestCase):
    def test_rejects_unverified_locations_and_deduplicates_anchors(self) -> None:
        review_file = reviewer.ReviewFile(
            "app.py",
            "RIGHT 5|+changed",
            frozenset({5}),
            frozenset({2}),
        )
        chunk = reviewer.ReviewChunk("diff", frozenset({"app.py"}), ("app.py",))
        valid = {
            "severity": "P2",
            "path": "app.py",
            "side": "RIGHT",
            "line": 5,
            "title": "Broken contract",
            "impact": "Clients receive the wrong value.",
            "fix": "Restore the expected value.",
        }
        responses = [
            {
                "summary": "chunk",
                "findings": [
                    valid,
                    {**valid, "title": "Duplicate at the same anchor"},
                    {**valid, "line": 4},
                    {**valid, "path": "unchanged.py"},
                ],
            }
        ]

        findings = reviewer.validate_findings(responses, (chunk,), [review_file])

        self.assertEqual(findings, [reviewer.Finding("P2", "app.py", "RIGHT", 5, "Broken contract", "Clients receive the wrong value.", "Restore the expected value.")])


class OpenRouterRequestTest(unittest.TestCase):
    def test_requests_strict_structured_output(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"summary": "Reviewed.", "findings": []})
                    }
                }
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.object(reviewer, "request_json", return_value=response) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
        ):
            result = reviewer.review_chunk("api-key", "review-model", chunk, 1, 2)

        self.assertEqual(result["findings"], [])
        request_body = request.call_args.args[3]
        self.assertEqual(request_body["model"], "review-model")
        self.assertTrue(request_body["provider"]["require_parameters"])
        self.assertEqual(request_body["response_format"]["type"], "json_schema")
        self.assertTrue(request_body["response_format"]["json_schema"]["strict"])
        self.assertIn("chunk 1 of 2", request_body["messages"][1]["content"])

    def test_retries_transient_provider_failures(self) -> None:
        response = {
            "choices": [
                {"message": {"content": json.dumps({"summary": "Reviewed.", "findings": []})}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.object(
                reviewer,
                "request_json",
                side_effect=[reviewer.RequestError("rate limited", status=429), response],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch.object(reviewer.time, "sleep") as sleep,
        ):
            reviewer.review_chunk("api-key", "review-model", chunk, 1, 1)

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)


class GitHubReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = reviewer.ReviewPlan(
            chunks=(reviewer.ReviewChunk("diff", frozenset({"app.py"}), ("app.py",)),),
            total_files=1,
            complete_files=frozenset({"app.py"}),
            partial_files=frozenset(),
            omitted_files=frozenset(),
        )
        self.finding = reviewer.Finding(
            "P2",
            "app.py",
            "RIGHT",
            5,
            "Broken contract",
            "Clients receive the wrong value.",
            "Restore the expected value.",
        )

    def test_publishes_formal_review_with_inline_comments(self) -> None:
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            reviewer.publish_review(
                "owner/repository", "2", "token", "review-model", "head-sha", self.plan, [self.finding]
            )

        post_call = request.call_args_list[1]
        self.assertEqual(post_call.args[0], "https://api.github.com/repos/owner/repository/pulls/2/reviews")
        self.assertEqual(post_call.args[1], "POST")
        payload = post_call.args[3]
        self.assertEqual(payload["commit_id"], "head-sha")
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"][0]["path"], "app.py")
        self.assertEqual(payload["comments"][0]["line"], 5)

    def test_skips_duplicate_review_for_the_same_commit(self) -> None:
        existing = [{"body": "<!-- openrouter-pr-review:head-sha -->\nAlready reviewed"}]
        with (
            mock.patch.object(reviewer, "request_json", return_value=existing) as request,
            mock.patch("builtins.print"),
        ):
            reviewer.publish_review(
                "owner/repository", "2", "token", "review-model", "head-sha", self.plan, []
            )

        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
