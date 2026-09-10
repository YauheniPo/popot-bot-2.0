"""Tests for non-interactive Hermes plugin configuration."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("configure-plugin.py")
SPEC = importlib.util.spec_from_file_location("configure_plugin", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
configure_plugin = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, "path", [str(MODULE_PATH.parent), *sys.path]):
    SPEC.loader.exec_module(configure_plugin)

MANAGED_COMPOSE = Path("/opt/hermes-bootstrap/vscode-server/docker-compose.yml").resolve()
MANAGED_RESTART = (
    "sudo docker compose --project-name hermes-vscode "
    f"--env-file {Path('/etc/code-server.env').resolve()} "
    f"-f {MANAGED_COMPOSE} "
    "restart code-server"
)


class ConfigurePluginTests(unittest.TestCase):
    def test_direct_configure_rejects_unsafe_arguments_before_mutating_config(self) -> None:
        for options in (
            {"vscode_project_name": "--help"},
            {"vscode_project_name": "project;id"},
            {"gateway_service": "gateway.service;id"},
            {"gateway_service": "--invalid.service"},
            {"gateway_service": "гермес.service"},
            {"gateway_service": "gateway@é.service"},
            {"gateway_service": "gateway\n.service"},
            {"vscode_env_file": Path("/tmp/env;id")},
            {"vscode_compose_file": Path("/tmp/$(id).yml")},
        ):
            with self.subTest(options=options):
                data = {}
                with self.assertRaises(ValueError):
                    configure_plugin.configure(data, Path("/home/hermes/.hermes"), **options)
                self.assertEqual(data, {})

    def test_service_names_accept_supported_ascii_forms(self) -> None:
        for service in ("hermes-gateway.service", "gateway@worker-1.service", "_worker.service", "app:worker.service"):
            with self.subTest(service=service):
                data = {}
                self.assertTrue(configure_plugin.configure(data, Path("/home/hermes"), gateway_service=service))

    def test_gateway_drop_in_uses_the_native_self_restart_protocol(self) -> None:
        drop_in = (
            MODULE_PATH.parent / "systemd" / "hermes-gateway-observability.conf"
        ).read_text(encoding="utf-8")

        self.assertIn("HERMES_GATEWAY_EXTERNAL_SUPERVISOR=1", drop_in)
        self.assertIn("RestartForceExitStatus=75", drop_in)

    def test_configure_adds_plugin_and_status_command(self) -> None:
        config: dict[str, object] = {}

        changed = configure_plugin.configure(
            config,
            Path("/home/hermes/.hermes"),
            vscode_compose_file=MANAGED_COMPOSE,
        )

        self.assertTrue(changed)
        self.assertEqual(config["plugins"], {"enabled": ["ops-observability"]})
        self.assertEqual(
            config["quick_commands"],
            {
                "status": {
                    "type": "exec",
                    "command": (
                        "HERMES_HOME=/home/hermes/.hermes "
                        "HERMES_GATEWAY_SERVICE=hermes-gateway.service "
                        "/usr/local/lib/hermes-ops/status-report.py"
                    ),
                },
                "docker_restart": {
                    "type": "exec",
                    "command": MANAGED_RESTART,
                },
            },
        )

    def test_configure_is_idempotent(self) -> None:
        config = {
            "plugins": {"enabled": ["ops-observability"]},
            "quick_commands": {
                "status": {
                    "type": "exec",
                    "command": (
                        "HERMES_HOME=/home/hermes/.hermes "
                        "HERMES_GATEWAY_SERVICE=hermes-gateway.service "
                        "/usr/local/lib/hermes-ops/status-report.py"
                    ),
                },
                "docker_restart": {
                    "type": "exec",
                    "command": MANAGED_RESTART,
                },
            },
        }

        self.assertFalse(
            configure_plugin.configure(
                config,
                Path("/home/hermes/.hermes"),
                vscode_compose_file=MANAGED_COMPOSE,
            )
        )

    def test_configure_uses_the_managed_compose_project_name(self) -> None:
        config: dict[str, object] = {}

        configure_plugin.configure(
            config,
            Path("/home/hermes/.hermes"),
            vscode_compose_file=MANAGED_COMPOSE,
            vscode_project_name="owner-vscode",
        )

        command = config["quick_commands"]["docker_restart"]["command"]
        self.assertIn("--project-name owner-vscode", command)

    def test_configure_removes_vscode_restart_when_feature_is_disabled(self) -> None:
        config = {
            "plugins": {"enabled": ["ops-observability"]},
            "quick_commands": {
                "status": {
                    "type": "exec",
                    "command": (
                        "HERMES_HOME=/home/hermes/.hermes "
                        "HERMES_GATEWAY_SERVICE=hermes-gateway.service "
                        "/usr/local/lib/hermes-ops/status-report.py"
                    ),
                },
                "docker_restart": {
                    "type": "exec",
                    "command": "sudo docker compose up -d --force-recreate",
                },
            },
        }

        changed = configure_plugin.configure(config, Path("/home/hermes/.hermes"))

        self.assertTrue(changed)
        self.assertNotIn("docker_restart", config["quick_commands"])

    def test_configure_rejects_invalid_plugin_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "plugins must be"):
            configure_plugin.configure({"plugins": []}, Path("/home/hermes/.hermes"))

    def test_main_rejects_a_config_path_not_named_config_yaml(self) -> None:
        argv = [
            "configure-plugin.py",
            "--config", "/tmp/not-config.yaml",
            "--hermes-home", "/home/hermes/.hermes",
        ]
        with mock.patch("sys.argv", argv):
            exit_code = configure_plugin.main()

        self.assertEqual(exit_code, 1)

    def test_main_rejects_an_unsafe_vscode_compose_file_path(self) -> None:
        # --config must match --hermes-home/config.yaml or main() rejects the
        # request before ever reaching the vscode-compose-file check below.
        argv = [
            "configure-plugin.py",
            "--config", "/home/hermes/.hermes/config.yaml",
            "--hermes-home", "/home/hermes/.hermes",
            "--vscode-enabled",
            "--vscode-compose-file", "/opt/hermes-bootstrap/my compose.yaml",
        ]
        with mock.patch("sys.argv", argv):
            exit_code = configure_plugin.main()

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
