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


if __name__ == "__main__":
    unittest.main()
