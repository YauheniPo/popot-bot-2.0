"""Tests for non-interactive superpowers plugin configuration."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("configure-superpowers.py")
SPEC = importlib.util.spec_from_file_location("configure_superpowers", MODULE_PATH)
assert SPEC and SPEC.loader
configure_superpowers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_superpowers)


class ConfigureSuperpowersTests(unittest.TestCase):
    def test_configure_adds_plugin_and_disallows_tool_override(self) -> None:
        config: dict[str, object] = {}

        changed = configure_superpowers.configure(config)

        self.assertTrue(changed)
        self.assertEqual(
            config["plugins"],
            {
                "enabled": ["superpowers"],
                "entries": {"superpowers": {"allow_tool_override": False}},
            },
        )

    def test_configure_is_idempotent(self) -> None:
        config: dict[str, object] = {
            "plugins": {
                "enabled": ["superpowers"],
                "entries": {"superpowers": {"allow_tool_override": False}},
            }
        }

        changed = configure_superpowers.configure(config)

        self.assertFalse(changed)

    def test_configure_preserves_existing_plugins(self) -> None:
        config: dict[str, object] = {
            "plugins": {
                "enabled": ["ops-observability"],
                "entries": {},
            }
        }

        changed = configure_superpowers.configure(config)

        self.assertTrue(changed)
        plugins = config["plugins"]
        assert isinstance(plugins, dict)
        self.assertEqual(plugins["enabled"], ["ops-observability", "superpowers"])

    def test_configure_rejects_non_mapping_entries(self) -> None:
        config: dict[str, object] = {
            "plugins": {
                "enabled": [],
                "entries": "corrupted",
            }
        }

        with self.assertRaises(ValueError):
            configure_superpowers.configure(config)


if __name__ == "__main__":
    unittest.main()
