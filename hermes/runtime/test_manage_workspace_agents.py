"""Regression tests for selective workspace/AGENTS.md ownership."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import runpy
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
    def run_cli(self, target, source, backup=None, state="present"):
        argv = ["manage-workspace-agents.py", "--target", str(target),
                "--managed-source", str(source), "--state", state]
        if backup is not None:
            argv.extend(["--backup-copy", str(backup)])
        with mock.patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            return manage_workspace_agents.main()

    def test_linked_destinations_are_rejected_before_either_file_is_changed(self) -> None:
        for destination in ("target", "backup"):
            for kind in ("symlink", "dangling", "hardlink"):
                with self.subTest(destination=destination, kind=kind), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp).resolve()
                    outside = root / "outside"
                    outside.mkdir(mode=0o755)
                    victim = outside / "notes.md"
                    victim.write_text("Outside notes\n")
                    target = root / "AGENTS.md"
                    backup = root / "backup.md"
                    source = root / "source.md"
                    for path in (target, backup, source):
                        path.write_text("Original\n")
                    attacked = target if destination == "target" else backup
                    untouched = backup if destination == "target" else target
                    attacked.unlink()
                    if kind == "hardlink":
                        os.link(victim, attacked)
                    else:
                        attacked.symlink_to(victim if kind == "symlink" else outside / "missing.md")
                    before_mode = stat.S_IMODE(outside.stat().st_mode)

                    self.assertEqual(self.run_cli(target, source, backup), 2)

                    self.assertEqual(untouched.read_text(), "Original\n")
                    self.assertEqual(victim.read_text(), "Outside notes\n")
                    self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before_mode)
                    self.assertFalse((outside / "missing.md").exists())

    def test_symlinked_parent_directories_are_rejected(self) -> None:
        for destination in ("target", "backup"):
            with self.subTest(destination=destination), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                outside = root / "outside"
                outside.mkdir(mode=0o755)
                (outside / "AGENTS.md").write_text("Outside notes\n")
                alias = root / "alias"
                alias.symlink_to(outside, target_is_directory=True)
                target = root / "AGENTS.md"
                target.write_text("Personal notes\n")
                source = root / "source.md"
                source.write_text("Managed rules\n")
                if destination == "target":
                    result = self.run_cli(alias / "AGENTS.md", source)
                else:
                    result = self.run_cli(target, source, alias / "AGENTS.md")
                self.assertEqual(result, 2)
                self.assertEqual((outside / "AGENTS.md").read_text(), "Outside notes\n")
                self.assertEqual(target.read_text(), "Personal notes\n")
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_dotdot_destination_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source.md"
            source.write_text("Managed rules\n")
            self.assertEqual(self.run_cli(root / "subdir/../AGENTS.md", source), 2)
            self.assertFalse((root / "AGENTS.md").exists())

    def test_atomic_replace_is_not_redirected_by_parent_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "AGENTS.md"
            target.write_text("Original\n")
            outside = root / "outside"
            outside.mkdir()
            (outside / "AGENTS.md").write_text("Outside notes\n")
            moved = root / "original-workspace"
            original_replace = os.replace

            def swap_parent(*args, **kwargs):
                workspace.rename(moved)
                workspace.symlink_to(outside, target_is_directory=True)
                return original_replace(*args, **kwargs)

            with mock.patch.object(os, "replace", side_effect=swap_parent):
                manage_workspace_agents.write_atomic(target, "Updated\n")
            self.assertEqual((outside / "AGENTS.md").read_text(), "Outside notes\n")
            self.assertEqual((moved / "AGENTS.md").read_text(), "Updated\n")
            self.assertEqual(set(moved.iterdir()), {moved / "AGENTS.md"})

    def test_atomic_write_failure_preserves_original_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "AGENTS.md"
            target.write_text("Original\n")
            with mock.patch.object(os, "replace", side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    manage_workspace_agents.write_atomic(target, "Updated\n")
            self.assertEqual(target.read_text(), "Original\n")
            self.assertEqual(set(root.iterdir()), {target})

    def test_new_files_are_private_and_repeated_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source.md"
            source.write_text("Managed rules\n")
            target = root / "workspace/AGENTS.md"
            backup = root / "state/backup.md"
            self.assertEqual(self.run_cli(target, source, backup), 0)
            self.assertEqual(target.read_text(), backup.read_text())
            times = (target.stat().st_mtime_ns, backup.stat().st_mtime_ns)
            self.assertEqual(self.run_cli(target, source, backup), 0)
            self.assertEqual((target.stat().st_mtime_ns, backup.stat().st_mtime_ns), times)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)

    def test_absent_state_with_no_personal_content_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source.md"
            source.write_text("Managed rules\n")
            target = root / "AGENTS.md"
            script = str(MODULE_PATH)
            argv = [script, "--target", str(target), "--managed-source", str(source), "--state", "absent"]
            with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(script, run_name="__main__")
            self.assertEqual(raised.exception.code, 0)
            self.assertFalse(target.exists())

    def test_atomic_writer_itself_rejects_linked_destinations(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                victim = root / "victim.md"
                victim.write_text("Outside\n")
                target = root / "AGENTS.md"
                if kind == "symlink":
                    target.symlink_to(victim)
                else:
                    os.link(victim, target)
                with self.assertRaises(manage_workspace_agents.ManagedBlockError):
                    manage_workspace_agents.write_atomic(target, "New content\n")
                self.assertEqual(victim.read_text(), "Outside\n")

    def test_identical_legacy_content_is_replaced_by_managed_block(self) -> None:
        result = manage_workspace_agents.reconcile("Managed rules\n", "Managed rules\n", present=True)
        self.assertEqual(result.count("Managed rules"), 1)
        self.assertIn(manage_workspace_agents.BEGIN_MARKER, result)


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
            target = Path(temp).resolve() / "AGENTS.md"
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
            root = Path(temporary_directory).resolve()
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
