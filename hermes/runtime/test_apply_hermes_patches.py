"""Regression tests for local Hermes gateway patch migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


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

    def test_new_patch_definitions_contain_only_underscore_command(self) -> None:
        retired = apply_hermes_patches._RETIRED_MODEL_GLOBAL
        patch_payload = "\n".join(
            part
            for _relative_path, marker, old, new in apply_hermes_patches._PATCHES
            for part in (marker, old, new)
        )

        self.assertIn("model_global", patch_payload)
        self.assertNotIn(retired, patch_payload)


if __name__ == "__main__":
    unittest.main()
