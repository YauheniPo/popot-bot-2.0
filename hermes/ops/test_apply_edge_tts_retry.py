"""Tests for the local Hermes Edge TTS transient-retry patch."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("apply-edge-tts-retry.py")
SPEC = importlib.util.spec_from_file_location("apply_edge_tts_retry", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
apply_edge_tts_retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_edge_tts_retry)


class ApplyEdgeTtsRetryTests(unittest.TestCase):
    def test_main_refuses_a_target_not_named_tts_tool_py(self) -> None:
        with mock.patch("sys.argv", ["apply-edge-tts-retry.py", "/tmp/not-tts-tool.py"]):
            exit_code = apply_edge_tts_retry.main()

        self.assertEqual(exit_code, 2)

    def test_main_refuses_a_tts_tool_py_outside_every_trusted_root(self) -> None:
        with mock.patch("sys.argv", ["apply-edge-tts-retry.py", "/tmp/tts_tool.py"]), \
                mock.patch.dict("os.environ", {"HERMES_HOME": "/home/hermes/.hermes", "HOME": "/home/hermes"}):
            exit_code = apply_edge_tts_retry.main()

        self.assertEqual(exit_code, 2)

    def test_main_accepts_a_tts_tool_py_discovered_under_hermes_home(self) -> None:
        # Mirrors hermes/ansible/tasks/services.yml: it "find"s tts_tool.py
        # under HERMES_HOME/.local and hermes_user_home/.local, then passes
        # the discovered path as argv[1] with HERMES_HOME/HOME set.
        argv = ["apply-edge-tts-retry.py", "/home/hermes/.hermes/some/nested/tts_tool.py"]
        with mock.patch("sys.argv", argv), \
                mock.patch.dict("os.environ", {"HERMES_HOME": "/home/hermes/.hermes", "HOME": "/home/hermes"}):
            exit_code = apply_edge_tts_retry.main()

        # 0 ("skipped: missing") not 2 ("refused") -- the guard let it through.
        self.assertEqual(exit_code, 0)

    def test_main_accepts_a_tts_tool_py_discovered_under_user_local(self) -> None:
        argv = ["apply-edge-tts-retry.py", "/home/hermes/.local/lib/some-pkg/tts_tool.py"]
        with mock.patch("sys.argv", argv), \
                mock.patch.dict("os.environ", {"HERMES_HOME": "/home/hermes/.hermes", "HOME": "/home/hermes"}):
            exit_code = apply_edge_tts_retry.main()

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
