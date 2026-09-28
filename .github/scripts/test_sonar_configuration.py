"""Regression checks for repository-owned SonarCloud settings."""

from pathlib import Path
import re
import unittest


class SonarConfigurationTests(unittest.TestCase):
    def test_python_version_matches_the_supported_ci_range(self) -> None:
        properties = (
            Path(__file__).resolve().parents[2] / "sonar-project.properties"
        ).read_text(encoding="utf-8")

        self.assertIn("sonar.python.version=3.11,3.12", properties)

    def test_telegram_bot_is_included_in_coverage_report(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / "workflows" / "sonarcloud.yml"
        ).read_text(encoding="utf-8")
        telegram_step = workflow.split(
            "      - name: Run telegram-user-info-bot tests with coverage", 1
        )[1].split("\n      - name:", 1)[0]
        coverage_command = next(
            line.strip() for line in telegram_step.splitlines()
            if "coverage run" in line
        )
        omit = re.search(r"--omit=(\S+)", coverage_command)

        if omit is not None:
            self.assertNotIn("bot.py", omit.group(1))


if __name__ == "__main__":
    unittest.main()
