from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
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

    def test_default_provider_requires_its_configured_api_key(self):
        with mock.patch.dict(os.environ, {"DIRECT_REVIEW_PROVIDER": "nvidia"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "NVIDIA_API_KEY"):
                ai_review_preflight.main(["--probe", "json"])

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
                    mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=[failure] * 3 + [self.response(response)]) as request,
                    mock.patch.object(ai_review_preflight.time, "sleep") as sleep,
                ):
                    ai_review_preflight.probe("test-key", "json")
                self.assertEqual(request.call_count, 4)
                self.assertEqual(sleep.call_args_list, [mock.call(15), mock.call(30), mock.call(45)])
                with (
                    mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=failure) as request,
                    mock.patch.object(ai_review_preflight.time, "sleep"),
                ):
                    with self.assertRaises(RuntimeError):
                        ai_review_preflight.probe("test-key", "json")
                self.assertEqual(request.call_count, 4)

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
                with self.assertRaisesRegex(RuntimeError, "No configured review model passed"):
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
            probe.assert_called_once_with("test-key", "json", "ollama-cloud", ai_review_preflight.MODEL)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["selected_model"], "moonshotai/kimi-k3")
            self.assertEqual(values["primary_model"], "moonshotai/kimi-k3")
            self.assertEqual(values["fallback_model"], "")
            self.assertEqual(values["fallback_ready"], "false")
            self.assertEqual(values["selected_mode"], "ordinary")
            self.assertEqual(values["provider"], "ollama-cloud")
            self.assertEqual(values["selected_provider"], "ollama-cloud")
            self.assertEqual(values["secondary_model"], "")
            self.assertEqual(values["secondary_provider"], "")

    def test_missing_key_stops_before_network_access(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(ai_review_preflight, "probe") as probe:
            with self.assertRaisesRegex(RuntimeError, "NVIDIA_API_KEY"):
                ai_review_preflight.main(["--probe", "json"])
        probe.assert_not_called()

    def test_fallback_model_is_probed_without_a_mode_setting(self):
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
            self.assertEqual(probe.call_args_list, [
                mock.call("test-key", "json", "nvidia", ai_review_preflight.MODEL),
                mock.call("test-key", "json", "nvidia", "backup"),
            ])
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["secondary_model"], "backup")
            self.assertEqual(values["secondary_provider"], "nvidia")
            self.assertEqual(values["fallback_ready"], "true")
            self.assertNotIn("secondary_mode", values)

    def test_fallback_provider_is_probed_and_exported_as_an_independent_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with (
                mock.patch.dict(os.environ, {
                    "OLLAMA_API_KEY": "primary-key", "NVIDIA_API_KEY": "fallback-key",
                    "DIRECT_REVIEW_PROVIDER": "ollama-cloud",
                    "DIRECT_REVIEW_FALLBACK_PROVIDER": "nvidia",
                    "DIRECT_REVIEW_FALLBACK_MODEL": "nvidia/backup",
                    "GITHUB_OUTPUT": str(output),
                }, clear=True),
                mock.patch.object(ai_review_preflight, "probe") as probe,
            ):
                self.assertEqual(ai_review_preflight.main(["--probe", "json"]), 0)
            self.assertEqual(probe.call_args_list, [
                mock.call("primary-key", "json", "ollama-cloud", ai_review_preflight.MODEL),
                mock.call("fallback-key", "json", "nvidia", "nvidia/backup"),
            ])
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["selected_provider"], "ollama-cloud")
            self.assertEqual(values["secondary_provider"], "nvidia")
            self.assertEqual(values["fallback_provider"], "nvidia")
            self.assertEqual(values["secondary_anthropic_base_url"], "")

    def test_fallback_readiness_requires_a_valid_response_on_the_reviewers_api(self):
        for kind, response in (
            ("json", {"choices": [{"message": {"content": '{"status":"ok"}'}}]}),
            ("tools", {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}),
        ):
            for outcome in ("valid", "forbidden", "invalid", "unavailable"):
                with self.subTest(kind=kind, outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "outputs"
                    replies = [self.response(response)]
                    if outcome == "forbidden":
                        replies.append(urllib.error.HTTPError("https://openrouter.ai/api", 403, "Forbidden", {}, io.BytesIO(b"private provider error")))
                    elif outcome == "unavailable":
                        replies.extend([TimeoutError()] * 4)
                    else:
                        replies.append(self.response(response if outcome == "valid" else {}))
                    with (
                        mock.patch.dict(os.environ, {
                            "DIRECT_REVIEW_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key",
                            "DIRECT_REVIEW_MODEL": "primary", "DIRECT_REVIEW_FALLBACK_MODEL": "backup",
                            "GITHUB_OUTPUT": str(output),
                        }, clear=True),
                        mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=replies) as request,
                        mock.patch.object(ai_review_preflight.time, "sleep") as sleep,
                        mock.patch.object(sys, "stderr", new_callable=io.StringIO) as errors,
                    ):
                        self.assertEqual(ai_review_preflight.main(["--probe", kind]), 0)
                    expected_models = ["primary", "backup"] + (["backup"] * 3 if outcome == "unavailable" else [])
                    self.assertEqual([json.loads(call.args[0].data)["model"] for call in request.call_args_list], expected_models)
                    self.assertEqual(sleep.call_count, 3 if outcome == "unavailable" else 0)
                    suffix = "messages" if kind == "tools" else "chat/completions"
                    self.assertTrue(all(call.args[0].full_url == f"https://openrouter.ai/api/v1/{suffix}" for call in request.call_args_list))
                    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                    self.assertEqual(values["primary_ready"], "true")
                    self.assertEqual(values["fallback_model"], "backup")
                    self.assertEqual(values["fallback_ready"], "true" if outcome == "valid" else "false")
                    self.assertEqual(values["secondary_model"], "backup" if outcome == "valid" else "")
                    self.assertEqual("::warning::" in errors.getvalue(), outcome != "valid")
                    self.assertNotIn("private provider error", errors.getvalue())
                    self.assertNotIn("test-key", errors.getvalue())

    def test_same_fallback_model_reuses_the_primary_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "outputs"
            with mock.patch.dict(os.environ, {
                "DIRECT_REVIEW_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key",
                "DIRECT_REVIEW_MODEL": "primary", "DIRECT_REVIEW_FALLBACK_MODEL": "primary",
                "GITHUB_OUTPUT": str(output),
            }, clear=True), mock.patch.object(ai_review_preflight, "probe") as probe:
                self.assertEqual(ai_review_preflight.main(["--probe", "tools"]), 0)
            probe.assert_called_once_with("test-key", "tools", "openrouter", "primary")
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["fallback_ready"], "true")
            self.assertEqual(values["secondary_model"], "primary")

    def test_primary_timeout_checks_fallback_and_selects_only_a_validated_model(self):
        for kind, provider, response in (
            ("json", "nvidia", {"choices": [{"message": {"content": '{"status":"ok"}'}}]}),
            ("tools", "openrouter", {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}),
        ):
            for fallback, outcome in (("backup", "valid"), ("backup", "invalid"), ("backup", "timeout"), ("primary", "same"), ("", "none")):
                with self.subTest(kind=kind, outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "outputs"
                    replies = [TimeoutError()] * 4
                    expected_models = ["primary"] * 4
                    if fallback == "backup":
                        expected_models.append("backup")
                        if outcome == "timeout":
                            replies.extend([TimeoutError()] * 4)
                            expected_models.extend(["backup"] * 3)
                        else:
                            replies.append(self.response(response if outcome == "valid" else {}))
                    with (
                        mock.patch.dict(os.environ, {
                            "DIRECT_REVIEW_PROVIDER": provider, "NVIDIA_API_KEY": "test-key", "OPENROUTER_API_KEY": "test-key",
                            "DIRECT_REVIEW_MODEL": "primary", "DIRECT_REVIEW_FALLBACK_MODEL": fallback,
                            "GITHUB_OUTPUT": str(output),
                        }, clear=True),
                        mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=replies) as request,
                        mock.patch.object(ai_review_preflight.time, "sleep") as sleep,
                        mock.patch.object(sys, "stderr", new_callable=io.StringIO) as errors,
                    ):
                        if outcome == "valid":
                            self.assertEqual(ai_review_preflight.main(["--probe", kind]), 0)
                        else:
                            with self.assertRaisesRegex(RuntimeError, "No configured review model passed"):
                                ai_review_preflight.main(["--probe", kind])
                    self.assertEqual([json.loads(call.args[0].data)["model"] for call in request.call_args_list], expected_models)
                    delays = [mock.call(15), mock.call(30), mock.call(45)]
                    self.assertEqual(sleep.call_args_list, delays * (2 if outcome == "timeout" else 1))
                    self.assertIn("primary", errors.getvalue())
                    self.assertIn("4/4", errors.getvalue())
                    self.assertNotIn("5/4", errors.getvalue())
                    if outcome == "valid":
                        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                        self.assertEqual(values["primary_model"], "primary")
                        self.assertEqual(values["primary_ready"], "false")
                        self.assertEqual(values["fallback_ready"], "true")
                        self.assertEqual(values["selected_model"], "backup")
                        self.assertEqual(values["secondary_model"], "")
                    else:
                        self.assertFalse(output.exists())

    def test_nvidia_tools_require_an_explicit_anthropic_gateway_before_network(self):
        for base_url in ("", "  "):
            with self.subTest(base_url=base_url), mock.patch.dict(os.environ, {"CLAUDE_REVIEW_BASE_URL": base_url}, clear=True), mock.patch.object(ai_review_preflight.urllib.request, "urlopen") as request:
                with self.assertRaisesRegex(RuntimeError, "CLAUDE_REVIEW_BASE_URL"):
                    ai_review_preflight.probe("test-key", "tools", "nvidia", "vendor/model")
                request.assert_not_called()

    def test_claude_fallback_gets_a_second_smoke_attempt(self):
        with mock.patch.object(ai_review_preflight, "probe") as probe:
            probe.return_value = None
            ai_review_preflight.probe_models("key", "claude", "openrouter", "primary", "backup")
            fallback_calls = [call for call in probe.call_args_list if call.args[3] == "backup"]
            self.assertEqual(ai_review_preflight.SMOKE_MAX_ATTEMPTS, 2)
            self.assertEqual(len(fallback_calls), 1)
            self.assertEqual(fallback_calls[0].kwargs["attempts_override"], 2)

    def test_tools_use_explicit_provider_routes_or_the_custom_gateway(self):
        for provider, endpoint in (
            ("ollama-cloud", "https://ollama.com/v1/messages"),
            ("openrouter", "https://openrouter.ai/api/v1/messages"),
            ("nous", "https://inference-api.nousresearch.com/v1/messages"),
        ):
            with self.subTest(provider=provider), mock.patch.dict(os.environ, {}, clear=True):
                request = ai_review_preflight._probe_request("test-key", "tools", provider, "vendor/model")
                self.assertEqual(request.full_url, endpoint)
        with mock.patch.dict(os.environ, {"CLAUDE_REVIEW_BASE_URL": "https://gateway.example/anthropic/"}, clear=True):
            request = ai_review_preflight._probe_request("test-key", "tools", "nvidia", "vendor/model")
            self.assertEqual(request.full_url, "https://gateway.example/anthropic/v1/messages")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            request = ai_review_preflight._probe_request("test-key", "json", "nvidia", "vendor/model")
            self.assertEqual(request.full_url, ai_review_preflight.NVIDIA_CHAT_COMPLETIONS_URL)

    def test_tool_probe_and_claude_share_a_normalized_base_url(self):
        cases = (
            ("https://openrouter.ai/api", "https://openrouter.ai/api"),
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api"),
            (" https://openrouter.ai/api/v1/ ", "https://openrouter.ai/api"),
            ("https://gateway.example/proxy/v1/messages/", "https://gateway.example/proxy"),
            ("https://gateway.example/v1/proxy", "https://gateway.example/v1/proxy"),
            ("https://gateway.example/v1/v1/messages", "https://gateway.example/v1"),
        )
        response = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        for configured, normalized in cases:
            with self.subTest(base=configured), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "outputs"
                with (
                    mock.patch.dict(os.environ, {
                        "DIRECT_REVIEW_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key",
                        "CLAUDE_REVIEW_BASE_URL": configured, "GITHUB_OUTPUT": str(output),
                    }, clear=True),
                    mock.patch.object(ai_review_preflight.urllib.request, "urlopen", return_value=self.response(response)) as request,
                ):
                    self.assertEqual(ai_review_preflight.main(["--probe", "tools"]), 0)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual(values.get("anthropic_base_url"), normalized)
                self.assertEqual(request.call_args.args[0].full_url, f"{normalized}/v1/messages")

    def test_claude_preflight_uses_its_own_fallback_not_the_direct_reviewers(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/pr-ai-review.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        step = next(step for job in workflow["jobs"].values() for step in job["steps"] if step.get("id") == "claude_models")
        self.assertEqual(step["env"].get("DIRECT_REVIEW_FALLBACK_MODEL"), "${{ env.CLAUDE_REVIEW_FALLBACK_MODEL }}")

    def test_ai_review_action_policy_check_is_independent_of_claude_action(self):
        root = Path(__file__).resolve().parents[2]
        policy = yaml.load(
            (root / ".github/workflows/ai-review-action-policy.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(policy["name"], "AI review action policy")
        self.assertIn("synchronize", policy["on"]["pull_request"]["types"])
        step = policy["jobs"]["verify-claude-action-allowlist"]["steps"][0]
        self.assertNotIn("uses", step)
        self.assertEqual(step["env"]["ACTIONS_ALLOWED_PATTERNS"], "${{ vars.ACTIONS_ALLOWED_PATTERNS }}")
        self.assertIn("git/trees", step["run"])
        self.assertIn('tree.get("truncated")', step["run"])
        sync = yaml.load(
            (root / ".github/workflows/sync-actions-allowlist.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(sync["name"], "Sync Actions allowlist")
        self.assertEqual(sync["on"]["push"]["branches"], ["main"])
        self.assertNotIn("if", sync["jobs"]["sync-dependabot-action-updates"])
        sync_step = sync["jobs"]["sync-dependabot-action-updates"]["steps"][0]
        self.assertNotIn("uses", sync_step)
        self.assertIn("actions/permissions/selected-actions", sync_step["run"])
        self.assertIn('author.get("login") != "dependabot[bot]"', sync_step["run"])

    def test_owner_approved_review_uses_a_trusted_workflow_and_reads_pr_as_data(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.load(
            (root / ".github/workflows/owner-approved-ai-review.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(workflow["on"]["pull_request_target"]["types"], ["labeled"])
        job = workflow["jobs"]["review"]
        self.assertIn("github.event.label.name == 'ai-review-approved'", job["if"])
        self.assertIn("github.actor == github.repository_owner", job["if"])
        self.assertEqual(job["permissions"]["contents"], "read")
        self.assertEqual(job["permissions"]["issues"], "write")
        self.assertEqual(job["permissions"]["pull-requests"], "write")
        steps = job["steps"]
        trusted = next(step for step in steps if step.get("id") == "trusted_tooling")
        target = next(step for step in steps if step.get("id") == "target")
        self.assertEqual(trusted["with"]["ref"], "main")
        self.assertEqual(target["with"]["ref"], "${{ github.event.pull_request.head.sha }}")
        review = next(step for step in steps if step.get("id") == "review")
        self.assertNotIn("working-directory", review)
        self.assertIn("python3 -I", review["run"])
        self.assertIn("runpy.run_path", review["run"])
        self.assertEqual(review["env"]["REQUIRE_REVIEW_RESULT"], "true")
        verify = next(step for step in steps if step.get("name", "").startswith("Verify the selected revision"))
        self.assertEqual(verify["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

    def test_ci_reviewers_use_provider_neutral_model_settings(self):
        root = Path(__file__).resolve().parents[2]
        automatic = yaml.load((root / ".github/workflows/pr-ai-review.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("direct-api-review", automatic["jobs"])
        self.assertNotIn("ollama-api-review", automatic["jobs"])
        self.assertEqual(automatic["jobs"]["claude-code-plugin-review"]["needs"], "direct-api-review")
        for key in (
            "DIRECT_REVIEW_PROVIDER",
            "DIRECT_REVIEW_MODEL",
            "DIRECT_REVIEW_FALLBACK_MODEL",
            "DIRECT_REVIEW_FALLBACK_PROVIDER",
            "CLAUDE_REVIEW_PROVIDER",
            "CLAUDE_REVIEW_MODEL",
            "CLAUDE_REVIEW_FALLBACK_MODEL",
        ):
            with self.subTest(key=key):
                self.assertIsInstance(automatic["env"][key], str)
                self.assertIn(f"vars.{key}", automatic["env"][key])
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_RPM"], "${{ vars.OLLAMA_REVIEW_RPM || '60' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_COOLDOWN_SECONDS"], "${{ vars.OLLAMA_REVIEW_COOLDOWN_SECONDS || '0' }}")
        self.assertEqual(automatic["env"]["OLLAMA_REVIEW_BUDGET_SECONDS"], "${{ vars.OLLAMA_REVIEW_BUDGET_SECONDS || '2400' }}")
        for job in automatic["jobs"].values():
            for step in job["steps"]:
                if "anthropics/claude-code-action@" in step.get("uses", ""):
                    step_id = step.get("id", "")
                    is_fallback = step_id in ("claude_review_fallback", "claude_review_fallback_retry")
                    if is_fallback:
                        self.assertEqual(step["env"]["ANTHROPIC_BASE_URL"], "${{ steps.claude_models.outputs.secondary_anthropic_base_url }}")
                        self.assertIn("steps.claude_models.outputs.fallback_provider", step["with"]["anthropic_api_key"])
                    else:
                        self.assertEqual(step["env"]["ANTHROPIC_BASE_URL"], "${{ steps.claude_models.outputs.anthropic_base_url }}")
                        self.assertIn("steps.claude_models.outputs.provider", step["with"]["anthropic_api_key"])
                    self.assertIn("OPENROUTER_API_KEY", step["with"]["anthropic_api_key"])
                    self.assertIn("OPENROUTER_API_KEY", step["env"]["ANTHROPIC_AUTH_TOKEN"])
                if "CLAUDE_REVIEW_ENDPOINT" in step.get("env", {}):
                    endpoint = step["env"]["CLAUDE_REVIEW_ENDPOINT"]
                    self.assertIn("steps.claude_models.outputs.anthropic_base_url", endpoint)
                    self.assertIn("steps.claude_models.outputs.secondary_anthropic_base_url", endpoint)
                    provider = step["env"]["CLAUDE_REVIEW_PROVIDER"]
                    self.assertIn("steps.claude_models.outputs.provider", provider)
                    self.assertIn("steps.claude_models.outputs.fallback_provider", provider)
        manual_text = (root / ".github/workflows/manual-ai-review.yml").read_text()
        manual = yaml.load(manual_text, Loader=yaml.BaseLoader)
        manual_inputs = manual["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(manual_inputs["model"]["type"], "string")
        self.assertIsInstance(manual_inputs["model"]["default"], str)
        self.assertTrue(manual_inputs["model"]["default"].strip())
        self.assertIn(manual_inputs["provider"]["default"], manual_inputs["provider"]["options"])
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
        self.assertEqual(model["type"], "string")
        self.assertIsInstance(model["default"], str)
        self.assertTrue(model["default"].strip())
        provider = next(parameter for parameter in azure["parameters"] if parameter["name"] == "provider")
        self.assertIn(provider["default"], provider["values"])
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
                review_step["env"]["DIRECT_REVIEW_PROVIDER"],
                "${{ steps." + preflight_id + ".outputs.selected_provider }}",
            )
            self.assertEqual(
                review_step["env"]["DIRECT_REVIEW_FALLBACK_PROVIDER"],
                "${{ steps." + preflight_id + ".outputs.secondary_provider }}",
            )
            self.assertEqual(
                review_step["env"]["DIRECT_REVIEW_MODEL_MODE"],
                "${{ steps." + preflight_id + ".outputs.selected_mode }}",
            )
            self.assertEqual(
                review_step["env"].get("DIRECT_REVIEW_PRIMARY_MODEL"),
                "${{ steps." + preflight_id + ".outputs.primary_model }}",
            )

        claude_steps = {step.get("id"): step for job in automatic["jobs"].values() for step in job["steps"]}
        # The model preflight is deliberately non-blocking: a provider outage or
        # rate limit must not fail the corroborating Claude job, which publishes
        # an "unavailable" report instead. The direct + observable reviews gate.
        self.assertIn("continue-on-error", claude_steps["claude_models"])
        for step_id in ("claude_review_primary", "claude_review_primary_retry"):
            self.assertIn("steps.claude_models.outputs.primary_ready == 'true'", claude_steps[step_id]["if"])
        self.assertIn("steps.claude_models.outputs.fallback_ready == 'true'", claude_steps["claude_review_fallback"]["if"])
        self.assertNotIn("outputs.primary_ready", claude_steps["claude_review_fallback"]["if"])
        self.assertIn("claude_review_fallback_retry", claude_steps)
        self.assertIn("steps.claude_models.outputs.fallback_ready == 'true'", claude_steps["claude_review_fallback_retry"]["if"])
        self.assertIn("steps.extract_claude_review_fallback.outcome != 'success'", claude_steps["claude_review_fallback_retry"]["if"])
        self.assertIn(
            "steps.claude_review_fallback_retry.outcome == 'success'",
            claude_steps["extract_claude_review_fallback_retry"]["if"],
        )
        unavailable = next(step for step in claude_steps.values() if step.get("id") == "report_claude_review_unavailable")
        self.assertIn("::warning::", unavailable["run"])
        self.assertIn("GITHUB_STEP_SUMMARY", unavailable["run"])
        self.assertNotIn("exit 1", unavailable["run"])
        # A GITHUB_STEP_SUMMARY-only report left the pull request with no signal
        # that one of three reviewers never ran. The step must also publish the
        # unavailability on the PR, deduplicated per run and best-effort.
        self.assertIn("claude-pr-review-unavailable", unavailable["run"])
        self.assertIn("issues/${PR_NUMBER}/comments", unavailable["run"])
        self.assertIn("gh api -X POST", unavailable["run"])
        self.assertIn("already published for run", unavailable["run"])
        # An event-derived value must reach the shell as an env var, never as an
        # interpolation inside the script body, and it must carry the real event
        # value — a hardcoded or stale export would pass a presence-only check.
        expected_env = {
            "PR_NUMBER": "${{ github.event.pull_request.number }}",
            "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
            "REVIEW_RUN_ID": "${{ github.run_id }}",
        }
        for name, expression in expected_env.items():
            self.assertEqual(unavailable["env"][name], expression)
            self.assertNotIn(expression, unavailable["run"])
        self.assertEqual(unavailable["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        # The preflight step is continue-on-error, so when it fails its outputs
        # are empty and the report would name no model at all. Both model labels
        # must fall back to the configured env values.
        self.assertIn("env.CLAUDE_REVIEW_MODEL", unavailable["env"]["PRIMARY_MODEL"])
        self.assertIn("env.CLAUDE_REVIEW_FALLBACK_MODEL", unavailable["env"]["FALLBACK_MODEL"])

        manual_summary = next(
            step for job in manual["jobs"].values() for step in job["steps"]
            if step.get("name") == "Summarize manual review"
        )
        for label in (
            "Requested provider/model", "Effective provider/model", "Requested publication",
            "Request ID", "Scope:",
        ):
            self.assertIn(label, manual_summary["run"])

    def test_claude_review_has_a_bounded_turn_and_wall_clock_budget(self):
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.load(
            (root / ".github/workflows/pr-ai-review.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        job = workflow["jobs"]["claude-code-plugin-review"]
        self.assertEqual(job["timeout-minutes"], "45")
        action_steps = [
            step for step in job["steps"]
            if "anthropics/claude-code-action@" in step.get("uses", "")
        ]
        self.assertEqual(len(action_steps), 4)
        for step in action_steps:
            with self.subTest(step=step["id"]):
                self.assertIn("--max-turns 48", step["with"]["claude_args"])
                self.assertEqual(
                    step["with"]["show_full_output"],
                    "${{ vars.CLAUDE_REVIEW_DEBUG == 'true' }}",
                )

    def test_job_timeouts_fit_two_model_probes_and_the_default_review_budget(self):
        root = Path(__file__).resolve().parents[2]
        probe_budget = 4 * ai_review_preflight.REQUEST_TIMEOUT_SECONDS + 15 + 30 + 45
        for name in ("pr-ai-review.yml", "manual-ai-review.yml"):
            workflow = yaml.load((root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                review_steps = [step for step in job["steps"] if "ai_pr_review.py" in step.get("run", "")]
                if not review_steps:
                    continue
                budget = review_steps[0]["env"].get("OLLAMA_REVIEW_BUDGET_SECONDS", workflow.get("env", {}).get("OLLAMA_REVIEW_BUDGET_SECONDS"))
                default_budget = int(re.search(r"\|\| '(\d+)'", budget).group(1))
                with self.subTest(workflow=name):
                    self.assertGreaterEqual(int(job["timeout-minutes"]) * 60, default_budget + 2 * probe_budget + 300)

    def test_model_selection_ignores_retired_alias_for_every_provider(self):
        for provider in ("ollama-cloud", "nvidia", "openrouter", "nous"):
            for selected_model, expected in ((None, ai_review_preflight.MODEL), ("vendor/chosen-model", "vendor/chosen-model")):
                environment = {"DIRECT_REVIEW_PROVIDER": provider, "OLLAMA_REVIEW_MODEL": "stale/model"}
                if selected_model is not None:
                    environment["DIRECT_REVIEW_MODEL"] = selected_model
                with self.subTest(provider=provider, selected_model=selected_model), mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(ai_review_preflight.configured_model(), expected)

    def test_probe_rejects_non_positive_attempt_and_timeout_overrides(self):
        for keyword in ("attempts_override", "timeout_override"):
            with self.subTest(keyword=keyword):
                with self.assertRaisesRegex(ValueError, f"{keyword} must be positive"):
                    ai_review_preflight.probe("test-key", "json", **{keyword: 0})


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

    def test_claude_smoke_probe_checks_tools_and_review_json_contract(self):
        tool = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        structured = {"content": [{"type": "text", "text": '{"summary":"ok","findings":[],"thread_verdicts":[]}'}]}
        replies = []
        for data in (tool, structured):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(data))
            replies.append(response)
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=replies) as request:
            ai_review_preflight.probe("test-key", "claude", "openrouter", "vendor/model")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[0].full_url == "https://openrouter.ai/api/v1/messages" for call in request.call_args_list))

    def test_claude_smoke_probe_rejects_non_contract_json(self):
        tool = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        invalid = {"content": [{"type": "text", "text": '{"summary":"ok"}'}]}
        replies = []
        for data in (tool, invalid):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(data))
            replies.append(response)
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "claude probe"):
                ai_review_preflight.probe("test-key", "claude", "openrouter", "vendor/model")

    def test_claude_smoke_probe_rejects_malformed_json(self):
        tool = {"content": [{"type": "tool_use", "name": "review_model_preflight", "input": {"status": "ok"}}]}
        malformed = {"content": [{"type": "text", "text": '{"summary":'}]}
        replies = []
        for data in (tool, malformed):
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(json.dumps(data))
            replies.append(response)
        with mock.patch.object(ai_review_preflight.urllib.request, "urlopen", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "claude probe"):
                ai_review_preflight.probe("test-key", "claude", "openrouter", "vendor/model")

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
                        step_id = step.get("id", "")
                        if step_id in ("claude_review_fallback", "claude_review_fallback_retry"):
                            self.assertEqual(step["env"]["ANTHROPIC_BASE_URL"], "${{ steps.claude_models.outputs.secondary_anthropic_base_url }}")
                        else:
                            self.assertEqual(step["env"]["ANTHROPIC_BASE_URL"], "${{ steps.claude_models.outputs.anthropic_base_url }}")
                        self.assertIn("NOUS_API_KEY", step["with"]["anthropic_api_key"])
                        self.assertEqual(step["with"]["anthropic_api_key"], step["env"]["ANTHROPIC_AUTH_TOKEN"])
        claude_preflight = next(
            step for step in automatic["jobs"]["claude-code-plugin-review"]["steps"]
            if "ai_review_preflight.py" in step.get("run", "")
        )
        self.assertIn("--probe claude", claude_preflight["run"])
        self.assertEqual(claude_preflight["env"]["CLAUDE_REVIEW_BASE_URL"], "${{ env.CLAUDE_REVIEW_BASE_URL }}")

    def test_azure_review_summary_explains_the_cross_platform_flow(self):
        root = Path(__file__).resolve().parents[2]
        launcher = (root / "azure-ci/azure-ai-code-review.yml").read_text()

        self.assertIn("## Azure DevOps → GitHub AI review", launcher)
        self.assertIn("Azure DevOps started this review; GitHub Actions executed it", launcher)
        self.assertIn("Published review", launcher)
        self.assertIn("No new actionable findings", launcher)
        self.assertNotIn("Review-report artifact", launcher)
        self.assertNotIn("${run_url}/artifacts", launcher)
        # The GitHub REST endpoint and Azure artifact upload are still needed.
        self.assertIn("${api}/actions/runs/${run_id}/artifacts", launcher)
        self.assertIn("##vso[artifact.upload", launcher)

    def test_github_review_report_has_no_broken_artifacts_page_link(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/manual-ai-review.yml").read_text()
        self.assertNotIn("Review-report artifact", workflow)
        self.assertNotIn("${run_url}/artifacts", workflow)
        self.assertIn("actions/upload-artifact@", workflow)


if __name__ == "__main__":
    unittest.main()
