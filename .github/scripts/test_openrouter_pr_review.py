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

    def test_full_review_rejects_partial_coverage(self) -> None:
        plan = reviewer.ReviewPlan(
            chunks=(),
            total_files=2,
            complete_files=frozenset(),
            partial_files=frozenset({"large.py"}),
            omitted_files=frozenset({"later.py"}),
        )
        with mock.patch.dict(
            reviewer.os.environ,
            {"REQUIRE_COMPLETE_REVIEW": "true"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "full-branch review exceeds"):
                reviewer.ensure_required_coverage(plan)

    def test_normal_review_allows_partial_coverage(self) -> None:
        plan = reviewer.ReviewPlan(
            chunks=(),
            total_files=1,
            complete_files=frozenset(),
            partial_files=frozenset(),
            omitted_files=frozenset({"later.py"}),
        )
        with mock.patch.dict(reviewer.os.environ, {}, clear=True):
            reviewer.ensure_required_coverage(plan)


class ChangedPathsTest(unittest.TestCase):
    def test_full_review_includes_reviewer_implementation_files(self) -> None:
        paths = (
            b".github/scripts/openrouter_pr_review.py\0"
            b".github/REVIEWER.md\0app.py\0"
        )
        with (
            mock.patch.object(reviewer, "run_git", return_value=paths),
            mock.patch.dict(
                reviewer.os.environ,
                {"REVIEW_INCLUDE_REVIEWER_FILES": "true"},
                clear=False,
            ),
        ):
            changed = reviewer.changed_paths("base", "head")

        self.assertEqual(
            changed,
            [
                ".github/scripts/openrouter_pr_review.py",
                ".github/REVIEWER.md",
                "app.py",
            ],
        )


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

    def test_rejects_mismatched_response_and_chunk_counts(self) -> None:
        review_file = reviewer.ReviewFile(
            "app.py",
            "RIGHT 5|+changed",
            frozenset({5}),
            frozenset(),
        )
        chunk = reviewer.ReviewChunk("diff", frozenset({"app.py"}), ("app.py",))

        with self.assertRaisesRegex(ValueError, "same length"):
            reviewer.validate_findings([], (chunk,), [review_file])


class OpenRouterRequestTest(unittest.TestCase):
    def test_extracts_review_json_from_mixed_provider_text(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            "Review complete.\n```json\n"
                            '{"summary":"Reviewed.","findings":[]}'
                            "\n```\nDone."
                        )
                    },
                    "finish_reason": "stop",
                }
            ]
        }

        result = reviewer.parse_review_response(response)

        self.assertEqual(result, {"summary": "Reviewed.", "findings": []})

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
        self.assertEqual(request_body["max_tokens"], reviewer.MAX_OUTPUT_TOKENS)
        self.assertEqual(
            request_body["reasoning"],
            {"effort": "none", "exclude": True},
        )
        self.assertEqual(request_body["plugins"], [{"id": "response-healing"}])
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

    def test_regenerates_after_malformed_structured_json(self) -> None:
        malformed = {
            "choices": [
                {
                    "message": {"content": '{"summary":"Reviewed.","findings":['},
                    "finish_reason": "length",
                }
            ]
        }
        valid = {
            "choices": [
                {"message": {"content": '{"summary":"Reviewed.","findings":[]}'}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[malformed, valid]) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print") as output,
        ):
            result = reviewer.review_chunk("api-key", "review-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 2)
        self.assertIn("finish_reason=length", output.call_args.args[0])

    def test_regenerates_after_empty_review_message(self) -> None:
        empty = {
            "choices": [
                {"message": {"content": ""}, "finish_reason": "length"}
            ]
        }
        valid = {
            "choices": [
                {"message": {"content": '{"summary":"Reviewed.","findings":[]}'}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.object(reviewer, "request_json", side_effect=[empty, valid]) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print"),
        ):
            result = reviewer.review_chunk("api-key", "review-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 2)

    def test_uses_fallback_after_repeated_malformed_primary_json(self) -> None:
        malformed = {
            "choices": [
                {
                    "message": {"content": "not-json"},
                    "finish_reason": "stop",
                }
            ]
        }
        valid = {
            "choices": [
                {"message": {"content": '{"summary":"Reviewed.","findings":[]}'}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
        with (
            mock.patch.dict(
                reviewer.os.environ,
                {"OPENROUTER_REVIEW_FALLBACK_MODEL": "fallback-model"},
            ),
            mock.patch.object(
                reviewer,
                "request_json",
                side_effect=[malformed, malformed, valid],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print"),
        ):
            result = reviewer.review_chunk("api-key", "primary-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args[3]["model"], "fallback-model")

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
            reviewer.MAX_OUTPUT_TOKENS,
        )
        self.assertEqual(
            relaxed_fallback_body["reasoning"],
            {"effort": "none", "exclude": True},
        )

    def test_enables_low_reasoning_when_fallback_requires_it(self) -> None:
        response = {
            "choices": [
                {"message": {"content": json.dumps({"summary": "Reviewed.", "findings": []})}}
            ]
        }
        chunk = reviewer.ReviewChunk("RIGHT 1|+value", frozenset({"app.py"}), ("app.py",))
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
                    reviewer.RequestError(
                        "Reasoning is mandatory for this endpoint and cannot be disabled.",
                        status=400,
                    ),
                    response,
                ],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch("builtins.print"),
        ):
            result = reviewer.review_chunk("api-key", "primary-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 3)
        reasoning_body = request.call_args_list[2].args[3]
        self.assertEqual(reasoning_body["model"], "fallback-model")
        self.assertEqual(
            reasoning_body["reasoning"],
            {"effort": "low", "exclude": True},
        )
        self.assertTrue(reasoning_body["provider"]["require_parameters"])
        self.assertEqual(reasoning_body["response_format"]["type"], "json_schema")

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

    def test_retries_transient_relaxed_fallback_failure(self) -> None:
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
                    reviewer.RequestError("upstream rate limited", status=429),
                    response,
                ],
            ) as request,
            mock.patch.object(reviewer, "read_review_rules", return_value="rules"),
            mock.patch.object(reviewer.time, "sleep") as sleep,
            mock.patch("builtins.print"),
        ):
            result = reviewer.review_chunk("api-key", "primary-model", (), chunk, 1, 1)

        self.assertEqual(result["findings"], [])
        self.assertEqual(request.call_count, 4)
        sleep.assert_called_once_with(reviewer.DEFAULT_RATE_LIMIT_RETRY_SECONDS)

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

    def test_accepts_manual_full_review_chunk_budget(self) -> None:
        with mock.patch.dict(
            reviewer.os.environ,
            {"OPENROUTER_MAX_REVIEW_CHUNKS": "60"},
        ):
            self.assertEqual(reviewer.configured_max_review_chunks(), 60)

    def test_rejects_excessive_manual_chunk_budget(self) -> None:
        with mock.patch.dict(
            reviewer.os.environ,
            {"OPENROUTER_MAX_REVIEW_CHUNKS": "101"},
        ):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 100"):
                reviewer.configured_max_review_chunks()


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
        self.assertIn(reviewer.REVIEWER_LABEL, payload["body"])
        self.assertIn(reviewer.REVIEWER_LABEL, payload["comments"][0]["body"])
        self.assertIn(
            "<!-- direct-openrouter-inline:head-sha:app.py:RIGHT:5 -->",
            payload["comments"][0]["body"],
        )

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
        self.assertIn(reviewer.REVIEWER_LABEL, reply.call_args.args[4])
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

    def test_publishes_check_run_when_a_pull_request_does_not_exist(self) -> None:
        left_finding = reviewer.Finding(
            "P1",
            "removed.py",
            "LEFT",
            7,
            "Removed validation",
            "Invalid input is accepted.",
            "Restore the validation.",
        )
        with (
            mock.patch.object(reviewer, "request_json", return_value={}) as request,
            mock.patch.dict(
                reviewer.os.environ,
                {
                    "CHECK_RUN_NAME": "Manual branch review",
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_SERVER_URL": "https://github.example",
                },
                clear=False,
            ),
            mock.patch("builtins.print"),
        ):
            reviewer.publish_check_run(
                "owner/repository",
                "token",
                "review-model",
                "b" * 40,
                self.plan,
                [self.finding, left_finding],
                "ado-42",
            )

        self.assertEqual(
            request.call_args.args[:3],
            (
                "https://api.github.com/repos/owner/repository/check-runs",
                "POST",
                reviewer._github_headers("token"),
            ),
        )
        payload = request.call_args.args[3]
        self.assertEqual(payload["name"], "Manual branch review")
        self.assertEqual(payload["head_sha"], "b" * 40)
        self.assertEqual(payload["conclusion"], "neutral")
        self.assertEqual(payload["external_id"], "ado-42")
        self.assertEqual(len(payload["output"]["annotations"]), 1)
        self.assertEqual(payload["output"]["annotations"][0]["path"], "app.py")
        self.assertIn("removed.py:7", payload["output"]["summary"])
        self.assertEqual(
            payload["details_url"],
            "https://github.example/owner/repository/actions/runs/123",
        )

    def test_successful_check_run_has_no_annotations(self) -> None:
        with (
            mock.patch.object(reviewer, "request_json", return_value={}) as request,
            mock.patch.dict(reviewer.os.environ, {}, clear=True),
            mock.patch("builtins.print"),
        ):
            reviewer.publish_check_run(
                "owner/repository",
                "token",
                "review-model",
                "b" * 40,
                self.plan,
                [],
                "manual-1",
            )

        payload = request.call_args.args[3]
        self.assertEqual(payload["conclusion"], "success")
        self.assertEqual(payload["output"]["annotations"], [])
        self.assertNotIn("details_url", payload)


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
