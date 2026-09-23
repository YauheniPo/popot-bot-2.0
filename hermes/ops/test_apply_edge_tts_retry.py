"""Tests for the local Hermes Edge TTS transient-retry patch."""

from __future__ import annotations

import importlib.util
import ast
import asyncio
import contextlib
import io
import os
import runpy
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
    def test_split_provider_import_is_wrapped_and_preserves_other_providers(self):
        source = '''from tools.tts_tool_providers import (
    _generate_edge_tts, _generate_elevenlabs, _generate_gemini_tts, _generate_minimax_tts,
    _generate_mistral_tts, _generate_xai_tts, _resolve_minimax_tts_runtime)
'''
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "tts_tool.py"
            target.write_text(source)
            self.assertEqual(apply_edge_tts_retry.patch_target(target), "installed Edge TTS transient retry")
            patched = target.read_text()
            self.assertIn("_generate_edge_tts as _upstream_generate_edge_tts", patched)
            self.assertIn("_generate_mistral_tts, _generate_xai_tts, _resolve_minimax_tts_runtime", patched)
            self.assertIn("already installed", apply_edge_tts_retry.patch_target(target))
            self.assertEqual(target.read_text(), patched)
            function = next(node for node in ast.parse(patched).body if isinstance(node, ast.AsyncFunctionDef))
            namespace = {"Path": Path, "logger": mock.Mock(), "asyncio": asyncio}
            exec(compile(ast.Module(body=[function], type_ignores=[]), "patched", "exec"), namespace)
            no_audio = type("NoAudioReceived", (Exception,), {})
            for errors, count in (([no_audio(), "audio"], 2), ([no_audio()] * 3, 3),
                                  ([ValueError()], 1), ([asyncio.CancelledError()], 1)):
                provider = mock.AsyncMock(side_effect=errors)
                namespace["_upstream_generate_edge_tts"] = provider
                with self.subTest(count=count), mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock) as sleep:
                    call = namespace["_generate_edge_tts"]("text", str(Path(temp) / "speech.mp3"), {"edge": {}})
                    if isinstance(errors[-1], BaseException):
                        expected_error = type(errors[-1])
                        with self.assertRaises(expected_error):
                            asyncio.run(call)
                    else:
                        self.assertEqual(asyncio.run(call), "audio")
                    self.assertEqual(provider.await_count, count)
                    self.assertEqual(sleep.await_count, count - 1)

    def test_main_reports_success_already_patched_and_changed_upstream(self) -> None:
        for source, message in (
            (apply_edge_tts_retry.OLD, "installed Edge TTS transient retry"),
            (apply_edge_tts_retry.NEW, "already installed"),
            ("# changed upstream\n", "upstream implementation changed"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                target = root / "tts_tool.py"
                target.write_text(source, encoding="utf-8")
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"HERMES_HOME": str(root), "HOME": str(root)}), \
                        mock.patch("sys.argv", ["apply-edge-tts-retry.py", str(target)]), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(apply_edge_tts_retry.main(), 0)
                self.assertIn(message, output.getvalue())

    def test_script_rejects_extra_arguments(self) -> None:
        script = str(MODULE_PATH)
        with mock.patch("sys.argv", [script, "first", "second"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(script, run_name="__main__")
        self.assertEqual(raised.exception.code, 2)

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

    def test_main_rejects_symlink_even_when_destination_is_trusted(self):
        for source, dangling in ((source, dangling) for source in ("cli", "env", "default")
                                 for dangling in (False, True)):
            with self.subTest(source=source, dangling=dangling), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                destination = root / "tts_tool.py"
                if not dangling:
                    destination.write_text(apply_edge_tts_retry.OLD, encoding="utf-8")
                alias = root / "alias.py"
                alias.symlink_to(destination)
                argv = ["apply-edge-tts-retry.py"] + ([str(alias)] if source == "cli" else [])
                environment = {"HERMES_HOME": str(root), "HOME": str(root)}
                if source == "env":
                    environment["HERMES_TTS_TOOL_PATH"] = str(alias)
                with mock.patch.dict(os.environ, environment, clear=True), \
                        mock.patch.object(apply_edge_tts_retry, "DEFAULT_TARGET", str(alias)), \
                        mock.patch("sys.argv", argv), \
                        contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(apply_edge_tts_retry.main(), 2)
                if not dangling:
                    self.assertEqual(destination.read_text(encoding="utf-8"), apply_edge_tts_retry.OLD)
                else:
                    self.assertFalse(destination.exists())

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
