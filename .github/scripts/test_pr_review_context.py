from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("pr_review_context.py")
SPEC = importlib.util.spec_from_file_location("pr_review_context", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
context = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = context
SPEC.loader.exec_module(context)


def comment(comment_id: int, body: str = "Tailscale auth key is exposed in argv") -> object:
    return {
        "id": f"comment-{comment_id}",
        "fullDatabaseId": str(comment_id),
        "body": body,
        "author": {"login": "reviewer"},
    }


def thread(
    *,
    thread_id: str = "thread-1",
    resolved: bool = False,
    line: int | None = 48,
    body: str = "Tailscale auth key is exposed in argv",
) -> object:
    return {
        "id": thread_id,
        "path": "hermes/ansible/tasks/network.yml",
        "diffSide": "RIGHT",
        "line": line,
        "originalLine": 48,
        "isResolved": resolved,
        "isOutdated": False,
        "viewerCanReply": True,
        "comments": {"nodes": [comment(101, body)]},
    }


class ReviewThreadFetchTest(unittest.TestCase):
    def test_fetches_only_unresolved_threads_and_parses_reply_id(self) -> None:
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [thread(), thread(thread_id="resolved", resolved=True)],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        with mock.patch.object(context, "_request_json", return_value=response):
            threads = context.fetch_unresolved_review_threads("owner/repo", "2", "token")

        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].reply_to_comment_id, 101)
        self.assertEqual(threads[0].line, 48)


class ReviewContextRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        parsed = context._parse_thread(thread())
        assert parsed is not None
        self.thread = parsed

    def test_renders_machine_readable_context_for_selected_paths(self) -> None:
        rendered = context.render_review_context(
            [self.thread],
            frozenset({"hermes/ansible/tasks/network.yml"}),
        )

        self.assertIn('"reply_to_comment_id": 101', rendered)
        self.assertIn("Tailscale auth key", rendered)
        self.assertEqual(context.render_review_context([self.thread], frozenset({"other.py"})), "[]")

    def test_matches_duplicate_by_anchor_and_meaning(self) -> None:
        match = context.match_existing_thread(
            "hermes/ansible/tasks/network.yml",
            "RIGHT",
            48,
            "The Tailscale authentication key is exposed through the process argv.",
            [self.thread],
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.relationship, "duplicate")

    def test_treats_different_evidence_on_same_anchor_as_extension(self) -> None:
        match = context.match_existing_thread(
            "hermes/ansible/tasks/network.yml",
            "RIGHT",
            48,
            "The command resets previously configured advertised routes.",
            [self.thread],
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.relationship, "extension")


class ReviewThreadReplyTest(unittest.TestCase):
    def test_posts_reply_to_the_rest_thread_endpoint(self) -> None:
        with mock.patch.object(context, "_request_json", return_value={}) as request:
            context.reply_to_review_thread("owner/repo", "2", "token", 101, "Additional evidence")

        self.assertEqual(
            request.call_args.args[0],
            "https://api.github.com/repos/owner/repo/pulls/2/comments/101/replies",
        )
        self.assertEqual(request.call_args.args[1], "POST")
        self.assertEqual(request.call_args.args[3], {"body": "Additional evidence"})

    def test_command_rejects_comment_outside_unresolved_threads(self) -> None:
        parsed = context._parse_thread(thread())
        assert parsed is not None
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token",
        }
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(
                context,
                "fetch_unresolved_review_threads",
                return_value=[parsed],
            ),
            mock.patch.object(context, "reply_to_review_thread") as reply,
        ):
            with self.assertRaisesRegex(RuntimeError, "not a replyable unresolved"):
                context._command_reply(999, "Do not post this")

        reply.assert_not_called()


