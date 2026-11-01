"""Regression checks for repository-owned SonarCloud settings."""

from pathlib import Path
import os
import re
import shlex
import subprocess
import tempfile
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

    def test_hermes_coverage_has_a_failure_threshold_after_tests(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / "workflows" / "sonarcloud.yml"
        ).read_text(encoding="utf-8")
        hermes_step = workflow.split(
            "      - name: Run Hermes tests with coverage", 1
        )[1].split("\n      - name:", 1)[0]
        test_command = hermes_step.index("coverage run")
        threshold_command = hermes_step.index("coverage report --fail-under=")
        report_command = hermes_step.index("coverage xml")

        self.assertLess(test_command, threshold_command)
        self.assertLess(threshold_command, report_command)

    def test_hermes_coverage_installs_required_ansible_collections(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / "workflows" / "sonarcloud.yml"
        ).read_text(encoding="utf-8")
        hermes_step = workflow.split(
            "      - name: Run Hermes tests with coverage", 1
        )[1].split("\n      - name:", 1)[0]
        install = (
            "ansible-galaxy collection install --requirements-file "
            "hermes/ansible/requirements.yml"
        )

        self.assertIn(install, hermes_step)
        self.assertLess(hermes_step.index("python -m pip install"), hermes_step.index(install))
        self.assertLess(hermes_step.index(install), hermes_step.index("coverage run"))


    def test_ansible_collection_retry_does_not_sleep_after_the_last_attempt(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / "workflows" / "sonarcloud.yml"
        ).read_text(encoding="utf-8")
        hermes_step = workflow.split(
            "      - name: Run Hermes tests with coverage", 1
        )[1].split("\n      - name:", 1)[0]
        start = hermes_step.index("for attempt in 1 2 3; do")
        end = hermes_step.index("done", start) + len("done")
        retry = hermes_step[start:end]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sleeps = root / "sleeps"
            galaxy = fake_bin / "ansible-galaxy"
            galaxy.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            sleep = fake_bin / "sleep"
            sleep.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$1\" >> " + shlex.quote(str(sleeps)) + "\n",
                encoding="utf-8",
            )
            galaxy.chmod(0o755)
            sleep.chmod(0o755)
            script = root / "retry.sh"
            script.write_text("#!/usr/bin/env bash\n" + retry + "\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin"},
            )
            self.assertIn("attempt 3 failed", result.stderr)
            self.assertEqual(sleeps.read_text(encoding="utf-8").split(), ["10", "20"])


if __name__ == "__main__":
    unittest.main()
