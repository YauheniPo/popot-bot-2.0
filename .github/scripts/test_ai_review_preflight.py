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
import ai_review_preflight


class OllamaReviewTest(unittest.TestCase):
    def response(self, data: object):
        result = mock.MagicMock()
        result.__enter__.return_value = io.StringIO(json.dumps(data))
        return result

    def test_cloud_payload_omits_unsupported_fields_without_changing_prompt(self):
        source = {
            "model": "moonshotai/kimi-k3", "messages": [{"role": "system", "content": "schema"}],
            "max_tokens": 10000, "temperature": 0,
            "response_format": {"type": "json_schema"}, "provider": {},
            "plugins": [{"id": "response-healing"}], "reasoning": {"effort": "none"},
        }
        payload = ai_review_preflight.completion_payload(source)
        self.assertEqual(set(payload), {"model", "messages", "max_tokens", "temperature"})
        self.assertEqual(payload["messages"], source["messages"])
        self.assertIn("response_format", source)

    def test_json_probe_authenticates_and_validates_plain_json(self):
        response = {"choices": [{"message": {"content": '{"status":"ok"}'}}]}
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", return_value=self.response(response)) as request:
            ai_review_preflight.probe("test-key", "json")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ollama.com/v1/chat/completions")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
        payload = json.loads(sent.data)
        self.assertEqual(payload["model"], "moonshotai/kimi-k3")
        self.assertNotIn("response_format", payload)

    def test_tool_probe_uses_anthropic_endpoint_and_checks_tool_input(self):
        response = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", return_value=self.response(response)) as request:
            ai_review_preflight.probe("test-key", "tools")
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://ollama.com/v1/messages")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(json.loads(sent.data)["model"], "moonshotai/kimi-k3")

    def test_invalid_successful_responses_do_not_pass_preflight(self):
        for kind, response in [
            ("json", {"choices": [{"message": {"content": "not JSON"}}]}),
            ("json", {"choices": [{"message": {"content": '{"status":"bad"}'}}]}),
            ("tools", {"content": [{"type": "text", "text": "ok"}]}),
            ("tools", {"content": [{"type": "tool_use", "name": "wrong", "input": {"status": "ok"}}]}),
        ]:
            with self.subTest(kind=kind, response=response):
                with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", return_value=self.response(response)):
                    with self.assertRaises(RuntimeError):
                        ai_review_preflight.probe("test-key", kind)

    def test_http_errors_do_not_echo_provider_body_or_token(self):
        error = urllib.error.HTTPError("https://ollama.com/v1/messages", 401, "Unauthorized", {}, io.BytesIO(b"test-key"))
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                ai_review_preflight.probe("test-key", "tools")
        self.assertNotIn("test-key", str(caught.exception))

    def test_transient_failure_retries_but_exhaustion_is_bounded(self):
        response = {"choices": [{"message": {"content": '```json\n{"status":"ok"}\n```'}}]}
        for failure in (
            TimeoutError("timed out"),
            urllib.error.HTTPError("https://ollama.com/v1/chat/completions", 503, "Unavailable", {}, None),
        ):
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=[failure, self.response(response)]) as request,
                    mock.patch.object(ai_review_preflight.time, "sleep") as sleep,
                ):
                    ai_review_preflight.probe("test-key", "json")
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(15)
                with (
                    mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=failure) as request,
                    mock.patch.object(ai_review_preflight.time, "sleep"),
                ):
                    with self.assertRaises(RuntimeError):
                        ai_review_preflight.probe("test-key", "json")
                self.assertEqual(request.call_count, 2)

    def test_non_json_or_unexpected_envelope_is_rejected(self):
        for content in ("not JSON", "[]", '{"choices":[]}', '{"choices":[{"message":{"content":null}}]}'):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(content)
            with self.subTest(content=content):
                with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(RuntimeError):
                        ai_review_preflight.probe("test-key", "json")

    def test_failed_probe_cannot_publish_ready_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "test-key", "DIRECT_REVIEW_PROVIDER": "ollama-cloud", "GITHUB_OUTPUT": str(output)}),
                mock.patch.object(ai_review_preflight, "probe", side_effect=RuntimeError("not ready")),
            ):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    ai_review_preflight.main(["--probe", "tools"])
            self.assertFalse(output.exists())

    def test_preflight_pins_outputs_and_never_discovers_another_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "test-key", "DIRECT_REVIEW_PROVIDER": "ollama-cloud", "GITHUB_OUTPUT": str(output)}),
                mock.patch.object(ai_review_preflight, "probe") as probe,
            ):
                self.assertEqual(ai_review_preflight.main(["--probe", "json"]), 0)
            probe.assert_called_once_with("test-key", "json")
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["selected_model"], "moonshotai/kimi-k3")
            self.assertEqual(values["primary_model"], "moonshotai/kimi-k3")
            self.assertEqual(values["fallback_model"], "")
            self.assertEqual(values["selected_mode"], "ordinary")
            self.assertEqual(values["provider"], "ollama-cloud")
            self.assertEqual(values["secondary_model"], "")

    def test_missing_key_stops_before_network_access(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ai_review_preflight, "probe") as probe:
            with self.assertRaisesRegex(RuntimeError, "NVIDIA_API_KEY"):
                ai_review_preflight.main(["--probe", "json"])
        probe.assert_not_called()

    def test_fallback_model_passes_through_without_a_mode_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {
                    "NVIDIA_API_KEY": "test-key", "GITHUB_OUTPUT": str(output),
                    "DIRECT_REVIEW_FALLBACK_MODEL": "backup",
                }, clear=True),
                mock.patch.object(ai_review_preflight, "probe") as probe,
            ):
                self.assertEqual(ai_review_preflight.main(["--probe", "json"]), 0)
            probe.assert_called_once()
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["secondary_model"], "backup")
            self.assertNotIn("secondary_mode", values)

    def test_ci_reviewers_use_provider_neutral_model_settings(self):
        root = Path(__file__).resolve().parents[2]
        automatic = yaml.load((root / ".github/workflows/pr-ai-review.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(automatic["env"]["DIRECT_REVIEW_MODEL"], "${{ vars.DIRECT_REVIEW_MODEL || 'moonshotai/kimi-k3' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_RPM"], "${{ vars.OLLAMA_REVIEW_RPM || '60' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_COOLDOWN_SECONDS"], "${{ vars.OLLAMA_REVIEW_COOLDOWN_SECONDS || '0' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_BUDGET_SECONDS"], "${{ vars.OLLAMA_REVIEW_BUDGET_SECONDS || '2400' }}")
        self.assertEqual(automatic["env"]["DIRECT_REVIEW_PROVIDER"], "${{ vars.DIRECT_REVIEW_PROVIDER || 'nvidia' }}")
        self.assertEqual(automatic["env"]["CLAUDE_REVIEW_PROVIDER"], "${{ vars.CLAUDE_REVIEW_PROVIDER || 'nvidia' }}")
        for job in automatic["jobs"].values():
            for step in job["steps"]:
                if "anthropics/claude-code-action@" in step.get("uses", ""):
                    self.assertIn("steps.claude_models.outputs.provider", step["env"]["ANTHROPIC_BASE_URL"])
                    self.assertIn("steps.claude_models.outputs.provider", step["with"]["anthropic_api_key"])
                    self.assertIn("OPENROUTER_API_KEY", step["with"]["anthropic_api_key"])
                    self.assertIn("OPENROUTER_API_KEY", step["env"]["ANTHROPIC_AUTH_TOKEN"])
        manual_text = (root / ".github/workflows/manual-ai-review.yml").read_text()
        manual = yaml.load(manual_text, Loader=yaml.BaseLoader)
        self.assertEqual(manual["on"]["workflow_dispatch"]["inputs"]["model"]["default"], "moonshotai/kimi-k3")
        validation = next(
            step for job in manual["jobs"].values() for step in job["steps"]
            if "REQUESTED_MODEL" in step.get("env", {})
        )
        for requested_model, expected_status in (("moonshotai/kimi-k3", 0), ("auto", 0), ("another-model", 0)):
            with self.subTest(requested_model=requested_model):
                result = subprocess.run(
                    ["bash", "-c", validation["run"]], capture_output=True, text=True,
                    env={**os.environ, "REQUESTED_MODEL": requested_model},
                )
                self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)
        azure = yaml.load((root / "azure-ci/azure-ai-code-review.yml").read_text(), Loader=yaml.BaseLoader)
        model = next(parameter for parameter in azure["parameters"] if parameter["name"] == "model")
        self.assertEqual(model["default"], "moonshotai/kimi-k3")
        for workflow in (manual_text, (root / ".github/workflows/pr-ai-review.yml").read_text()):
            self.assertIn("OPENROUTER_API_KEY", workflow)
            self.assertNotIn("vars.OPENROUTER_REVIEW_MODEL", workflow)
            self.assertNotIn("openrouter_model_preflight.py", workflow)
            self.assertIn("ai_review_preflight.py", workflow)

        for workflow, preflight_id in ((automatic, "direct_models"), (manual, "models")):
            review_step = next(
                step for job in workflow["jobs"].values() for step in job["steps"]
                if "ai_pr_review.py" in step.get("run", "")
            )
            self.assertEqual(
                review_step["env"]["DIRECT_REVIEW_MODEL"],
                "${{ steps." + preflight_id + ".outputs.selected_model }}",
            )
            self.assertEqual(
                review_step["env"]["DIRECT_REVIEW_MODEL_MODE"],
                "${{ steps." + preflight_id + ".outputs.selected_mode }}",
            )

    def test_model_selection_ignores_retired_alias_for_every_provider(self):
        for provider in ("ollama-cloud", "nvidia", "openrouter", "nous"):
            for selected_model, expected in ((None, ai_review_preflight.MODEL), ("vendor/chosen-model", "vendor/chosen-model")):
                environment = {"DIRECT_REVIEW_PROVIDER": provider, "OLLAMA_REVIEW_MODEL": "stale/model"}
                if selected_model is not None:
                    environment["DIRECT_REVIEW_MODEL"] = selected_model
                with self.subTest(provider=provider, selected_model=selected_model), mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(ai_review_preflight.configured_model(), expected)


class NousReviewTest(unittest.TestCase):
    def test_provider_uses_only_the_nous_key_and_preserves_model_ids(self):
        for provider in ("nous", "nous-portal", "nous-api"):
            with self.subTest(provider=provider), mock.patch.dict(os.environ, {
                "DIRECT_REVIEW_PROVIDER": provider, "NOUS_API_KEY": "nous-test-key",
                "OLLAMA_API_KEY": "wrong-key", "NVIDIA_API_KEY": "wrong-key",
                "DIRECT_REVIEW_MODEL": "vendor/chosen-model",
                "DIRECT_REVIEW_FALLBACK_MODEL": "vendor/backup-model",
            }, clear=True):
                self.assertEqual(ai_review_preflight.provider_config(), (
                    "nous", "nous-test-key", "https://inference-api.nousresearch.com/v1/chat/completions",
                ))
                self.assertEqual(ai_review_preflight.configured_model(), "vendor/chosen-model")
                self.assertEqual(ai_review_preflight.configured_fallback_model(), "vendor/backup-model")

    def test_missing_nous_key_fails_before_network_even_with_other_keys(self):
        with mock.patch.dict(os.environ, {
            "DIRECT_REVIEW_PROVIDER": "nous", "OLLAMA_API_KEY": "wrong-key",
        }, clear=True), mock.patch.object(ai_review_preflight, "probe") as probe:
            with self.assertRaisesRegex(RuntimeError, "NOUS_API_KEY"):
                ai_review_preflight.main(["--probe", "json"])
            probe.assert_not_called()

    def test_json_and_tool_probes_authenticate_on_the_correct_endpoints(self):
        for kind, endpoint, result in (
            ("json", "chat/completions", {"choices": [{"message": {"content": '{"status":"ok"}'}}]}),
            ("tools", "messages", {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}),
        ):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(result))
            with self.subTest(kind=kind), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                ai_review_preflight.urllib.request, "urlopen", return_value=response,
            ) as request:
                ai_review_preflight.probe("nous-test-key", kind, "nous", "vendor/chosen-model")
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, f"https://inference-api.nousresearch.com/v1/{endpoint}")
            self.assertEqual(sent.get_header("Authorization"), "Bearer nous-test-key")
            payload = json.loads(sent.data)
            self.assertEqual(payload["model"], "vendor/chosen-model")
            if kind == "tools":
                self.assertEqual(sent.get_header("Anthropic-version"), "2023-06-01")
                self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "review_model_preflight"})
                self.assertEqual(payload["tools"][0]["input_schema"]["required"], ["status"])

    def test_tool_probe_uses_the_same_custom_base_as_claude_action(self):
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_BASE_URL": "https://gateway.example/anthropic/"}):
            request = ai_review_preflight._probe_request("test-key", "tools", "nous", "vendor/model")
            self.assertEqual(request.full_url, "https://gateway.example/anthropic/v1/messages")

    def test_nous_payload_respects_api_limit_without_mutating_request(self):
        source = {
            "model": "vendor/model", "max_tokens": 32768,
            "messages": [{"role": "system", "content": "required schema"}],
            "reasoning": {}, "response_format": {}, "plugins": [], "provider": {},
        }
        payload = ai_review_preflight.completion_payload(source, provider="nous")
        self.assertEqual(payload, {
            "model": "vendor/model", "max_tokens": 32000, "messages": source["messages"],
        })
        self.assertEqual(source["max_tokens"], 32768)
        self.assertEqual(ai_review_preflight.completion_payload({"max_tokens": 4096}, provider="nous"), {"max_tokens": 4096})

    def test_nous_is_wired_through_github_claude_manual_and_azure(self):
        root = Path(__file__).resolve().parents[2]
        automatic = yaml.load((root / ".github/workflows/pr-ai-review.yml").read_text(), Loader=yaml.BaseLoader)
        manual = yaml.load((root / ".github/workflows/manual-ai-review.yml").read_text(), Loader=yaml.BaseLoader)
        azure = yaml.load((root / "azure-ci/azure-ai-code-review.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("nous", manual["on"]["workflow_dispatch"]["inputs"]["provider"]["options"])
        provider = next(p for p in azure["parameters"] if p["name"] == "provider")
        self.assertIn("nous", provider["values"])
        for workflow in (automatic, manual):
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if any(name in step.get("run", "") for name in ("ai_pr_review.py", "ai_review_preflight.py")):
                        self.assertEqual(step["env"]["NOUS_API_KEY"], "${{ secrets.NOUS_API_KEY }}")
                    if "anthropics/claude-code-action@" in step.get("uses", ""):
                        self.assertIn("inference-api.nousresearch.com", step["env"]["ANTHROPIC_BASE_URL"])
                        self.assertIn("== 'nous'", step["env"]["ANTHROPIC_BASE_URL"])
                        self.assertIn("NOUS_API_KEY", step["with"]["anthropic_api_key"])
                        self.assertEqual(step["with"]["anthropic_api_key"], step["env"]["ANTHROPIC_AUTH_TOKEN"])


if __name__ == "__main__":
    unittest.main()
