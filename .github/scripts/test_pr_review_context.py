from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
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


if __name__ == "__main__":
    unittest.main()
