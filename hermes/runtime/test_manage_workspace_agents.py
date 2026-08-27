"""Regression tests for selective workspace/AGENTS.md ownership."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("manage-workspace-agents.py")
SPEC = importlib.util.spec_from_file_location("manage_workspace_agents", MODULE_PATH)
assert SPEC and SPEC.loader
manage_workspace_agents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage_workspace_agents)


class ManageWorkspaceAgentsTests(unittest.TestCase):
    def test_new_managed_block_keeps_personal_instructions(self) -> None:
        result = manage_workspace_agents.reconcile(
            "# Personal\n\nRemember my repositories.\n",
            "# Host administration\n\nUse sudo carefully.\n",
            present=True,
        )

        self.assertIn(manage_workspace_agents.BEGIN_MARKER, result)
        self.assertIn("Use sudo carefully.", result)
        self.assertIn("Remember my repositories.", result)

    def test_existing_managed_block_is_replaced_without_touching_personal_text(self) -> None:
        old = manage_workspace_agents.reconcile("Personal tail\n", "Old managed\n", present=True)

        result = manage_workspace_agents.reconcile(old, "New managed\n", present=True)

        self.assertNotIn("Old managed", result)
        self.assertIn("New managed", result)
        self.assertEqual(result.count("Personal tail"), 1)

    def test_legacy_full_copy_is_migrated_to_a_managed_block(self) -> None:
        managed = "# Host administration\n\nUse sudo carefully."
        existing = managed + "\n\n<!-- BEGIN MANAGED VAULT ENVIRONMENT NAMES -->\nNames\n"

        result = manage_workspace_agents.reconcile(existing, managed, present=True)

        self.assertEqual(result.count("Use sudo carefully."), 1)
        self.assertIn("MANAGED VAULT ENVIRONMENT NAMES", result)

    def test_disabling_host_admin_removes_only_managed_block(self) -> None:
        existing = manage_workspace_agents.reconcile("Personal tail\n", "Managed\n", present=True)

        result = manage_workspace_agents.reconcile(existing, "Managed\n", present=False)

        self.assertEqual(result, "Personal tail\n")

    def test_malformed_markers_fail_closed(self) -> None:
        with self.assertRaises(manage_workspace_agents.ManagedBlockError):
            manage_workspace_agents.reconcile(
                manage_workspace_agents.BEGIN_MARKER + "\n",
                "Managed\n",
                present=True,
            )

    def test_backup_copy_restores_personal_instructions_when_target_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "workspace" / "AGENTS.md"
            managed_source = root / "managed.md"
            backup_copy = root / ".hermes" / "operator-state" / "workspace-AGENTS.md"
            managed_source.write_text("Managed current\n", encoding="utf-8")
            backup_copy.parent.mkdir(parents=True)
            backup_copy.write_text("Personal restored\n", encoding="utf-8")

            with mock.patch(
                "sys.argv",
                [
                    "manage-workspace-agents.py",
                    "--target",
                    str(target),
                    "--managed-source",
                    str(managed_source),
                    "--backup-copy",
                    str(backup_copy),
                    "--state",
                    "present",
                ],
            ):
                self.assertEqual(manage_workspace_agents.main(), 0)

            restored = target.read_text(encoding="utf-8")
            self.assertIn("Personal restored", restored)
            self.assertIn("Managed current", restored)
            self.assertEqual(backup_copy.read_text(encoding="utf-8"), restored)


if __name__ == "__main__":
    unittest.main()
