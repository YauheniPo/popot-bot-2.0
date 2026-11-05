"""Contracts for the Hermes infrastructure CI workflow."""

from pathlib import Path
from importlib.metadata import PackageNotFoundError, metadata
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class HermesCiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        workflow_path = ROOT / ".github/workflows/hermes-ci.yml"
        cls.workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def test_dependency_install_jobs_use_python_supported_by_ansible(self) -> None:
        try:
            requires_python = metadata("ansible-core")["Requires-Python"]
        except PackageNotFoundError:
            self.skipTest("ansible-core is required for controller compatibility checks")
        from packaging.specifiers import SpecifierSet

        supported = SpecifierSet(requires_python)

        def step_groups(value):
            if isinstance(value, dict):
                if isinstance(value.get("steps"), list):
                    yield value["steps"]
                for child in value.values():
                    yield from step_groups(child)
            elif isinstance(value, list):
                for child in value:
                    yield from step_groups(child)

        checked = 0
        for path in [*sorted((ROOT / ".github/workflows").glob("*.yml")),
                     *sorted((ROOT / "azure-ci").glob("*.yml"))]:
            workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            for steps in step_groups(workflow):
                python_version = None
                for step in steps:
                    if step.get("uses", "").startswith("actions/setup-python@"):
                        python_version = step["with"]["python-version"]
                    elif step.get("task", "").startswith("UsePythonVersion@"):
                        python_version = step["inputs"]["versionSpec"]
                    command = step.get("run", step.get("bash", ""))
                    if "hermes/requirements-dev.txt" not in command:
                        continue
                    checked += 1
                    with self.subTest(path=str(path.relative_to(ROOT)), python=python_version):
                        self.assertIsNotNone(python_version)
                        self.assertIn(python_version, supported)
        self.assertGreater(checked, 0)

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
