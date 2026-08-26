from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("openrouter_pr_review.py")
sys.path.insert(0, str(SCRIPT_PATH.parent))
import pr_review_context

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
            result = reviewer.review_chunk("api-key", "review-model", (), chunk, 1, 2)

        self.assertEqual(result["findings"], [])
        request_body = request.call_args.args[3]
        self.assertEqual(request_body["model"], "review-model")
        self.assertTrue(request_body["provider"]["require_parameters"])
        self.assertEqual(request_body["response_format"]["type"], "json_schema")
        self.assertTrue(request_body["response_format"]["json_schema"]["strict"])
        self.assertIn("rules", request_body["messages"][0]["content"])
        self.assertIn("OpenRouter adapter instructions", request_body["messages"][0]["content"])
        self.assertIn("chunk 1 of 2", request_body["messages"][1]["content"])
        self.assertIn("UNRESOLVED_REVIEW_THREADS", request_body["messages"][1]["content"])

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
            reviewer.review_chunk("api-key", "review-model", (), chunk, 1, 1)

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(15.0)

    def test_relaxes_parameter_filter_for_incompatible_fallback(self) -> None:
        response = {
            "choices": [
                {"message": {"content": json.dumps({"summary": "Reviewed.", "findings": []})}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        routing_error = reviewer.RequestError(
            "POST failed with HTTP 404: No endpoints found that can handle the requested parameters",
            status=404,
        )
        with (
            mock.patch.dict(
                reviewer.os.environ,
                {"OPENROUTER_REVIEW_FALLBACK_MODEL": "fallback-model"},
            ),
            mock.patch.object(
                reviewer,
                "request_json",
                side_effect=[
                    reviewer.RequestError("primary rejected request", status=400),
                    routing_error,
                    response,
                ],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print"),
        ):
            result = reviewer.review_chunk("api-key", "primary-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 3)
        strict_fallback_body = request.call_args_list[1].args[3]
        relaxed_fallback_body = request.call_args_list[2].args[3]
        self.assertEqual(strict_fallback_body["model"], "fallback-model")
        self.assertTrue(strict_fallback_body["provider"]["require_parameters"])
        self.assertEqual(relaxed_fallback_body["model"], "fallback-model")
        self.assertFalse(relaxed_fallback_body["provider"]["require_parameters"])
        self.assertEqual(relaxed_fallback_body["response_format"]["type"], "json_schema")
        self.assertEqual(
            relaxed_fallback_body["max_tokens"],
            reviewer.RELAXED_FALLBACK_MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(
            relaxed_fallback_body["reasoning"],
            {"effort": "minimal", "exclude": True},
        )

    def test_does_not_relax_unrelated_fallback_404(self) -> None:
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.dict(
                reviewer.os.environ,
                {"OPENROUTER_REVIEW_FALLBACK_MODEL": "missing-model"},
            ),
            mock.patch.object(
                reviewer,
                "request_json",
                side_effect=[
                    reviewer.RequestError("primary rejected request", status=400),
                    reviewer.RequestError("model not found", status=404),
                ],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print"),
        ):
            with self.assertRaisesRegex(reviewer.RequestError, "model not found"):
                reviewer.review_chunk("api-key", "primary-model", (), chunk, 1, 1)

        self.assertEqual(request.call_count, 2)

    def test_uses_provider_reset_header_for_rate_limit_retry(self) -> None:
        response = {
            "choices": [
                {"message": {"content": json.dumps({"summary": "Reviewed.", "findings": []})}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        error_body = json.dumps(
            {
                "error": {
                    "metadata": {
                        "headers": {"X-RateLimit-Reset": "2000000000000"}
                    }
                }
            }
        ).encode()
        http_error = urllib.error.HTTPError(
            reviewer.OPENROUTER_URL,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(error_body),
        )
        self.addCleanup(http_error.close)
        second_response = mock.MagicMock()
        second_response.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
        with (
            mock.patch.object(
                reviewer.urllib.request,
                "urlopen",
                side_effect=[http_error, second_response],
            ),
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch.object(reviewer.time, "sleep") as sleep,
            mock.patch.object(reviewer.time, "time", return_value=1_999_999_950.0),
        ):
            reviewer.review_chunk("api-key", "review-model", (), chunk, 1, 1)

        sleep.assert_called_once_with(51.0)

    def test_spaces_chunk_requests_below_configured_rpm(self) -> None:
        chunks = (
            reviewer.ReviewChunk("first", frozenset({"a.py"}), ("a.py",)),
            reviewer.ReviewChunk("second", frozenset({"b.py"}), ("b.py",)),
        )
        with (
            mock.patch.dict(reviewer.os.environ, {"OPENROUTER_REVIEW_RPM": "10"}),
            mock.patch.object(
                reviewer,
                "review_chunk",
                side_effect=[
                    {"summary": "one", "findings": []},
                    {"summary": "two", "findings": []},
                ],
            ) as review_chunk,
            mock.patch.object(reviewer.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 1.0, 6.0]),
            mock.patch.object(reviewer.time, "sleep") as sleep,
        ):
            responses = reviewer.review_chunks("api-key", "model", (), chunks)

        self.assertEqual(len(responses), 2)
        self.assertEqual(review_chunk.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_rejects_invalid_rpm_configuration(self) -> None:
        with mock.patch.dict(reviewer.os.environ, {"OPENROUTER_REVIEW_RPM": "0"}):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 60"):
                reviewer.configured_requests_per_minute()


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

    def publication(self) -> reviewer.PublicationPlan:
        return reviewer.PublicationPlan((self.finding,), (), ())

    def review_thread(self) -> reviewer.ReviewThread:
        return reviewer.ReviewThread(
            node_id="thread-1",
            path="app.py",
            side="RIGHT",
            line=5,
            original_line=5,
            outdated=False,
            viewer_can_reply=True,
            comments=(
                pr_review_context.ReviewComment(
                    "comment-node",
                    101,
                    "reviewer",
                    "The current implementation breaks a different behavior.",
                ),
            ),
        )

    def test_publishes_formal_review_with_inline_comments(self) -> None:
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            reviewer.publish_review(
                "owner/repository",
                "2",
                "token",
                "review-model",
                "head-sha",
                self.plan,
                self.publication(),
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
                "owner/repository",
                "2",
                "token",
                "review-model",
                "head-sha",
                self.plan,
                reviewer.PublicationPlan((), (), ()),
            )

        request.assert_called_once()

    def test_replies_to_existing_thread_without_duplicate_inline_comment(self) -> None:
        follow_up = reviewer.FindingFollowUp(self.finding, self.review_thread())
        publication = reviewer.PublicationPlan((), (follow_up,), ())
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[[], {}]) as request,
            mock.patch.object(reviewer, "reply_to_review_thread") as reply,
            mock.patch("builtins.print"),
        ):
            reviewer.publish_review(
                "owner/repository",
                "2",
                "token",
                "review-model",
                "head-sha",
                self.plan,
                publication,
            )

        reply.assert_called_once()
        self.assertEqual(reply.call_args.args[:4], ("owner/repository", "2", "token", 101))
        self.assertIn("openrouter-thread-followup:head-sha:thread-1", reply.call_args.args[4])
        review_payload = request.call_args_list[1].args[3]
        self.assertEqual(review_payload["comments"], [])

    def test_does_not_repeat_follow_up_for_the_same_head_and_thread(self) -> None:
        original = self.review_thread()
        thread = reviewer.ReviewThread(
            node_id=original.node_id,
            path=original.path,
            side=original.side,
            line=original.line,
            original_line=original.original_line,
            outdated=original.outdated,
            viewer_can_reply=original.viewer_can_reply,
            comments=(
                *original.comments,
                pr_review_context.ReviewComment(
                    "existing-reply",
                    102,
                    "github-actions",
                    "<!-- openrouter-thread-followup:head-sha:thread-1 -->",
                ),
            ),
        )
        publication = reviewer.PublicationPlan(
            (),
            (reviewer.FindingFollowUp(self.finding, thread),),
            (),
        )
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[[], {}]),
            mock.patch.object(reviewer, "reply_to_review_thread") as reply,
            mock.patch("builtins.print"),
        ):
            reviewer.publish_review(
                "owner/repository",
                "2",
                "token",
                "review-model",
                "head-sha",
                self.plan,
                publication,
            )

        reply.assert_not_called()


class PublicationPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.finding = reviewer.Finding(
            "P1",
            "network.yml",
            "RIGHT",
            48,
            "Tailscale key exposed",
            "The Tailscale authentication key is visible in process argv.",
            "Read the key from a protected file.",
        )

    def review_thread(self, body: str) -> reviewer.ReviewThread:
        return reviewer.ReviewThread(
            node_id="thread-1",
            path="network.yml",
            side="RIGHT",
            line=48,
            original_line=48,
            outdated=False,
            viewer_can_reply=True,
            comments=(
                pr_review_context.ReviewComment("comment-node", 101, "reviewer", body),
            ),
        )

    def test_suppresses_semantic_duplicate(self) -> None:
        publication = reviewer.build_publication_plan(
            [self.finding],
            (self.review_thread("Tailscale auth key exposed in argv"),),
        )

        self.assertEqual(publication.new_findings, ())
        self.assertEqual(publication.follow_ups, ())
        self.assertEqual(publication.duplicates, (self.finding,))

    def test_routes_distinct_same_anchor_evidence_to_reply(self) -> None:
        publication = reviewer.build_publication_plan(
            [self.finding],
            (self.review_thread("This command resets previously advertised routes"),),
        )

        self.assertEqual(publication.new_findings, ())
        self.assertEqual(publication.follow_ups[0].finding, self.finding)
        self.assertEqual(publication.duplicates, ())


if __name__ == "__main__":
    unittest.main()
