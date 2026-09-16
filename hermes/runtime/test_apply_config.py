"""Tests for the shared Hermes VPS runtime configuration planner."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("apply-config.py")
SPEC = importlib.util.spec_from_file_location("apply_config", MODULE_PATH)
assert SPEC and SPEC.loader
apply_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_config)


class ApplyConfigTests(unittest.TestCase):
    def test_repository_settings_have_required_structure_and_valid_types(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)

        for section in (
            "vps_deploy",
            "vps_hermes",
            "vps_runtime",
            "vps_github",
            "vps_ops",
            "vps_services",
        ):
            self.assertIsInstance(settings[section], dict)

        identity = settings["vps_deploy"]["identity"]
        for key in (
            "user",
            "user_home",
            "hermes_home",
            "hermes_bin",
            "workspace",
            "backup_dir",
        ):
            self.assertIsInstance(identity[key], str)
            self.assertTrue(identity[key])

        source = settings["vps_deploy"]["hermes_source"]
        for key in (
            "raw_base_url",
            "branch",
            "version",
            "release",
            "commit",
            "installer_sha256",
        ):
            self.assertIsInstance(source[key], str)
            self.assertTrue(source[key])
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(source["installer_sha256"], r"^[0-9a-f]{64}$")

        features = settings["vps_deploy"]["features"]
        self.assertTrue(features)
        self.assertTrue(all(isinstance(value, bool) for value in features.values()))
        self.assertTrue(features["searxng"])
        self.assertTrue(features["lock_public_ssh"])
        self.assertIsInstance(settings["vps_deploy"]["bundle"]["dir"], str)
        searxng = settings["vps_searxng"]
        self.assertEqual(searxng["image"], "docker.io/searxng/searxng:latest")
        self.assertEqual(searxng["bind_address"], "127.0.0.1")
        self.assertEqual(searxng["host_port"], 8888)
        self.assertEqual(searxng["valkey_image"], "docker.io/valkey/valkey:8-alpine")
        self.assertEqual(searxng["valkey_host"], "valkey")
        self.assertEqual(searxng["valkey_port"], 6379)
        self.assertTrue(settings["vps_tailscale"]["serve"]["enabled"])
        self.assertEqual(len(settings["vps_tailscale"]["serve"]["services"]), 5)

        overlay = settings["vps_hermes"]["config"]["managed_overlay"]
        self.assertEqual(overlay["model"], {"provider": "ollama-cloud", "default": "deepseek-v4-pro"})
        self.assertEqual(
            overlay["fallback_providers"],
            [{"provider": "nvidia", "model": "deepseek-ai/deepseek-v4-pro-0813"}],
        )
        self.assertEqual(overlay["cron"]["model_provider"], "nvidia")
        self.assertEqual(overlay["cron"]["model"], "deepseek-ai/deepseek-v4-pro-0813")
        self.assertEqual(
            overlay["auxiliary"]["compression"],
            {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        )
        self.assertIsInstance(overlay["model"]["default"], str)
        self.assertTrue(overlay["model"]["default"])
        self.assertIsInstance(overlay["model"]["provider"], str)
        self.assertTrue(overlay["model"]["provider"])
        self.assertIs(type(overlay["user_char_limit"]), int)
        self.assertGreater(overlay["user_char_limit"], 0)
        self.assertIs(type(overlay["memory_char_limit"]), int)
        self.assertGreater(overlay["memory_char_limit"], 0)
        self.assertIn(type(overlay["compression"]["threshold"]), (int, float))
        self.assertGreater(overlay["compression"]["threshold"], 0)
        self.assertLessEqual(overlay["compression"]["threshold"], 1)
        self.assertIsInstance(overlay["auxiliary"]["compression"]["provider"], str)
        self.assertIsInstance(overlay["auxiliary"]["compression"]["model"], str)
        self.assertIsInstance(overlay["cron"]["model_provider"], str)
        self.assertIsInstance(overlay["cron"]["model"], str)
        self.assertIsInstance(overlay["cron"]["model_drift_guard"], bool)
        self.assertEqual(settings["vps_runtime"]["set"]["model.max_tokens"], 32768)
        self.assertIsInstance(overlay["web"]["search_backend"], str)
        self.assertIsInstance(overlay["web"]["extract_backend"], str)

        values = apply_config.build_asset_values(
            settings,
            hermes_user=identity["user"],
            hermes_group=identity["user"],
            user_home=identity["user_home"],
            hermes_home=identity["hermes_home"],
            hermes_bin=identity["hermes_bin"],
            workspace=identity["workspace"],
            backup_dir=identity["backup_dir"],
        )
        self.assertTrue(values)
        self.assertTrue(all(isinstance(value, str) and (value or key == "SEARXNG_URL") for key, value in values.items()))
        self.assertEqual(values["API_RETRY_PROVIDER"], "nvidia")
        self.assertEqual(values["API_RETRY_MODEL"], "deepseek-ai/deepseek-v4-pro-0813")
        self.assertEqual(values["SEARXNG_URL"], "")

    def assert_runtime_contract(self, runtime: dict) -> None:
        # These are supported modes in the pinned Hermes gateway/display_config.py.
        # Numeric defaults remain tunable; budgets must stay bounded and positive.
        self.assertIsInstance(runtime["display.tool_progress"], str)
        self.assertIn(runtime["display.tool_progress"], {"off", "new", "all", "verbose", "log"})
        self.assertIsInstance(runtime["browser.backend"], str)
        self.assertTrue(runtime["browser.backend"].strip())
        for key in (
            "agent.max_turns",
            "goals.max_turns",
            "model.max_tokens",
            "tool_loop_guardrails.hard_stop_after.exact_failure",
            "tool_loop_guardrails.hard_stop_after.same_tool_failure",
            "tool_loop_guardrails.hard_stop_after.idempotent_no_progress",
            "tool_loop_guardrails.loop_caps.max_web_searches",
            "tool_loop_guardrails.loop_caps.max_subagents",
        ):
            self.assertIs(type(runtime[key]), int, key)
            self.assertGreater(runtime[key], 0, key)
        for key in ("proactive_prune_tokens", "agent.agent_cache.idle_ttl_secs"):
            self.assertIs(type(runtime[key]), int, key)
            self.assertGreaterEqual(runtime[key], 0, key)
        for key in (
            "tool_loop_guardrails.hard_stop_enabled",
            "display.platforms.telegram.interim_assistant_messages",
        ):
            self.assertIs(type(runtime[key]), bool, key)

    def test_repository_runtime_settings_follow_contract(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        self.assert_runtime_contract(settings["vps_runtime"]["set"])

    def test_runtime_contract_accepts_tuning_and_rejects_invalid_values(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        runtime = settings["vps_runtime"]["set"]
        for mode in ("off", "new", "all", "verbose", "log"):
            with self.subTest(mode=mode):
                self.assert_runtime_contract({**runtime, "display.tool_progress": mode, "agent.max_turns": 75})
        for key, invalid in (
            ("display.tool_progress", "typo"),
            ("display.tool_progress", True),
            ("agent.max_turns", "200"),
            ("agent.max_turns", True),
            ("agent.max_turns", 0),
            ("goals.max_turns", -1),
            ("proactive_prune_tokens", -1),
            ("agent.agent_cache.idle_ttl_secs", 1.5),
            ("tool_loop_guardrails.hard_stop_after.exact_failure", False),
            ("tool_loop_guardrails.hard_stop_enabled", "true"),
        ):
            with self.subTest(key=key, invalid=invalid):
                with self.assertRaises(AssertionError):
                    self.assert_runtime_contract({**runtime, key: invalid})

    def assert_fallback_contract(self, overlay: dict) -> None:
        chain = overlay["fallback_providers"]
        self.assertIsInstance(chain, list)
        routes = {(overlay["model"]["provider"], overlay["model"]["default"])}
        for entry in chain:
            self.assertIsInstance(entry, dict)
            for key in ("provider", "model"):
                self.assertIsInstance(entry.get(key), str, key)
                self.assertTrue(entry[key].strip(), key)
            route = (entry["provider"], entry["model"])
            self.assertNotIn(route, routes, "fallback must use a distinct provider/model pair")
            routes.add(route)

    def test_repository_fallback_chain_is_complete_and_distinct(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        self.assert_fallback_contract(settings["vps_hermes"]["config"]["managed_overlay"])

    def test_fallback_contract_rejects_entries_hermes_would_ignore(self) -> None:
        for chain in ({}, [{}], [{"provider": "ollama-cloud", "model": " "}],
                      [{"provider": "ollama-cloud", "model": "kimi-k3"}]):
            with self.subTest(chain=chain):
                with self.assertRaises(AssertionError):
                    self.assert_fallback_contract({
                        "model": {"provider": "ollama-cloud", "default": "kimi-k3"},
                        "fallback_providers": chain,
                    })

    def test_disabled_skills_are_unique_and_never_essential(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)
        disabled = settings["vps_hermes"]["config"]["managed_overlay"]["skills"]["disabled"]

        self.assertIsInstance(disabled, list)
        self.assertTrue(all(isinstance(skill, str) and skill.strip() for skill in disabled))
        self.assertEqual(len(disabled), len(set(disabled)))
        # hermes-agent is an ESSENTIAL_SKILL upstream drops from the list;
        # the local skills this deployment relies on must stay enabled.
        for kept in (
            "hermes-agent",
            "github",
            "codebase-inspection",
            "hermes-vps-ops",
            "telegram-logs-parser",
            "architecture-diagram",
            "excalidraw",
        ):
            self.assertNotIn(kept, disabled)

    def test_matt_pocock_engineering_skills_are_pinned_and_enabled(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")

        source = settings["vps_external_skills"]["matt_pocock_engineering"]
        self.assertIs(source["enabled"], True)
        self.assertEqual(source["repository"], "https://github.com/mattpocock/skills.git")
        self.assertRegex(source["revision"], r"^[0-9a-f]{40}$")
        self.assertTrue(source["checkout_dir"].startswith("/opt/"))
        self.assertEqual(
            source["skills_dir"],
            f"{source['checkout_dir']}/skills/engineering",
        )

    def test_repository_security_policy_requires_approval_and_github_auth(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        # Security invariants are intentionally separate from tunable defaults.
        self.assertEqual(settings["vps_runtime"]["set"]["approvals.mode"], "manual")
        self.assertIs(settings["vps_github"]["require_auth"], True)
        self.assertIs(settings["vps_github"]["enabled"], True)

    def test_repository_runtime_and_github_settings_have_valid_structure(self) -> None:
        settings = apply_config.load_settings(MODULE_PATH.parent.parent / "config" / "vps-defaults.yml")
        runtime_settings = settings["vps_runtime"]["set"]
        self.assertTrue(runtime_settings)
        self.assertTrue(
            all(isinstance(key, str) and key for key in runtime_settings)
        )
        self.assertNotIn("unset", settings["vps_runtime"])
        self.assertIsInstance(settings["vps_github"]["enabled"], bool)
        self.assertIsInstance(settings["vps_github"]["require_auth"], bool)
        self.assertIsInstance(settings["vps_github"]["expected_login"], str)
        self.assertTrue(settings["vps_github"]["expected_login"].strip())
        self.assertIsInstance(settings["vps_github"]["access_probe_repository"], str)
        self.assertRegex(settings["vps_github"]["access_probe_repository"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        self.assertIsInstance(settings["vps_github"]["write_owners"], list)
        self.assertIsInstance(settings["vps_github"]["write_repositories"], list)
        self.assertIsInstance(settings["vps_services"]["gateway"], list)
        self.assertTrue(
            all(
                isinstance(owner, str) and owner
                for owner in settings["vps_github"]["write_owners"]
            )
        )
        self.assertTrue(
            all(
                isinstance(service, str) and service
                for service in settings["vps_services"]["gateway"]
            )
        )

    def test_manual_deploy_reads_pin_from_settings(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        # The manual deploy script must not carry its own copy of the source
        # pin: resolve_source_pin reads it from vps-defaults.yml at runtime.
        self.assertIn("resolve_source_pin", deploy_script)
        self.assertNotIn("DEFAULT_HERMES_COMMIT", deploy_script)
        self.assertNotIn("DEFAULT_INSTALLER_SHA256", deploy_script)
        self.assertIn('data["vps_deploy"]["hermes_source"]', deploy_script)
        self.assertIn("8#$settings_perms & 8#022", deploy_script)
        self.assertNotIn("settings_owner", deploy_script)
        self.assertNotIn("must be owned by root", deploy_script)
        self.assertIn('HERMES_BRANCH="${HERMES_BRANCH:-$source_branch}"', deploy_script)
        self.assertIn('HERMES_COMMIT="${HERMES_COMMIT:-$source_commit}"', deploy_script)
        self.assertIn('INSTALLER_SHA256="${INSTALLER_SHA256:-$source_installer_sha256}"', deploy_script)
        self.assertIn(
            "verify_updated_kanban_state\n  apply_local_hermes_patches",
            deploy_script,
        )

    def test_manual_deploy_preserves_path_options_and_resolves_gateway_service(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        for option, variable in (
            ("--user-home", "REQUESTED_USER_HOME"),
            ("--hermes-home", "REQUESTED_HERMES_HOME"),
            ("--workspace", "REQUESTED_HERMES_WORKSPACE"),
            ("--backup-dir", "REQUESTED_HERMES_BACKUP_DIR"),
        ):
            self.assertIn(option, deploy_script)
            self.assertIn(f'{variable}="$2"', deploy_script)
        self.assertIn("resolve_user_paths\n  resolve_managed_runtime", deploy_script)
        self.assertIn('systemctl start "$HERMES_GATEWAY_SERVICE"', deploy_script)

    @staticmethod
    def _asset_values(settings: dict) -> dict:
        identity = settings["vps_deploy"]["identity"]
        return apply_config.build_asset_values(
            settings,
            hermes_user=identity["user"],
            hermes_group=identity["user"],
            user_home=identity["user_home"],
            hermes_home=identity["hermes_home"],
            hermes_bin=identity["hermes_bin"],
            workspace=identity["workspace"],
            backup_dir=identity["backup_dir"],
        )

    def test_vps_web_searxng_url_validation_rejects_malformed_values(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )

        # The default carries an empty searxng_url; build_asset_values must keep
        # SEARXNG_URL as an allowed empty string.
        values = self._asset_values(settings)
        self.assertEqual(values["SEARXNG_URL"], "")

        # vps_web must be a mapping.
        for bad_web in (None, "http://127.0.0.1:8888", 8888, ["http://127.0.0.1:8888"]):
            with self.subTest(vps_web=bad_web):
                with self.assertRaisesRegex(ValueError, "vps_web must be a mapping"):
                    self._asset_values({**settings, "vps_web": bad_web})

        # searxng_url must be a single-line string.
        for bad_url in (123, ["http://x"], "http://a\nb", "http://a\rb"):
            with self.subTest(searxng_url=bad_url):
                with self.assertRaisesRegex(ValueError, "vps_web.searxng_url must be a single-line string"):
                    self._asset_values({**settings, "vps_web": {"searxng_url": bad_url}})

        # A valid endpoint flows through to the rendered value.
        endpoint = "http://127.0.0.1:8888"
        self.assertEqual(
            self._asset_values({**settings, "vps_web": {"searxng_url": endpoint}})["SEARXNG_URL"],
            endpoint,
        )

    def test_tailscale_serve_endpoints_render_and_reject_malformed_values(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )

        # The repository default renders the managed endpoint list for the script.
        values = self._asset_values(settings)
        self.assertEqual(values["TAILSCALE_SERVE_ENDPOINTS"], "https 443 http://127.0.0.1:9119,"
                         "https 3000 http://127.0.0.1:3000,"
                         "https 9090 http://127.0.0.1:9090,"
                         "https 3001 http://127.0.0.1:3001,"
                         "https 8888 http://127.0.0.1:8888")

        # serve must be a mapping.
        with self.assertRaisesRegex(ValueError, "vps_tailscale.serve must be a mapping"):
            self._asset_values({**settings, "vps_tailscale": {"serve": "nope"}})

        # services must be a non-empty list.
        for bad_services in ([], "nope", None):
            with self.subTest(services=bad_services):
                with self.assertRaisesRegex(
                    ValueError, "vps_tailscale.serve.services must be a non-empty list"
                ):
                    self._asset_values({**settings, "vps_tailscale": {"serve": {"services": bad_services}}})

        # each entry must be a mapping with a valid port and loopback target.
        with self.assertRaisesRegex(ValueError, "entries must be mappings"):
            self._asset_values({**settings, "vps_tailscale": {"serve": {"services": ["nope"]}}})
        for bad_port in (0, 70000, True, "443"):
            with self.subTest(port=bad_port):
                with self.assertRaisesRegex(ValueError, "port must be 1-65535"):
                    self._asset_values(
                        {**settings, "vps_tailscale": {"serve": {"services": [{"protocol": "https", "port": bad_port, "target": "http://127.0.0.1:9119"}]}}}
                    )
        for bad_target in ("http://0.0.0.0:9119", "http://evil.example:9119", "https://127.0.0.1:9119", "ftp://127.0.0.1:80", 9119):
            with self.subTest(target=bad_target):
                with self.assertRaisesRegex(ValueError, "target must be a loopback URL"):
                    self._asset_values(
                        {**settings, "vps_tailscale": {"serve": {"services": [{"protocol": "https", "port": 443, "target": bad_target}]}}}
                    )

        # Protocol, port, and target render as one endpoint for the bootstrap script.
        custom = {"serve": {"services": [{"protocol": "http", "port": 443, "target": "http://127.0.0.1:9119"}]}}
        self.assertEqual(
            self._asset_values({**settings, "vps_tailscale": custom})["TAILSCALE_SERVE_ENDPOINTS"],
            "http 443 http://127.0.0.1:9119",
        )

        invalid_protocol = {"serve": {"services": [{"protocol": "tcp", "port": 443, "target": "http://127.0.0.1:9119"}]}}
        with self.assertRaisesRegex(ValueError, "protocol must be http or https"):
            self._asset_values({**settings, "vps_tailscale": invalid_protocol})

    def test_tailscale_disabled_does_not_require_serve_settings(self) -> None:
        settings = apply_config.load_settings(
            MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        )
        settings["vps_deploy"]["features"]["tailscale"] = False
        settings.pop("vps_tailscale")

        self.assertEqual(self._asset_values(settings)["TAILSCALE_SERVE_ENDPOINTS"], "")

    def test_build_operations_applies_defaults_capabilities_and_unsets_overrides(self) -> None:
        settings = {
            "vps_runtime": {
                "set": {"terminal.cwd": "${HERMES_WORKSPACE}", "feature.enabled": True},
                "set_if_missing": {"model.max_tokens": 4096, "preserved.value": "new"},
                "unset": ["agent.max_turns", "missing.value"],
                "capabilities": {
                    "search": {
                        "set": {"web.search_backend": "provider"},
                        "unset_when_missing": ["web.search_backend"],
                    },
                    "extract": {"set": {"web.extract_backend": "extractor"}},
                },
            }
        }
        current = {
            "terminal": {"cwd": "/old"},
            "feature": {"enabled": True},
            "model": {},
            "preserved": {"value": "keep"},
            "agent": {"max_turns": 3},
            "web": {"search_backend": "old"},
        }

        operations = apply_config.build_operations(
            settings,
            current,
            {"HERMES_WORKSPACE": "/home/hermes/workspace"},
            {"search"},
        )

        self.assertEqual(
            operations,
            [
                apply_config.Operation("set", "terminal.cwd", "/home/hermes/workspace"),
                apply_config.Operation("set", "model.max_tokens", 4096),
                apply_config.Operation("unset", "agent.max_turns"),
                apply_config.Operation("set", "web.search_backend", "provider"),
            ],
        )

    def test_missing_capability_removes_only_its_explicit_fallback(self) -> None:
        settings = {
            "vps_runtime": {
                "capabilities": {
                    "search": {
                        "set": {"web.search_backend": "provider"},
                        "unset_when_missing": ["web.search_backend"],
                    },
                    "extract": {"set": {"web.extract_backend": "extractor"}},
                }
            }
        }
        current = {"web": {"search_backend": "old", "extract_backend": "portal"}}

        operations = apply_config.build_operations(settings, current, {}, set())

        self.assertEqual(operations, [apply_config.Operation("unset", "web.search_backend")])

    def test_load_settings_rejects_a_path_not_named_vps_defaults_yml(self) -> None:
        with self.assertRaisesRegex(ValueError, "vps-defaults.yml"):
            apply_config.load_settings(Path("/tmp/not-vps-defaults.yml"))

    def test_load_settings_accepts_a_literal_dotdot_path(self) -> None:
        # install/common.sh builds --settings as "${SCRIPT_DIR}/../config/
        # vps-defaults.yml" -- a literal ".." component, never normalized by
        # the shell. load_settings() must resolve it, not reject it.
        settings_path = MODULE_PATH.parent / ".." / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)

        user = settings["vps_deploy"]["identity"]["user"]
        self.assertIsInstance(user, str)
        self.assertTrue(user)

    def test_main_apply_rejects_an_unsafe_hermes_bin_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "vps-defaults.yml"
            settings_path.write_text("vps_runtime: {}\n", encoding="utf-8")
            argv = [
                "apply-config.py",
                "apply",
                "--settings", str(settings_path),
                "--hermes-home", str(Path(temporary_directory) / "home"),
                "--hermes-bin", "/opt/hermes-bootstrap/bin/hermes bin",
                "--workspace", "/home/hermes/workspace",
            ]
            with mock.patch("sys.argv", argv):
                exit_code = apply_config.main()

        self.assertEqual(exit_code, 1)

    def test_service_groups_are_deduplicated_in_order(self) -> None:
        settings = {"vps_services": {"gateway": ["gateway.service"], "ops": ["a.service", "gateway.service"]}}

        self.assertEqual(
            apply_config.service_names(settings, ["gateway", "ops"]),
            ["gateway.service", "a.service"],
        )


if __name__ == "__main__":
    unittest.main()
