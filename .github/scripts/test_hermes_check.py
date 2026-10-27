"""Run the real check script against small isolated working trees."""

import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[2]
HAS_PYTEST = importlib.util.find_spec("pytest") is not None


@unittest.skipUnless(shutil.which("git") and shutil.which("rg"), "git and rg are required")
class HermesCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hermes-check-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("git", "rg", "bash", "sort", "grep", "mktemp", "rm", "uname", "dirname", "mkdir"):
            path = shutil.which(name)
            if path:
                (self.tools / name).symlink_to(path)
        for name in ("shellcheck", "promtool", "ansible-playbook"):
            self.executable(name, "#!/bin/sh\nexit 0\n")
        self.python(sys.executable)
        self.write("hermes/check.sh", (ROOT / "hermes/check.sh").read_text())
        test = "import unittest\nclass Example(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
        self.write("hermes/test_example.py", test)
        self.write(".github/scripts/test_ci_example.py", test)
        self.run_git("init", "--quiet")
        self.run_git("add", "hermes", ".github/scripts")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def executable(self, name, text):
        path = self.tools / name
        path.write_text(text)
        path.chmod(0o755)

    def python(self, path):
        self.executable("python3", "#!/bin/sh\nexec " + shlex.quote(str(path)) + ' "$@"\n')

    def run_git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, timeout=15)

    def check(self, strict=False):
        return subprocess.run(
            ["bash", "hermes/check.sh", *(["--require-tools"] if strict else [])],
            cwd=self.root, capture_output=True, text=True, timeout=40,
            env={**os.environ, "PATH": str(self.tools), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )

    def test_deleted_tracked_sources_and_tests_are_skipped(self):
        names = ("hermes/obsolete.sh", "hermes/obsolete.py", "hermes/test_obsolete.py")
        for name in names:
            self.write(name, "# obsolete\n")
        self.run_git("add", *names)
        for name in names:
            (self.root / name).unlink()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all Hermes checks passed", result.stdout)

    @unittest.skipUnless(HAS_PYTEST, "pytest is required for real collection checks")
    def test_untracked_hermes_files_are_ignored_by_every_suite(self):
        self.write("hermes/scratch.sh", "invalid shell syntax )\n")
        self.write("hermes/scratch.py", "invalid python syntax !!!\n")
        self.write("hermes/test_scratch.py", "def test_scratch():\n    assert False, 'untracked test collected'\n")
        result = self.check(strict=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running pytest suite", result.stdout)

    def test_tracked_unittest_failure_still_fails(self):
        self.write("hermes/test_example.py", "import unittest\nclass Failure(unittest.TestCase):\n    def test_fail(self):\n        self.fail('tracked unittest failed')\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked unittest failed", result.stderr)

    @unittest.skipUnless(HAS_PYTEST, "pytest is required for real collection checks")
    def test_tracked_pytest_only_failure_still_fails(self):
        self.write("hermes/test_pytest_failure.py", "def test_fail():\n    assert False, 'tracked pytest failed'\n")
        self.run_git("add", "hermes/test_pytest_failure.py")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked pytest failed", result.stdout, result.stderr)

    @unittest.skipUnless(HAS_PYTEST, "pytest is required for real collection checks")
    def test_tracked_pytest_suffix_failure_still_fails(self):
        self.write("hermes/example_test.py", "def test_fail():\n    assert False, 'tracked suffix test failed'\n")
        self.run_git("add", "hermes/example_test.py")
        result = self.check()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tracked suffix test failed", result.stdout, result.stderr)

    @unittest.skipUnless(HAS_PYTEST, "pytest is required for real interpreter checks")
    def test_pytest_module_runs_without_a_path_entrypoint(self):
        result = self.check(strict=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running pytest suite", result.stdout)

    def test_foreign_pytest_entrypoint_does_not_bypass_missing_module(self):
        environment = self.root / "without-pytest"
        venv.EnvBuilder(with_pip=False).create(environment)
        self.python(environment / "bin/python")
        self.executable("pytest", "#!/bin/sh\nexit 0\n")
        for strict in (False, True):
            with self.subTest(strict=strict):
                result = self.check(strict=strict)
                self.assertEqual(result.returncode, 1 if strict else 0, result.stdout + result.stderr)
                self.assertIn("required Python module is missing: pytest" if strict else
                              "skipping pytest suite: pytest is not installed for", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
