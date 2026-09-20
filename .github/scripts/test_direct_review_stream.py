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


class StreamTest(unittest.TestCase):
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
                with self.assertRaisesRegex(stream.StreamFailure, expected):
                    stream.read_completion(sse(*events), stream.Progress(io.StringIO()))

    def test_keepalive_does_not_count_as_model_progress(self):
        progress = stream.Progress(io.StringIO())
        before = progress.last_activity
        with self.assertRaises(stream.StreamFailure):
            stream.read_completion(io.BytesIO(b": keepalive\n\nevent: ping\n\n"), progress)
        self.assertEqual(progress.last_activity, before)
        self.assertEqual(progress.events, 0)

    def test_bounded_response(self):
        with mock.patch.object(stream, "MAX_RESPONSE_BYTES", 20):
            with self.assertRaisesRegex(stream.StreamFailure, "response_limit"):
                stream.read_completion(sse(delta(content="x" * 25)), stream.Progress(io.StringIO()))

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
                with self.assertRaisesRegex(stream.StreamFailure, "invalid_stream"):
                    stream.read_completion(sse(event), stream.Progress(io.StringIO()))

    def test_http_response_stream_and_oversized_json(self):
        response = sse(delta(content="{}"), {"choices": [{"finish_reason": "stop"}]}, "[DONE]")
        response.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.assertEqual(stream.read_response(response, stream.Progress(io.StringIO()))["choices"][0]["message"]["content"], "{}")
        with mock.patch.object(stream, "MAX_RESPONSE_BYTES", 2):
            with self.assertRaisesRegex(stream.StreamFailure, "response_limit"):
                stream.read_response(io.BytesIO(b"{} "), stream.Progress(io.StringIO()))

    def test_does_not_overwrite_an_existing_alarm(self):
        with mock.patch.object(signal, "getitimer", return_value=(1, 0)), mock.patch.object(signal, "signal") as install:
            with self.assertRaisesRegex(stream.StreamFailure, "watchdog_already_active"):
                with stream.watchdog(total=1, idle=1, heartbeat=1, log=io.StringIO()):
                    self.fail("must not enter with another watchdog active")
            install.assert_not_called()

    def test_watchdog_interrupts_blocked_io_and_restores_signal(self):
        previous = signal.getsignal(signal.SIGALRM)
        log = io.StringIO()
        started = time.monotonic()
        with self.assertRaisesRegex(stream.StreamFailure, "inactivity_timeout"):
            with stream.watchdog(total=1, idle=.08, heartbeat=.02, log=log):
                time.sleep(2)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.assertIn("provider_processing=unknown", log.getvalue())

    def test_total_deadline_wins_even_when_provider_keeps_thinking(self):
        with self.assertRaisesRegex(stream.StreamFailure, "attempt_timeout"):
            with stream.watchdog(total=.08, idle=1, heartbeat=.02, log=io.StringIO()) as progress:
                while True:
                    progress.record(reasoning="x")
                    time.sleep(.005)


if __name__ == "__main__":
    unittest.main()
