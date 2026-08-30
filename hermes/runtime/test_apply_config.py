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
        source = settings["vps_deploy"]["hermes_source"]
        self.assertEqual(source["branch"], "main")
        self.assertEqual(source["version"], "0.20.5")
        self.assertEqual(source["release"], "v2026.8.19")
        self.assertEqual(
            source["commit"],
            "f293e7206b4ddd66042329442c6afebc19a8808d",
        )
        self.assertEqual(
            source["installer_sha256"],
            "d5558cd419c8d46bdc958064cb97f963d1ea793866414c025906ec15033512ed",
        )
        self.assertTrue(settings["vps_deploy"]["features"]["observability"])
        self.assertTrue(settings["vps_deploy"]["features"]["browser_automation"])
        self.assertTrue(settings["vps_deploy"]["features"]["development_clis"])
        self.assertTrue(settings["vps_deploy"]["features"]["google_workspace_cli"])
        self.assertNotIn("model.default", settings["vps_runtime"]["set"])
        self.assertEqual(settings["vps_deploy"]["bundle"]["dir"], "/opt/hermes-bootstrap")
        self.assertEqual(
            settings["vps_hermes"]["config"]["managed_overlay"],
            {"user_char_limit": 3000, "memory_char_limit": 4000},
        )
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
