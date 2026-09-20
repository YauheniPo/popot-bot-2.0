from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("pr_review_context.py")
SPEC = importlib.util.spec_from_file_location("pr_review_context", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
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
    def test_all_publishers_can_deduplicate_settled_observable_threads_without_owning_them(self):
        settled = context.ReviewThread("settled", "app.py", "RIGHT", 1, 1, False, True,
            (context.ReviewComment("comment", 7, context.AUTOMATED_REVIEW_AUTHOR,
                "<!-- observable-inline:" + "a"*40 + ":123:1:digest -->\nExisting finding"),), resolved=True)
        with mock.patch.object(context, "_fetch_review_threads", return_value=[settled]):
            self.assertEqual(context.fetch_resolved_machine_threads("owner/repo", "1", "token"), [settled])
        self.assertFalse(context.is_machine_thread(settled))

    def test_review_thread_page_rejects_invalid_graphql_shapes(self) -> None:
        cases = [
            object(),
            {"errors": ["boom"]},
            {"data": {}},
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": {}, "pageInfo": {}}}}}},
        ]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(context.GitHubRequestError):
                context._review_thread_page(response)

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
        self.assertFalse(threads[0].resolved)

    def test_fetches_only_resolved_threads_opened_by_a_reviewer_bot(self) -> None:
        machine_body = f"<!-- claude-inline:{'c' * 40}:0123456789abcdef -->\nFinding"
        nodes = [
            thread(thread_id="unresolved-machine", body=machine_body),
            thread(thread_id="resolved-human", resolved=True),
            thread(thread_id="resolved-machine", resolved=True, body=machine_body),
        ]
        nodes[2]["comments"]["nodes"][0]["author"] = {"login": context.AUTOMATED_REVIEW_AUTHOR}
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        with mock.patch.object(context, "_request_json", return_value=response):
            threads = context.fetch_resolved_machine_threads("owner/repo", "2", "token")

        self.assertEqual([item.node_id for item in threads], ["resolved-machine"])


class MachineThreadTest(unittest.TestCase):
    def test_review_summary_groups_outcome_activity_and_metadata(self) -> None:
        body = context._render_review_summary(
            head_sha="b" * 40,
            summary="No additional findings were found.",
            new_inline_findings=0,
            unanchored_findings=1,
            follow_ups=0,
            duplicate_findings=2,
            previously_settled_findings=3,
            confirmed_machine_findings=1,
            fixed_machine_findings=4,
            rejected_machine_findings=5,
            machine_findings_needing_human=6,
            changed_file_count=7,
        )

        self.assertIn("### Review outcome", body)
        self.assertIn("### Technical metadata", body)
        self.assertIn("Complete base-to-head diff supplied · 7 changed file(s)", body)
        self.assertIn("**Action required**", body)
        self.assertIn("### Finding activity", body)
        self.assertIn("| New inline findings | 0 |", body)
        self.assertIn("### Existing machine-review threads", body)
        self.assertIn("| Confirmed and still open | 1 |", body)
        self.assertIn("Reviewed head: `" + "b" * 40 + "`", body)

    def test_execution_metadata_does_not_enter_future_model_context_or_duplicate_matching(self):
        finding = "The API returns a stale account status."
        metadata = "\n<!-- review-execution -->\nConnection nvidia fallback transport_error\n<!-- /review-execution -->"
        review_thread = self._thread(finding + metadata)
        self.assertEqual(review_thread.searchable_text.strip(), finding)
        rendered = context.render_review_context([review_thread])
        self.assertIn(finding, rendered)
        self.assertNotIn("transport_error", rendered)

    @staticmethod
    def _thread(body: str, *, author: str = context.AUTOMATED_REVIEW_AUTHOR) -> object:
        return context.ReviewThread(
            node_id="thread",
            path="app.py",
            side="RIGHT",
            line=12,
            original_line=12,
            outdated=False,
            viewer_can_reply=True,
            comments=(context.ReviewComment("node", 1, author, body),),
        )

    def test_accepts_either_reviewer_marker_from_an_earlier_revision(self) -> None:
        older_sha = "a" * 40
        for prefix in ("<!-- direct-openrouter-inline:", "<!-- claude-inline:"):
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    context.is_machine_thread(self._thread(f"{prefix}{older_sha}:app.py -->\nx"))
                )

    def test_rejects_a_bot_thread_without_a_reviewer_marker(self) -> None:
        self.assertFalse(context.is_machine_thread(self._thread("A plain bot comment")))

    def test_a_fix_claim_needs_a_changed_file_and_an_earlier_revision(self) -> None:
        head_sha = "b" * 40
        stale = self._thread(f"<!-- claude-inline:{'a' * 40}:app.py -->")
        current = self._thread(f"<!-- claude-inline:{head_sha}:app.py -->")

        self.assertTrue(context.may_be_auto_fixed(stale, head_sha, {"app.py"}))
        # Nothing has changed since a thread opened against this same revision.
        self.assertFalse(context.may_be_auto_fixed(current, head_sha, {"app.py"}))
        # The revision does not touch the file the thread is about.
        self.assertFalse(context.may_be_auto_fixed(stale, head_sha, {"other.py"}))

    def test_rejects_a_human_authored_thread(self) -> None:
        marker = f"<!-- claude-inline:{'a' * 40}:app.py -->"
        self.assertFalse(context.is_machine_thread(self._thread(marker, author="owner")))


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
    def test_resolves_thread_after_github_confirms_it(self) -> None:
        with mock.patch.object(
            context,
            "_request_json",
            return_value={"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}},
        ) as request:
            context.resolve_review_thread("token", "thread-1")

        self.assertEqual(request.call_args.args[1], "POST")

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
    def test_decode_execution_document_supports_legacy_and_rejects_invalid_data(self) -> None:
        self.assertEqual(context._decode_execution_document('{"event": "one"}'), [{"event": "one"}])
        self.assertEqual(context._decode_execution_document('{"events": []}'), [])
        self.assertEqual(context._decode_execution_document('{"messages": []}'), [])
        with self.assertRaises(RuntimeError):
            context._decode_execution_document("\n")
        with self.assertRaises(RuntimeError):
            context._decode_execution_document("null")
        with self.assertRaises(context.json.JSONDecodeError):
            context._decode_execution_document("not-json")

    def test_validate_cli_path_rejects_relative_and_traversal_paths(self) -> None:
        for path in (Path("relative.json"), Path("/tmp/../etc/passwd")):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                context._validate_cli_path(path, "test path")

    def test_validate_cli_path_maps_resolution_errors(self) -> None:
        with mock.patch.object(Path, "resolve", side_effect=OSError("denied")), \
                self.assertRaisesRegex(RuntimeError, "unavailable"):
            context._validate_cli_path(Path("/tmp/output.json"), "test path")

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

    def test_processes_all_validated_thread_verdicts_up_to_contract_limit(self) -> None:
        result = {
            "summary": "Re-checked all threads.",
            "findings": [],
            "thread_verdicts": [
                {"thread_id": f"thread-{index}", "verdict": "confirmed", "reason": "Still applies."}
                for index in range(200)
            ],
        }
        with mock.patch.object(context, "_changed_paths", return_value=set()):
            _, _, verdicts = context._validated_claude_result(
                context.json.dumps(result), "a" * 40, "b" * 40
            )

        self.assertEqual(len(verdicts), 200)

    def test_discards_invalid_findings_without_crashing(self) -> None:
        raw_findings = [
                None,
                {"severity": "P3", "path": "app.py", "side": "RIGHT", "line": 1},
                {"severity": "P2", "path": "app.py", "side": "RIGHT", "line": True},
                {"severity": "P2", "path": "other.py", "side": "RIGHT", "line": 1},
                {"severity": "P2", "path": "app.py", "side": "RIGHT", "line": 99},
                {"severity": "P2", "path": "app.py", "side": "RIGHT", "line": 1,
                 "title": "", "impact": "x", "fix": "y"},
            ]
        raw_verdicts = [None, {"thread_id": "", "verdict": "confirmed", "reason": "x"},
                        {"thread_id": "t", "verdict": "unknown", "reason": "x"}]
        with mock.patch.object(context, "_changed_paths", return_value={"app.py"}), \
                mock.patch.object(context, "changed_diff_lines", return_value={"LEFT": set(), "RIGHT": {1}}):
            findings = context._parse_review_findings(raw_findings, "a" * 40, "b" * 40)
            verdicts = context._parse_thread_verdicts(raw_verdicts)
        self.assertEqual(findings, [])
        self.assertEqual(verdicts, [])

    def test_discards_finding_with_empty_text_field(self) -> None:
        raw_findings = [
            {"severity": "P2", "path": "app.py", "side": "RIGHT", "line": 1,
             "title": "", "impact": "Impact.", "fix": "Fix."},
        ]
        with mock.patch.object(context, "_changed_paths", return_value={"app.py"}), \
                mock.patch.object(context, "changed_diff_lines", return_value={"LEFT": set(), "RIGHT": {1}}):
            findings = context._parse_review_findings(raw_findings, "a" * 40, "b" * 40)
        self.assertEqual(findings, [])

    def test_extract_rejects_json_without_successful_diff_read(self):
        final = {"type":"result", "is_error":False, "result":context.json.dumps({
            "summary":"No findings", "findings":[], "thread_verdicts":[]})}
        call = {"type":"assistant", "message":{"content":[{
            "type":"tool_use", "id":"read-1", "name":"Read",
            "input":{"file_path":str(Path.cwd()/".ci-pr-review.diff")}}]}}
        result = {"type":"user", "message":{"content":[{
            "type":"tool_result", "tool_use_id":"read-1", "content":"1→diff --git a/app.py b/app.py\n2→+new"}]}}
        variants = [[], [call], [result], [call, final, result],
            [call, {"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"read-1", "is_error":True, "content":"1→permission denied"}]}}],
            [call, {"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"read-1", "content":"File is empty."}]}}],
            [call, {"type":"assistant", "message":result["message"]}],
            [call, {"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"wrong", "content":"1→+new"}]}}],
        ]
        for index, events in enumerate(variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                execution, output = Path(directory)/"execution.json", Path(directory)/"review.json"
                execution.write_text(context.json.dumps([*events, final]))
                with mock.patch.dict(context.os.environ, {"BASE_SHA":"a"*40, "HEAD_SHA":"b"*40}, clear=True), mock.patch.object(context, "_changed_paths", return_value=set()):
                    with self.assertRaisesRegex(RuntimeError, "diff_not_read"):
                        context._command_extract(execution, output)
                self.assertFalse(output.exists())

    def test_is_diff_read_call_swallows_resolve_errors(self) -> None:
        block = {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/x"}}

        class BadPath:
            def __init__(self, *args, **kwargs):
                pass

            def resolve(self):
                raise OSError("boom")

        with mock.patch.object(context, "Path", BadPath):
            self.assertFalse(context._is_diff_read_call(block, Path("/diff")))

    def test_extracts_and_validates_plain_json_from_claude_execution_file(self) -> None:
        review = {
            "summary": "No actionable findings.",
            "findings": [],
            "thread_verdicts": [],
        }
        execution = [
            {"type": "system", "subtype": "init"},
            {"type":"assistant", "message":{"content":[{
                "type":"tool_use", "id":"read-1", "name":"Read",
                "input":{"file_path":str(Path.cwd()/".ci-pr-review.diff")}}]}},
            {"type":"user", "message":{"content":[{
                "type":"tool_result", "tool_use_id":"read-1", "content":"1→diff --git a/app.py b/app.py\n2→+new"}]}},
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

    def test_read_evidence_accepts_sdk_shapes_and_rejects_wrong_scope(self):
        def events(path, output, *, tool="Read", parent=None):
            return [
                {"type":"assistant", "parent_tool_use_id":parent, "message":{"content":[{
                    "type":"tool_use", "name":tool, "id":"r1", "input":{"file_path":path}}]}},
                {"type":"user", "parent_tool_use_id":parent, "message":{"content":[{
                    "type":"tool_result", "tool_use_id":"r1", "content":output}]}},
            ]
        for output in ("    1→diff --git a/app.py b/app.py", "1\t+new", [{"type":"text", "text":"2: +new"}]):
            with self.subTest(output=output):
                context._require_claude_diff_read(events("./.ci-pr-review.diff", output))
        invalid = [events("app.py", "1→+new"), events("/another/repo/.ci-pr-review.diff", "1→+new"),
                   events(".ci-pr-review.diff", "1→+new", tool="Grep"),
                   events(".ci-pr-review.diff", "1→+new", parent="subagent"),
                   events(None, "1→+new"), events(".ci-pr-review.diff", "1→   \n"),
                   [*events(".ci-pr-review.diff", "1→+new"), {"type":"system", "subtype":"init"}],
                   [events(".ci-pr-review.diff", "1→+new")[0], *events("app.py", "1→other file")]]
        for index, stream in enumerate(invalid):
            with self.subTest(index=index), self.assertRaisesRegex(RuntimeError, "diff_not_read"):
                context._require_claude_diff_read(stream)

    def test_decodes_json_lines_execution_output(self) -> None:
        self.assertEqual(
            context._decode_execution_document('{"type":"system"}\n{"type":"result"}'),
            [{"type": "system"}, {"type": "result"}],
        )

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

    def test_extracts_schema_valid_json_from_surrounding_prose(self) -> None:
        review = {"summary": "ok", "findings": [], "thread_verdicts": []}

        extracted = context._json_response_text(
            f"Review complete.\n```json\n{context.json.dumps(review)}\n```\nDone."
        )

        self.assertEqual(context.json.loads(extracted), review)

    def test_rejects_prose_without_schema_valid_review_json(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no schema-valid JSON object"):
            context._json_response_text('Review complete: {"status":"ok"}')

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
        invalid_payload = context.json.dumps(invalid_review)
        with self.assertRaisesRegex(RuntimeError, "invalid finding value"):
            context._normalized_claude_result(
                invalid_payload,
                "a" * 40,
                "b" * 40,
            )

    def test_rejects_empty_impact_and_fix_values(self) -> None:
        for empty_field in ("impact", "fix"):
            invalid_review = {
                "summary": "Found one issue.",
                "findings": [
                    {
                        "severity": "P2",
                        "path": "app.py",
                        "side": "RIGHT",
                        "line": 12,
                        "title": "Wrong value",
                        "impact": "The API returns stale data.",
                        "fix": "Return the current value.",
                        empty_field: "   ",
                    }
                ],
                "thread_verdicts": [],
            }
            with self.subTest(field=empty_field):
                invalid_payload = context.json.dumps(invalid_review)
                with self.assertRaisesRegex(RuntimeError, "invalid finding value"):
                    context._normalized_claude_result(
                        invalid_payload,
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
            "CLAUDE_REVIEW_PROVIDER": "openrouter",
            "CLAUDE_REVIEW_ENDPOINT": "https://openrouter.ai/api",
            "CLAUDE_REVIEW_PRIMARY_MODEL": "review-model",
            "CLAUDE_REVIEW_FALLBACK_MODEL": "fallback-model",
            "CLAUDE_REVIEW_PRIMARY_OUTCOME": "failure",
            "CLAUDE_REVIEW_RETRY_OUTCOME": "failure",
            "CLAUDE_REVIEW_FALLBACK_OUTCOME": "success",
            "CLAUDE_REVIEW_FALLBACK_VALIDATION": "success",
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
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[]),
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
        self.assertIn("### Technical metadata", summary_payload["body"])
        self.assertIn("<!-- claude-pr-review:" + "b" * 40 + ":123 -->", summary_payload["body"])
        for body in (summary_payload["body"], create.call_args.args[7]):
            self.assertIn("openrouter", body)
            self.assertIn("fallback-model", body)
            self.assertNotIn("Provider: Ollama Cloud", body)
        self.assertIn("CI attempt #3", create.call_args.args[7])

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
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[]),
            mock.patch.object(context, "create_inline_comment", side_effect=anchor_error),
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("| New inline findings | 0 |", summary_payload["body"])
        self.assertIn("Findings without inline anchors:", summary_payload["body"])
        self.assertIn("`app.py:12`", summary_payload["body"])
        self.assertIn("The API returns stale data.", summary_payload["body"])
        self.assertIn("Return the current value.", summary_payload["body"])

    def test_publisher_rejects_and_resolves_an_untouched_machine_thread(self) -> None:
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
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[]),
            mock.patch.object(context, "reply_to_review_thread") as reply,
            mock.patch.object(context, "resolve_review_thread") as resolve,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        reply.assert_called_once()
        self.assertIn("Rejected finding", reply.call_args.args[4])
        resolve.assert_called_once_with("token", "direct-thread")
        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("| Rejected and auto-resolved | 1 |", summary_payload["body"])

    def test_publisher_replies_to_confirmed_machine_thread(self) -> None:
        self._publish_thread_verdict("confirmed")

    def test_publisher_replies_to_needs_human_machine_thread(self) -> None:
        self._publish_thread_verdict("needs_human")

    def _publish_thread_verdict(self, verdict: str) -> None:
        head_sha = "b" * 40
        result = {
            "summary": "Thread review.",
            "findings": [],
            "thread_verdicts": [{"thread_id": "direct-thread", "verdict": verdict, "reason": "Evidence."}],
        }
        thread = context.ReviewThread(
            node_id="direct-thread", path="playbook.yml", side="RIGHT", line=42,
            original_line=42, outdated=False, viewer_can_reply=True,
            comments=(context.ReviewComment(
                "comment-node", 101, context.AUTOMATED_REVIEW_AUTHOR,
                f"{context.DIRECT_REVIEWER_INLINE_PREFIX}{'a' * 40}:playbook.yml:RIGHT:42 -->",
            ),),
        )
        environment = {
            "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "2", "GITHUB_TOKEN": "token",
            "BASE_SHA": "a" * 40, "HEAD_SHA": head_sha, "CLAUDE_REVIEW_MODEL": "review-model",
            "CLAUDE_REVIEW_RUN_ID": "123", "CLAUDE_REVIEW_RESULT": context.json.dumps(result),
        }
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(context, "_changed_paths", return_value={"playbook.yml"}),
            mock.patch.object(context, "fetch_unresolved_review_threads", return_value=[thread]),
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[]),
            mock.patch.object(context, "reply_to_review_thread") as reply,
            mock.patch.object(context, "resolve_review_thread") as resolve,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]),
            mock.patch("builtins.print"),
        ):
            context._command_publish()
        reply.assert_called_once()
        self.assertIn("Evidence: Evidence.", reply.call_args.args[4])
        resolve.assert_not_called()

    def _stale_machine_thread(self) -> object:
        """A reviewer thread opened on an older revision, with no human reply."""
        return context.ReviewThread(
            node_id="stale-thread",
            path="playbook.yml",
            side="RIGHT",
            line=42,
            original_line=42,
            outdated=True,
            viewer_can_reply=True,
            comments=(
                context.ReviewComment(
                    "comment-node",
                    101,
                    context.AUTOMATED_REVIEW_AUTHOR,
                    f"{context.DIRECT_REVIEWER_INLINE_PREFIX}{'a' * 40}:playbook.yml:RIGHT:42 -->",
                ),
            ),
        )

    def _publish_verdict(
        self,
        verdict: str,
        *,
        changed_paths: set[str],
    ) -> tuple[mock.Mock, mock.Mock, mock.Mock]:
        result = {
            "summary": "Checked the earlier finding against this revision.",
            "findings": [],
            "thread_verdicts": [
                {
                    "thread_id": "stale-thread",
                    "verdict": verdict,
                    "reason": "playbook.yml:383 now passes follow: false.",
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
            mock.patch.object(context, "_changed_paths", return_value=changed_paths),
            mock.patch.object(
                context,
                "fetch_unresolved_review_threads",
                return_value=[self._stale_machine_thread()],
            ),
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[]),
            mock.patch.object(context, "reply_to_review_thread") as reply,
            mock.patch.object(context, "resolve_review_thread") as resolve,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()
        return reply, resolve, request

    def test_publisher_resolves_a_fixed_thread_from_an_earlier_revision(self) -> None:
        reply, resolve, request = self._publish_verdict(
            "fixed",
            changed_paths={"playbook.yml"},
        )

        reply.assert_called_once()
        self.assertIn("the requested change is present", reply.call_args.args[4])
        resolve.assert_called_once_with("token", "stale-thread")
        self.assertIn("| Fixed and auto-resolved | 1 |", request.call_args_list[1].args[3]["body"])

    def test_publisher_downgrades_fixed_when_the_file_is_untouched(self) -> None:
        reply, resolve, request = self._publish_verdict(
            "fixed",
            changed_paths={"other.py"},
        )

        reply.assert_not_called()
        resolve.assert_not_called()
        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("| Waiting for human review | 1 |", summary_payload["body"])
        self.assertIn("| Fixed and auto-resolved | 0 |", summary_payload["body"])

    def test_publisher_suppresses_a_finding_that_repeats_a_resolved_thread(self) -> None:
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
        settled = context.ReviewThread(
            node_id="settled-thread",
            path="app.py",
            side="RIGHT",
            line=12,
            original_line=12,
            outdated=False,
            viewer_can_reply=True,
            comments=(
                context.ReviewComment(
                    "comment-node",
                    101,
                    context.AUTOMATED_REVIEW_AUTHOR,
                    f"<!-- claude-inline:{'a' * 40}:0123456789abcdef -->\n"
                    "Wrong value. The API returns stale data. Return the current value.",
                ),
            ),
            resolved=True,
        )
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
            mock.patch.object(context, "fetch_resolved_machine_threads", return_value=[settled]),
            mock.patch.object(context, "create_inline_comment") as create,
            mock.patch.object(context, "_request_json", side_effect=[[], {}]) as request,
            mock.patch("builtins.print"),
        ):
            context._command_publish()

        create.assert_not_called()
        summary_payload = request.call_args_list[1].args[3]
        self.assertIn("| Resolved findings not re-raised | 1 |", summary_payload["body"])
        self.assertIn("Repeats of already-resolved threads, suppressed:", summary_payload["body"])

    def test_machine_thread_never_auto_resolves_after_a_human_reply(self) -> None:
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

        self.assertFalse(context.is_machine_thread(thread))


class PublisherHelpersTest(unittest.TestCase):
    def test_fetch_review_threads_rejects_non_integer_pr_number(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PR_NUMBER must be an integer"):
            context._fetch_review_threads(
                "owner/repo", "not-a-number", "token", resolved=False, limit=1
            )

    def test_fetch_review_threads_stops_at_limit(self) -> None:
        nodes = [thread(thread_id="a"), thread(thread_id="b")]
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
        with mock.patch.object(context, "_request_json", return_value=response):
            threads = context._fetch_review_threads(
                "owner/repo", "2", "token", resolved=False, limit=1
            )
        self.assertEqual(len(threads), 1)

    def test_result_event_text_raises_on_error_result(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsuccessful result"):
            context._result_event_text({"type": "result", "is_error": True})

    def test_assistant_event_text_handles_missing_and_non_text_content(self) -> None:
        self.assertIsNone(context._assistant_event_text({"type": "assistant"}))
        self.assertIsNone(
            context._assistant_event_text({"type": "assistant", "message": {"content": "  "}})
        )
        self.assertIsNone(
            context._assistant_event_text(
                {"type": "assistant", "message": {"content": [{"type": "tool", "text": "x"}]}}
            )
        )
        self.assertEqual(
            context._assistant_event_text(
                {"type": "assistant", "message": {"content": "final text"}}
            ),
            "final text",
        )

    def test_review_already_posted_detects_marker_and_short_page(self) -> None:
        with mock.patch.object(context, "_request_json", return_value=[{"body": "has-marker"}]):
            self.assertTrue(context._review_already_posted("url", "has-marker", "token"))
        with mock.patch.object(context, "_request_json", return_value=[]):
            self.assertFalse(context._review_already_posted("url", "marker", "token"))

    def test_review_already_posted_returns_false_after_exhausting_pages(self) -> None:
        # 100 comment objects without the marker on every page: the loop never
        # breaks on a short page and falls through to the final return False.
        full_page = [{"body": "unrelated comment"} for _ in range(100)]
        with mock.patch.object(context, "_request_json", return_value=full_page) as request:
            self.assertFalse(context._review_already_posted("url", "needle-marker", "token"))
        self.assertEqual(request.call_count, 50)

    def test_auto_resolve_verdict_skips_without_reply_comment(self) -> None:
        thread = context.ReviewThread(
            node_id="t", path="p", side="RIGHT", line=1, original_line=1,
            outdated=False, viewer_can_reply=False, comments=(),
        )
        verdict = context.ThreadVerdict("t", "rejected", "reason")
        result = context._auto_resolve_verdict(
            thread, "marker", verdict, "owner/repo", "1", "token", "", set()
        )
        self.assertEqual(result, "skipped")

    def test_process_single_verdict_skips_non_machine_thread(self) -> None:
        human_thread = context.ReviewThread(
            node_id="t", path="p", side="RIGHT", line=1, original_line=1,
            outdated=False, viewer_can_reply=True,
            comments=(context.ReviewComment("c", 101, "human", "body"),),
        )
        verdict = context.ThreadVerdict("t", "rejected", "reason")
        result = context._process_single_verdict(
            verdict, {"t": human_thread}, "b" * 40, set(),
            "owner/repo", "1", "token", "", set(),
        )
        self.assertEqual(result, "skipped")

    def test_process_single_verdict_skips_already_posted_marker(self) -> None:
        machine_body = f"<!-- claude-inline:{'a' * 40}:0123456789abcdef -->\nFinding"
        machine_thread = context.ReviewThread(
            node_id="t", path="p", side="RIGHT", line=1, original_line=1,
            outdated=False, viewer_can_reply=True,
            comments=(
                context.ReviewComment("c", 101, context.AUTOMATED_REVIEW_AUTHOR, machine_body),
            ),
        )
        verdict = context.ThreadVerdict("t", "rejected", "reason")
        with mock.patch.object(context, "may_be_auto_fixed", return_value=True), \
                mock.patch.object(context, "_auto_resolve_verdict") as auto:
            auto.return_value = "skipped"
            # Mark the verdict marker as already present by mocking any() via comments
            # containing the marker. We craft the marker to appear in the body.
            verdict_marker = f"<!-- claude-thread-verdict:{'b' * 40}:t -->"
            thread_with_marker = context.ReviewThread(
                node_id="t", path="p", side="RIGHT", line=1, original_line=1,
                outdated=False, viewer_can_reply=True,
                comments=(
                    context.ReviewComment(
                        "c", 101, context.AUTOMATED_REVIEW_AUTHOR,
                        f"{machine_body}\n{verdict_marker}",
                    ),
                ),
            )
            result = context._process_single_verdict(
                verdict, {"t": thread_with_marker}, "b" * 40, set(),
                "owner/repo", "1", "token", "", set(),
            )
        self.assertEqual(result, "skipped")
        auto.assert_not_called()

    def test_command_publish_rejects_non_numeric_run_id(self) -> None:
        environment = {
            "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token", "BASE_SHA": "a" * 40, "HEAD_SHA": "b" * 40,
            "CLAUDE_REVIEW_MODEL": "review-model", "CLAUDE_REVIEW_RUN_ID": "abc",
        }
        with mock.patch.dict(context.os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "CLAUDE_REVIEW_RUN_ID must be numeric"):
                context._command_publish()

    def test_command_publish_skips_when_review_already_posted(self) -> None:
        environment = {
            "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "2",
            "GITHUB_TOKEN": "token", "BASE_SHA": "a" * 40, "HEAD_SHA": "b" * 40,
            "CLAUDE_REVIEW_MODEL": "review-model", "CLAUDE_REVIEW_RUN_ID": "123",
        }
        with (
            mock.patch.dict(context.os.environ, environment, clear=True),
            mock.patch.object(context, "_review_already_posted", return_value=True),
            mock.patch("builtins.print"),
        ):
            context._command_publish()


if __name__ == "__main__":
    unittest.main()
