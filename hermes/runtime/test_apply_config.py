"""Tests for the shared Hermes VPS runtime configuration planner."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("apply-config.py")
SPEC = importlib.util.spec_from_file_location("apply_config", MODULE_PATH)
assert SPEC and SPEC.loader
apply_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_config)


class ApplyConfigTests(unittest.TestCase):
    def test_repository_settings_keep_critical_values_visible_and_typed(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"

        settings = apply_config.load_settings(settings_path)

        self.assertEqual(settings["vps_deploy"]["identity"]["user"], "hermes")
        source = settings["vps_deploy"]["source"]
        self.assertEqual(source["branch"], "main")
        self.assertEqual(source["version"], "0.20.5")
        self.assertEqual(source["release"], "v2026.8.19")
        self.assertEqual(
            source["commit"],
            "fcbd1076a93841fa88855acce810e342a5b78101",
        )
        self.assertEqual(
            source["installer_sha256"],
            "0582d9b1562efcb6e0ac62f4451021667830b830a72ce7d91eaea9fee8b6c09b",
        )
        self.assertTrue(settings["vps_deploy"]["features"]["observability"])
        self.assertEqual(settings["vps_runtime"]["set"]["approvals.mode"], "manual")
        self.assertEqual(settings["vps_runtime"]["set"]["browser.backend"], "off")
        self.assertEqual(settings["vps_runtime"]["set"]["display.tool_progress"], "off")
        self.assertIn("agent.max_turns", settings["vps_runtime"]["unset"])
        self.assertTrue(settings["vps_github"]["enabled"])
        self.assertTrue(settings["vps_github"]["require_auth"])
        self.assertEqual(settings["vps_github"]["expected_login"], "YauheniPo")
        self.assertEqual(
            settings["vps_github"]["access_probe_repository"],
            "YauheniPo/popot-bot-2.0",
        )
        self.assertEqual(settings["vps_github"]["write_owners"], ["YauheniPo"])
        self.assertIn("hermes-gateway.service", settings["vps_services"]["gateway"])

    def test_manual_deploy_defaults_match_global_source_pin(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        settings = apply_config.load_settings(hermes_dir / "config" / "vps-defaults.yml")
        source = settings["vps_deploy"]["source"]
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        expected_defaults = {
            "DEFAULT_HERMES_BRANCH": source["branch"],
            "DEFAULT_HERMES_VERSION": source["version"],
            "DEFAULT_HERMES_RELEASE": source["release"],
            "DEFAULT_HERMES_COMMIT": source["commit"],
            "DEFAULT_INSTALLER_SHA256": source["installer_sha256"],
        }
        for variable, value in expected_defaults.items():
            self.assertIn(f'readonly {variable}="{value}"', deploy_script)

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

    def test_service_groups_are_deduplicated_in_order(self) -> None:
        settings = {"vps_services": {"gateway": ["gateway.service"], "ops": ["a.service", "gateway.service"]}}

        self.assertEqual(
            apply_config.service_names(settings, ["gateway", "ops"]),
            ["gateway.service", "a.service"],
        )


if __name__ == "__main__":
    unittest.main()
