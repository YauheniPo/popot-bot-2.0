"""Regression tests for local Hermes gateway patch migrations."""

from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("apply-hermes-patches.py")
SPEC = importlib.util.spec_from_file_location("apply_hermes_patches", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
apply_hermes_patches = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_hermes_patches)


class ApplyHermesPatchesTests(unittest.TestCase):
    def test_backup_only_cli_dispatch_and_repeat(self):
        script_path = str(MODULE_PATH)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hermes_cli/backup.py"
            target.parent.mkdir()
            target.write_text('_EXCLUDED_NAMES = {".backup.lock", "gateway.pid", "cron.pid"}\n')
            with mock.patch.dict(os.environ, {"HERMES_INSTALL_DIR": directory}), \
                    mock.patch.object(sys, "argv", [script_path, "--backup-only"]):
                for _ in range(2):
                    with self.assertRaises(SystemExit) as result:
                        runpy.run_path(script_path, run_name="__main__")
                    self.assertEqual(result.exception.code, 0)
                    self.assertEqual(target.read_text().count('"gateway.lock"'), 1)

    def test_backup_only_patches_without_gateway_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "hermes_cli/backup.py"
            target.parent.mkdir()
            target.write_text('_EXCLUDED_NAMES = {".backup.lock", "gateway.pid", "cron.pid"}\n')
            with mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", root), \
                    mock.patch.object(apply_hermes_patches, "_migrate_installed_model_global") as migrate:
                self.assertEqual(apply_hermes_patches.main(backup_only=True), 0)
                first = target.read_text()
                self.assertIn('"gateway.lock"', first)
                self.assertEqual(apply_hermes_patches.main(backup_only=True), 0)
                self.assertEqual(target.read_text(), first)
                migrate.assert_not_called()

    def test_backup_only_rejects_unknown_source_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "hermes_cli/backup.py"
            target.parent.mkdir()
            source = "# unknown backup implementation\n"
            target.write_text(source)
            with mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", root):
                self.assertEqual(apply_hermes_patches.main(backup_only=True), 1)
                self.assertEqual(target.read_text(), source)

    def test_registry_migration_makes_underscore_name_canonical(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        source = f'''    # Local Hermes: {retired} CommandDef
    CommandDef("{retired}", "Set the global default model for all topics/sessions", "Configuration",
               aliases=("model_global",),
               args_hint="[model] [--provider name]",
               busy_policy="reject", busy_handler="{retired}"),
'''

        migrated, changed = apply_hermes_patches._migrate_model_global_source(
            "hermes_cli/commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn('CommandDef("model_global"', migrated)
        self.assertIn('busy_handler="model"', migrated)
        self.assertNotIn('aliases=("model_global",)', migrated)
        self.assertNotIn(retired, migrated)

    def test_old_usage_handler_migrates_to_global_picker(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        source = f'''    async def _handle_model_global_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /{retired} — switch model persistently for ALL topics/sessions.
        """
        raw_args = event.get_command_args().strip()
        if not raw_args:
            return (
                "Usage: /{retired} <model> [--provider <provider>]\\n"
                "Sets the global default model in config.yaml — applies to every "
                "topic/session, not just this one."
            )
        # Local Hermes: {retired} handler
'''

        migrated, changed = apply_hermes_patches._migrate_model_global_source(
            "gateway/slash_commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn('event.text = "/model --global"', migrated)
        self.assertIn("# Local Hermes: model_global handler", migrated)
        self.assertNotIn("Usage:", migrated)
        self.assertNotIn(retired, migrated)

        second_pass, second_changed = apply_hermes_patches._migrate_model_global_source(
            "gateway/slash_commands.py", migrated
        )
        self.assertFalse(second_changed)
        self.assertEqual(second_pass, migrated)

    def test_route_migration_removes_retired_alias(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        source = f'''        if canonical in ("{retired}", "model_global"):
            # Local Hermes: {retired} route
            return await self._handle_model_global_command(event)
'''

        migrated, changed = apply_hermes_patches._migrate_model_global_source(
            "gateway/run.py", source
        )

        self.assertTrue(changed)
        self.assertIn('if canonical == "model_global":', migrated)
        self.assertNotIn(retired, migrated)

    def test_route_migration_ignores_unrelated_retired_text(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        source = f'''        # Upstream migration note: /{retired} was renamed.
        if canonical in ("{retired}", "model_global"):
            # Local Hermes: {retired} route
            return await self._handle_model_global_command(event)
'''

        migrated, changed = apply_hermes_patches._migrate_model_global_source(
            "gateway/run.py", source
        )

        self.assertTrue(changed)
        self.assertIn(f"/{retired} was renamed", migrated)
        self.assertNotIn(f"# Local Hermes: {retired} route", migrated)
        self.assertIn('if canonical == "model_global":', migrated)

    def test_gw_restart_registry_migration_makes_it_canonical(self) -> None:
        source = '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw-restart",)),
'''

        migrated, changed = apply_hermes_patches._migrate_gw_restart_source(
            "hermes_cli/commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn("# Local Hermes: gw-restart canonical", migrated)
        self.assertIn('CommandDef("gw-restart"', migrated)
        self.assertIn('aliases=("restart", "gw_restart")', migrated)

    def test_gw_restart_route_migration_makes_it_canonical(self) -> None:
        source = '''        if canonical in ("restart", "gw-restart"):
            # /gw-restart is the user-facing alias; /restart is kept for
            # backward compatibility with scripts that already use it.
            return await self._handle_restart_command(event)
'''

        migrated, changed = apply_hermes_patches._migrate_gw_restart_source(
            "gateway/run.py", source
        )

        self.assertTrue(changed)
        self.assertIn("# Local Hermes: gw-restart route", migrated)
        self.assertIn('if canonical in ("restart", "gw-restart"):', migrated)

    def test_gw_restart_migration_accepts_legacy_underscore_aliases(self) -> None:
        registry = '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw-restart", "gw_restart")),
'''
        route = '''        if canonical in ("restart", "gw-restart", "gw_restart"):
            # Earlier local route comment.
            return await self._handle_restart_command(event)
'''

        migrated_registry, registry_changed = apply_hermes_patches._migrate_gw_restart_source(
            "hermes_cli/commands.py", registry
        )
        migrated_route, route_changed = apply_hermes_patches._migrate_gw_restart_source(
            "gateway/run.py", route
        )

        self.assertTrue(registry_changed)
        self.assertTrue(route_changed)
        self.assertIn("# Local Hermes: gw-restart canonical", migrated_registry)
        self.assertIn("# Local Hermes: gw-restart route", migrated_route)

    def test_gw_restart_registry_migration_accepts_underscore_only_alias(self) -> None:
        source = '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw_restart",)),
'''

        migrated, changed = apply_hermes_patches._migrate_gw_restart_source(
            "hermes_cli/commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn('CommandDef("gw-restart"', migrated)

    def test_gw_restart_registry_migration_adds_missing_marker_to_canonical_form(self) -> None:
        source = '''    CommandDef("gw-restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("restart", "gw_restart")),
'''

        migrated, changed = apply_hermes_patches._migrate_gw_restart_source(
            "hermes_cli/commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn("# Local Hermes: gw-restart canonical", migrated)
        self.assertIn('aliases=("restart", "gw_restart")', migrated)

    def test_main_recovers_from_partially_migrated_legacy_patch_set(self) -> None:
        # Look patches up by marker: index-based lookups silently rot whenever
        # an upstream refactor adds, drops, or reorders a patch.
        replacements = {
            marker: new for _path, marker, _old, new in apply_hermes_patches._PATCHES
        }
        legacy_markers = [
            "# Local Hermes: model_global CommandDef",
            "# Local Hermes: gw-restart canonical",
            "# Local Hermes: model_global handler",
            "# Local Hermes: model_global route",
            "# Local Hermes: gw-restart route",
            "# Local Hermes: status reasoning",
        ]
        legacy_paths = dict.fromkeys(legacy_markers, "gateway/slash_commands.py")
        for marker in ("# Local Hermes: model_global CommandDef", "# Local Hermes: gw-restart canonical"):
            legacy_paths[marker] = "hermes_cli/commands.py"
        for marker in ("# Local Hermes: model_global route", "# Local Hermes: gw-restart route"):
            legacy_paths[marker] = "gateway/run.py"
        legacy_patches = [
            # This fixture represents the old monolithic layout, even when
            # the current release's registry targets split upstream modules.
            (legacy_paths[marker], marker, old, new)
            for _path, marker, old, new in apply_hermes_patches._PATCHES
            if marker in legacy_markers
        ]
        self.assertEqual(len(legacy_patches), len(legacy_markers))

        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            commands = install_dir / "hermes_cli" / "commands.py"
            slash_commands = install_dir / "gateway" / "slash_commands.py"
            run = install_dir / "gateway" / "run.py"
            commands.parent.mkdir(parents=True)
            slash_commands.parent.mkdir(parents=True)
            commands.write_text(
                replacements["# Local Hermes: model_global CommandDef"]
                + '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw-restart",)),
''',
                encoding="utf-8",
            )
            slash_commands.write_text(
                replacements["# Local Hermes: model_global handler"]
                + replacements["# Local Hermes: status reasoning"],
                encoding="utf-8",
            )
            run.write_text(
                replacements["# Local Hermes: model_global route"]
                + '''        if canonical in ("restart", "gw-restart"):
            # /gw-restart is the user-facing alias; /restart is kept for
            # backward compatibility with scripts that already use it.
            return await self._handle_restart_command(event)
''',
                encoding="utf-8",
            )

            with (
                mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", install_dir),
                mock.patch.object(apply_hermes_patches, "_PATCHES", legacy_patches),
            ):
                self.assertEqual(apply_hermes_patches.main(), 0)

            self.assertIn(
                "# Local Hermes: gw-restart canonical",
                commands.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "# Local Hermes: gw-restart route",
                run.read_text(encoding="utf-8"),
            )

    def test_new_patch_definitions_contain_only_underscore_command(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        patch_payload = "\n".join(
            part
            for _relative_path, marker, old, new in apply_hermes_patches._PATCHES
            for part in (marker, old, new)
        )

        self.assertIn("model_global", patch_payload)
        self.assertNotIn(retired, patch_payload)

    def test_doctor_patch_is_a_gateway_only_fixed_argument_diagnostic(self) -> None:
        patches = {
            marker: new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
        }

        self.assertIn(
            'CommandDef("doctor", "Run read-only Hermes diagnostics", "Info"',
            patches["# Local Hermes: doctor CommandDef"],
        )
        self.assertIn(
            "gateway_only=True", patches["# Local Hermes: doctor CommandDef"]
        )
        self.assertIn('"doctor",', patches["# Local Hermes: doctor handler"])
        self.assertIn(
            "asyncio.create_subprocess_exec", patches["# Local Hermes: doctor handler"]
        )
        self.assertIn(
            "*_resolve_hermes_bin()", patches["# Local Hermes: doctor handler"]
        )
        self.assertNotIn(
            "str(_resolve_hermes_bin())", patches["# Local Hermes: doctor handler"]
        )
        self.assertNotIn("shell=True", patches["# Local Hermes: doctor handler"])
        # The split upstream dispatcher derives handler names from this tuple.
        self.assertIn(
            '"doctor",',
            patches["# Local Hermes: doctor route"],
        )

    def test_doctor_handler_migration_expands_the_resolved_command_argv(self) -> None:
        handler = next(
            new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
            if marker == "# Local Hermes: doctor handler"
        ).replace("*_resolve_hermes_bin()", "str(_resolve_hermes_bin())")

        migrated, changed = apply_hermes_patches._migrate_doctor_handler_source(
            "gateway/slash_commands.py", handler
        )

        self.assertTrue(changed)
        self.assertIn("*_resolve_hermes_bin()", migrated)
        self.assertNotIn("str(_resolve_hermes_bin())", migrated)

    def test_telegram_usage_ranking_preserves_pins_and_refreshes_the_menu(self) -> None:
        patches = {
            marker: new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
        }

        ranking = patches["# Local Hermes: telegram usage ranking"]
        state = patches["# Local Hermes: telegram usage state"]
        refresh = patches["# Local Hermes: telegram usage refresh"]
        record = patches["# Local Hermes: telegram usage record"]

        # Usage ordering applies only to the last-resort tier: upstream's
        # configured-priority and default tiers stay above it, so a pinned
        # command can never be pushed below an unpinned one by usage counts.
        self.assertIn("return (tier, indexes[table], stable_index)", ranking)
        self.assertIn("-_telegram_command_usage_count(final_name)", ranking)
        self.assertIn("def _telegram_command_usage_count", state)
        self.assertIn("telegram-command-usage.json", state)
        self.assertIn("record_telegram_command_usage", state)
        self.assertIn("refresh_every", state)
        self.assertIn("set_my_commands", refresh)
        self.assertIn("_record_telegram_command_usage(event.text)", record)

    def test_status_topic_model_never_renders_the_override_credentials(self) -> None:
        status = next(
            new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
            if marker == "# Local Hermes: status reasoning"
        )

        # _session_model_overrides[key] is a provider config mapping holding
        # api_key/base_url. /status must render only the model name from it.
        self.assertIn('session_override.get("model")', status)
        self.assertIn("isinstance(session_override, dict)", status)
        self.assertNotIn(
            'session_model = (getattr(self, "_session_model_overrides"', status
        )

    def test_status_reports_live_subagents_for_the_current_chat(self) -> None:
        status = next(
            new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
            if marker == "# Local Hermes: status reasoning"
        )

        self.assertIn("list_async_delegations", status)
        self.assertIn('d.get("session_key")', status)
        self.assertIn('d.get("parent_session_id")', status)
        self.assertIn("**Subagents:**", status)

    def test_status_model_display_resolves_split_module_dependencies(self):
        status = next(new for _, marker, _, new in apply_hermes_patches._PATCHES
                      if marker == "# Local Hermes: status reasoning")
        block = status.split("        # Local Hermes: model info (global + topic)\n", 1)[1]
        override = {"model": "session-model", "api_key": "private-key"}
        runner = SimpleNamespace(_session_model_override=mock.Mock(return_value=override))
        config = {"model": {"default": "global-model"}}
        gateway_run = SimpleNamespace(_load_gateway_config=lambda: config,
                                      _resolve_gateway_model=lambda value: value["model"]["default"])
        namespace = {"self": runner, "session_key": "chat-a", "status_agent": None,
                     "_AGENT_PENDING_SENTINEL": object(), "_clean_str": str.strip, "lines": []}
        with mock.patch.dict(sys.modules, {"gateway": mock.Mock(), "gateway.run": gateway_run}):
            exec(textwrap.dedent(block), namespace)
        self.assertEqual(namespace["lines"], ["**Global model:** global-model", "**Topic model:** session-model *(override)*"])
        runner._session_model_override.assert_called_once_with("chat-a")
        self.assertNotIn("private-key", "\n".join(namespace["lines"]))

    def render_activity(self, processes=(), delegations=(), session_key="chat-a", **values):
        """Execute the actual injected block, using the pinned registry's API shape."""
        status = next(new for _, marker, _, new in apply_hermes_patches._PATCHES
                      if marker == "# Local Hermes: status reasoning")
        block = status.split("        # Local Hermes: status subagent activity\n", 1)[1].split(
            "        # Local Hermes: status reasoning\n", 1
        )[0]
        registry = mock.Mock()
        registry.list_sessions.side_effect = lambda *, session_key: [
            p for p in processes if p.get("session_key") == session_key
        ]
        registry.get.side_effect = lambda pid: next(
            (SimpleNamespace(**p) for p in processes if p["session_id"] == pid), None
        )
        ns = dict(lines=["Agent Running: No"], is_running=False, agent=None,
                  _AGENT_PENDING_SENTINEL=object(), session_key=session_key,
                  session_entry=SimpleNamespace(session_id="session-a"),
                  t=lambda key, **kw: f"{key}: {kw}")
        ns.update(values)
        def list_delegations():
            if isinstance(delegations, Exception):
                raise delegations
            return delegations

        modules = {
            "tools": mock.Mock(),
            "tools.process_registry": SimpleNamespace(process_registry=registry),
            "tools.async_delegation": SimpleNamespace(list_async_delegations=list_delegations),
        }
        with mock.patch.dict(sys.modules, modules):
            exec(textwrap.dedent(block), ns)
        return "\n".join(ns["lines"]), registry

    @staticmethod
    def process(pid="proc_123abc", **kwargs):
        return dict(session_id=pid, session_key="chat-a", parent_session_id="session-a",
                    status="running", exited=False, uptime_seconds=463,
                    notify_on_complete=True, pid=123, command="TOKEN=secret",
                    output_preview="private output", **kwargs)

    def test_status_background_work_is_distinct_from_foreground_agent(self):
        output, registry = self.render_activity([self.process()])
        self.assertIn("Agent Running: No", output)
        self.assertIn("**Work:** background active", output)
        self.assertIn("**Subagents:** 0 active", output)
        self.assertIn("**Background processes:** 1 running", output)
        self.assertIn("proc_123abc", output)
        self.assertIn("7m 43s", output)
        self.assertIn("agent notification on exit: enabled", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("private output", output)
        registry.list_sessions.assert_called_once_with(session_key="chat-a")
        registry.read_log.assert_not_called()
        registry.wait.assert_not_called()

    def test_status_excludes_foreign_finished_and_previous_session_processes(self):
        active = self.process()
        foreign = dict(self.process("proc_abcdef"), session_key="chat-b")
        previous = dict(self.process("proc_123def"), parent_session_id="old-session")
        finished = dict(self.process("proc_123aaa"), status="exited", exited=True)
        output, _ = self.render_activity([active, foreign, previous, finished])
        self.assertIn("**Background processes:** 1 running", output)
        for process in (foreign, previous, finished):
            self.assertNotIn(process["session_id"], output)

    def test_status_no_session_key_never_lists_all_processes(self):
        output, registry = self.render_activity([self.process()], session_key="")
        registry.list_sessions.assert_not_called()
        self.assertIn("**Background processes:** unavailable", output)
        self.assertNotIn("proc_123abc", output)

    def test_status_registry_error_reports_unknown_not_idle(self):
        class BrokenProcesses:
            def __iter__(self):
                raise RuntimeError("sensitive details")
        output, _ = self.render_activity(BrokenProcesses())
        self.assertIn("**Background processes:** unavailable", output)
        self.assertIn("**Work:** unknown", output)
        self.assertNotIn("sensitive details", output)

    def test_status_idle_and_disabled_notification(self):
        output, _ = self.render_activity()
        self.assertIn("**Work:** idle", output)
        process = dict(self.process(), notify_on_complete=False)
        output, _ = self.render_activity([process])
        self.assertIn("agent notification on exit: disabled", output)

    def test_status_subagents_are_scoped_and_do_not_claim_foreground_running(self):
        delegations = [
            dict(status="running", session_key="chat-a", parent_session_id="session-a"),
            dict(status="stalling", session_key="chat-b", parent_session_id="session-b"),
            dict(status="completed", session_key="chat-a", parent_session_id="session-a"),
            dict(status="running", session_key="chat-a", parent_session_id="old-session"),
        ]
        output, _ = self.render_activity(delegations=delegations)
        self.assertIn("Agent Running: No", output)
        self.assertIn("**Subagents:** 1 active", output)
        self.assertIn("**Work:** background active", output)

    def test_status_limits_process_details_without_hiding_total(self):
        processes = [self.process(f"proc_{n:012x}") for n in range(12)]
        output, _ = self.render_activity(processes)
        self.assertIn("**Background processes:** 12 running", output)
        self.assertEqual(output.count("agent notification on exit:"), 5)
        self.assertIn("7 more", output)

    def test_status_foreground_work_has_priority(self):
        output, _ = self.render_activity([self.process()], is_running=True)
        self.assertIn("**Work:** agent responding", output)

    def test_status_pending_agent_is_not_idle(self):
        sentinel = object()
        output, _ = self.render_activity(agent=sentinel, _AGENT_PENDING_SENTINEL=sentinel)
        self.assertIn("**Work:** agent starting", output)

    def test_status_subagent_failure_does_not_hide_running_process(self):
        output, _ = self.render_activity([self.process()], delegations=RuntimeError("secret"))
        self.assertIn("**Subagents:** unavailable", output)
        self.assertIn("**Background processes:** 1 running", output)
        self.assertIn("**Work:** background active", output)
        self.assertNotIn("secret", output)

    def test_status_handles_invalid_age_and_untrusted_id_without_leaking(self):
        process = dict(self.process("TOKEN=secret\n**injected**"), uptime_seconds=float("nan"))
        output, _ = self.render_activity([process])
        self.assertIn("• process — age unknown", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("injected", output)

    def test_status_rechecks_process_that_exited_during_listing(self):
        process = dict(self.process(), status="running", exited=True)
        output, _ = self.render_activity([process])
        self.assertIn("**Background processes:** 0 running", output)
        self.assertIn("**Work:** idle", output)

    def test_status_accepts_legacy_chat_owned_process_and_parent_only_delegation(self):
        process = dict(self.process(), parent_session_id="", uptime_seconds=3661)
        delegations = [dict(status="finalizing", parent_session_id="session-a"),
                       dict(status="running", parent_session_id="", session_key="")]
        output, _ = self.render_activity([process], delegations)
        self.assertIn("**Subagents:** 1 active", output)
        self.assertIn("**Background processes:** 1 running", output)
        self.assertIn("1h 1m 1s", output)

    def test_status_includes_portal_provider_and_tool_info(self) -> None:
        patches = {
            marker: new
            for _path, marker, _old, new in apply_hermes_patches._PATCHES
        }
        portal = patches["# Local Hermes: portal info"]
        self.assertIn('"portal",', portal)
        self.assertIn('"info",', portal)
        self.assertIn("Provider and tools", portal)
        self.assertIn("asyncio.wait_for", portal)
        self.assertIn("portal_process.kill()", portal)
        self.assertIn("await portal_process.communicate()", portal)

    def test_portal_migration_preserves_status_handler_tail(self) -> None:
        legacy = '''class StatusMixin:
    async def status(self):
        lines = []
        # Local Hermes: portal info
        try:
            portal_info = "configured"
            if portal_info:
                lines.append("**Provider and tools:**
```
" + portal_info + "
```")
        except Exception:
            lines.append("**Provider and tools:** unavailable")
        lines.append("Connected Platforms: telegram")
        return "\\n".join(lines)

    async def next_handler(self):
        return "ok"
'''
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            target = install_dir / "gateway" / "slash_commands.py"
            target.parent.mkdir(parents=True)
            target.write_text(legacy, encoding="utf-8")
            with mock.patch.object(
                apply_hermes_patches, "HERMES_AGENT_DIR", install_dir
            ):
                self.assertEqual(
                    apply_hermes_patches._migrate_installed_portal_info(), 1
                )

            migrated = target.read_text(encoding="utf-8")
            self.assertIn('lines.append("Connected Platforms: telegram")', migrated)
            self.assertIn("async def next_handler", migrated)
            compile(migrated, str(target), "exec")

    def test_telegram_usage_ranking_migration_adds_missing_marker(self) -> None:
        marker = "# Local Hermes: telegram usage ranking"
        source = """def _prioritize_telegram_menu_commands(\n    commands: list[tuple[str, str]],\n) -> list[tuple[str, str]]:\n    menu_cfg = _telegram_command_menu_config()\n    configured_priority = _dedupe_sanitized_names(menu_cfg[\"priority\"])\n"""

        migrated, changed = apply_hermes_patches._migrate_telegram_usage_ranking_source(
            "hermes_cli/commands.py", source
        )

        self.assertTrue(changed)
        self.assertIn(marker, migrated)
        second_pass, second_changed = apply_hermes_patches._migrate_telegram_usage_ranking_source(
            "hermes_cli/commands.py", migrated
        )
        self.assertFalse(second_changed)
        self.assertEqual(second_pass, migrated)

    def test_missing_required_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            with (
                mock.patch.object(
                    apply_hermes_patches,
                    "HERMES_AGENT_DIR",
                    Path(temp_directory),
                ),
                mock.patch.object(
                    apply_hermes_patches,
                    "_PATCHES",
                    [("missing.py", "marker", "old", "new")],
                ),
            ):
                self.assertEqual(apply_hermes_patches.main(), 1)

    def test_missing_install_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            missing = Path(temp_directory) / "missing"
            with mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", missing):
                self.assertEqual(apply_hermes_patches.main(), 1)

    def test_changed_upstream_source_fails_closed_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            target = install_dir / "gateway" / "run.py"
            target.parent.mkdir(parents=True)
            target.write_text("upstream changed\n", encoding="utf-8")
            with (
                mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", install_dir),
                mock.patch.object(
                    apply_hermes_patches,
                    "_PATCHES",
                    [("gateway/run.py", "marker", "old", "new")],
                ),
            ):
                self.assertEqual(apply_hermes_patches.main(), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "upstream changed\n")

    def test_classify_patch_reports_apply_for_fresh_change(self) -> None:
        action = apply_hermes_patches._classify_patch(
            "old source", "marker", "old source", "new source", None
        )
        self.assertEqual(action, "apply")

    def test_classify_patch_reports_old_missing_when_marker_and_old_absent(self) -> None:
        action = apply_hermes_patches._classify_patch(
            "unrelated", "marker", "old", "new", None
        )
        self.assertEqual(action, "old-missing")

    def test_classify_patch_reports_record_when_marker_present_without_state(self) -> None:
        action = apply_hermes_patches._classify_patch(
            "marker\nnew source", "marker", "old", "new source", None
        )
        self.assertEqual(action, "record")

    def test_classify_patch_reports_skip_when_digest_matches(self) -> None:
        digest = apply_hermes_patches._patch_digest("new source")
        state = {"digest": digest, "source": "new source"}
        action = apply_hermes_patches._classify_patch(
            "marker\nnew source", "marker", "old", "new source", state
        )
        self.assertEqual(action, "skip")

    def test_classify_patch_reports_refresh_when_new_already_present(self) -> None:
        state = {"digest": "stale", "source": "old source"}
        action = apply_hermes_patches._classify_patch(
            "marker\nnew source", "marker", "old", "new source", state
        )
        self.assertEqual(action, "refresh")

    def test_classify_patch_reports_locally_changed(self) -> None:
        state = {"digest": "stale", "source": "old source"}
        action = apply_hermes_patches._classify_patch(
            "marker\nlocally changed", "marker", "old", "new source", state
        )
        self.assertEqual(action, "locally-changed")

    def test_classify_patch_reports_upgrade_when_previous_source_present(self) -> None:
        state = {"digest": "stale", "source": "old source"}
        action = apply_hermes_patches._classify_patch(
            "marker\nold source", "marker", "old", "new source", state
        )
        self.assertEqual(action, "upgrade")

    def _run_main_with_single_patch(
        self,
        install_dir: Path,
        file_content: str,
        state: dict[str, dict[str, str]],
    ) -> tuple[int, Path]:
        target = install_dir / "gateway" / "run.py"
        target.parent.mkdir(parents=True)
        target.write_text(file_content, encoding="utf-8")
        with (
            mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", install_dir),
            mock.patch.object(
                apply_hermes_patches,
                "_PATCHES",
                [("gateway/run.py", "marker", "old source", "new source")],
            ),
            mock.patch.object(apply_hermes_patches, "_load_patch_state", return_value=state),
            mock.patch.object(apply_hermes_patches, "_save_patch_state"),
            mock.patch(
                "sys.stdout",
                new_callable=lambda: __import__("io").StringIO(),
            ),
        ):
            result = apply_hermes_patches.main()
        return result, target

    def test_main_skip_branch_when_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            digest = apply_hermes_patches._patch_digest("new source")
            state = {"marker": {"digest": digest, "source": "new source"}}
            result, target = self._run_main_with_single_patch(
                install_dir, "marker\nnew source\n", state
            )
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "marker\nnew source\n")

    def test_main_refresh_branch_when_new_already_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            state = {"marker": {"digest": "stale", "source": "old source"}}
            result, target = self._run_main_with_single_patch(
                install_dir, "marker\nnew source\n", state
            )
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "marker\nnew source\n")

    def test_main_locally_changed_branch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            state = {"marker": {"digest": "stale", "source": "old source"}}
            result, target = self._run_main_with_single_patch(
                install_dir, "marker\nlocally changed\n", state
            )
            self.assertEqual(result, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "marker\nlocally changed\n")

    def test_main_apply_branch_without_prior_state(self) -> None:
        # Fresh install: marker absent, old code present, no state entry.
        # _classify_patch returns "apply"; main() must not KeyError on
        # patch_state[marker] when that marker is missing from the state dict.
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            result, target = self._run_main_with_single_patch(
                install_dir, "old source\n", {}
            )
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "new source\n")

    def test_main_apply_branch_replaces_old_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            result, target = self._run_main_with_single_patch(
                install_dir, "prefix old source suffix\n", {}
            )
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "prefix new source suffix\n")

    def test_main_upgrade_branch_replaces_previous_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            state = {"marker": {"digest": "stale", "source": "old source"}}
            result, target = self._run_main_with_single_patch(
                install_dir, "marker\nold source\n", state
            )
            self.assertEqual(result, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), "marker\nnew source\n")


if __name__ == "__main__":
    unittest.main()
