"""Regression tests for the deliberately non-secret Docker config export."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path

import yaml


MODULE_PATH = Path(__file__).with_name("export-vps-config.py")
SPEC = importlib.util.spec_from_file_location("export_vps_config", MODULE_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ExportVpsConfigTests(unittest.TestCase):
    def test_local_hermes_base_image_is_pinned_by_digest(self) -> None:
        dockerfile = MODULE_PATH.with_name("Dockerfile").read_text(encoding="utf-8")

        self.assertRegex(
            dockerfile,
            r"ARG HERMES_IMAGE=[^\s]+@sha256:[0-9a-f]{64}",
        )

        compose = MODULE_PATH.with_name("docker-compose.local.yml").read_text(
            encoding="utf-8"
        )
        external_images = re.findall(r"^\s*image: ((?!hermes-local-test)[^\s]+)$", compose, re.MULTILINE)
        self.assertTrue(external_images)
        self.assertTrue(all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in external_images))

    def test_nested_mapping_is_not_exported_even_when_its_key_is_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "stt": {
                            "enabled": True,
                            "language": "en",
                            "openai": {"api_key": "must-not-leave-docker"},
                        },
                        "agent": {"max_turns": 3, "reasoning_effort": "high"},
                        "model": {"provider": "openrouter", "default": "safe-model"},
                    }
                ),
                encoding="utf-8",
            )
            previous_path = exporter.CONFIG_PATH
            exporter.CONFIG_PATH = config_path
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(exporter.main(), 0)
            finally:
                exporter.CONFIG_PATH = previous_path

        rendered = output.getvalue()
        self.assertNotIn("must-not-leave-docker", rendered)
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("max_turns", rendered)
        self.assertEqual(
            yaml.safe_load(rendered)["vps_hermes"]["config"]["managed_overlay"],
            {
                "agent": {"reasoning_effort": "high"},
                "stt": {"enabled": True, "language": "en"},
            },
        )


if __name__ == "__main__":
    unittest.main()
