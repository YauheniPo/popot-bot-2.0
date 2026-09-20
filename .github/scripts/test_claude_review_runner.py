"""Tests for the observable, read-only Claude review runner."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
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
    def bounded_worker(self, *, max_turns=4, repeat=False, ignore_final=False, read_diff=True):
        payloads = []
        final = '{"summary":"Checked available evidence","findings":[],"thread_verdicts":[]}'

        def respond(_endpoint, _key, payload, *_):
            payloads.append(json.loads(json.dumps(payload)))
            if not payload.get("tools") and not ignore_final:
                return {"content": [{"type": "text", "text": final}], "stop_reason": "end_turn"}
            turn = len(payloads)
            name = "Read" if repeat or turn == 1 else "Grep"
            args = {"path": runner.REVIEW_DIFF_PATH if read_diff else "app.py"} if name == "Read" else {"pattern": f"query{turn}"}
            return {"content": [{"type": "tool_use", "id": f"t{turn}", "name": name, "input": args}], "stop_reason": "tool_use"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / runner.REVIEW_DIFF_PATH).write_text("-old\n+new\n")
            (root / "app.py").write_text("context evidence\n")
            pipe = mock.Mock()
            with mock.patch.object(runner, "request_message", side_effect=respond), \
                    mock.patch.object(runner, "execute_tool", wraps=runner.execute_tool) as execute, \
                    mock.patch.object(runner.signal, "signal"):
                runner._worker(pipe, "https://example.test", "secret-key", "model", "review prompt", root,
                               {runner.REVIEW_DIFF_PATH, "app.py"}, max_turns, 5)
        return payloads, pipe, execute

    def test_last_turn_is_reserved_for_json_without_tools(self):
        payloads, pipe, execute = self.bounded_worker()
        self.assertEqual(pipe.send.call_args.args[0][0], "result")
        self.assertEqual(len(payloads), 4)
        self.assertEqual(execute.call_count, 3)
        self.assertNotIn("tools", payloads[-1])
        self.assertIn("1: -old", json.dumps(payloads[-1]))
        for message in payloads[-1]["messages"]:
            if isinstance(message["content"], list):
                self.assertTrue(all(block["type"] == "text" for block in message["content"]))
        # Transforming the final transcript must not mutate earlier requests.
        self.assertEqual(payloads[1]["messages"][1]["content"][0]["type"], "tool_use")
        self.assertIn("finalization_started reason=turn_budget", str(pipe.send.call_args_list))

    def test_repeated_reads_are_not_executed_and_finalize_early(self):
        payloads, pipe, execute = self.bounded_worker(max_turns=12, repeat=True)
        self.assertEqual(pipe.send.call_args.args[0][0], "result")
        self.assertLess(len(payloads), 12)
        execute.assert_called_once()
        self.assertIn("tool_cache_hit", str(pipe.send.call_args_list))
        self.assertIn("finalization_started reason=repeated_tools", str(pipe.send.call_args_list))

    def test_tool_calls_in_final_round_are_not_executed_or_accepted(self):
        payloads, pipe, execute = self.bounded_worker(ignore_final=True)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "turn_limit"))
        self.assertNotIn("tools", payloads[-1])
        self.assertEqual(execute.call_count, 3)

    def test_forced_finalization_cannot_bypass_diff_read_requirement(self):
        _, pipe, _ = self.bounded_worker(read_diff=False)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "diff_not_read"))

    def test_tool_cache_keeps_pagination_and_argument_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / runner.REVIEW_DIFF_PATH).write_text("-old\n+new\n")
            args = {"path": runner.REVIEW_DIFF_PATH, "offset": 1, "limit": 1}
            blocks = [{"id": "a", "name": "Read", "input": args},
                      {"id": "b", "name": "Read", "input": dict(reversed(list(args.items())))},
                      {"id": "c", "name": "Read", "input": {**args, "offset": 2}}]
            read_files, cache = set(), {}
            with mock.patch.object(runner, "execute_tool", wraps=runner.execute_tool) as execute:
                results = runner._tool_results(blocks, root, {runner.REVIEW_DIFF_PATH}, mock.Mock(), read_files, cache)
            self.assertEqual(execute.call_count, 3)
            self.assertIn("1: -old", results[1]["content"])
            self.assertIn("2: +new", results[2]["content"])
            self.assertEqual(read_files, {runner.REVIEW_DIFF_PATH})

    def test_distinct_same_input_tool_calls_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / runner.REVIEW_DIFF_PATH).write_text("-old\n+new\n")
            args = {"path": runner.REVIEW_DIFF_PATH, "offset": 1, "limit": 1}
            blocks = [{"id": "a", "name": "Read", "input": args},
                      {"id": "b", "name": "Read", "input": args}]
            with mock.patch.object(runner, "execute_tool", wraps=runner.execute_tool) as execute:
                runner._tool_results(blocks, root, {runner.REVIEW_DIFF_PATH}, mock.Mock(), set(), {})
            self.assertEqual(execute.call_count, 2)

    def rate_limit(self, headers=None, body=None):
        error = runner.urllib.error.HTTPError("https://example.test", 429, "secret-key", headers or {},
            io.BytesIO(json.dumps({} if body is None else body).encode()))
        with mock.patch.object(runner.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(runner.RateLimitFailure) as caught:
                runner.request_message("https://example.test", "secret-key", {}, 5)
        self.assertTrue(error.closed)
        self.assertEqual(str(caught.exception), "http_429")
        self.assertNotIn("secret-key", json.dumps(caught.exception.details))
        return caught.exception.details

    def test_rate_limit_preserves_only_safe_diagnostics(self):
        details = self.rate_limit({"Retry-After": "45", "X-RateLimit-Remaining": "0"},
            {"error": {"message": "Rate limit exceeded: free-models-per-day. secret-key"}})
        self.assertEqual(details["retry_after_seconds"], 45)
        self.assertEqual(details["scope"], "platform")
        self.assertEqual(details["quota"], "free_daily")
        self.assertEqual(details["remaining"], 0)

    def test_rate_limit_provider_and_unknown_are_not_daily_quota(self):
        details = self.rate_limit(body={"error": {"metadata": {"provider_code": 429,
            "provider_name": "secret-key"}}})
        self.assertEqual(details["scope"], "provider")
        self.assertEqual(details["quota"], "unknown")
        for body in ({}, {"error": "secret-key"}, {"error": {"metadata": []}}, []):
            with self.subTest(body=body):
                details = self.rate_limit({"Retry-After": "NaN"}, body)
                self.assertEqual(details["scope"], "unknown")
                self.assertNotIn("retry_after_seconds", details)

    def test_rate_limit_dates_and_reset_headers(self):
        with mock.patch.object(runner.time, "time", return_value=1_700_000_000):
            for headers, expected in (({"Retry-After": "Tue, 14 Nov 2023 22:14:20 GMT"}, 60),
                                      ({"X-RateLimit-Reset": "1700000060000"}, 60),
                                      ({"X-RateLimit-Reset": "1700000060", "Retry-After": "90"}, 90)):
                with self.subTest(headers=headers):
                    self.assertEqual(self.rate_limit(headers)["retry_after_seconds"], expected)

    def test_rate_limit_survives_worker_pipe(self):
        failure = runner.RateLimitFailure({"scope": "provider", "quota": "unknown", "retry_after_seconds": 45})
        pipe = mock.Mock()
        with mock.patch.object(runner, "request_message", side_effect=failure), mock.patch.object(runner.signal, "signal"):
            runner._worker(pipe, "https://example.test", "key", "model", "p", Path.cwd(), set(), 1, 5)
        kind, value, turns = pipe.send.call_args.args[0]
        output = Path("unused.json")
        log = io.StringIO()
        with self.assertRaises(runner.RateLimitFailure) as caught:
            runner._handle_message(kind, value, turns, output, 0, 0, log)
        self.assertEqual(caught.exception.details, failure.details)

    def test_rate_limit_diagnostics_survive_real_worker_and_cleanup(self):
        failure = runner.RateLimitFailure({"scope": "provider", "quota": "unknown", "retry_after_seconds": 45})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runner, "request_message", side_effect=failure):
            root = Path(directory)
            before = {p.pid for p in multiprocessing.active_children()}
            output = root / "result.json"
            allowed_files = set()
            log = io.StringIO()
            with self.assertRaises(runner.RateLimitFailure) as caught:
                runner.run_review(endpoint="https://example.test", api_key="secret-key", model="m", prompt="p",
                    workspace=root, output=output, allowed_files=allowed_files, max_turns=1,
                    attempt_timeout_seconds=5, inactivity_timeout_seconds=2, heartbeat_seconds=1, log=log)
            self.assertEqual(caught.exception.details, failure.details)
            self.assertFalse((root/"result.json").exists())
            self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_rate_limit_body_is_bounded_and_unparseable_body_keeps_headers(self):
        for raw in (b"secret-key not json", b"x" * 20_000):
            error = mock.Mock(headers={"Retry-After": "12"})
            error.read.return_value = raw
            details = runner._rate_limit_details(error)
            error.read.assert_called_once_with(16_385)
            self.assertEqual(details, {"scope":"unknown", "quota":"unknown", "retry_after_seconds":12})

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
            payload = request.call_args.args[2]
            self.assertEqual(payload["temperature"], 0)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
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
        emit = mock.Mock()
        stream = io.BytesIO(b'data: {"type":"ping"}\n\n')
        with self.assertRaisesRegex(runner.ReviewFailure, "stream_incomplete"):
            runner._stream_response(stream, emit)
        stream = io.BytesIO(b"x" * (runner.MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(runner.ReviewFailure, "response_limit"):
            runner._stream_response(stream, emit)
        stream = io.BytesIO(b'data: {"type":"error","message":"secret"}\n')
        with self.assertRaisesRegex(runner.ReviewFailure, "stream_error"):
            runner._stream_response(stream, emit)

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
            log = io.StringIO()
            workspace = Path(directory)
            with mock.patch.object(runner, "request_message", return_value={
                "content": [{"type": "text", "text": "no review JSON"}], "stop_reason": "end_turn",
            }):
                with self.assertRaisesRegex(RuntimeError, "invalid_result"):
                    runner.run_review(
                        endpoint="https://example.test/v1/messages", api_key="key", model="test",
                        prompt="review", workspace=workspace, output=output, max_turns=2,
                        attempt_timeout_seconds=3, inactivity_timeout_seconds=2, heartbeat_seconds=1,
                        log=log, allowed_files=set(),
                    )
            self.assertFalse(output.exists())

    def test_tracked_files_lists_git_files_and_enforces_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "app.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            self.assertEqual(runner.tracked_files(root), {"app.py"})
        huge = "\0".join(f"f{i}" for i in range(runner.MAX_FILES + 1))
        cwd = Path.cwd()
        with mock.patch.object(runner.subprocess, "run", return_value=mock.Mock(stdout=huge.encode())):
            with self.assertRaisesRegex(runner.ReviewFailure, "repository_file_limit"):
                runner.tracked_files(cwd)

    def test_workspace_path_rejects_non_string_missing_and_non_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            allowed = {"sub"}
            with self.assertRaises(ValueError):
                runner._workspace_path(root, None, allowed)
            with self.assertRaises(ValueError):
                runner._workspace_path(root, "", allowed)
            with self.assertRaises(ValueError):
                runner._workspace_path(root, "sub", allowed)

    def test_read_lines_truncates_at_output_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big"
            path.write_text("".join(f"line {i}\n" for i in range(20000)))
            result = runner._read_lines(path, 1, 20000)
            self.assertIn("output limit; continue at line", result)

    def test_matching_files_skips_unresolvable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to(root / "missing-target")
            allowed = {"link"}
            self.assertEqual(list(runner._matching_files(root, allowed, "*")), [])

    def test_search_breaks_at_match_limit(self):
        paths = [(f"f{i}.py", Path(f"/x/f{i}.py")) for i in range(150)]
        with mock.patch.object(runner, "_matching_files", return_value=iter(paths)), \
                mock.patch.object(runner, "_grep_file", return_value=["m:1:needle"]):
            result = runner._search("Grep", {"pattern": "needle", "glob": "*"}, Path("/x"), set())
        self.assertIn("needle", result)
        self.assertIn("at most 100 results", result)

    def test_execute_tool_rejects_non_dict_arguments(self):
        self.assertIn("Tool error", runner.execute_tool("Read", "not-a-dict", Path.cwd(), set()))

    def test_no_redirect_rejects_redirects(self):
        redirector = runner.NoRedirect()
        with self.assertRaisesRegex(runner.ReviewFailure, "provider_redirect_rejected"):
            redirector.redirect_request(None, None, 302, "Found", {}, "https://evil.test")

    def test_apply_delta_accumulates_text_and_thinking(self):
        blocks, tool_json = {0: {}}, {}
        label = runner._apply_delta({"index": 0, "delta": {"type": "text_delta", "text": "hi"}}, blocks, tool_json)
        self.assertEqual(label, "provider_content_delta")
        self.assertEqual(blocks[0]["text"], "hi")
        label = runner._apply_delta({"index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}, blocks, tool_json)
        self.assertEqual(label, "provider_reasoning_delta")
        self.assertEqual(blocks[0]["thinking"], "hmm")

    def test_request_message_stream_and_error_branches(self):
        stream_response = mock.MagicMock()
        stream_response.headers = {"Content-Type": "text/event-stream"}
        stream_response.readline.side_effect = [b'data: {"type":"message_stop"}\n', b""]
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=stream_response)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            result = runner.request_message("https://example.test", "key", {}, 5)
        self.assertEqual(result["content"], [])

        oversize = mock.MagicMock()
        oversize.headers = {"Content-Type": "application/json"}
        oversize.read.return_value = b"x" * (runner.MAX_RESPONSE_BYTES + 1)
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=oversize)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(runner.ReviewFailure, "provider_response_limit"):
                runner.request_message("https://example.test", "key", {}, 5)

        bad_json = mock.MagicMock()
        bad_json.headers = {"Content-Type": "application/json"}
        bad_json.read.return_value = b"not json"
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=bad_json)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(runner.ReviewFailure, "provider_invalid_response"):
                runner.request_message("https://example.test", "key", {}, 5)

        not_dict = mock.MagicMock()
        not_dict.headers = {"Content-Type": "application/json"}
        not_dict.read.return_value = b"[]"
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=not_dict)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(runner.ReviewFailure, "provider_invalid_response"):
                runner.request_message("https://example.test", "key", {}, 5)

        with mock.patch.object(runner.urllib.request, "build_opener", side_effect=OSError("down")):
            with self.assertRaisesRegex(runner.ReviewFailure, "provider_connection_error"):
                runner.request_message("https://example.test", "key", {}, 5)

    def test_final_result_accepts_ignored_thread_verdicts(self):
        result = runner._final_result({"stop_reason": "end_turn"},
            [{"type": "text", "text": '{"summary":"s","findings":[],"thread_verdicts":[{"thread_id":"t","verdict":"confirmed","reason":"still valid"}]}'}])
        self.assertIn('"thread_verdicts"', result)

    def test_final_result_requires_end_turn(self):
        with self.assertRaisesRegex(runner.ReviewFailure, "provider_incomplete_result"):
            runner._final_result({"stop_reason": "max_tokens"},
                [{"type": "text", "text": '{"summary":"s","findings":[],"thread_verdicts":[]}'}])

    def test_worker_handles_invalid_content_and_turn_limit(self):
        with mock.patch.object(runner, "request_message", return_value={"content": "not-a-list"}), \
                mock.patch.object(runner.signal, "signal"):
            pipe = mock.Mock()
            runner._worker(pipe, "https://example.test", "key", "model", "prompt", Path.cwd(), set(), 2, 5)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "provider_invalid_response"))

        with mock.patch.object(runner, "request_message", return_value={
                "content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
                "stop_reason": "tool_use"}), mock.patch.object(runner.signal, "signal"):
            pipe = mock.Mock()
            runner._worker(pipe, "https://example.test", "key", "model", "prompt", Path.cwd(), set(), 2, 5)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "turn_limit"))

    def test_worker_rejects_tool_use_with_wrong_stop_reason(self):
        # Tool blocks present but stop_reason is not tool_use -> invalid_tool_response.
        with mock.patch.object(runner, "request_message", return_value={
                "content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}],
                "stop_reason": "end_turn"}), mock.patch.object(runner.signal, "signal"):
            pipe = mock.Mock()
            runner._worker(pipe, "https://example.test", "key", "model", "prompt", Path.cwd(), set(), 2, 5)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "invalid_tool_response"))

    def test_worker_reports_worker_error_on_unexpected_exception(self):
        with mock.patch.object(runner, "request_message", side_effect=RuntimeError("boom")), \
                mock.patch.object(runner.signal, "signal"):
            pipe = mock.Mock()
            runner._worker(pipe, "https://example.test", "key", "model", "prompt", Path.cwd(), set(), 2, 5)
        self.assertEqual(pipe.send.call_args.args[0][:2], ("failure", "worker_error"))

    def test_validate_limits_rejects_non_positive(self):
        with self.assertRaisesRegex(runner.ReviewFailure, "invalid_limits"):
            runner._validate_limits(0, 1, 1, 1, 8192)
        with self.assertRaisesRegex(runner.ReviewFailure, "invalid_limits"):
            runner._validate_limits(1, 1, 1, 1, 0)

    def test_terminate_worker_handles_no_pid_and_stubborn_worker(self):
        runner._terminate_worker(mock.Mock(pid=None))
        stub = mock.Mock()
        stub.pid = 123
        stub.is_alive.return_value = True
        stub.join.return_value = None
        runner._terminate_worker(stub)
        self.assertTrue(stub.kill.called)

    def test_cancel_signal_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            runner._cancel_signal()

    def test_recv_message_raises_on_eof(self):
        receive = mock.Mock()
        receive.recv.side_effect = EOFError
        with self.assertRaisesRegex(runner.ReviewFailure, "worker_exited_without_result"):
            runner._recv_message(receive)


if __name__ == "__main__":
    unittest.main()
