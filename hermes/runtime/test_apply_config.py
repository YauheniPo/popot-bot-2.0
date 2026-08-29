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
        hermes_source = settings["vps_deploy"]["hermes_source"]
        self.assertEqual(hermes_source["branch"], "main")
        self.assertEqual(hermes_source["version"], "0.20.5")
        self.assertEqual(hermes_source["release"], "v2026.8.19")
        self.assertEqual(
            hermes_source["commit"],
            "fcbd1076a93841fa88855acce810e342a5b78101",
        )
        self.assertEqual(
            hermes_source["installer_sha256"],
            "0582d9b1562efcb6e0ac62f4451021667830b830a72ce7d91eaea9fee8b6c09b",
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
        self.assertEqual(settings["vps_runtime"]["set"]["model.max_tokens"], 4096)
        self.assertIn("agent.max_turns", settings["vps_runtime"]["unset"])
        self.assertTrue(settings["vps_github"]["enabled"])
        self.assertTrue(settings["vps_github"]["require_auth"])
        self.assertEqual(settings["vps_github"]["expected_login"], "YauheniPo")
        self.assertEqual(
            settings["vps_github"]["access_probe_repository"],
            "YauheniPo/popot-bot-2.0",
        )
        self.assertEqual(settings["vps_github"]["write_owners"], ["YauheniPo"])
        self.assertEqual(settings["vps_ops"]["backup"]["retention_days"], 14)
        self.assertEqual(settings["vps_ops"]["backup"]["full_keep"], 5)
        self.assertEqual(settings["vps_ops"]["backup"]["deployment_keep"], 10)
        self.assertEqual(
            settings["vps_tools"]["google_workspace_cli"]["version"],
            "0.22.5",
        )
        self.assertIn("@sha256:", settings["vps_vscode"]["image"])
        self.assertEqual(settings["vps_browser"]["agent_browser_version"], "0.35.0")
        self.assertIn("hermes-gateway.service", settings["vps_services"]["gateway"])

    def test_manual_deploy_defaults_match_global_hermes_source_pin(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        settings = apply_config.load_settings(hermes_dir / "config" / "vps-defaults.yml")
        hermes_source = settings["vps_deploy"]["hermes_source"]
        deploy_script = (hermes_dir / "deploy-hermes.sh").read_text(encoding="utf-8")

        expected_defaults = {
            "DEFAULT_HERMES_BRANCH": hermes_source["branch"],
            "DEFAULT_HERMES_VERSION": hermes_source["version"],
            "DEFAULT_HERMES_RELEASE": hermes_source["release"],
            "DEFAULT_HERMES_COMMIT": hermes_source["commit"],
            "DEFAULT_INSTALLER_SHA256": hermes_source["installer_sha256"],
            "DEFAULT_HERMES_RAW_BASE_URL": hermes_source["raw_base_url"],
        }
        for variable, value in expected_defaults.items():
            self.assertIn(f'readonly {variable}="{value}"', deploy_script)

        expected_feature_defaults = {
            "WITH_BROWSER": settings["vps_deploy"]["features"]["browser_automation"],
            "INSTALL_DEV_CLIS": settings["vps_deploy"]["features"]["development_clis"],
            "INSTALL_GOOGLE_CLI": settings["vps_deploy"]["features"]["google_workspace_cli"],
        }
        for variable, value in expected_feature_defaults.items():
            self.assertIn(f"{variable}={str(value).lower()}", deploy_script)

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

    def test_repository_owned_values_replace_old_config_without_touching_personal_state(self) -> None:
        settings_path = MODULE_PATH.parent.parent / "config" / "vps-defaults.yml"
        settings = apply_config.load_settings(settings_path)
        current = {
            "model": {"max_tokens": 1024},
            "personal": {"preferred_workflow": "keep-this"},
        }

        operations = apply_config.build_operations(
            settings,
            current,
            {"HERMES_WORKSPACE": "/home/hermes/workspace"},
            set(),
        )

        self.assertIn(
            apply_config.Operation("set", "model.max_tokens", 4096),
            operations,
        )
        self.assertFalse(
            any(operation.key.startswith("personal.") for operation in operations)
        )

    def test_service_groups_are_deduplicated_in_order(self) -> None:
        settings = {"vps_services": {"gateway": ["gateway.service"], "ops": ["a.service", "gateway.service"]}}

        self.assertEqual(
            apply_config.service_names(settings, ["gateway", "ops"]),
            ["gateway.service", "a.service"],
        )

    def test_managed_ops_assets_render_from_repository_settings(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        settings = apply_config.load_settings(hermes_dir / "config" / "vps-defaults.yml")
        values = apply_config.build_asset_values(
            settings,
            hermes_user="hermes",
            hermes_group="hermes",
            user_home="/home/hermes",
            hermes_home="/home/hermes/.hermes",
            hermes_bin="/home/hermes/.local/bin/hermes",
            workspace="/home/hermes/workspace",
            backup_dir="/home/hermes/hermes-backups",
        )
        templates = [
            hermes_dir / "ops" / "templates" / "hermes-ops.conf",
            hermes_dir / "ops" / "templates" / "hermes-ops.logrotate",
            hermes_dir / "ops" / "templates" / "hermes-prometheus.yml",
            hermes_dir / "ops" / "templates" / "grafana-hermes-prometheus.yml",
            *sorted((hermes_dir / "ops" / "systemd").glob("*.service")),
            *sorted((hermes_dir / "ops" / "systemd").glob("*.timer")),
            *sorted((hermes_dir / "ops" / "systemd").glob("*.conf")),
        ]

        rendered = {
            template.name: apply_config.render_asset(
                template.read_text(encoding="utf-8"),
                values,
            )
            for template in templates
        }

        self.assertIn("HERMES_BACKUP_RETENTION_DAYS=14", rendered["hermes-ops.conf"])
        self.assertIn("HERMES_FULL_BACKUP_KEEP=5", rendered["hermes-ops.conf"])
        self.assertIn("HERMES_DEPLOYMENT_BACKUP_KEEP=10", rendered["hermes-ops.conf"])
        self.assertIn(
            "HERMES_OBSERVABILITY_DATABASE_RETENTION_DAYS=90",
            rendered["hermes-ops.conf"],
        )
        self.assertIn("OnUnitActiveSec=1d", rendered["hermes-backup.timer"])
        self.assertIn("OnUnitActiveSec=1d", rendered["hermes-observability-prune.timer"])
        self.assertIn("127.0.0.1:9090", rendered["hermes-prometheus.service"])
        self.assertTrue(all("@" not in content for content in rendered.values()))

    def test_asset_renderer_rejects_public_observability_bind_address(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        settings = apply_config.load_settings(hermes_dir / "config" / "vps-defaults.yml")
        settings["vps_observability"]["prometheus"]["bind_address"] = "0.0.0.0"

        with self.assertRaisesRegex(ValueError, "prometheus.bind_address"):
            apply_config.build_asset_values(
                settings,
                hermes_user="hermes",
                hermes_group="hermes",
                user_home="/home/hermes",
                hermes_home="/home/hermes/.hermes",
                hermes_bin="/home/hermes/.local/bin/hermes",
                workspace="/home/hermes/workspace",
                backup_dir="/home/hermes/hermes-backups",
            )

    def test_deploy_consumers_use_global_tooling_and_ops_settings(self) -> None:
        hermes_dir = MODULE_PATH.parent.parent
        browser_installer = (
            hermes_dir / "ops" / "install-browser-automation.sh"
        ).read_text(encoding="utf-8")
        asset_installer = (hermes_dir / "ops" / "install" / "assets.sh").read_text(
            encoding="utf-8"
        )
        services = (hermes_dir / "ansible" / "tasks" / "services.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('"agent-browser@${AGENT_BROWSER_VERSION}"', browser_installer)
        self.assertIn("vps_browser.agent_browser_version", services)
        self.assertIn(
            'render "${SCRIPT_DIR}/templates/hermes-ops.conf" /etc/hermes-ops.conf 0644',
            asset_installer,
        )
        self.assertNotIn("preserving existing /etc/hermes-ops.conf", asset_installer)
        runtime_installer = (hermes_dir / "deploy" / "runtime.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"@googleworkspace/cli@$google_cli_version"', runtime_installer)
        ansible_runtime = (hermes_dir / "ansible" / "tasks" / "runtime.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("npm audit fix", ansible_runtime)


if __name__ == "__main__":
    unittest.main()
