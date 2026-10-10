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
import json
import os
import logging
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
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
            tree = ast.parse(self.patched_source(source))
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
            profile_names = ("builder", "cache", "models", "runtimes", "node", "browser_profiles",
                             "images", "audio", "videos", "documents", "screenshots", "citations")
            for prefix in ("", "skills/custom/", *(f"profiles/{name}/" for name in profile_names)):
                for relative in ("config.yaml", "SOUL.md", "gateway.lock", "personal.lock", "hermes-agent/SKILL.md", "node/bin/node",
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
            # Reproduce shutdown cleanup between inventory and archive writes.
            for lock in home.rglob("gateway.lock"):
                lock.unlink()
            with zipfile.ZipFile(archive_path, "w") as archive:
                for path, relative in native_files:
                    archive.write(path, relative.as_posix())
            snapshot = verifier.create_snapshot(home)
            self.assertEqual(set(snapshot["files"]), {str(relative) for _, relative in native_files})
            verifier.verify_backup(archive_path, snapshot)
            self.assertIn("personal.lock", snapshot["files"])
            # Only the known runtime lock is disposable, not arbitrary files.
            (home / "personal.lock").unlink()
            missing_file = home / "personal.lock"
            with zipfile.ZipFile(home.parent / "incomplete.zip", "w") as archive:
                with self.assertRaises(FileNotFoundError):
                    archive.write(missing_file, "personal.lock")

    def patched_source(self, path):
        source = (Path(UPSTREAM) / path).read_text()
        for target, marker, old, new in patches._PATCHES:
            if target == path:
                self.assertIn(old, source, marker)
                source = source.replace(old, new, 1)
        return source

    def tool_guardrails(self, **overrides):
        # Execute the pinned controller and result classifier. Only the unrelated
        # utils import is isolated; all inputs here are valid JSON.
        classification = ModuleType("agent.tool_result_classification")
        exec((Path(UPSTREAM) / "agent/tool_result_classification.py").read_text(),
             classification.__dict__)
        guardrails = ModuleType("hermes_guardrails_fixture")
        with mock.patch.dict(sys.modules, {
            "agent.tool_result_classification": classification,
            "utils": SimpleNamespace(safe_json_loads=json.loads),
            guardrails.__name__: guardrails,
        }):
            exec(self.patched_source("agent/tool_guardrails.py"), guardrails.__dict__)
        config = guardrails.ToolCallGuardrailConfig(**{
            "hard_stop_enabled": True, "exact_failure_warn_after": 1,
            "exact_failure_block_after": 4, "same_tool_failure_halt_after": 6,
            **overrides,
        })
        return guardrails, guardrails.ToolCallGuardrailController(config)

    def test_patch_failure_guidance_requires_fresh_read_and_current_revision(self):
        guardrails, controller = self.tool_guardrails()
        result = json.dumps({"error": "Could not find a match for old_string"})
        decision = controller.after_call("patch", {"path": "test.py"}, result)
        self.assertEqual(decision.action, "warn")
        self.assertFalse(decision.should_halt)
        for instruction in ("read_file", "already", "HEAD", "same", "diff"):
            self.assertIn(instruction, decision.message)
        self.assertIn(decision.message, guardrails.append_toolguard_guidance(result, decision))

    def test_write_refusal_guidance_preserves_read_before_write(self):
        _guardrails, controller = self.tool_guardrails()
        decision = controller.after_call("write_file", {"path": "test.py"},
                                         json.dumps({"error": "Refusing to overwrite unread file"}))
        self.assertEqual(decision.action, "warn")
        self.assertIn("read_file", decision.message)
        self.assertIn("overwrite", decision.message)
        self.assertIn("bypass", decision.message)

    def test_mutation_guidance_does_not_echo_replacement_contents(self):
        _guardrails, controller = self.tool_guardrails()
        args = {"path": "test.py", "old_string": "SYNTHETIC_SECRET_OLD",
                "new_string": "SYNTHETIC_SECRET_NEW"}
        decision = controller.after_call("patch", args, json.dumps({"error": "no match"}))
        self.assertNotIn(args["old_string"], decision.message)
        self.assertNotIn(args["new_string"], decision.message)

    def test_exact_failure_limit_survives_readonly_rechecks(self):
        for limit in (2, 4, 5):
            with self.subTest(limit=limit):
                _guardrails, controller = self.tool_guardrails(exact_failure_block_after=limit)
                args = {"path": "test.py", "old_string": "old", "new_string": "new"}
                for _ in range(limit):
                    self.assertEqual(controller.before_call("patch", args).action, "allow")
                    controller.after_call("patch", args, json.dumps({"error": "no match"}))
                controller.after_call("read_file", {"path": "test.py"}, json.dumps({"content": "new"}))
                decision = controller.before_call("patch", args)
                self.assertEqual(decision.code, "repeated_exact_failure_block")
                self.assertTrue(decision.should_halt)

    def test_corrected_payload_and_landed_patch_allow_progress(self):
        _guardrails, controller = self.tool_guardrails()
        args = {"path": "test.py", "old_string": "stale", "new_string": "fixed"}
        controller.after_call("patch", args, json.dumps({"error": "no match"}))
        corrected = {**args, "old_string": "current"}
        self.assertEqual(controller.before_call("patch", corrected).action, "allow")
        decision = controller.after_call("patch", corrected,
                                         json.dumps({"success": True, "diff": "landed"}))
        self.assertEqual(decision.action, "allow")
        self.assertEqual(controller.before_call("patch", args).action, "allow")

    def test_same_tool_failure_halt_remains_enabled(self):
        _guardrails, controller = self.tool_guardrails()
        for attempt in range(controller.config.same_tool_failure_halt_after):
            decision = controller.after_call("patch", {"old_string": str(attempt)},
                                             json.dumps({"error": "no match"}))
        self.assertEqual(decision.code, "same_tool_failure_halt")
        self.assertTrue(decision.should_halt)

    def test_disabled_warnings_do_not_force_recovery_guidance(self):
        _guardrails, controller = self.tool_guardrails(warnings_enabled=False)
        decision = controller.after_call("patch", {}, json.dumps({"error": "no match"}))
        self.assertEqual(decision.action, "allow")

    def test_landed_patch_with_diagnostics_is_not_reported_as_failed(self):
        _guardrails, controller = self.tool_guardrails()
        result = json.dumps({"success": True, "diff": "landed",
                             "lsp_diagnostics": "ERROR: synthetic diagnostic"})
        decision = controller.after_call("patch", {"path": "test.py"}, result)
        self.assertEqual(decision.action, "allow")
        self.assertFalse(controller._exact_failure_counts)

    def test_other_tools_keep_native_exact_failure_warning(self):
        _guardrails, controller = self.tool_guardrails()
        decision = controller.after_call("web_extract", {}, json.dumps({"error": "timeout"}))
        self.assertEqual(decision.code, "repeated_exact_failure_warning")
        self.assertNotIn("read_file", decision.message)

    def test_attached_cron_skills_use_native_selection_and_global_denies(self):
        # Execute the pinned scheduler and model_tools selectors rather than a
        # second implementation of their allow/deny logic. Avoid importing the
        # unrelated providers, databases, and gateway dependency graph.
        toolsets = {}
        exec(self.patched_source("toolsets.py"), toolsets)
        namespace = {"os": os, "_LEGACY_TOOLSET_MAP": {},
                     "_is_delegated_child_context": lambda: False,
                     "_is_dispatcher_owned_worker": lambda: False}
        names = {"_with_cron_skill_tools", "_resolve_cron_enabled_toolsets",
                 "_resolve_cron_disabled_toolsets", "_merge_mcp_into_per_job_toolsets",
                 "_apply_toolset_selection", "_select_tool_names"}
        for path in ("cron/scheduler.py", "model_tools.py"):
            tree = ast.parse(self.patched_source(path))
            selected = [node for node in tree.body if getattr(node, "name", "") in names]
            exec("from __future__ import annotations\n" +
                 ast.unparse(ast.Module(body=selected, type_ignores=[])), namespace)
        static_tools = SimpleNamespace(
            get_toolset=lambda name: toolsets["TOOLSETS"].get(name),
            resolve_toolset=lambda name: toolsets["resolve_toolset"](name, include_registry=False),
            validate_toolset=lambda name: name in toolsets["TOOLSETS"],
            bundle_non_core_tools=toolsets["bundle_non_core_tools"])
        namespace.update(resolve_toolset=static_tools.resolve_toolset,
                         validate_toolset=static_tools.validate_toolset)
        modules = {"toolsets": static_tools,
                   "hermes_cli.tools_config": SimpleNamespace(
                       enabled_mcp_server_names=lambda cfg: set(),
                       _get_platform_tools=lambda cfg, platform: cfg["platform_toolsets"][platform]),
                   "agent.skill_utils": SimpleNamespace(parse_config_string_list=lambda value: value or [])}
        cfg = {"platform_toolsets": {"cron": ["web", "file"]}}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {}, clear=True):
            for enabled in (["terminal", "web", "file"], None):
                job = {"skills": ["competitor-news-monitor"]}
                if enabled is not None:
                    job["enabled_toolsets"] = enabled
                with self.subTest(enabled=enabled):
                    selected = namespace["_resolve_cron_enabled_toolsets"](job, cfg)
                    disabled = namespace["_resolve_cron_disabled_toolsets"](cfg)
                    effective = namespace["_select_tool_names"](selected, disabled, True)
                    self.assertLessEqual({"skill_view", "skills_list", "web_search", "web_extract"}, effective)
                    self.assertNotIn("skill_manage", effective)
                    denied_cfg = {**cfg, "agent": {"disabled_toolsets": ["skills", "terminal"]}}
                    denied = namespace["_resolve_cron_disabled_toolsets"](denied_cfg)
                    effective = namespace["_select_tool_names"](selected, denied, True)
                    self.assertNotIn("skill_view", effective)
                    self.assertNotIn("skills_list", effective)
                    self.assertNotIn("terminal", effective)
                    self.assertIn("web_search", effective)

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
                target.write_bytes(subprocess.check_output(
                    ['git', '-C', str(UPSTREAM), 'show', f'HEAD:{path}']))
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
