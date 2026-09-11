from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import ollama_review


class OllamaReviewTest(unittest.TestCase):
    def response(self, data: object):
        result = mock.MagicMock()
        result.__enter__.return_value = io.StringIO(json.dumps(data))
        return result

    def test_cloud_payload_omits_unsupported_fields_without_changing_prompt(self):
        source = {
            "model": "kimi-k3", "messages": [{"role": "system", "content": "schema"}],
            "max_tokens": 10000, "temperature": 0,
            "response_format": {"type": "json_schema"}, "provider": {},
            "plugins": [{"id": "response-healing"}], "reasoning": {"effort": "none"},
        }
        payload = ollama_review.completion_payload(source)
        self.assertEqual(set(payload), {"model", "messages", "max_tokens", "temperature"})
        self.assertEqual(payload["messages"], source["messages"])
        self.assertIn("response_format", source)

    def test_json_probe_authenticates_and_validates_plain_json(self):
        response = {"choices": [{"message": {"content": '{"status":"ok"}'}}]}
        with mock.patch.object(ollama_review.urllib.request, "urlopen", return_value=self.response(response)) as request:
            ollama_review.probe("test-key", "json")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ollama.com/v1/chat/completions")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
        payload = json.loads(sent.data)
        self.assertEqual(payload["model"], "kimi-k3")
        self.assertNotIn("response_format", payload)

    def test_tool_probe_uses_anthropic_endpoint_and_checks_tool_input(self):
        response = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        with mock.patch.object(ollama_review.urllib.request, "urlopen", return_value=self.response(response)) as request:
            ollama_review.probe("test-key", "tools")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ollama.com/v1/messages")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(json.loads(sent.data)["model"], "kimi-k3")

    def test_invalid_successful_responses_do_not_pass_preflight(self):
        for kind, response in [
            ("json", {"choices": [{"message": {"content": "not JSON"}}]}),
            ("json", {"choices": [{"message": {"content": '{"status":"bad"}'}}]}),
            ("tools", {"content": [{"type": "text", "text": "ok"}]}),
            ("tools", {"content": [{"type": "tool_use", "name": "wrong", "input": {"status": "ok"}}]}),
        ]:
            with self.subTest(kind=kind, response=response):
                with mock.patch.object(ollama_review.urllib.request, "urlopen", return_value=self.response(response)):
                    with self.assertRaises(RuntimeError):
                        ollama_review.probe("test-key", kind)

    def test_http_errors_do_not_echo_provider_body_or_token(self):
        error = urllib.error.HTTPError("https://ollama.com/v1/messages", 401, "Unauthorized", {}, io.BytesIO(b"test-key"))
        with mock.patch.object(ollama_review.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                ollama_review.probe("test-key", "tools")
        self.assertNotIn("test-key", str(caught.exception))

    def test_transient_failure_retries_but_exhaustion_is_bounded(self):
        response = {"choices": [{"message": {"content": '```json\n{"status":"ok"}\n```'}}]}
        for failure in (
            TimeoutError("timed out"),
            urllib.error.HTTPError("https://ollama.com/v1/chat/completions", 503, "Unavailable", {}, None),
        ):
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(ollama_review.urllib.request, "urlopen", side_effect=[failure, self.response(response)]) as request,
                    mock.patch.object(ollama_review.time, "sleep") as sleep,
                ):
                    ollama_review.probe("test-key", "json")
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(15)
                with (
                    mock.patch.object(ollama_review.urllib.request, "urlopen", side_effect=failure) as request,
                    mock.patch.object(ollama_review.time, "sleep"),
                ):
                    with self.assertRaises(RuntimeError):
                        ollama_review.probe("test-key", "json")
                self.assertEqual(request.call_count, 3)

    def test_non_json_or_unexpected_envelope_is_rejected(self):
        for content in ("not JSON", "[]", '{"choices":[]}', '{"choices":[{"message":{"content":null}}]}'):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(content)
            with self.subTest(content=content):
                with mock.patch.object(ollama_review.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(RuntimeError):
                        ollama_review.probe("test-key", "json")

    def test_failed_probe_cannot_publish_ready_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "test-key", "GITHUB_OUTPUT": str(output)}),
                mock.patch.object(ollama_review, "probe", side_effect=RuntimeError("not ready")),
            ):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    ollama_review.main(["--probe", "tools"])
            self.assertFalse(output.exists())

    def test_preflight_pins_outputs_and_never_discovers_another_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "test-key", "GITHUB_OUTPUT": str(output)}),
                mock.patch.object(ollama_review, "probe") as probe,
            ):
                self.assertEqual(ollama_review.main(["--probe", "json"]), 0)
            probe.assert_called_once_with("test-key", "json")
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["selected_model"], "kimi-k3")
            self.assertEqual(values["primary_model"], "kimi-k3")
            self.assertEqual(values["fallback_model"], "kimi-k3")
            self.assertEqual(values["selected_mode"], "ordinary")
            self.assertEqual(values["secondary_model"], "")

    def test_missing_key_stops_before_network_access(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ollama_review, "probe") as probe:
            with self.assertRaisesRegex(RuntimeError, "OLLAMA_API_KEY"):
                ollama_review.main(["--probe", "json"])
        probe.assert_not_called()

    def test_all_ci_reviewers_pin_kimi_and_use_the_ollama_secret(self):
        root = Path(__file__).resolve().parents[2]
        automatic = yaml.load((root / ".github/workflows/pr-ai-review.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_MODEL"], "kimi-k3")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_RPM"], "${{ vars.OLLAMA_REVIEW_RPM || '60' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_COOLDOWN_SECONDS"], "${{ vars.OLLAMA_REVIEW_COOLDOWN_SECONDS || '0' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_BUDGET_SECONDS"], "${{ vars.OLLAMA_REVIEW_BUDGET_SECONDS || '2400' }}")
        for job in automatic["jobs"].values():
            for step in job["steps"]:
                if "anthropics/claude-code-action@" in step.get("uses", ""):
                    self.assertEqual(step["env"]["ANTHROPIC_BASE_URL"], "https://ollama.com")
                    self.assertEqual(step["with"]["anthropic_api_key"], "${{ secrets.OLLAMA_API_KEY }}")
                    self.assertEqual(step["env"]["ANTHROPIC_AUTH_TOKEN"], "${{ secrets.OLLAMA_API_KEY }}")
                    model_output = "fallback_model" if step["id"] == "claude_review_fallback" else "primary_model"
                    for name in ("ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                        self.assertEqual(step["env"][name], "${{ steps.claude_models.outputs." + model_output + " }}")
        manual_text = (root / ".github/workflows/manual-ai-review.yml").read_text()
        manual = yaml.load(manual_text, Loader=yaml.BaseLoader)
        self.assertEqual(manual["on"]["workflow_dispatch"]["inputs"]["model"]["default"], "kimi-k3")
        validation = next(
            step for job in manual["jobs"].values() for step in job["steps"]
            if "REQUESTED_MODEL" in step.get("env", {})
        )
        for requested_model, expected_status in (("kimi-k3", 0), ("auto", 0), ("another-model", 1)):
            with self.subTest(requested_model=requested_model):
                result = subprocess.run(
                    ["bash", "-c", validation["run"]], capture_output=True, text=True,
                    env={**os.environ, "REQUESTED_MODEL": requested_model},
                )
                self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)
        azure = yaml.load((root / "azure-ci/azure-ai-code-review.yml").read_text(), Loader=yaml.BaseLoader)
        model = next(parameter for parameter in azure["parameters"] if parameter["name"] == "model")
        self.assertEqual(model["default"], "kimi-k3")
        for workflow in (manual_text, (root / ".github/workflows/pr-ai-review.yml").read_text()):
            self.assertNotIn("secrets.OPENROUTER_API_KEY", workflow)
            self.assertNotIn("vars.OPENROUTER_REVIEW_MODEL", workflow)
            self.assertNotIn("openrouter_model_preflight.py", workflow)
            self.assertIn("ollama_review.py", workflow)


if __name__ == "__main__":
    unittest.main()
