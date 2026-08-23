"""Tests for the managed GitHub CLI environment wrapper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("github-cli-wrapper.py")
SPEC = importlib.util.spec_from_file_location("github_cli_wrapper", MODULE_PATH)
assert SPEC and SPEC.loader
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


class GitHubCliWrapperTests(unittest.TestCase):
    def test_reads_only_supported_token_names_with_documented_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                'OPENROUTER_API_KEY="not-a-github-token"\n'
                'GITHUB_PERSONAL_ACCESS_TOKEN="personal-token"\n'
                'GITHUB_TOKEN="preferred-token"\n',
                encoding="utf-8",
            )

            self.assertEqual(wrapper.managed_token(dotenv), "preferred-token")

    def test_existing_environment_token_wins_without_reading_dotenv(self) -> None:
        missing_home = Path("/path/that/does/not/exist")

        environment = wrapper.github_environment(
            {"GH_TOKEN": "runtime-token", "GITHUB_TOKEN": "fallback-token"},
            missing_home,
        )

        self.assertEqual(environment["GH_TOKEN"], "runtime-token")

    def test_rejects_control_characters_in_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text('GITHUB_TOKEN="bad\\ntoken"\n', encoding="utf-8")

            self.assertEqual(wrapper.managed_token(dotenv), "")


if __name__ == "__main__":
    unittest.main()
