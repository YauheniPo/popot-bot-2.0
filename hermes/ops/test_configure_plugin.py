"""Tests for non-interactive Hermes plugin configuration."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("configure-plugin.py")
SPEC = importlib.util.spec_from_file_location("configure_plugin", MODULE_PATH)
assert SPEC and SPEC.loader
configure_plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_plugin)

MANAGED_COMPOSE = Path("/opt/hermes-bootstrap/vscode-server/docker-compose.yml")
MANAGED_RESTART = (
    "sudo docker compose --project-name hermes-vscode "
    "--env-file /etc/code-server.env "
    "-f /opt/hermes-bootstrap/vscode-server/docker-compose.yml "
    "restart code-server"
)


class ConfigurePluginTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
