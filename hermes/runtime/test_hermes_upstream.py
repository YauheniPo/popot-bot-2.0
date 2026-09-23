"""Opt-in compatibility checks against an unmodified, pinned Hermes checkout.

Set HERMES_UPSTREAM_DIR to a local upstream checkout. These tests do not
download/install dependencies, use credentials, or contact a deployed gateway.
"""

import ast
import asyncio
from collections.abc import Mapping
import contextlib
import hashlib
import importlib.util
import io
import os
import logging
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upstream_patches", ROOT / "runtime/apply-hermes-patches.py")
patches = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patches)
UPSTREAM = os.environ.get("HERMES_UPSTREAM_DIR")


@unittest.skipUnless(UPSTREAM, "HERMES_UPSTREAM_DIR is not configured")
class HermesUpstreamTests(unittest.TestCase):
    def test_native_full_backup_walker_agrees_with_update_verifier(self):
        # Execute the pinned source's real walker and its constants without
        # importing the unrelated CLI/provider dependency graph.
        names = {"_QUICK_SNAPSHOTS_DIR", "_EXCLUDED_DIRS", "_EXCLUDED_ROOT_DIRS",
                 "_EXCLUDED_BACKUP_ROOT_DIRS", "_KEPT_CACHE_SUBDIRS", "_in_excluded_root_dir",
                 "_SQLITE_SIDECAR_SUFFIXES", "_EXCLUDED_SUFFIXES", "_EXCLUDED_NAMES",
                 "_EXCLUDED_PREFIXES", "_is_non_regular_path", "_should_exclude", "_iter_backup_files",
                 "LOCAL_RUNTIME_ROOT_DIRS", "RETIRED_GENERATION_DIR_SUFFIX"}
        namespace = {"Path": Path, "os": os, "stat": stat, "suppress": contextlib.suppress}
        for source in ("hermes_constants.py", "hermes_state_dbfile.py", "hermes_cli/backup.py"):
            tree = ast.parse((Path(UPSTREAM) / source).read_text())
            selected = [node for node in tree.body if getattr(node, "name", "") in names
                        or isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id in names for target in node.targets)
                        or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                        and node.target.id in names]
            exec("from __future__ import annotations\n" + ast.unparse(ast.Module(body=selected, type_ignores=[])),
                 namespace)
        spec = importlib.util.spec_from_file_location("backup_verifier", ROOT / "runtime/verify-update-state.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve() / ".hermes"
            for prefix in ("", "profiles/builder/", "skills/custom/"):
                for relative in ("config.yaml", "SOUL.md", "hermes-agent/SKILL.md", "node/bin/node",
                                 "models/model", "runtimes/tool", "cache/catalog.json", "cache/delegation/task.log",
                                 "cache/images/photo", "cache/audio/message", "cache/videos/clip",
                                 "cache/documents/file", "cache/screenshots/screen", "cache/citations/evidence",
                                 "browser-profile/Login Data", "browser-profiles/default/Cookies",
                                 "browser_profiles/default/Cookies", "skills/.archive/custom/SKILL.md"):
                    path = home / (prefix + relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture")
            archive_path = home.parent / "backup.zip"
            native_files = list(namespace["_iter_backup_files"](home, archive_path))
            with zipfile.ZipFile(archive_path, "w") as archive:
                for path, relative in native_files:
                    archive.write(path, relative.as_posix())
            snapshot = verifier.create_snapshot(home)
            self.assertEqual(set(snapshot["files"]), {str(relative) for _, relative in native_files})
            verifier.verify_backup(archive_path, snapshot)

    def patched_source(self, path):
        source = (Path(UPSTREAM) / path).read_text()
        for target, marker, old, new in patches._PATCHES:
            if target == path:
                self.assertIn(old, source, marker)
                source = source.replace(old, new, 1)
        return source

    def test_split_gateway_dispatch_preserves_custom_commands(self):
        tree = ast.parse(self.patched_source("gateway/run_busy.py"))
        gateway = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "GatewayBusySessionMixin")
        names = {"_COMMAND_HANDLER_ALIASES", "_PLAIN_COMMANDS", "_IDLE_COMMANDS",
                 "_command_handler_table", "_gateway_plain_command_handlers", "_gateway_idle_command_handlers"}
        gateway.body = [node for node in gateway.body if getattr(node, "name", "") in names
                        or isinstance(node, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id in names for target in node.targets)]
        model_tree = ast.parse(self.patched_source("gateway/slash_commands_model.py"))
        gateway.body.append(next(node for node in ast.walk(model_tree)
                                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_model_global_command"))
        namespace = {}
        exec("from __future__ import annotations\n" + ast.unparse(gateway), namespace)
        runner = namespace["GatewayBusySessionMixin"]()
        for name in (*runner._PLAIN_COMMANDS, *runner._IDLE_COMMANDS):
            method = runner._COMMAND_HANDLER_ALIASES.get(name, f"_handle_{name.replace('-', '_')}_command")
            if method != "_handle_model_global_command":
                setattr(runner, method, mock.AsyncMock(return_value="handled"))
        plain = runner._gateway_plain_command_handlers()
        self.assertIs(plain["gw-restart"], runner._handle_restart_command)
        self.assertIs(plain["doctor"], runner._handle_doctor_command)
        self.assertNotIn("model_global", plain)
        handler = runner._gateway_idle_command_handlers()["model_global"]
        for args, expected in (("", "/model --global"), ("example --provider local", "/model example --provider local --global")):
            event = SimpleNamespace(text="/model_global", get_command_args=lambda: args)
            self.assertEqual(asyncio.run(handler(event)), "handled")
            self.assertEqual(event.text, expected)
            runner._handle_model_command.assert_awaited_with(event)

    def test_split_telegram_usage_preserves_priority_and_persistence(self):
        tree = ast.parse(self.patched_source("hermes_cli/commands_platforms.py"))
        functions = {"_telegram_usage_ranking_config", "_telegram_usage_state", "_write_telegram_usage_state",
                     "_telegram_command_usage_counts", "_telegram_command_usage_count", "record_telegram_command_usage",
                     "_prioritize_telegram_menu_candidates", "_sanitized_rank", "_sanitize_telegram_name",
                     "_telegram_command_menu_config"}
        constants = {"_TG_INVALID_CHARS", "_TG_MULTI_UNDERSCORE", "_CMD_NAME_LIMIT",
                     "_TELEGRAM_PRIORITY_TIERS", "_TELEGRAM_MENU_PRIORITY", "_telegram_usage_cache",
                     "_DEFAULT_TELEGRAM_MENU_MAX_COMMANDS", "_TELEGRAM_BOT_API_MAX_COMMANDS"}
        selected = [node for node in tree.body if getattr(node, "name", "") in functions
                    or isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in constants for target in node.targets)
                    or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in constants]
        config = {"platforms": {"telegram": {"extra": {"command_menu": {
            "usage_ranking": {"enabled": True, "refresh_every": 2},
            "priority": ["pinned"], "priority_mode": "prepend"}}}}}
        modules = {"hermes_cli": mock.Mock(), "hermes_cli.config": SimpleNamespace(read_raw_config=lambda: config)}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, modules):
            namespace = {"os": os, "re": __import__("re"), "Mapping": Mapping, "logger": logging.getLogger(__name__),
                         "_telegram_usage_state_path": lambda: str(Path(directory) / "usage.json")}
            code = ast.unparse(ast.Module(body=selected, type_ignores=[]))
            exec("from __future__ import annotations\n" + code, namespace)
            record = namespace["record_telegram_command_usage"]
            self.assertFalse(record("popular"))
            self.assertTrue(record("popular"))
            candidates = [(name, name, "core", name) for name in ("unranked", "popular", "help", "pinned")]
            ranked = namespace["_prioritize_telegram_menu_candidates"](candidates)
            self.assertEqual([item[0] for item in ranked], ["pinned", "help", "popular", "unranked"])
            self.assertEqual(namespace["_telegram_usage_state"](), ({"popular": 2}, 0))

    def test_pin_matches_source_and_installer(self):
        upstream = Path(UPSTREAM)
        settings = yaml.safe_load((ROOT / "config/vps-defaults.yml").read_text())
        pin = settings["vps_deploy"]["hermes_source"]
        commit = subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True,
        ).strip()
        self.assertEqual(commit, pin["commit"])
        project = tomllib.loads((upstream / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], pin["version"])
        self.assertEqual(
            hashlib.sha256((upstream / "scripts/install.sh").read_bytes()).hexdigest(),
            pin["installer_sha256"],
        )

    def test_all_gateway_patches_apply_compile_and_are_idempotent(self):
        paths = {path for path, _, _, _ in patches._PATCHES}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for path in paths:
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(UPSTREAM) / path, target)
            with mock.patch.object(patches, "HERMES_AGENT_DIR", destination):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = patches.main()
                self.assertEqual(result, 0, output.getvalue())
                first = {path: (destination / path).read_text() for path in paths}
                for path, source in first.items():
                    compile(source, path, "exec")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(patches.main(), 0)
                self.assertEqual(first, {path: (destination / path).read_text() for path in paths})


if __name__ == "__main__":
    unittest.main()
