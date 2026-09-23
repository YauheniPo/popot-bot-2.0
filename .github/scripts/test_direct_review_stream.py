from __future__ import annotations

import io
import json
from pathlib import Path
import signal
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import direct_review_stream as stream


def sse(*events):
    return io.BytesIO(b"".join(
        b"data: " + (event.encode() if isinstance(event, str) else json.dumps(event).encode()) + b"\n\n"
        for event in events
    ))


def delta(**values):
    return {"choices": [{"index": 0, "delta": values, "finish_reason": None}]}


def enter_existing_alarm_watchdog(log):
    with stream.watchdog(total=1, idle=1, heartbeat=1, log=log):
        raise AssertionError("must not enter with another watchdog active")


def run_blocked_watchdog(watchdog):
    with watchdog:
        time.sleep(2)


def run_reasoning_watchdog(watchdog):
    with watchdog as progress:
        while True:
            progress.record(reasoning="x")
            time.sleep(.005)


class StreamTest(unittest.TestCase):
    def test_request_diagnostics_distinguish_stream_endings_without_content(self):
        stop = {"choices": [{"finish_reason": "stop"}]}
        for tail, reason, finish, done, eof in (
            ((stop, "[DONE]"), None, "stop", True, False),
            ((stop,), "stream_incomplete", "stop", False, True),
            (("[DONE]",), "stream_incomplete", "none", True, False),
            ((), "stream_incomplete", "none", False, True),
            (({"choices": [{"finish_reason": "PRIVATE"}]}, "[DONE]"),
             "stream_incomplete", "other", True, False),
            (({"choices": [{"finish_reason": "length"}]},),
             "output_limit", "length", False, True),
            (({"error": {"message": "PRIVATE"}},),
             "provider_stream_error", "none", False, False),
        ):
            with self.subTest(tail=tail):
                log = io.StringIO()
                response = sse(delta(reasoning_content="PRIVATE"), delta(content="SECRET"), *tail)
                wire_bytes = len(response.getvalue())
                response.headers = {"Content-Type": "text/event-stream; PRIVATE"}
                response.status = 200
                def read():
                    with stream.watchdog(total=1, idle=1, heartbeat=1, log=log) as progress:
                        return stream.read_response(response, progress)
                if reason:
                    with self.assertRaisesRegex(stream.StreamFailure, reason):
                        read()
                else:
                    read()
                line, = log.getvalue().splitlines()
                diagnostic = json.loads(line.removeprefix("[direct-review] request_end "))
                self.assertEqual(diagnostic["outcome"], reason or "received")
                self.assertEqual(diagnostic["finish_reason"], finish)
                self.assertEqual(diagnostic["done_seen"], done)
                self.assertEqual(diagnostic["eof_seen"], eof)
                self.assertEqual(diagnostic["http_status"], 200)
                self.assertEqual(diagnostic["response_format"], "sse")
                self.assertEqual(diagnostic["wire_bytes"], wire_bytes)
                self.assertEqual(diagnostic["sse_events"], 2 + len(tail))
                self.assertEqual(diagnostic["content_chars"], 6)
                self.assertEqual(diagnostic["reasoning_chars"], 7)
                self.assertNotIn("PRIVATE", line)
                self.assertNotIn("SECRET", line)

    def test_request_diagnostics_cover_transport_failure_before_headers(self):
        log = io.StringIO()
        with self.assertRaises(ConnectionError):
            with stream.watchdog(total=1, idle=1, heartbeat=1, log=log):
                raise ConnectionError("PRIVATE token and URL")
        diagnostic = json.loads(log.getvalue().split("request_end ")[1])
        self.assertEqual(diagnostic["outcome"], "transport_error")
        self.assertEqual(diagnostic["state"], "waiting_for_headers")
        self.assertEqual(diagnostic["http_status"], None)
        self.assertNotIn("PRIVATE", log.getvalue())

    def test_reassembles_content_but_never_logs_reasoning_or_text(self):
        progress = stream.Progress(io.StringIO())
        response = sse(delta(reasoning_content="PRIVATE thinking"),
                       delta(content='{"summary":"ok",'), delta(content='"findings":[]}'),
                       {"id": "abc", "model": "test", "choices": [
                           {"index": 0, "delta": {}, "finish_reason": "stop"}]},
                       {"choices": [], "usage": {"completion_tokens": 12}}, "[DONE]")
        result = stream.read_completion(response, progress)
        self.assertEqual(json.loads(result["choices"][0]["message"]["content"])["findings"], [])
        self.assertEqual(result["model"], "test")
        self.assertEqual(result["usage"]["completion_tokens"], 12)
        self.assertEqual(progress.reasoning_chars, len("PRIVATE thinking"))
        progress.heartbeat()
        log = progress.log.getvalue()
        self.assertIn("reasoning_chars=16", log)
        self.assertNotIn("PRIVATE", log)
        self.assertNotIn('"summary"', log)

    def test_rejects_incomplete_error_and_truncated_streams(self):
        for events, expected in (
            ((delta(content='{"summary":"ok","findings":[]}'),), "stream_incomplete"),
            ((delta(content="{}"), "[DONE]"), "stream_incomplete"),
            (({"error": {"message": "PRIVATE"}},), "provider_stream_error"),
            (({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}, "[DONE]"), "output_limit"),
            (("not-json",), "invalid_stream"),
        ):
            with self.subTest(expected=expected):
                response = sse(*events)
                progress = stream.Progress(io.StringIO())
                with self.assertRaisesRegex(stream.StreamFailure, expected):
                    stream.read_completion(response, progress)

    def test_keepalive_does_not_count_as_model_progress(self):
        progress = stream.Progress(io.StringIO())
        before = progress.last_activity
        response = io.BytesIO(b": keepalive\n\nevent: ping\n\n")
        with self.assertRaises(stream.StreamFailure):
            stream.read_completion(response, progress)
        self.assertEqual(progress.last_activity, before)
        self.assertEqual(progress.events, 0)

    def test_bounded_response(self):
        response = sse(delta(content="x" * 25))
        progress = stream.Progress(io.StringIO())
        with mock.patch.object(stream, "MAX_RESPONSE_BYTES", 20):
            with self.assertRaisesRegex(stream.StreamFailure, "response_limit"):
                stream.read_completion(response, progress)

    def test_final_event_without_newline_and_reasoning_details(self):
        response = sse({"choices": [{"index": 1, "delta": {"content": "ignore"}}]},
                       delta(reasoning_details=[{"text": "private"}]), delta(content="{}"),
                       {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        response = io.BytesIO(response.getvalue() + b"data: [DONE]")
        progress = stream.Progress(io.StringIO())
        self.assertEqual(stream.read_completion(response, progress)["choices"][0]["message"]["content"], "{}")
        self.assertGreater(progress.reasoning_chars, 0)

    def test_bad_event_shapes_are_rejected(self):
        for event in ([], delta(content=["bad"]), {"choices": [None]}):
            with self.subTest(event=event):
                response = sse(event)
                progress = stream.Progress(io.StringIO())
                with self.assertRaisesRegex(stream.StreamFailure, "invalid_stream"):
                    stream.read_completion(response, progress)

    def test_http_response_stream_and_oversized_json(self):
        response = sse(delta(content="{}"), {"choices": [{"finish_reason": "stop"}]}, "[DONE]")
        response.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.assertEqual(stream.read_response(response, stream.Progress(io.StringIO()))["choices"][0]["message"]["content"], "{}")
        oversized_response = io.BytesIO(b"{} ")
        progress = stream.Progress(io.StringIO())
        with mock.patch.object(stream, "MAX_RESPONSE_BYTES", 2):
            with self.assertRaisesRegex(stream.StreamFailure, "response_limit"):
                stream.read_response(oversized_response, progress)

    def test_does_not_overwrite_an_existing_alarm(self):
        log = io.StringIO()
        with mock.patch.object(signal, "getitimer", return_value=(1, 0)), mock.patch.object(signal, "signal") as install:
            with self.assertRaisesRegex(stream.StreamFailure, "watchdog_already_active"):
                enter_existing_alarm_watchdog(log)
            install.assert_not_called()

    def test_watchdog_interrupts_blocked_io_and_restores_signal(self):
        previous = signal.getsignal(signal.SIGALRM)
        log = io.StringIO()
        started = time.monotonic()
        watchdog = stream.watchdog(total=1, idle=.08, heartbeat=.02, log=log)
        with self.assertRaisesRegex(stream.StreamFailure, "inactivity_timeout"):
            run_blocked_watchdog(watchdog)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.assertIn("provider_processing=unknown", log.getvalue())
        self.assertIn('"outcome": "inactivity_timeout"', log.getvalue())

    def test_total_deadline_wins_even_when_provider_keeps_thinking(self):
        log = io.StringIO()
        watchdog = stream.watchdog(total=.08, idle=1, heartbeat=.02, log=log)
        with self.assertRaisesRegex(stream.StreamFailure, "attempt_timeout"):
            run_reasoning_watchdog(watchdog)


if __name__ == "__main__":
    unittest.main()
