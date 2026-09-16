"""Regression checks for repository-owned SonarCloud settings."""

from pathlib import Path
import unittest


class SonarConfigurationTests(unittest.TestCase):
    def test_python_version_matches_the_supported_ci_range(self) -> None:
        properties = (
            Path(__file__).resolve().parents[2] / "sonar-project.properties"
        ).read_text(encoding="utf-8")

        self.assertIn("sonar.python.version=3.11,3.12", properties)


if __name__ == "__main__":
    unittest.main()
