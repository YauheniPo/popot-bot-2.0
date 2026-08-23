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


class ConfigurePluginTests(unittest.TestCase):
    def test_configure_adds_plugin_and_status_command(self) -> None:
        config: dict[str, object] = {}

        changed = configure_plugin.configure(config, Path("/home/hermes/.hermes"))

        self.assertTrue(changed)
        self.assertEqual(config["plugins"], {"enabled": ["ops-observability"]})
        self.assertEqual(
            config["quick_commands"],
            {
                "status": {
                    "type": "exec",
                    "command": (
                        "HERMES_HOME=/home/hermes/.hermes "
                        "/usr/local/lib/hermes-ops/status-report.py"
                    ),
                },
                "docker_restart": {
                    "type": "exec",
                    "command": (
                        "sudo docker compose -f /home/hermes/workspace/"
                        "repositories/YauheniPo/popot-bot-2.0/hermes/vscode-server/"
                        "docker-compose.yml up -d --force-recreate"
                    ),
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
                        "/usr/local/lib/hermes-ops/status-report.py"
                    ),
                },
                "docker_restart": {
                    "type": "exec",
                    "command": (
                        "sudo docker compose -f /home/hermes/workspace/"
                        "repositories/YauheniPo/popot-bot-2.0/hermes/vscode-server/"
                        "docker-compose.yml up -d --force-recreate"
                    ),
                },
            },
        }

        self.assertFalse(configure_plugin.configure(config, Path("/home/hermes/.hermes")))

    def test_configure_rejects_invalid_plugin_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "plugins must be"):
            configure_plugin.configure({"plugins": []}, Path("/home/hermes/.hermes"))


if __name__ == "__main__":
    unittest.main()
