"""Tests for the observable, read-only Claude review runner."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import multiprocessing
import sys
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("claude_review_runner.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("claude_review_runner", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ClaudeReviewRunnerTests(unittest.TestCase):
    def test_final_json_requires_successful_diff_read(self):
        for read_args in (None, {"path":"app.py"}, {"path":".ci-observable-review.diff", "offset":999},
                          {"path":"missing.diff"}):
            with self.subTest(read_args=read_args), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root/"app.py").write_text("answer = 1\n")
                (root/".ci-observable-review.diff").write_text("-old\n+new\n")
                responses = []
                if read_args is not None:
                    responses.append({"content":[{"type":"tool_use", "id":"t1", "name":"Read", "input":read_args}], "stop_reason":"tool_use"})
                responses.append({"content":[{"type":"text", "text":'{"summary":"Reviewed","findings":[],"thread_verdicts":[]}'}], "stop_reason":"end_turn"})
                pipe = mock.Mock()
                with mock.patch.object(runner, "request_message", side_effect=responses), mock.patch.object(runner.signal, "signal"):
                    runner._worker(pipe, "https://example.test", "key", "model", "prompt", root,
                                   {"app.py", ".ci-observable-review.diff"}, 4, 5)
                self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "diff_not_read"))

    def test_read_paginates_past_oversized_lines(self):
        for oversized in ("x" * (runner.MAX_TOOL_BYTES * 3), "я" * runner.MAX_TOOL_BYTES):
            with self.subTest(multibyte=oversized.startswith("я")), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/"diff"
                path.write_text(oversized + "\n@@ next hunk @@\n+changed\n")
                self.assertEqual(runner._read_lines(path, 2, 2), "2: @@ next hunk @@\n3: +changed\n")
                page = runner._read_lines(path, 1, 3)
                self.assertIn("line 1 exceeds", page)
                self.assertIn("3: +changed", page)

    def test_oversized_final_line_is_omitted_once_and_empty_page_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"diff"
            path.write_text("x" * (runner.MAX_TOOL_BYTES * 3))
            self.assertEqual(runner._read_lines(path, 1, 3).count("line omitted"), 1)
            self.assertEqual(runner._read_lines(path, 2, 1), "No lines in requested range.")

    def test_worker_tool_loop_validates_json_without_logging_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/runner.REVIEW_DIFF_PATH).write_text("+answer = 1\n")
            pipe = mock.Mock()
            final = '{"summary":"Checked app.py","findings":[],"thread_verdicts":[]}'
            responses = [
                {"content":[{"type":"tool_use", "id":"t1", "name":"Read", "input":{"path":"./" + runner.REVIEW_DIFF_PATH}}], "stop_reason":"tool_use"},
                {"content":[{"type":"text", "text":final}], "stop_reason":"end_turn"},
            ]
            with mock.patch.object(runner, "request_message", side_effect=responses) as request, mock.patch.object(runner.signal, "signal"):
                runner._worker(pipe, "https://example.test", "key", "model", "prompt", root, {runner.REVIEW_DIFF_PATH}, 4, 5)
            self.assertEqual(pipe.send.call_args.args[0][0], "result")
            self.assertEqual(json.loads(pipe.send.call_args.args[0][1])["findings"], [])
            self.assertEqual(request.call_count, 2)
            pipe.close.assert_called_once()

    def test_worker_rejects_truncated_results_and_thread_verdicts(self):
        for response, reason in (
            ({"content":[], "stop_reason":"max_tokens"}, "provider_incomplete_result"),
            ({"content":[{"type":"text", "text":"not JSON"}], "stop_reason":"end_turn"}, "invalid_result"),
            ({"content":[{"type":"tool_use", "name":"Read"}], "stop_reason":"tool_use"}, "invalid_tool_response"),
        ):
            with self.subTest(reason=reason), mock.patch.object(runner, "request_message", return_value=response), mock.patch.object(runner.signal, "signal"):
                pipe = mock.Mock()
                runner._worker(pipe, "https://example.test", "key", "model", "prompt", Path.cwd(), set(), 1, 5)
                self.assertEqual(pipe.send.call_args.args[0][0:2], ("failure", reason))

    def test_read_pagination_and_literal_grep(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/"app.py").write_text("first\n[x]+\nlast\n")
            allowed = {"app.py"}
            self.assertEqual(runner.execute_tool("Read", {"path":"app.py", "offset":2, "limit":1}, root, allowed), "2: [x]+\n")
            self.assertIn("app.py:2:[x]+", runner.execute_tool("Grep", {"pattern":"[x]+"}, root, allowed))
            self.assertIn("app.py", runner.execute_tool("Glob", {"pattern":"*.py"}, root, allowed))
            for name, args in (("Shell", {}), ("Read", {"path":"app.py", "offset":False}), ("Grep", {"pattern":""})):
                self.assertIn("Tool error", runner.execute_tool(name, args, root, allowed))

    def test_http_transport_sends_stream_request_and_never_leaks_error_body(self):
        response = mock.MagicMock()
        response.headers = {"Content-Type":"application/json"}
        response.read.return_value = b'{"content":[],"stop_reason":"end_turn"}'
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=response)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            result = runner.request_message("https://example.test", "secret-key", {"model":"model"}, 5)
            self.assertEqual(result["stop_reason"], "end_turn")
            request = opener.open.call_args.args[0]
            self.assertTrue(json.loads(request.data)["stream"])
            opener.open.side_effect = runner.urllib.error.HTTPError("https://example.test", 429, "secret-error", {}, None)
            with self.assertRaisesRegex(runner.ReviewFailure, "^http_429$"):
                runner.request_message("https://example.test", "secret-key", {}, 5)

    def test_stream_assembles_fragmented_tool_input_and_ignores_pings(self):
        events = [
            {"type":"message_start", "message":{"content":[], "role":"assistant"}},
            {"type":"ping"},
            {"type":"content_block_start", "index":0,
             "content_block":{"type":"tool_use", "id":"tool-1", "name":"Read", "input":{}}},
            {"type":"content_block_delta", "index":0,
             "delta":{"type":"input_json_delta", "partial_json":'{"path":'}},
            {"type":"content_block_delta", "index":0,
             "delta":{"type":"input_json_delta", "partial_json":'"app.py"}'}},
            {"type":"content_block_stop", "index":0},
            {"type":"message_delta", "delta":{"stop_reason":"tool_use"}},
            {"type":"message_stop"},
        ]
        emit = mock.Mock()
        stream = io.BytesIO(b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events))
        result = runner._stream_response(stream, emit)
        self.assertEqual(result["content"][0]["input"], {"path":"app.py"})
        self.assertEqual(result["stop_reason"], "tool_use")
        self.assertEqual(emit.call_count, 5)

    def test_stream_requires_completion_and_limits_response_size(self):
        with self.assertRaisesRegex(runner.ReviewFailure, "stream_incomplete"):
            runner._stream_response(io.BytesIO(b'data: {"type":"ping"}\n\n'), mock.Mock())
        with self.assertRaisesRegex(runner.ReviewFailure, "response_limit"):
            runner._stream_response(io.BytesIO(b"x" * (runner.MAX_RESPONSE_BYTES + 1)), mock.Mock())
        with self.assertRaisesRegex(runner.ReviewFailure, "stream_error"):
            runner._stream_response(io.BytesIO(b'data: {"type":"error","message":"secret"}\n'), mock.Mock())

    def test_read_does_not_misnumber_an_oversized_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root/"app.py").write_text("x" * (runner.MAX_TOOL_BYTES + 1) + "\nnext\n")
            result = runner.execute_tool("Read", {"path":"app.py"}, root, {"app.py"})
            self.assertIn("line 1 exceeds", result)
            self.assertNotIn("2: x", result)

    def test_event_labels_never_include_model_content(self) -> None:
        response = {
            "content": [{
                "type": "tool_use",
                "id": "tool-1",
                "name": "Read",
                "input": {"path": "secret.txt", "content": "do-not-log"},
            }],
            "stop_reason": "tool_use",
        }

        self.assertEqual(runner.event_label(response), "tool_call=Read")

    def test_runner_records_tool_activity_and_writes_final_result(self) -> None:
        responses = iter((
            {
                "content": [{
                    "type": "tool_use", "id": "tool-1", "name": "Read",
                    "input": {"path": runner.REVIEW_DIFF_PATH},
                }],
                "stop_reason": "tool_use",
            },
            {
                "content": [{
                    "type": "text",
                    "text": '{"summary":"No findings.","findings":[],"thread_verdicts":[]}',
                }],
                "stop_reason": "end_turn",
            },
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / runner.REVIEW_DIFF_PATH).write_text("+print('ok')\n", encoding="utf-8")
            output = root / "execution.json"
            log = io.StringIO()

            with mock.patch.object(runner, "request_message", side_effect=lambda *_: next(responses)):
                runner.run_review(
                    endpoint="https://gateway.example/v1/messages",
                    api_key="test-key",
                    model="test/model",
                    prompt="Review the diff.",
                    workspace=root,
                    output=output,
                    max_turns=4,
                    attempt_timeout_seconds=60,
                    inactivity_timeout_seconds=10,
                    heartbeat_seconds=1,
                    log=log,
                    allowed_files={runner.REVIEW_DIFF_PATH},
                )

            execution = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(execution[-1]["type"], "result")
            self.assertIn("No findings.", execution[-1]["result"])
            self.assertIn("tool_call=Read", log.getvalue())
            self.assertNotIn("print('ok')", log.getvalue())

    def test_tool_rejects_paths_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = runner.execute_tool("Read", {"path": "../outside.txt"}, root)

        self.assertIn("outside the review workspace", result)

    def test_untracked_files_and_symlinks_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "token").write_text("secret")
            (root / "link").symlink_to(root / "token")
            for path in ("token", "link", ".git/config"):
                result = runner.execute_tool("Read", {"path": path}, root, {"link"})
                self.assertNotIn("secret", result)

    def test_inactivity_has_heartbeat_and_kills_worker(self):
        self.check_timeout("inactivity_timeout", 0.12, 2)

    def test_total_deadline_cannot_be_reset_by_provider_activity(self):
        self.check_timeout("attempt_timeout", 2, 0.12)

    def check_timeout(self, expected, inactivity, total):
        before = {p.pid for p in multiprocessing.active_children()}
        with tempfile.TemporaryDirectory() as directory:
            log = io.StringIO()
            started = time.monotonic()
            with mock.patch.object(runner, "request_message", side_effect=lambda *_: time.sleep(5)):
                with self.assertRaisesRegex(runner.ReviewTimeout, expected):
                    runner.run_review(
                        endpoint="https://example.test/v1/messages", api_key="secret", model="test",
                        prompt="private prompt", workspace=Path(directory), output=Path(directory)/"out",
                        max_turns=2, attempt_timeout_seconds=total,
                        inactivity_timeout_seconds=inactivity, heartbeat_seconds=0.03,
                        log=log, allowed_files=set(),
                    )
            self.assertLess(time.monotonic() - started, 2)
            self.assertIn("heartbeat", log.getvalue())
            self.assertIn("provider_processing=unknown", log.getvalue())
            self.assertNotIn("secret", log.getvalue())
            self.assertEqual(before, {p.pid for p in multiprocessing.active_children()})

    def test_invalid_final_json_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/"out"
            with mock.patch.object(runner, "request_message", return_value={
                "content": [{"type": "text", "text": "no review JSON"}], "stop_reason": "end_turn",
            }):
                with self.assertRaisesRegex(RuntimeError, "invalid_result"):
                    runner.run_review(
                        endpoint="https://example.test/v1/messages", api_key="key", model="test",
                        prompt="review", workspace=Path(directory), output=output, max_turns=2,
                        attempt_timeout_seconds=3, inactivity_timeout_seconds=2, heartbeat_seconds=1,
                        log=io.StringIO(), allowed_files=set(),
                    )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
