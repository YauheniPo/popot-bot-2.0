"""Tests for the local Hermes Edge TTS transient-retry patch."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import stat
import tempfile
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
    def test_patch_preserves_mode_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "tts_tool.py"
            target.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
            target.chmod(0o640)
            result = apply_edge_tts_retry.patch_target(target)
            self.assertEqual(result, "installed Edge TTS transient retry")
            content = target.read_text(encoding="utf-8")
            self.assertEqual(content, apply_edge_tts_retry.NEW)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertIn("already installed", apply_edge_tts_retry.patch_target(target))
            self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_patch_does_not_change_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "tts_tool.py"
            target.write_text("# different upstream implementation\n", encoding="utf-8")
            before = target.read_bytes()
            self.assertIn("upstream implementation changed", apply_edge_tts_retry.patch_target(target))
            self.assertEqual(target.read_bytes(), before)

    def test_patch_rejects_symlinks_and_hardlinks(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                victim = Path(temp) / "outside.py"
                victim.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
                target = Path(temp) / "tts_tool.py"
                if kind == "symlink":
                    target.symlink_to(victim)
                else:
                    os.link(victim, target)
                with self.assertRaises((OSError, ValueError)):
                    apply_edge_tts_retry.patch_target(target)
                self.assertEqual(victim.read_text(encoding="utf-8"), apply_edge_tts_retry.OLD)

    def test_symlink_swapped_after_validation_cannot_redirect_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "tts_tool.py"
            target.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
            victim = root / "unrelated.py"
            victim.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
            original_open = os.open

            def swap_before_open(path, flags):
                target.unlink()
                target.symlink_to(victim)
                return original_open(path, flags)

            with mock.patch.dict(os.environ, {"HERMES_HOME": temp, "HOME": temp}), \
                    mock.patch("sys.argv", ["apply-edge-tts-retry.py", str(target)]), \
                    mock.patch.object(os, "open", side_effect=swap_before_open), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(apply_edge_tts_retry.main(), 2)
            self.assertEqual(victim.read_text(encoding="utf-8"), apply_edge_tts_retry.OLD)

    def test_default_path_symlink_does_not_whitelist_an_external_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            victim = root / "outside.py"
            victim.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
            default = root / "tts_tool.py"
            default.symlink_to(victim)
            with mock.patch.object(apply_edge_tts_retry, "DEFAULT_TARGET", str(default)), \
                    mock.patch.dict(os.environ, {"HOME": str(root / "home"), "HERMES_HOME": str(root / "home/.hermes")}), \
                    mock.patch("sys.argv", ["apply-edge-tts-retry.py", str(default)]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(apply_edge_tts_retry.main(), 2)
            self.assertEqual(victim.read_text(encoding="utf-8"), apply_edge_tts_retry.OLD)

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
