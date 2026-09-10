"""Filesystem safety checks shared by the standalone config writers."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_module(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "path", [str(path.parent), *sys.path]):
        spec.loader.exec_module(module)
    return module


WRITERS = (
    (load_module("configure-plugin"), ".config.yaml.ops.tmp"),
    (load_module("configure-superpowers"), ".config.yaml.sp.tmp"),
)


class ConfigWriteSecurityTests(unittest.TestCase):
    def test_load_accepts_missing_empty_and_mapping_configs(self):
        module = WRITERS[0][0]
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yaml"
            self.assertEqual(module.load_config(config), {})
            for content, expected in (("", {}), ("null\n", {}), ("custom: preserved\n", {"custom": "preserved"})):
                with self.subTest(content=content):
                    config.write_text(content, encoding="utf-8")
                    self.assertEqual(module.load_config(config), expected)

    def test_main_rejects_non_mapping_yaml_without_overwriting_it(self):
        for module, _ in WRITERS:
            for content in ("[]\n", "false\n", "0\n", "''\n", "- item\n", "broken: [\n"):
                with self.subTest(writer=module.__name__, content=content), \
                        tempfile.TemporaryDirectory() as temp:
                    config = Path(temp) / "config.yaml"
                    config.write_text(content, encoding="utf-8")
                    argv = [module.__name__, "--config", str(config)]
                    if module.__name__ == "configure-plugin":
                        argv.extend(["--hermes-home", temp])
                    with mock.patch("sys.argv", argv), \
                            mock.patch.dict(os.environ, {"HERMES_HOME": temp}), \
                            contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(module.main(), 1)
                    self.assertEqual(config.read_text(encoding="utf-8"), content)

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
            for operation, owner, method in (
                ("create", tempfile, "NamedTemporaryFile"),
                ("serialize", module.yaml, "safe_dump"),
                ("replace", os, "replace"),
            ):
                with self.subTest(writer=module.__name__, operation=operation), \
                        tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    target = root / "config.yaml"
                    target.write_text("custom: original\n", encoding="utf-8")
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

    def test_copied_bundle_runs_from_an_unrelated_directory_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle" / "ops"
            bundle.mkdir(parents=True)
            source = Path(__file__).parent
            # Ansible ships the whole ops directory. Copy only the configurators
            # and their dependency here to detect accidental repository imports.
            for name in ("configure-plugin.py", "configure-superpowers.py", "hermes_config_io.py"):
                shutil.copy2(source / name, bundle / name)
            hermes_home = root / "hermes-home"
            hermes_home.mkdir()
            config = hermes_home / "config.yaml"
            config.write_text("custom: preserved\n", encoding="utf-8")
            for module, _ in WRITERS:
                with self.subTest(writer=module.__name__):
                    argv = [sys.executable, "-E", "-B", str(bundle / f"{module.__name__}.py"),
                            "--config", str(config)]
                    if module.__name__ == "configure-plugin":
                        argv.extend(["--hermes-home", str(hermes_home)])
                    environment = {**os.environ, "HERMES_HOME": str(hermes_home)}
                    first = subprocess.run(argv, cwd=root, env=environment, capture_output=True,
                                           text=True, check=True, timeout=15)
                    self.assertEqual(first.stdout.strip(), "changed")
                    content = config.read_bytes()
                    mtime = config.stat().st_mtime_ns
                    second = subprocess.run(argv, cwd=root, env=environment, capture_output=True,
                                            text=True, check=True, timeout=15)
                    self.assertEqual(second.stdout.strip(), "unchanged")
                    self.assertEqual(config.read_bytes(), content)
                    self.assertEqual(config.stat().st_mtime_ns, mtime)
                    self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            data = WRITERS[0][0].load_config(config)
            self.assertEqual(data["custom"], "preserved")
            self.assertEqual(set(data["plugins"]["enabled"]), {"ops-observability", "superpowers"})
            self.assertEqual(set(hermes_home.iterdir()), {config})


if __name__ == "__main__":
    unittest.main()
