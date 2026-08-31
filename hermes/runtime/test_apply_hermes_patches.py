"""Regression tests for local Hermes gateway patch migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("apply-hermes-patches.py")
SPEC = importlib.util.spec_from_file_location("apply_hermes_patches", MODULE_PATH)
assert SPEC and SPEC.loader
apply_hermes_patches = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_hermes_patches)


class ApplyHermesPatchesTests(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as temp_directory:
            install_dir = Path(temp_directory)
            commands = install_dir / "hermes_cli" / "commands.py"
            slash_commands = install_dir / "gateway" / "slash_commands.py"
            run = install_dir / "gateway" / "run.py"
            commands.parent.mkdir(parents=True)
            slash_commands.parent.mkdir(parents=True)
            commands.write_text(
                apply_hermes_patches._PATCHES[0][3]
                + '''    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True, busy_policy="dispatch", aliases=("gw-restart",)),
''',
                encoding="utf-8",
            )
            slash_commands.write_text(
                apply_hermes_patches._PATCHES[2][3]
                + apply_hermes_patches._PATCHES[6][3],
                encoding="utf-8",
            )
            run.write_text(
                apply_hermes_patches._PATCHES[3][3]
                + '''        if canonical in ("restart", "gw-restart"):
            # /gw-restart is the user-facing alias; /restart is kept for
            # backward compatibility with scripts that already use it.
            return await self._handle_restart_command(event)
'''
                + apply_hermes_patches._PATCHES[5][3],
                encoding="utf-8",
            )

            with (
                mock.patch.object(apply_hermes_patches, "HERMES_AGENT_DIR", install_dir),
                mock.patch.object(apply_hermes_patches, "_PATCHES", apply_hermes_patches._PATCHES[:7]),
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
        self.assertIn(
            'if canonical == "doctor":', patches["# Local Hermes: doctor route"]
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

        self.assertIn("pinned_indexes", ranking)
        self.assertIn("absolute first tier", ranking)
        self.assertIn("-usage_counts.get(name, 0)", ranking)
        self.assertIn("telegram-command-usage.json", state)
        self.assertIn("record_telegram_command_usage", state)
        self.assertIn("refresh_every", state)
        self.assertIn("set_my_commands", refresh)
        self.assertIn("_record_telegram_command_usage(event.text)", record)

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


if __name__ == "__main__":
    unittest.main()
