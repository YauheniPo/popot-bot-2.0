"""Regression tests for selective workspace/AGENTS.md ownership."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import stat
import unittest
from unittest import mock

import yaml

MODULE_PATH = Path(__file__).with_name("manage-workspace-agents.py")
SPEC = importlib.util.spec_from_file_location("manage_workspace_agents", MODULE_PATH)
assert SPEC and SPEC.loader
manage_workspace_agents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage_workspace_agents)


class ManageWorkspaceAgentsTests(unittest.TestCase):
    def test_all_ansible_writers_keep_workspace_instructions_private(self) -> None:
        ansible = MODULE_PATH.parents[1] / "ansible"
        playbook = yaml.safe_load((ansible / "playbook.yml").read_text())
        tasks = [task for play in playbook for task in play.get("tasks", [])]
        tasks.extend(yaml.safe_load((ansible / "tasks/github.yml").read_text()))
        checked = []
        for task in tasks:
            for action in ("ansible.builtin.copy", "ansible.builtin.file", "ansible.builtin.blockinfile"):
                options = task.get(action, {})
                if options.get("path", options.get("dest")) == "{{ hermes_workspace }}/AGENTS.md":
                    self.assertEqual(options["mode"], "0600", task["name"])
                    checked.append(task["name"])
        self.assertTrue(checked)

    def test_tts_patch_runs_as_the_service_account(self) -> None:
        tasks = yaml.safe_load((MODULE_PATH.parents[1] / "ansible/tasks/services.yml").read_text())
        task = next(task for task in tasks if task["name"] == "Install transient Edge TTS retry when the upstream tool is found")
        self.assertEqual(task["become_user"], "{{ hermes_user }}")

    def test_atomic_write_keeps_workspace_instructions_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "AGENTS.md"
            manage_workspace_agents.write_atomic(target, "Private operator notes\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_text(), "Private operator notes\n")

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

    def test_legacy_prefix_in_personal_notes_is_not_stripped(self) -> None:
        managed = "# Host administration\n\nUse sudo carefully."
        existing = managed + "\n\nMy personal note: keep this wording.\n"

        result = manage_workspace_agents.reconcile(existing, managed, present=True)

        self.assertEqual(result.count("Use sudo carefully."), 2)
        self.assertIn("My personal note: keep this wording.", result)

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
