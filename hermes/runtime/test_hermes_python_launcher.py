"""In-process coverage for the managed Python launcher's argument and exec modes."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent


def load_launcher():
    spec = importlib.util.spec_from_file_location("hermes_python_launcher", ROOT / "hermes-python.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class KeepOpen(io.StringIO):
    """Stay readable after helpers close their (redirected) stdout."""

    def close(self):
        pass


def managed_launchers(store_python: str | None) -> str:
    """Return a hermes_cli._launchers module source for the requested store state."""
    resolved = "None" if store_python is None else f"Path({store_python!r})"
    return (
        "import sys\n"
        "from pathlib import Path\n"
        "def resolve_store_python(root):\n"
        f"    return {resolved}\n"
        "def runtime_command(root, args, *, code, python, home):\n"
        "    return [str(python), '-c', code, *args]\n"
    )


class HermesPythonInProcessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "hermes-agent"
        self.module = load_launcher()
        self.saved_argv = list(sys.argv)
        self.addCleanup(self.restore_process_state)

    def restore_process_state(self):
        sys.argv = self.saved_argv
        for name in ("hermes_cli", "hermes_cli._launchers"):
            sys.modules.pop(name, None)

    def capture(self, function, *args, stdin=""):
        output = KeepOpen()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(output):
            function(*args)
        return output.getvalue()

    def managed_repo(self, store_python: str | None) -> None:
        (self.repo / "pm").mkdir(parents=True, exist_ok=True)
        (self.repo / "pm/environments.py").write_text("")
        (self.repo / "hermes_cli").mkdir(exist_ok=True)
        (self.repo / "hermes_cli/__init__.py").write_text("")
        (self.repo / "hermes_cli/_launchers.py").write_text(managed_launchers(store_python))

    def test_argument_modes_run_code_modules_stdin_version_and_scripts(self):
        run_python = self.module.run_python

        self.assertEqual(
            self.capture(run_python, ["-c", "import sys; print(sys.argv[1])", "keep spaces"]), "keep spaces\n"
        )
        self.assertEqual(self.capture(run_python, ["-c", "print('inline code')"]), "inline code\n")
        self.assertTrue(self.capture(run_python, ["-V"]).startswith("Python "))
        self.assertTrue(self.capture(run_python, ["--version"]).startswith("Python "))
        self.assertEqual(self.capture(run_python, ["-"], stdin="print('stdin mode')"), "stdin mode\n")
        self.assertEqual(json.loads(self.capture(run_python, ["-m", "json.tool"], stdin='{"b": 1, "a": 2}')),
                         {"a": 2, "b": 1})

        script = self.root / "script with spaces.py"
        script.write_text("import sys\nprint(sys.argv[1])\n")
        self.assertEqual(self.capture(run_python, [str(script), "a b"]), "a b\n")

    def test_usage_errors_are_reported(self):
        with self.assertRaisesRegex(SystemExit, "Usage: python"):
            self.module.run_python([])
        with self.assertRaisesRegex(SystemExit, "-c requires an argument"):
            self.module.run_python(["-c"])
        with self.assertRaisesRegex(SystemExit, "-m requires an argument"):
            self.module.run_python(["-m"])

    def test_entry_point_reports_a_missing_runtime(self):
        home = self.root / "no-runtime-here"
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"HERMES_INSTALL_DIR": str(home)}), \
                contextlib.redirect_stderr(errors), \
                self.assertRaises(SystemExit) as raised:
            runpy.run_path(str(ROOT / "hermes-python.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("ERROR: Hermes runtime is missing", errors.getvalue())

    def test_pre_pm_legacy_interpreter_is_executed_directly(self):
        legacy = self.repo / "venv/bin/python"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("")
        executor = mock.Mock(side_effect=SystemExit(0))
        with mock.patch.dict(os.environ, {"HERMES_INSTALL_DIR": str(self.repo)}), \
                mock.patch.object(os, "execv", executor), \
                self.assertRaises(SystemExit):
            self.module.main()
        executor.assert_called_once_with(str(legacy), [str(legacy), *self.saved_argv[1:]])

    def test_managed_runtime_without_store_interpreter_fails_closed(self):
        self.managed_repo(None)
        with mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.dict(os.environ, {"HERMES_INSTALL_DIR": str(self.repo)}), \
                self.assertRaisesRegex(RuntimeError, "Hermes PM runtime is missing"):
            self.module.main()

    def test_managed_runtime_executes_the_store_interpreter(self):
        self.managed_repo("/managed/store/python")
        executor = mock.Mock(side_effect=SystemExit(0))
        with mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.dict(os.environ, {"HERMES_INSTALL_DIR": str(self.repo)}), \
                mock.patch.object(os, "execv", executor), \
                self.assertRaises(SystemExit):
            self.module.main()
        command = executor.call_args[0]
        self.assertEqual(command[0], "/managed/store/python")
        self.assertEqual(command[1][:2], ["/managed/store/python", "-c"])
        self.assertIn("runpy.run_path(", command[1][2])
        self.assertIn("['run_python'](sys.argv[1:])", command[1][2])
        self.assertEqual(command[1][3:], self.saved_argv[1:])


if __name__ == "__main__":
    unittest.main()
