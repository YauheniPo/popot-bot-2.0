"""Contracts for the Hermes infrastructure CI workflow."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class HermesCiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        workflow_path = ROOT / ".github/workflows/hermes-ci.yml"
        cls.workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_verification_job_has_time_for_checks_after_slow_package_mirrors(self) -> None:
        job_timeout = int(self.workflow["jobs"]["verify"]["timeout-minutes"])

        self.assertGreaterEqual(job_timeout, 30)

    def test_install_uses_only_required_apt_packages_without_recommends(self) -> None:
        steps = self.workflow["jobs"]["verify"]["steps"]
        install_step = next(step for step in steps if step.get("name") == "Install verification dependencies")

        self.assertIn(
            "sudo apt-get install --yes --no-install-recommends prometheus ripgrep shellcheck",
            install_step["run"],
        )


if __name__ == "__main__":
    unittest.main()
