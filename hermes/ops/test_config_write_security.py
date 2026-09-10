"""Filesystem safety checks shared by the standalone config writers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WRITERS = (
    (load_module("configure-plugin"), ".config.yaml.ops.tmp"),
    (load_module("configure-superpowers"), ".config.yaml.sp.tmp"),
)


class ConfigWriteSecurityTests(unittest.TestCase):
    def test_preexisting_temp_symlink_is_not_followed(self):
        for module, old_name in WRITERS:
            with self.subTest(writer=module.__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "config.yaml"
                victim = root / "unrelated.yaml"
                victim.write_text("keep me", encoding="utf-8")
                old_temp = root / old_name
                old_temp.symlink_to(victim)

                module.write_config(target, {"custom": "preserved"})

                self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
                self.assertTrue(old_temp.is_symlink())
                self.assertFalse(target.is_symlink())
                self.assertEqual(module.load_config(target), {"custom": "preserved"})
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(set(root.iterdir()), {target, victim, old_temp})

    def test_temp_is_private_before_serialization(self):
        for module, _ in WRITERS:
            with self.subTest(writer=module.__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "config.yaml"
                original_dump = module.yaml.safe_dump

                def inspect_dump(*args, **kwargs):
                    files = list(root.iterdir())
                    self.assertEqual(len(files), 1)
                    self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
                    return original_dump(*args, **kwargs)

                previous_umask = os.umask(0)
                try:
                    with mock.patch.object(module.yaml, "safe_dump", side_effect=inspect_dump):
                        module.write_config(target, {"custom": "value"})
                finally:
                    os.umask(previous_umask)

    def test_failed_write_preserves_config_and_cleans_temp(self):
        for module, _ in WRITERS:
            for operation in ("serialize", "replace"):
                with self.subTest(writer=module.__name__, operation=operation), \
                        tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    target = root / "config.yaml"
                    target.write_text("custom: original\n", encoding="utf-8")
                    owner, method = (module.yaml, "safe_dump") if operation == "serialize" else (module.os, "replace")
                    with mock.patch.object(owner, method, side_effect=OSError("injected failure")), \
                            self.assertRaisesRegex(OSError, "injected failure"):
                        module.write_config(target, {"custom": "new"})
                    self.assertEqual(target.read_text(encoding="utf-8"), "custom: original\n")
                    self.assertEqual(set(root.iterdir()), {target})

    def test_main_reads_and_writes_the_validated_canonical_path(self):
        for module, _ in WRITERS:
            with self.subTest(writer=module.__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "config.yaml"
                alias = root / "alias.yaml"
                alias.symlink_to(target)
                argv = [module.__name__, "--config", str(alias)]
                if module.__name__ == "configure-plugin":
                    argv.extend(["--hermes-home", str(root)])
                with mock.patch("sys.argv", argv), \
                        mock.patch.dict(os.environ, {"HERMES_HOME": str(root)}), \
                        mock.patch.object(module, "load_config", return_value={}) as read, \
                        mock.patch.object(module, "write_config") as write, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(), 0)
                read.assert_called_once_with(target.resolve())
                write.assert_called_once_with(target.resolve(), read.return_value)


if __name__ == "__main__":
    unittest.main()