class InlineCommentTest(unittest.TestCase):
    def test_tracks_changed_lines_on_both_sides(self) -> None:
        with (
            mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
            mock.patch.object(
                context,
                "_run_git",
                return_value="""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -4,2 +4,2 @@
-old
+new
 context
""",
            ),
        ):
            lines = context.changed_diff_lines("a" * 40, "b" * 40, "app.py")

        self.assertEqual(lines["LEFT"], {4})
        self.assertEqual(lines["RIGHT"], {4})

    def test_validates_structured_findings_against_the_diff(self) -> None:
        result = {
            "summary": "Found one issue.",
            "thread_verdicts": [
                {
                    "thread_id": "machine-thread",
                    "verdict": "rejected",
                    "reason": "The assertion immediately below validates the keys.",
                }
            ],
            "findings": [
                {
                    "severity": "P2",
                    "path": "app.py",
                    "side": "RIGHT",
                    "line": 12,
                    "title": "Wrong value",
                    "impact": "The API returns stale data.",
                    "fix": "Return the current value.",
                },
                {
                    "severity": "P1",
                    "path": "app.py",
                    "side": "RIGHT",
                    "line": 11,
                    "title": "Context-only claim",
                    "impact": "Invalid.",
                    "fix": "Invalid.",
                },
            ],
        }
        with (
            mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
            mock.patch.object(
                context,
                "changed_diff_lines",
                return_value={"LEFT": set(), "RIGHT": {12}},
            ),
        ):
            summary, findings, verdicts = context._validated_claude_result(
                context.json.dumps(result),
                "a" * 40,
                "b" * 40,
            )

        self.assertEqual(summary, "Found one issue.")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 12)
        self.assertEqual(verdicts[0].verdict, "rejected")

    def test_extracts_and_validates_plain_json_from_claude_execution_file(self) -> None:
        review = {
            "summary": "No actionable findings.",
            "findings": [],
            "thread_verdicts": [],
        }
        execution = [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": context.json.dumps(review),
            },
        ]
        environment = {"BASE_SHA": "a" * 40, "HEAD_SHA": "b" * 40}
        with tempfile.TemporaryDirectory() as temporary_directory:
            execution_file = Path(temporary_directory) / "execution.json"
            output_file = Path(temporary_directory) / "review.json"
            execution_file.write_text(context.json.dumps(execution), encoding="utf-8")
            with (
                mock.patch.dict(context.os.environ, environment, clear=True),
                mock.patch.object(context, "_changed_paths", return_value=set()),
                mock.patch("builtins.print"),
            ):
                context._command_extract(execution_file, output_file)
            extracted = context.json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(extracted, review)

    def test_extracts_json_fence_from_last_assistant_event(self) -> None:
        review = {
            "summary": "No actionable findings.",
            "findings": [],
            "thread_verdicts": [],
        }
        execution = {
            "events": [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"```json\n{context.json.dumps(review)}\n```",
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "result": ""},
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            execution_file = Path(temporary_directory) / "execution.json"
            execution_file.write_text(context.json.dumps(execution), encoding="utf-8")
            response = context._claude_final_response(execution_file)

        self.assertEqual(context.json.loads(context._json_response_text(response)), review)

    def test_rejects_final_response_with_surrounding_prose(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
            context._json_response_text(
                'Review complete: {"summary":"ok","findings":[],"thread_verdicts":[]}'
            )

    def test_rejects_json_that_does_not_match_the_review_contract(self) -> None:
        invalid_review = {
            "summary": "Found one issue.",
            "findings": [
                {
                    "severity": "P3",
                    "path": "app.py",
                    "side": "RIGHT",
                    "line": 12,
                    "title": "Wrong value",
                    "impact": "The API returns stale data.",
                    "fix": "Return the current value.",
                }
            ],
            "thread_verdicts": [],
        }
        with self.assertRaisesRegex(RuntimeError, "invalid finding value"):
            context._normalized_claude_result(
                context.json.dumps(invalid_review),
                "a" * 40,
                "b" * 40,
            )

    def test_publisher_posts_validated_inline_and_summary_comments(self) -> None:
        result = {
            "summary": "Found one issue.",
            "thread_verdicts": [],
            "findings": [
                {
                    "severity": "P2",
                    "path": "app.py",
                    "side": "RIGHT",
                    "line": 12,
                    "title": "Wrong value",
                    "impact": "The API returns stale data.",
                    "fix": "Return the current value.",
                }
            ],
        }
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token",
            "BASE_SHA": "a" * 40,
            "HEAD_SHA": "b" * 40,
            "CLAUDE_REVIEW_MODEL": "review-model",
            "CLAUDE_REVIEW_RUN_ID": "123",
            "CLAUDE_REVIEW_RESULT": context.json.dumps(result),
        }
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
            mock.patch.object(
                context,
                "changed_diff_lines",
                return_value={"LEFT": set(), "RIGHT": {12}},
            ),
            mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[]),
            mock.patch.object(context, "create_inline_comment") as create,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        self.assertEqual(create.call_count, 1)
        self.assertIn(context.CLAUDE_REVIEWER_LABEL, create.call_args.args[7])
        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("Found one issue.", summary_payload["body"])
        self.assertIn(context.CLAUDE_REVIEWER_LABEL, summary_payload["body"])
        self.assertIn("<!-- claude-pr-review:" + "b" * 40 + ":123 -->", summary_payload["body"])

    def test_publisher_falls_back_to_summary_when_github_rejects_an_inline_anchor(self) -> None:
        result = {
            "summary": "Found one issue.",
            "thread_verdicts": [],
            "findings": [
                {
                    "severity": "P2",
                    "path": "app.py",
                    "side": "RIGHT",
                    "line": 12,
                    "title": "Wrong value",
                    "impact": "The API returns stale data.",
                    "fix": "Return the current value.",
                }
            ],
        }
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token",
            "BASE_SHA": "a" * 40,
            "HEAD_SHA": "b" * 40,
            "CLAUDE_REVIEW_MODEL": "review-model",
            "CLAUDE_REVIEW_RUN_ID": "123",
            "CLAUDE_REVIEW_RESULT": context.json.dumps(result),
        }
        anchor_error = context.GitHubRequestError(
            "POST https://api.github.com/repos/owner/repo/pulls/2/comments "
            "failed with HTTP 422: could not be resolved"
        )
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(context, "_changed_paths", return_value={"app.py"}),
            mock.patch.object(
                context,
                "changed_diff_lines",
                return_value={"LEFT": set(), "RIGHT": {12}},
            ),
            mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[]),
            mock.patch.object(context, "create_inline_comment", side_effect=anchor_error),
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("New inline findings: 0.", summary_payload["body"])
        self.assertIn("Findings without inline anchors:", summary_payload["body"])
        self.assertIn("`app.py:12`", summary_payload["body"])

    def test_publisher_rejects_and_resolves_only_a_current_machine_thread(self) -> None:
        head_sha = "b" * 40
        result = {
            "summary": "The first reviewer has one false positive.",
            "findings": [],
            "thread_verdicts": [
                {
                    "thread_id": "direct-thread",
                    "verdict": "rejected",
                    "reason": "The validation appears later in the same assert task.",
                }
            ],
        }
        thread = context.ReviewThread(
            node_id="direct-thread",
            path="playbook.yml",
            side="RIGHT",
            line=42,
            original_line=42,
            outdated=False,
            viewer_can_reply=True,
            comments=(
                context.ReviewComment(
                    "comment-node",
                    101,
                    context.AUTOMATED_REVIEW_AUTHOR,
                    f"{context.DIRECT_REVIEWER_INLINE_PREFIX}{head_sha}:playbook.yml:RIGHT:42 -->",
                ),
            ),
        )
        environment = {
            "GITHUB_REPOSITORY": "owner/repo",
            "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token",
            "BASE_SHA": "a" * 40,
            "HEAD_SHA": head_sha,
            "CLAUDE_REVIEW_MODEL": "review-model",
            "CLAUDE_REVIEW_RUN_ID": "123",
            "CLAUDE_REVIEW_RESULT": context.json.dumps(result),
        }
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(context, "_changed_paths", return_value={"playbook.yml"}),
            mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[thread]),
            mock.patch.object(context, "reply_to_review_thread") as reply,
            mock.patch.object(context, "resolve_review_thread") as resolve,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        reply.assert_called_once()
        self.assertIn("Rejected OpenRouterAPI finding", reply.call_args.args[4])
        resolve.assert_called_once_with("token", "direct-thread")
        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("rejected and auto-resolved: 1", summary_payload["body"])

    def test_direct_machine_thread_never_auto_resolves_after_human_reply(self) -> None:
        head_sha = "b" * 40
        thread = context.ReviewThread(
            node_id="direct-thread",
            path="playbook.yml",
            side="RIGHT",
            line=42,
            original_line=42,
            outdated=False,
            viewer_can_reply=True,
            comments=(
                context.ReviewComment(
                    "machine-comment",
                    101,
                    context.AUTOMATED_REVIEW_AUTHOR,
                    f"{context.DIRECT_REVIEWER_INLINE_PREFIX}{head_sha}:playbook.yml:RIGHT:42 -->",
                ),
                context.ReviewComment("human-reply", 102, "owner", "Please keep this open."),
            ),
        )

        self.assertFalse(context._is_direct_machine_thread(thread, head_sha))


if __name__ == "__main__":
    unittest.main()
